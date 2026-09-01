# SWE-bench 评测侧（eval）流程 — 源码级梳理

> 基于本地仓库 `SWE-bench @ 334882d`。本文件只写「评测」这一半，agent/infer 那一半见 FLOW_agent_side.md。

## 0. 三段式总览

```
swebench infer  ──►  preds.json (每实例一个 model_patch)  ──►  swebench eval  ──►  report.json
   (调 agent)                                                    (Docker 跑测试)      (判分)
```

CLI 在 `swebench/cli/cli.py` 用 typer 挂载：
- `infer`  → `swebench/cli/infer.py`      → 子进程调用 mini-SWE-agent
- `eval`   → `swebench/harness/run_evaluation.py:main`
- `report` → 同一个 main，但 `rewrite_reports=True`（只重算，不起容器）
- `images` / `dataset` / `submit` 为辅助子命令

## 1. 数据集就是任务定义

`swebench/cli/_datasets.py` 的别名表：

| alias | HuggingFace id |
|---|---|
| full | SWE-bench/SWE-bench |
| lite | SWE-bench/SWE-bench_Lite |
| verified | SWE-bench/SWE-bench_Verified |
| multilingual | SWE-bench/SWE-bench_Multilingual |
| multimodal | SWE-bench/SWE-bench_Multimodal |

当前版本数据集每行（一个 instance）字段：

```
repo, instance_id, base_commit, environment_setup_commit, version,
problem_statement,   <- 喂给 agent 的唯一输入（GitHub issue 正文）
patch,               <- gold patch（参考答案，agent 看不到）
test_patch,          <- 测试补丁（agent 看不到）
FAIL_TO_PASS,        <- 修好后必须由失败转通过的测试
PASS_TO_PASS,        <- 必须保持通过的测试（防回归）
image,               <- 预构建评测镜像名，如 swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest
eval_script,         <- 整段 bash 评测脚本，直接内嵌在数据集里
log_parser,          <- 测试日志解析器名，如 parse_log_astropy
eval_type,           <- pass_and_fail | fail_only
hints_text, created_at, difficulty
```

**关键点**：这一版把 `image` / `eval_script` / `log_parser` / `eval_type` 直接烘进了数据集。
harness 不再在本地生成 Dockerfile、不再按 repo+version 查表拼装安装命令 —— 那些都在建数据集时就定死了。
`swebench/harness/utils.py:make_test_spec()` 只是把这些字段搬进 `TestSpec` dataclass。

镜像名规则见 `swebench/image_builder/image_spec.py:ImageSpec.name`：
`sweb.eval.{arch}.{instance_id}:{tag}`，且因为 Docker Hub 不允许双下划线，`__` 被替换成 `_1776_`，最后整体小写。
所以 `astropy__astropy-12907` → `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`。

## 2. `swebench eval` 的执行链

入口 `swebench/harness/run_evaluation.py:main()`：

1. `get_predictions_from_file()` 读预测。
   - `--gold` 时不读文件，直接把数据集里的 `patch` 当作 `model_patch`，`model_name_or_path="gold"`。这就是"金标准冒烟测试"。
   - 否则读 `.json`（list 或 dict[instance_id]）/ `.jsonl`。
2. `write_run_metadata()` 把 dataset/split/task_repo 写进 `logs/evaluation/<run_id>/run.json`，供以后 `swebench report` 重算时知道该拿哪个数据集判分。
3. `get_dataset_from_preds()` 求交集：数据集 ∩ 有预测 ∩ 未完成（已有 report.json 的直接跳过，天然支持断点续跑）；空 patch 的实例直接剔除。
4. `run_instances()` → `run_threadpool(run_instance, payloads, max_workers)`，每个实例一个线程。
5. `make_run_report()` 汇总成 `<run_id>.<model>.json`。

## 3. `run_instance()` —— 单个实例在容器里到底做了什么

按顺序（`run_evaluation.py:220-410`）：

1. **幂等短路**：`logs/evaluation/<run_id>/<model>/<instance_id>/report.json` 已存在就直接返回旧结果。
2. **create_container()**：
   - `client.images.get(image)`，没有就 `client.images.pull(image)`（所以不需要本地 build）。
   - 容器名 `sweb.eval.<instance_id>.<run_id>`；同名旧容器 force remove（处理幽灵容器）；409 冲突时加时间戳后缀重试。
   - `command="tail -f /dev/null"` 让容器空转待命，`cap_add=["SYS_ADMIN"]`（浏览器沙箱类测试需要 CLONE_NEWUSER）。
3. **打补丁**：把 `model_patch` 写到宿主机 `patch.diff`，`copy_to_container` 到容器 `/tmp/patch.diff`，然后依次尝试四条命令，成功一条即止：
   ```
   git apply --verbose
   git apply --verbose --3way
   git apply --verbose --reject
   patch --batch --forward --fuzz=5 -p1 -i
   ```
   每次重试前先 `git checkout -- . ; git clean -fd` 还原工作区（`--reject` 会留下半成品）。
   四条全败时再 `git apply --check --reverse` 兜底判断"其实已经打上了"；仍失败则记 `>>>>> Patch Apply Failed` 并抛 `EvaluationError`。
4. **前后 diff 取证**：跑测试前后各执行一次 `git -c core.fileMode=false diff`，比对是否被测试脚本改动。
5. **写 eval.sh**：`TestSpec.eval_script` 落盘到 log_dir，再 `copy_to_container` 到容器 `/eval.sh`。
6. **跑测试**：`exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout)`，默认 1800s。输出整段存 `test_output.txt`。
7. **判分**：`get_eval_report()`。
8. **finally**：`cleanup_container()` 删容器，关 logger。

## 4. eval.sh 长什么样（以 astropy__astropy-12907 为例）

```bash
#!/bin/bash
set -uxo pipefail                      # 注意没有 -e
source /opt/miniconda3/bin/activate
conda activate testbed                 # 镜像里预置好的环境
cd /testbed
git config --global --add safe.directory /testbed
git status
git show
git -c core.fileMode=false diff d16bfe05a744909de4b27f5875fe0d4ed41ce607
source /opt/miniconda3/bin/activate
conda activate testbed
python -m pip install -e .[test] --verbose      # 重新安装，让 model patch 生效
git checkout d16bfe05... astropy/modeling/tests/test_separable.py   # 先把测试文件还原到 base
git apply -v - <<'EOF_...'
<test_patch 内容>                                # 再打官方测试补丁
EOF_...
: '>>>>> Start Test Output'
pytest -rA astropy/modeling/tests/test_separable.py
: '>>>>> End Test Output'
git checkout d16bfe05... astropy/modeling/tests/test_separable.py   # 收尾还原测试文件
```

三个设计要点：
- **`git checkout <base> <test_file>` 在 `git apply` 测试补丁之前**：如果 agent 偷改了测试文件，这一步会把它冲掉。这是 SWE-bench 防作弊的主要手段。
- **`Start/End Test Output` 标记**：判分时只解析这两个标记之间的内容，避免 pip 安装日志里的字符串污染。
- **退出码补丁**：`utils.py:record_test_exit_code()` 会在 End 标记前插入 `SWEBENCH_TEST_EXIT_CODE=$?`，End 标记后 `echo ">>>>> Test Exit Code: $..."`。
  因为脚本以 `git checkout` 收尾且没有 `set -e`，脚本自身退出码是 checkout 的，不是 pytest 的；把 `$?` 单独抓出来放在解析区之外，才能交叉验证。

## 5. 判分（`swebench/harness/grading.py`）

`get_logs_eval()`：
1. 日志里出现 `>>>>> Patch Apply Failed` / `Reset Failed` / `Tests Errored` / `Tests Timed Out` 任一 → 判定 `found=False`，直接不及格。
2. 必须同时含 Start/End 标记，否则 `found=False`。
3. 截取 Start..End 之间，交给 `PARSER_REGISTRY[log_parser]`（astropy 走 pytest 系解析器），得到 `{test_id: PASSED|FAILED|SKIPPED|ERROR|XFAIL}`。
4. 解析为空时退化成解析全文；仍为空且 `SUITE_RAN` 正则也匹配不到"测试确实跑过"的证据 → `found=False`（防止浏览器起不来的空跑被算成满分）。
5. 交叉校验：测试退出码非 0 但状态表里一个 FAILED/ERROR 都没有 → 判日志不可信，`found=False`。

`get_eval_tests_report()` + `get_resolution_status()`：
- F2P 里每个用例必须 PASSED 或 XFAIL（SKIPPED 也算失败）。
- P2P 里每个用例必须 PASSED/XFAIL/SKIPPED（跳过不算回归）。
- `f2p==1 and p2p==1` → `RESOLVED_FULL`（report 里 `resolved: true`）
- `0<f2p<1 and p2p==1` → `RESOLVED_PARTIAL`（**注意：仍然 `resolved: false`**）
- 其余 → `RESOLVED_NO`

最终每实例 `report.json` 形如：
```json
{"astropy__astropy-12907": {
  "patch_is_None": false, "patch_exists": true,
  "patch_successfully_applied": true, "resolved": true,
  "infra_failure": false,
  "tests_status": {"FAIL_TO_PASS": {"success": [...], "failure": []},
                   "PASS_TO_PASS": {"success": [...], "failure": []}, ...}}}
```

## 6. 产物目录

```
logs/evaluation/<run_id>/run.json                     # 这次 run 用的 dataset/split
logs/evaluation/<run_id>/<model>/<instance_id>/
    run_instance.log     # harness 自己的日志（建容器/打补丁/判分）
    patch.diff           # 送进容器的 model_patch
    eval.sh              # 送进容器的评测脚本
    test_output.txt      # 容器里 eval.sh 的完整 stdout+stderr
    report.json          # 单实例判分结果
<run_id>.<model>.json                                 # 全量汇总报告
```
