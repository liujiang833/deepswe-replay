# SWE-bench 跑通记录 + 流程梳理

用 DeepSeek 跑通了 SWE-bench Verified 的**第一个用例** `astropy__astropy-12907`，
从任务启动 → 调 agent → 评估全链路走通，并统计了 agent 在容器里执行的每一条 bash。

## 结果速览

| 项 | 值 |
|---|---|
| 用例 | `astropy__astropy-12907`（Verified / Lite 的第 1 条，两者相同） |
| 模型 | `deepseek/deepseek-v4-pro` |
| gold 冒烟 | resolved **1/1** |
| DeepSeek 预测 | **resolved = true**，F2P 2/2，P2P 13/13 |
| 产出 patch | 代码改动与 gold **完全相同**（同一个 hunk、同一行 `= 1` → `= right`）；文本上多出一行 `index a308e2729..45bea3608 100755` —— `git diff` 会带 blob 哈希行，数据集的 gold patch 剥掉了。470B vs 504B |
| agent 用量 | 12 次 LM 调用 / 14 次 bash / 30 个 shell 操作 / $0.0318 / 65.7 s |

## 文件

| 文件 | 内容 |
|---|---|
| `FLOW_eval_side.md` | 评测侧源码级梳理：数据集字段、eval.sh、容器动作、判分规则 |
| `FLOW_agent_side.md` | 推理侧源码级梳理：infer 怎么调 mini-SWE-agent、agent 主循环、容器与 prompt 契约 |
| `OPERATIONS_astropy-12907.md` | **本次运行的操作统计与 bash 内部逐条剖析** |
| `analyze_traj.py` | 轨迹分析脚本（拆 shell 命令、分类意图、统计程序/返回码/延迟） |
| `reproduce.sh` | 一键复现 |
| `traj_analysis.json` | 分析结果结构化输出 |
| `logs/inference/ds-v4pro/` | preds.json + 完整 trajectory |
| `logs/evaluation/{gold-smoke,ds-v4pro}/` | eval.sh / patch.diff / test_output.txt / report.json |
| `gold.ds-v4pro.json` | **反例证据**：`swebench report ds-v4pro` 的输出，500 个实例全 error，见下文第六节第 1 条 |

---

## 一、总体架构：SWE-bench 只管「出题 + 判卷」，不管「答题」

```
  HuggingFace 数据集                SWE-bench 仓库                    外部 agent
 ┌──────────────────┐        ┌───────────────────────┐        ┌──────────────────┐
 │ problem_statement│───────►│ swebench infer        │───────►│ mini-SWE-agent   │
 │ patch (gold)     │        │  (只拼 argv)          │        │  LM + bash 循环  │
 │ test_patch       │        └───────────────────────┘        │  在 docker 容器里│
 │ FAIL_TO_PASS     │                                          └────────┬─────────┘
 │ PASS_TO_PASS     │                                                   │ preds.json
 │ image            │        ┌───────────────────────┐                  │
 │ eval_script      │───────►│ swebench eval         │◄─────────────────┘
 │ log_parser       │        │  起容器/打patch/跑测试 │
 └──────────────────┘        └───────────┬───────────┘
                                         │ report.json
                             ┌───────────▼───────────┐
                             │ swebench report       │  从日志重新判分（不起容器）
                             └───────────────────────┘
```

关键认知：**SWE-bench 仓库里没有 agent**。`swebench/inference/mini_swe_agent.py` 只有一个
`build_command()` 负责拼出 `python -m minisweagent.run.benchmarks.swebench ...`，然后 `subprocess.call`。
两侧唯一的接口是 `preds.json`：`{instance_id: {model_name_or_path, instance_id, model_patch}}`。

## 二、一个 task 是如何启动的

```bash
swebench infer verified -m deepseek/deepseek-v4-pro --run-id ds-v4pro -w 1 -- --filter astropy__astropy-12907
```

展开成（`--dry-run` 可以看到）：

```bash
python -m minisweagent.run.benchmarks.swebench \
  --subset SWE-bench/SWE-bench_Verified --split test \
  -w 1 -o logs/inference/ds-v4pro \
  -c <site-packages>/minisweagent/config/benchmarks/swebench.yaml \
  -m deepseek/deepseek-v4-pro \
  --filter astropy__astropy-12907
```

mini 的 runner 里，一个 task 的生命周期（`process_instance`）：

1. 清掉 `preds.json` 里该 id 的旧记录和旧 traj（避免半残状态）
2. `model = get_model(...)` → `LitellmModel`
3. `task = instance["problem_statement"]` —— **agent 唯一能看到的题面**，看不到 gold patch / test_patch / F2P 列表
4. `env = get_sb_environment(...)` → `docker run -d --name minisweagent-<uuid8> -w /testbed --rm <image> sleep 2h`
5. `agent.run(task)`
6. `finally`：写 `<instance_id>.traj.json`，把 `info["submission"]` 作为 `model_patch` 写进 `preds.json`

**agent 和评测用的是同一个镜像**，但是两个独立容器：agent 在自己的容器里改 `/testbed`，
评测重新起一个干净容器，把 agent 输出的 patch 文本打进去。agent 对容器做的任何其他改动都不会带过去。

## 三、agent 是如何被调用的

**只有一个工具：`bash`**（参数只有 `command` 一个字符串）。没有 read/write/edit 专用工具。

主循环（`minisweagent/agents/default.py`）：

```
messages = [system_template, instance_template(problem_statement)]
loop:
    query():
        if n_calls >= 250 or cost >= $3: raise LimitsExceeded
        litellm.completion(model, messages, tools=[BASH_TOOL], drop_params=True, parallel_tool_calls=True)
    execute_actions():
        for each tool_call:  docker exec -w /testbed -e ... <cid> bash -c "<command>"   # 60s 超时
        把 <returncode>/<output> 拼成 role="tool" 消息回灌
until messages[-1].role == "exit"
```

三个终止路径：
- **Submitted**：某条命令输出的**第一行**恰为 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 且 rc==0
  → env 层抛 `Submitted`，其余行就是 patch
- **LimitsExceeded**：步数 ≥250 或成本 ≥$3
- **RepeatedFormatError**：连续 3 次没给出合法 tool call

容器的两个"坑"，配置里都做了处理：
- `interpreter: ["bash","-c"]` 是非 login shell，不会 source `~/.bashrc` → 用 `BASH_ENV=/root/.bashrc`
  把镜像里的 `conda activate testbed` 拉起来
- 每条命令都是**独立子 shell**，`cd`/`export` 不保留 → prompt 明确告知，模型实际每条命令都写 `cd /testbed &&`

## 四、是如何评估的

```bash
swebench eval verified -p logs/inference/ds-v4pro/preds.json --run-id ds-v4pro -i astropy__astropy-12907 -j 1
```

`run_instance()` 在容器里的动作序列：

1. `docker create` 一个新容器（`sweb.eval.<id>.<run_id>`，`tail -f /dev/null` 空转，`cap_add=SYS_ADMIN`）
2. 把 `model_patch` 写进容器 `/tmp/patch.diff`，依次尝试直到成功：
   `git apply --verbose` → `--3way` → `--reject` → `patch --batch --forward --fuzz=5 -p1 -i`
   （每次重试前 `git checkout -- . ; git clean -fd` 还原）
3. 跑测试前后各 `git -c core.fileMode=false diff` 一次做取证
4. 把数据集里的 `eval_script` 写成 `/eval.sh`，`bash /eval.sh`（默认 1800s 超时）
5. `get_eval_report()` 判分，写 `report.json`
6. `finally` 删容器

`eval.sh` 的防作弊设计（本例）：

```bash
python -m pip install -e .[test] --verbose                     # 让 model patch 生效
git checkout <base> astropy/modeling/tests/test_separable.py   # ← 先把测试文件冲回原样
git apply -v - <<'EOF'  <test_patch>  EOF                      # 再打官方测试补丁
: '>>>>> Start Test Output'
pytest -rA astropy/modeling/tests/test_separable.py
: '>>>>> End Test Output'
git checkout <base> astropy/modeling/tests/test_separable.py
```

- agent 若偷改测试，会被第 2 行的 `git checkout` 冲掉
- 只解析 `Start/End Test Output` 之间的内容，避免 pip 日志污染
- harness 还会自动插入 `SWEBENCH_TEST_EXIT_CODE=$?` 并在 End 标记**之后**回显，
  用来交叉验证"日志说全过但退出码非 0"这种不可信情况

判分规则（`grading.py`）：
- F2P 每条必须 PASSED/XFAIL（SKIPPED 算失败）
- P2P 每条必须 PASSED/XFAIL/SKIPPED
- 两者都 100% → `RESOLVED_FULL` → `resolved: true`；部分通过是 `RESOLVED_PARTIAL`，**仍然 `resolved: false`**

## 五、本次运行的 agent 操作统计（详见 OPERATIONS_astropy-12907.md）

12 次 LM 调用 → 14 次 bash → 拆开是 30 个 shell 操作：

```
navigate 12   read_file 5   edit_file 3   make_patch 3   run_tests 3
search_files 1   search_content 1   vcs 1   submit 1
```

五个阶段：定位（ls/find/grep/sed 分段读）→ 复现（`python - <<'PY'` 照抄 issue 代码）→
修改（`python - <<'PY'` 带 `assert` 锚点校验，同一次调用里跟 `git diff` 自检）→
验证（3 次 pytest，先窄后宽再窄；唯一一次 rc=1 是与本改动无关的浮点噪声，模型识别出来后没去动测试）→
提交（`git status` → `git diff > patch.txt && cat` → `echo COMPLETE_TASK_... && cat patch.txt`）。

## 六、遇到的坑

1. **`swebench report <run_id>` 只能重判 gold 运行**。`cli/evaluate.py:report_command` 里
   `predictions_path="gold"` 是硬编码的，于是它去 `logs/evaluation/<run_id>/gold/` 找日志；
   模型运行的日志在 `logs/evaluation/<run_id>/<model_name>/`，找不到就把全部 500 个实例报成 error。
   对 gold 运行（如 `swebench report gold-smoke -d verified -i ...`）则正常。
2. **DeepSeek 的 litellm 模型名**：`~/.bashrc` 里的 `ANTHROPIC_AUTH_TOKEN` 就是 DeepSeek 的 key，
   但要用 `DEEPSEEK_API_KEY` 这个变量名喂给 litellm。可用模型是 `deepseek-v4-pro` / `deepseek-v4-flash`
   （`deepseek-chat` 会路由到 flash），不是文档里常见的 `deepseek-coder`。
3. **mini 自己的数据集别名指向旧副本**（`princeton-nlp/*`，没有 `image`/`eval_script` 列）。
   `swebench infer` 特意把别名展开成完整 HF id 再传 `--subset`，绕开了这张表。
4. **`-c` 会丢默认配置**：mini 只要看到任何 `-c` 就丢掉自带默认，所以 `build_command` 总是显式带上
   `<site-packages>/minisweagent/config/benchmarks/swebench.yaml`。
