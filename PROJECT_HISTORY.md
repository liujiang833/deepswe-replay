# Project History

## 2026-09-01: 跑通 SWE-bench 首个用例并梳理全流程

**Goal:** 用 DeepSeek 跑通 SWE-bench 中任意一个（首选第一个）用例，梳理「任务如何启动 / agent 如何被调用 / 如何评估」的完整流程，并统计该次运行中 agent 执行的各类操作——特别是深入分析 bash 命令内部的语义，而非只统计"调了几次 bash"。

**Steps:**
1. 侦察环境与仓库结构 — success（Docker 28.3.3 / 16 核 / 818G 空闲；确认三段式 CLI）
2. 阅读评测侧源码（cli, harness/run_evaluation, grading, utils, log_parsers）— success
3. 选定用例 `astropy__astropy-12907`（Verified 与 Lite 的第 1 条相同）— success
4. 验证 DeepSeek 凭据与 litellm 通路（含 function calling）— success
5. 建 conda env `swebench`，装 SWE-bench(editable) + mini-swe-agent 2.4.6 — success
6. 拉评测镜像 `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907`（2.89GB）— success
7. gold 冒烟评测 — success（resolved 1/1，F2P 2/2，P2P 13/13，测试耗时 44.1s）
8. 阅读 agent 侧源码（mini runner / DefaultAgent / DockerEnvironment / LitellmModel / swebench.yaml）— success
9. DeepSeek 推理产出 patch — success（exit_status=Submitted，$0.0318，65.7s）
10. 评估该预测 — success（**resolved = true**）
11. 写轨迹分析脚本并统计 bash 内部操作 — success
12. 独立验证（verifier subagent）— success

**Key Findings:**
- **SWE-bench 仓库本身不含 agent**。`swebench/inference/mini_swe_agent.py` 只是个纯 argv 组装器，
  真正干活的是外部包 mini-SWE-agent。两侧唯一接口是 `preds.json`。
- 当前版本数据集已把 `image` / `eval_script` / `log_parser` / `eval_type` 烘进数据行，
  harness 不再本地生成 Dockerfile 或按 repo+version 查表拼安装命令。
- agent 只有**一个工具 `bash`**。读文件、改文件、跑测试全靠模型自己写 shell，
  所以 bash 内部语义就是 agent 行为的全部。
- 防作弊靠 `eval.sh` 里 `git apply <test_patch>` **之前**的 `git checkout <base> <test_file>`：
  agent 改测试会被冲掉；再加上 P2P 回归检查和测试退出码交叉校验。
- DeepSeek v4-pro 一次通过，产出 patch 的**代码改动与 gold 完全相同**（文本上多一行 `git diff` 的
  `index <blob>..<blob>` 哈希行，gold 版本剥掉了；不影响 `git apply`）。行为模式：
  分段读文件规避观察截断、用 `python - <<'PY'` heredoc（带 `assert` 锚点）改文件而非 `sed -i`、
  复现脚本走 stdin 不落盘以保证 diff 干净、改完立刻 `git diff` 自检、
  测试范围先窄后宽再窄且正确识别出与本改动无关的浮点噪声失败。
- **发现一处局限**：`swebench report <run_id>` 把 `predictions_path` 硬编码为 `"gold"`
  （`cli/evaluate.py:report_command`），因此无法重判模型运行——会把全部实例报成 error。

**Files Changed:**
- `EXEC_LOG_2026-09-01.md` - 新增，逐步执行日志
- `PROJECT_HISTORY.md` - 新增，本条目
- `swebench-run/README.md` - 新增，总览 + 流程梳理 + 踩坑
- `swebench-run/FLOW_eval_side.md` - 新增，评测侧源码级梳理
- `swebench-run/FLOW_agent_side.md` - 新增，推理/agent 侧源码级梳理
- `swebench-run/OPERATIONS_astropy-12907.md` - 新增，本次运行 bash 操作逐条剖析
- `swebench-run/analyze_traj.py` - 新增，轨迹分析脚本
- `swebench-run/reproduce.sh` - 新增，一键复现
- `swebench-run/logs/` - 运行产物（preds.json、trajectory、eval.sh、test_output.txt、report.json）
- `SWE-bench/` - **未改动**（git status 干净，仅 editable 安装）

13. 修正统计口径（剥离 cd 前缀）、补 agent loop 与 per-step 开销、出饼图 — success

**Key Findings（续）:**
- 统计 bash 必须剥掉 `cd` 前缀：14/14 次调用都以 `cd /testbed &&` 开头（子 shell 不保留 cwd），
  不剥离会让 `cd` 占掉 42% 的"操作量"，还会把 3 次 `python` heredoc 误记成 `cd`。
  剥离后：33 个 shell 操作 = 14 次 cd 前缀 + 19 个真实操作。
- agent loop 是 IO-bound on the LLM：67.9s 里 83% 在等模型，17% 在容器里跑命令，
  框架自身每步 `save()` 落盘轨迹实测累计 0.02s，可忽略——没有隐藏的框架税。
- 成本结构：累计输入 102,926 token 是末轮上下文 12,699 的 8.1 倍，随步数近似平方增长；
  prompt 缓存整体命中 90.7% 是把账单压住的关键，而命中率回落恰好发生在上一步回灌大块观察之后。
- step 时长由 reasoning token 决定而非命令执行：最贵的 step 4 花 1,119 reasoning token / 19.3s。

**Commit:** 4ad57bc（首次）+ 本次
