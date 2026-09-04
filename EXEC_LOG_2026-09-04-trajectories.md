# Task Execution Log: 113 个 task 的 trajectory 选取与下载

**Date:** 2026-09-04
**Goal:** 为 `deepswe/data/tasks_extracted/` 下的 113 个 task 各选定唯一一条 **pass** 的 trial 轨迹并下载产物。

## 选取规则（用户给定 + 本次确认的 tie-break）

模型优先级（逐级回退，只在本级无 pass 时才降级）：
1. `claude-fable-5`
2. `gpt-5-6-sol`
3. `claude-sonnet-5`

硬性条件：`outcome == "pass"` 且 `has_trajectory == true`。

同一模型内多条 pass 的 tie-break（用户 2026-09-04 确认）：
**`n_agent_steps` 最小；并列时取 `cost_usd` 更低者；再并列取 `trial_name` 字典序**（保证可复现）。

产物范围（用户确认）：`trajectory.json` + `model.patch` + 全部 `verifier_files`。
（不拉 `agent_log`，内容与 trajectory 大体重叠。）

## Problems to Solve
1. 113 个 task 是否都能在三级偏好内找到 pass 轨迹 —— 若有缺口需显式报告，不得静默降级到其它模型
2. tie-break 必须确定性，重跑要能得到同一批 trial
3. `outcome` 字段来自索引，属于自报；需用 verifier 产物独立复核 pass 判定
4. 下载 113 × (1 traj + 1 patch + ~6 verifier files) ≈ 900 次请求，需容错与重试，且不能把失败当成功

## 数据来源
- 任务清单：`deepswe/data/tasks_extracted/`（113 个目录名即 task_name）
- 索引：`deepswe/data/trials.json`（29357 行，本地已有）
- 下载链路：`deepswe/fetch_trial_artifacts.py`（release.json → CloudFront，公开无鉴权）

## Steps Log

### Step 0: 覆盖度预检
- **Status:** success
- **Result location:** 本文件
- **Success result:** 113/113 全覆盖，无缺口。分级命中：
  `claude-fable-5` 110、`gpt-5-6-sol` 2、`claude-sonnet-5` 1。
  单 task 的 pass 候选数 1~20 条不等，故 tie-break 必需。

### Step 1: 写选取脚本并产出清单
- **Status:** success（执行方自报，待 Step 3 复核）
- **Result location:** `deepswe/select_trajectories.py` → `deepswe/TRAJECTORY_SELECTION.json`（70KB）
- **Success result:** 113/113 全部解出，`unresolved` 为空。分级命中 fable-5 110 / gpt-5-6-sol 2 / sonnet-5 1。
  3 个非 tier-1 的 task 均经确认 tier-1 **零**合格 trial，不是走捷径：
  - `gql-incremental-graphql-delivery` → claude-sonnet-5（fable 0 / sol 0 / sonnet 2）
  - `pwntools-tube-multiplexing` → gpt-5-6-sol（fable 0 / sol 8）
  - `updo-policy-alerting` → gpt-5-6-sol（fable 0 / sol 12）
  重跑两次除 `generated_at` 外逐字节一致。

### Step 2: 下载 113 份产物
- **Status:** success（执行方自报，待 Step 3 复核）
- **Result location:** `deepswe/data/trajectories/<task_name>/`（该目录被 .gitignore 忽略）
- **Success result:** 1095/1095 文件、282,271,843 B（274MB）、0 失败、exit 0。
  每个 task 目录含 `trajectory.json` / `model.patch` / `verifier/*` / `meta.json`（记 sha256 与字节数）。
  断点续传幂等：第三次跑 new=0 / resumed=1095 / 1.1s。
- **一个需要留意的点：82 个 verifier 文件是 0 字节。** 执行方称 CDN 对这些返回
  `HTTP 200 + content-length: 0`，即上游文件本身为空，而非下载失败；其中 78 个是
  `verifier/run.log`，其余为 `reports/gate.log` / `gate_build.log` / `base_tsc.log` / `new_tsc.log`。
  脚本已把它们显式计入 `meta.json` 的 `empty_files` 并在汇总里单列，未当作静默成功。
  **此说法列为 Step 3 的独立核实项。**

### Step 3: 独立验证
- **Status:** success
- **Result location:** `deepswe/TRAJECTORY_VERIFY.md`（入库副本）/ `deepswe/data/trajectories/VERIFY.md`
- **Success result:** 6 项全 PASS，不一致项 0。验证方未复用被验证脚本的代码，
  从规格重新独立实现选取逻辑（`min()` + 显式 tie-break 元组，而非原脚本的 `sort()` 路径）。

| # | 检查项 | 结论 | 关键证据 |
|---|---|---|---|
| 1 | 选取正确性 | PASS | 独立重推 113 条，逐条比对 8 个字段全等 |
| 2 | 覆盖与降级 | PASS | 三方集合（清单 / tasks_extracted / 下载目录）相等，113 不多不少；3 个降级 task 的上级 tier 均 20/20 全 fail |
| 3 | pass 独立复核 | PASS | 113/113 三路互独立证据全判 pass |
| 4 | 产物完整性 | PASS | 目录 / 解析 / steps / patch / verifier 集合 113/113 |
| 5 | 82 个 0 字节文件 | 说法属实 | 直连 CDN 得 `200 + content-length: 0`，ETag 为空串 MD5 |
| 6 | 确定性 | PASS | 2491 行中仅 `generated_at` 一行不同 |

**Step 3 的方法要点（这些才是结论可信的原因）：**
- **pass 判定不采信索引的 `outcome` 字段**，改用三路互相独立的判据：`reward.json`；
  `ctrf.json` **不读 summary、直接从 `results.tests` 原始列表重算**；`test-stdout.txt` 的
  `===== grade =====` 行。三者 113/113 一致且互不矛盾。全部 237229 条 graded 用例
  状态分布 `{passed: 237229}`，零 failed；`[f2p]` 合计 5877 / `[p2p]` 合计 231352，
  与索引 `f2p_total` / `p2p_total` 的求和**精确相等**。
  另解析 `reports/` 下 base/new 原始跑测报告（CTRF JSON + JUnit XML）回连 6429 条用例，
  "graded=passed 但原始报告=failed" 的矛盾数为 **0**。
- **82 个空文件的决定性证据是 ETag** = `d41d8cd98f00b204e9800998ecf8427e`，即空字符串的 MD5；
  S3 的 ETag 就是对象内容 MD5，直证上游对象本身为 0 字节。对照组显示"不存在的 key"返回
  **403 而非 200**，故"缺失"与"为空"可区分。验证方未停在抽样：对全部 **1095 个产物逐个 HEAD**，
  状态码全 200、size 与本地全等、1086 个 MD5 与 ETag 一致（另 9 个为分片上传 ETag，已比 size），
  上游空文件集合与本地 82 个**完全相同**。无一截断。

## 两处表面异常，已核实为非缺陷
1. 2 个 tier-2 task 的 trajectory 内 `agent.model_name` 是 `openai/gpt-5.6-sol`（**点分**），
   索引里是 `gpt-5-6-sol`（**连字符**）。仅命名风格差异，不是下错 trial。
   更强的身份核对：113/113 的 `total_cost_usd`、`total_prompt/completion/cached_tokens`、`harness`
   与索引精确相等，且每个 task 内 `(steps, cost)` 组合唯一（共享同一组合的 task 数为 0），足以唯一锁定身份。
2. `total_steps` 恒比索引 `n_agent_steps` 大 2（分布 `{2: 113}`）：ATIF 把 system prompt 与
   初始 user 消息也计为 step，每条轨迹前两步来源恒为 `{system, user}`。**口径差异，非数据错误。**

## 已知局限
- 78 个 task 的上游 `run.log` 为空，这些 task **无运行日志证据可查**。
  但 pass 裁定所依赖的 `reward.json` / `ctrf.json` / `test-stdout.txt` 在 113 个 task 上均非空，故裁定不受影响。
- 下载产物 274MB 位于被 .gitignore 忽略的 `deepswe/data/trajectories/`，**不入版本库**；
  可由 `select_trajectories.py` + `download_trajectories.py` 完整复现（选取确定性已验证）。

## 交付清单
| 文件 | 说明 | 入库 |
|---|---|---|
| `deepswe/select_trajectories.py` | 选取，仅标准库，可重跑 | 是 |
| `deepswe/download_trajectories.py` | 下载，重试 / 断点续传 / 失败非零退出 | 是 |
| `deepswe/TRAJECTORY_SELECTION.json` | 113 条选取清单（70KB） | 是 |
| `deepswe/TRAJECTORY_VERIFY.md` | 独立验证报告 | 是 |
| `deepswe/data/trajectories/<task>/` | 1095 个产物 / 274MB | 否（可复现） |
