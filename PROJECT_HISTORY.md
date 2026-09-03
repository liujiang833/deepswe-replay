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

---

## 2026-09-03: 确认 DeepSWE 榜单能否拿到 agent trace

**Goal:** 确认 `https://deepswe.datacurve.ai/data/v1.1/trials/abs-module-cache-flags__4kU2tLe`
这类 trial 页面能否拿到底层 agent trace，以及能否批量。

**Steps:**
1. curl trial 页面 — 拿到 33MB HTML，但里面只有 SSR 内嵌的 trial 元数据，无 trace
2. 从 `modulepreload` 的 `use-artifact-*.js` / `trials_._trialName-*.js` 反查取数逻辑 — 发现
   `release.artifact_base_url` + `release.artifact_patterns` 的模板拼接
3. 从 SSR payload 尾部提取到 `release.json` 内容，命中 CloudFront base URL — success
4. 实测下载 trajectory / model.patch / agent log / verifier 四类产物，均 HTTP 200 — success
5. 下载全量索引 `artifacts/v1.1/trials.json`（29357 行）统计覆盖度 — success
6. 写 `deepswe/fetch_trial_artifacts.py` 并端到端跑通复现 — success

**Key Findings:**
- trace 公开无鉴权：`https://d3ujjcmjq6o8v6.cloudfront.net/v1.1/trial-artifacts/{trial_name}/agent/trajectory.json`
  纯 GET 即可，不需要 cookie/token/referer。
- 覆盖度接近满：29357 个 trial 中 29356 个有 trajectory，29335 个有 agent log，28815 个有 model.patch。
  harness 全部是 mini-swe-agent，和我们本地跑 SWE-bench 的形态可直接对照。
- trajectory 粒度足够做分析：逐 step 的 reasoning 文本 + bash 命令 + 完整 stdout/returncode，
  外加 system/instance prompt 原文和 final_metrics（token / cache / cost / peak context）。
- 全量索引 `artifacts/v1.1/trials.json` 自带 cost_usd、n_agent_steps、peak_context_tokens、
  agent_duration_seconds 等，很多横向统计不用下 trace 就能算。
- 本例 `abs-module-cache-flags__4kU2tLe`：claude-fable-5 / vertex_ai / reasoning_effort=high，
  49 steps，reward 1（f2p 20/20，p2p 3/3），$5.25，peak context 76969，agent 耗时 606s。

**Files Changed:**
- `deepswe/README.md` - 新增，链路、覆盖度、trajectory schema
- `deepswe/fetch_trial_artifacts.py` - 新增，按 trial_name 拉全部产物 + 可选全量索引

**Commit:** pending

---

## 2026-09-03（续）: 设计容器侧负载重放流程

**Goal:** 目标负载确定为**容器侧真实 CPU/IO，且必须包含 agent 的探索过程**（非仅"打补丁跑测试"）。
为此设计 replay 流程并验证前提。

**Steps:**
1. 拉取最重的一条 trace `gql-incremental-graphql-delivery__nnFNKRL`（439 步）逐条剖析命令 — success
2. 拉全 113 个 task 定义（31.9 MB），统计语言/规格/镜像分布 — success
3. 验证 replay 的 5 条前提（自包含性、状态依赖、失败保留、非确定性、网络） — success
4. 探镜像可获取性：`docker manifest inspect` 通过，`docker pull` 遭 ECR 匿名限流 — 部分成功
5. 写 `deepswe/REPLAY_DESIGN.md`，含完整 trace 获取链路 + 五段流程 + 风险表 — success

**Key Findings:**
- **跨 step 有状态依赖**：49 个 /tmp 路径、59 次跨 step 引用（如 `git commit -F /tmp/commit_message.txt`），
  所以重放必须**长驻容器串行 exec**，不能一条命令一个 `docker run`。这是最关键的设计约束。
- **重放保真度可自动判定**：`pre_artifacts.sh` 定义提交物 = `git diff --binary <base_sha> HEAD`，
  重放后做同样 diff 与 model.patch 逐字节比对即可。这是整条流程的地基。
- 439 条命令里 15 条非 0 退出（rc=1×10, -1×3, 2×1, 128×1），rc=-1 是 `timeout` 打死；
  重放不能 set -e，失败是负载的一部分。
- trace 里命令输出被 harness 截断（`elided_chars` + "Output too long."，上限约 11 K 字符），
  只能做前 11 K 的弱比对；但重放产生的是完整输出，对负载而言重放的才是真的。
- 113 个 task = **113 个互不相同的镜像**，单个约 800 MB 压缩，全在 public.ecr.aws；
  **ECR 匿名限流是当前最大瓶颈**：manifest 读取正常，`docker pull` 秒拒 `toomanyrequests: Rate exceeded`。
- 113/113 规格统一：cpus=2, memory_mb=8192, allow_internet=false。重放须对齐，否则时间不可比。
- 语言分布：typescript 35 / go 34 / python 34 / rust 5 / javascript 5，抽样要分层。
- 该条 trace 的操作构成：读文件 38%、写文件 41%、搜索 21%、含 pytest 89 条（51 条 `timeout ... pytest`），
  零构建/零装依赖（Python 环境已预装）。

**Files Changed:**
- `deepswe/REPLAY_DESIGN.md` - 新增，trace 获取链路 + 重放五段流程 + 风险表 + 推进顺序
- `.gitignore` - 新增 `deepswe/data/`（34 MB 拉取产物，可由脚本复现）

**Commit:** pending
