# 独立验证报告：DeepSWE 轨迹选取与下载

**日期：** 2026-09-04
**验证方：** 独立 verifier agent（不信任执行方自报结论）
**验证对象：**
- `deepswe/select_trajectories.py`、`deepswe/download_trajectories.py`
- `deepswe/TRAJECTORY_SELECTION.json`
- `deepswe/data/trajectories/<task_name>/`（113 个目录）

**总体结论：全部 6 项 PASS。未发现任何不一致项。**

## 方法学声明

按要求**未复用被验证脚本的任何代码**。所有校验逻辑均从规格说明重新独立实现，
放在临时目录（不入库）：

| 校验脚本 | 作用 |
|---|---|
| `v_select.py` | 从 `trials.json` 独立重推 113 条选取（用 `min()` + 显式 tie-break 元组，与被验证脚本的 `sort()` 实现路径不同） |
| `v_cmp.py` | 与 `TRAJECTORY_SELECTION.json` 逐条比对 + 覆盖集合 + 降级依据 |
| `v_artifacts.py` | 产物完整性 + 独立 pass 裁定（三路证据） |
| `v_identity.py` | 确认下载到的是"选中那条 trial"的产物 |
| `v_cdn.sh` / 内联 probe | 直连 CloudFront 看状态码与响应头 |
| `v_headall.py` | 对全部 1095 个产物 HEAD，逐个比对 size 与 MD5/ETag |
| `v_basenew.py` | 用 base/new 原始跑测报告反查 graded ctrf.json |

唯一"运行了被验证脚本"的地方是第 6 项确定性检查——那是该项的要求本身（重跑到临时目录）。
仓库 `git status` 确认原始脚本与产物**未被修改**，也未做任何 commit。

---

## 1. 选取正确性 — PASS

独立重推结果与清单**逐条完全一致，0 处不一致**。

比对字段：`trial_name`、`model`、`tier`、`n_agent_steps`、`cost_usd`、`reward`、
`n_pass_candidates`、`verifier_files`（全 8 项，113 × 8 全等）。

```
MISMATCHES: 0
manifest set == my set: True
selections sorted asc: True
duplicate task_name in selections: False
```

层级分布（双方一致）：

| tier | model | 数量 |
|---|---|---|
| 1 | `claude-fable-5` | 110 |
| 2 | `gpt-5-6-sol` | 2 |
| 3 | `claude-sonnet-5` | 1 |
| — | unresolved | 0 |

附加健壮性检查（被验证脚本未显式覆盖，独立补做）：

- **无歧义 tie**：113 个 task 里，同 tier 内按 `(n_agent_steps, cost_usd, trial_name)`
  排序后，**没有任何一个 task 存在完全相同的三元组**。也就是说选取结果不依赖
  排序算法的稳定性，任何正确实现都会得到同一条。
- **无 None 参与 tie-break**：所有入选 tier 的候选 trial，其 `n_agent_steps` 与
  `cost_usd` 均非 None，故两边对缺失值的处理差异不会影响结果。
- **模型白名单**：113 条选取全部落在 `{claude-fable-5, gpt-5-6-sol, claude-sonnet-5}` 内，
  越界 0 条。
- **tier ↔ model 自洽**：113 条的 `tier` 字段与其 `model` 的优先级序号完全对应，不一致 0 条。
- **`tier_counts` 重算**：`{1:110, 2:2, 3:1}`，与清单声明一致。
- 硬条件字段取值域已核查：`outcome ∈ {pass:15735, fail:13467, excluded_error:155}`，
  全是 `str`，无 `True`/`"Pass"` 之类的坑；`has_trajectory` 仅有 `True`(29356) / `False`(1)。

**关于那唯一一条 `has_trajectory: False`：**
`ipython-session-bundle-replay__U5rMHWW`（`claude-sonnet-5`, `outcome=pass`,
`n_agent_steps=None`）。该 trial 被硬条件正确排除。注意它的 `n_agent_steps` 恰好是
`None` —— 若过滤写成 truthy 判断而漏掉它，它就会带着 None 进入 tie-break。
实际未发生，两边行为一致。

## 2. 覆盖与降级 — PASS

**集合恰好相等，不多不少：**

```
tasks_extracted 目录数            : 113
TRAJECTORY_SELECTION.selections   : 113
manifest set == tasks_extracted set : True
  in manifest not in tasks_extracted: []
  in tasks_extracted not in manifest: []
trajectories/ 目录数              : 113
dirs == manifest tasks            : True   (extra: [], missing: [])
trajectories/ 下的非目录条目      : []
```

另确认 `trials.json` 覆盖面无缺口：其中出现的 task_name 集合与 `tasks_extracted`
完全相同（`tasks_extracted` 中零行的 task 为 0 个；`trials.json` 中不属于 113 的 task 为 0 个）。

**3 个非 tier-1 task，独立确认其上级确实零合格 trial：**

| task | 选中 tier/model | claude-fable-5 情况 | gpt-5-6-sol 情况 |
|---|---|---|---|
| `pwntools-tube-multiplexing` | 2 / `gpt-5-6-sol` | 20 条 trial，**全部 `fail`**，合格 0 | — |
| `updo-policy-alerting` | 2 / `gpt-5-6-sol` | 20 条 trial，**全部 `fail`**，合格 0 | — |
| `gql-incremental-graphql-delivery` | 3 / `claude-sonnet-5` | 20 条 trial，**全部 `fail`**，合格 0 | 20 条 trial，**全部 `fail`**，合格 0 |

三者的 tier-1 候选 `has_trajectory` 均为 True，即**降级原因确实是 outcome 不过，
而非轨迹缺失**，符合"只有本级完全没有合格 trial 才降级"的规格。

## 3. pass 判定独立复核（113 个全查）— PASS

不采信索引的 `outcome` 字段，改用下载下来的 verifier 产物自行裁定。
对每个 task 用**三路互相独立的证据**分别判定：

1. **`verifier/reward.json`** — 判据：`reward == 1` 且 `f2p_passed == f2p_total`
   且 `p2p_passed == p2p_total` 且 `f2p_total > 0`
2. **`verifier/ctrf.json`** — 判据：**不读 `summary` 字段**，直接从 `results.tests`
   原始列表重新计数，要求无 `failed`，且 `[f2p]`/`[p2p]` 前缀的用例全部 `passed`
3. **`verifier/test-stdout.txt`** — 判据：正则抽取 `===== grade =====` 段的
   `P2P a/b pass c fail; F2P d/e pass f fail; PARTIAL g; BINARY h`，要求 `BINARY 1` 且两处 fail 为 0

```
ctrf.json    verdict=True : 113
reward.json  verdict=True : 113
test-stdout  verdict=True : 113
tasks independently adjudicated PASS: 113 / 113
三路证据互相矛盾的 task: 0
缺证据（三路全无）的 task: 0
```

三个核心证据文件在 113 个 task 上**均非空**（`reward.json` 113/113、`ctrf.json` 113/113、
`test-stdout.txt` 113/113），因此裁定不存在"因文件为空而降级为无证据"的情况。

**与索引 `reward` / `f2p_passed` / `p2p_passed` 的对照（正向计数，非"无报错"）：**

| 对照项 | 一致数 |
|---|---|
| `reward` 与索引一致 | 113/113 |
| `f2p_passed` 与索引一致 | 113/113 |
| `f2p_total` 与索引一致 | 113/113 |
| `p2p_passed` 与索引一致 | 113/113 |
| `p2p_total` 与索引一致 | 113/113 |
| `reward == 1`（产物侧） | 113/113 |
| ctrf 每条用例均 passed | 113/113 |
| test-stdout `BINARY 1` | 113/113 |
| 索引 `outcome == "pass"` | 113/113 |

另外还比对了 ctrf.json 重算出的用例数与索引：
`[f2p]` 前缀用例合计 **5877** == 索引 `f2p_total` 合计 **5877**；
`[p2p]` 前缀用例合计 **231352** == 索引 `p2p_total` 合计 **231352**。
全部 graded 用例状态分布：`{passed: 237229}` —— 零 failed、零 skipped、零 other、零无前缀用例。

**额外的深层反查（`v_basenew.py`）：**
graded 的 `ctrf.json` 是汇总产物，理论上可能与底层实际跑测结果不符。因此又解析了
各 task `verifier/reports/` 下的原始跑测报告（CTRF JSON 与 JUnit XML 两种格式），
把 graded 用例回连到 new/gate 侧的原始报告上核对：

```
tasks with raw reports                    : 113 / 113
graded tests matched into a raw new-side report : 6429
"graded=passed 但原始报告=failed" 的矛盾   : 0
无法解析的原始报告                        : 0
```

**结论：113 个 task 全部经独立证据确认为真实 pass，与索引自报值零冲突。**

## 4. 产物完整性 — PASS

```
113 个目录全部存在，且与清单 task 集合严格相等
trajectory.json 可 JSON 解析          : 113/113
trajectory.json steps 非空            : 113/113（无空 steps、无非 list）
model.patch 存在且非空                : 113/113
model.patch 可识别为 unified diff     : 113/113
model.patch 字节数 min/中位/max       : 7,153 / 24,576 / 138,187
verifier/ 文件名集合 == verifier_files: 113/113（缺 0、多 0）
清单 verifier_files == 索引行 verifier_files : 113/113
目录内非预期文件                      : 0
```

`verifier_files` 形态共 31 种（不同语言/框架的 verifier 产出不同），
合计 **869** 个 verifier 文件。文件总数核对：

```
869 (verifier) + 113 (trajectory.json) + 113 (model.patch) = 1095
1095 + 113 (meta.json，下载器自身写的元信息) = 1208 = 磁盘实际文件数 ✓
```
（`meta.json` 位于 task 目录根部而非 `verifier/` 下，不影响 `verifier/` 集合严格相等的判定。）

**模型字段核对（含一处需要说明的表面差异）：**

| trajectory `agent.model_name` | 清单 `model` | 数量 |
|---|---|---|
| `vertex_ai/claude-fable-5` | `claude-fable-5` | 110 |
| `openai/gpt-5.6-sol` | `gpt-5-6-sol` | 2 |
| `anthropic/claude-sonnet-5` | `claude-sonnet-5` | 1 |

去掉 provider 前缀后，2 个 tier-2 task（`pwntools-tube-multiplexing`、
`updo-policy-alerting`）的 `gpt-5.6-sol`（点分）与索引的 `gpt-5-6-sol`（连字符）
字面不等。**这是上下游命名风格差异，不是下错 trial。** 证据来自更强的身份核对：

对全部 113 个 task，把 `trajectory.json` 的 `final_metrics` 与索引行逐项比对——

- `total_cost_usd` vs `cost_usd`：**113/113 精确相等**（误差 < 1e-6）
- `total_prompt_tokens` vs `n_input_tokens`：**113/113 相等**
- `total_completion_tokens` vs `n_output_tokens`：**113/113 相等**
- `total_cached_tokens` vs `n_cache_tokens`：**113/113 相等**
- `agent.name` vs `harness`：**113/113 相等**

且经检验，在每个 task 内部 `(n_agent_steps, cost_usd)` 组合是**唯一**的
（"其他 trial 与选中 trial 共享同一 steps+cost"的 task 数 = 0），
因此成本与 token 的精确吻合足以唯一锁定 trial 身份。产物确属所选 trial。

**关于 `total_steps` 与 `n_agent_steps` 的系统性差值：**
113 个 task 的 `total_steps - n_agent_steps` **恒等于 2**（分布 `{2: 113}`），
且每条轨迹 `steps` 的前两步来源恒为 `{system, user}`。即 ATIF 轨迹把 system prompt 与
初始 user 消息也计为 step，而索引的 `n_agent_steps` 只计 agent 轮次。属口径差异，非缺漏。

## 5. "82 个 0 字节文件源于上游为空"的说法 — PASS（说法属实）

### 5.1 本地 0 字节文件统计：恰好 82，与执行方声称一致

| 文件 | 数量 |
|---|---|
| `verifier/run.log` | 78 |
| `verifier/reports/gate.log` | 1 |
| `verifier/reports/gate_build.log` | 1 |
| `verifier/reports/base_tsc.log` | 1 |
| `verifier/reports/new_tsc.log` | 1 |
| **合计** | **82** |

### 5.2 直连 CloudFront 抽样（含要求的 `run.log` 与 `reports/gate.log`）

base URL 由 `https://deepswe.datacurve.ai/artifacts/v1.1/release.json` 现取
（`artifact_base_url = https://d3ujjcmjq6o8v6.cloudfront.net`），自行拼接 URL，
用 `curl -I`（HEAD）与 `curl`（GET）分别观测：

| 探测目标 | HTTP | content-length | ETag | GET 实际字节 |
|---|---|---|---|---|
| `arcane-drift-detection-baselines__Eg9A7iC/verifier/reports/gate.log` | **200** | **0** | `d41d8cd98f00b204e9800998ecf8427e` | 0 |
| `adaptix-name-mapping-aliases__wtGF4BR/verifier/run.log` | **200** | **0** | `d41d8cd98f00b204e9800998ecf8427e` | 0 |
| `aiomonitor-task-snapshots-diff__TAdGEJP/verifier/run.log` | **200** | **0** | `d41d8cd98f00b204e9800998ecf8427e` | 0 |
| `task-task-graph-export__7yRV3qm/verifier/reports/gate_build.log` | **200** | **0** | `d41d8cd98f00b204e9800998ecf8427e` | 0 |
| `valibot-recursive-schema-composi__SUQiK76/verifier/reports/base_tsc.log` | **200** | **0** | `d41d8cd98f00b204e9800998ecf8427e` | 0 |
| 对照组：`abs-module-cache-flags__RiQqZb3/verifier/run.log` | 200 | 19559 | `05b8f25a…` | 19559 |
| 对照组：`adaptix-…__wtGF4BR/verifier/reward.json` | 200 | 127 | `4c1fc927…` | 127 |
| 对照组：**不存在**的路径 `…/this-file-does-not-exist.log` | **403** | — | — | 111（S3 XML 错误体） |

三条决定性证据：

1. **ETag `d41d8cd98f00b204e9800998ecf8427e` 正是空字符串的 MD5。**
   S3 的 ETag（非分片上传时）即对象内容的 MD5。这直接证明**上游 S3 对象本身就是 0 字节**，
   而不是传输被截断——截断的响应不可能带着"空内容"的 ETag，且响应头 `server: AmazonS3`
   与 `last-modified` 均正常。
2. **响应是 `HTTP 200 + content-length: 0`，非错误码。**
3. **对照组证明"缺失"与"为空"可区分**：不存在的 key 返回 **403**（桶未开 ListBucket 时
   S3 对缺失对象的标准响应），而非 200。所以这 82 个 200 响应确实对应真实存在的空对象。

### 5.3 不止抽样——对全部 1095 个产物做了完整上游核对

对清单里全部 1095 个产物 URL 逐个 HEAD，并把本地文件的 MD5 与上游 ETag 比对：

```
探测文件数                      : 1095
状态码分布                      : {200: 1095}     （非 200: 0）
上游 content-length != 本地字节 : 0
ETag(MD5) == 本地 MD5           : 1086
ETag(MD5) != 本地 MD5           : 0
分片上传 ETag（无法比 MD5，已比 size）: 9
上游 content-length == 0 的文件 : 82
本地 0 字节文件                 : 82
两个集合完全相同                : True
82 个上游空文件全部带空串 ETag  : True
```

**结论：执行方的说法完全属实。** 82 个 0 字节文件的上游对象本身就是空的
（HTTP 200 + content-length: 0 + 空串 MD5 ETag），不是下载失败。
并且全部 1095 个产物与上游**逐字节吻合**，无一截断、无一损坏。

**一点数据质量提醒（非缺陷）：** 78 个 task 的 `verifier/run.log` 上游为空，
意味着这些 task 无法从 `run.log` 获取运行日志证据。但第 3 项的裁定依赖的是
`reward.json` / `ctrf.json` / `test-stdout.txt`，这三者在 113 个 task 上均非空，
故 pass 裁定不受影响。

## 6. 确定性 — PASS

把 `select_trajectories.py` 连续重跑两次到临时目录（`-o` 指向 scratchpad，未触碰原文件）：

```
run1 == run2 (剔除 generated_at)                : True
run1 == 已产出的 TRAJECTORY_SELECTION.json      : True
行数 run1=2491  run2=2491  已产出=2491
run1 vs run2   字面不同的行数: 1  ->  仅 "generated_at"
run1 vs 已产出 字面不同的行数: 1  ->  仅 L2 "generated_at"
   L2: rerun="generated_at": "2026-09-04T07:11:22Z"
       committed="generated_at": "2026-09-04T06:44:42Z"
```

stdout 两次也完全相同（唯一差异是回显的输出文件名 `run1.json` / `run2.json`）：

```
tasks           : 113
selections      : 113
  tier 1 claude-fable-5  : 110
  tier 2 gpt-5-6-sol     : 2
  tier 3 claude-sonnet-5 : 1
unresolved      : 0
files to fetch  : 1095      ← 与实际下载的 1095 个产物吻合
```

**结论：除时间戳外输出逐字节一致，脚本确定性成立。**

---

## 汇总

| # | 检查项 | 结论 | 关键证据 |
|---|---|---|---|
| 1 | 选取正确性 | **PASS** | 独立重推 113 条，逐条 8 字段全等，不一致 0；无 tie 歧义；模型越界 0 |
| 2 | 覆盖与降级 | **PASS** | 清单集合 == tasks_extracted == trajectories 目录集合（113，不多不少）；3 个降级 task 的上级 tier 均为 0 合格（20/20 全 fail） |
| 3 | pass 独立复核 | **PASS** | 113/113 三路证据（reward.json / ctrf 原始重算 / grade 行）全判 pass 且互不矛盾；与索引 5 项指标 113/113 一致；237229 条 graded 用例零 failed；6429 条回连原始报告零矛盾 |
| 4 | 产物完整性 | **PASS** | 113 目录齐；trajectory 全可解析且 steps 非空；patch 全非空且为合法 diff；verifier 集合 113/113 严格相等；成本+token 精确匹配确认 trial 身份无误 |
| 5 | 0 字节文件成因 | **PASS（说法属实）** | 本地 82 个，与声称一致；直连 CDN 得 `200 + content-length: 0 + ETag=空串MD5`；缺失路径对照返回 403；全部 1095 产物 size/MD5 与上游逐字节吻合 |
| 6 | 确定性 | **PASS** | 两次重跑 + 已产出清单，2491 行中仅 `generated_at` 一行不同 |

**不一致项：无。全部通过。**

### 说明性观察（均已核实为非缺陷，仅备案）

1. 2 个 tier-2 task 的 trajectory 内模型名为 `gpt-5.6-sol`（点分），索引为
   `gpt-5-6-sol`（连字符）——命名风格差异；已用 cost/token 精确匹配确认 trial 身份正确。
2. `total_steps` 恒比 `n_agent_steps` 大 2 —— ATIF 把 system + 初始 user 消息计入 step，口径差异。
3. 78 个 task 的上游 `run.log` 为空，这些 task 无运行日志可查；pass 裁定不依赖该文件，不受影响。
4. `deepswe/data/` 在 `.gitignore` 中（`.gitignore:8`），故 113 个产物目录不入库；
   `TRAJECTORY_SELECTION.json`、两个脚本以及 `EXEC_LOG_2026-09-04-trajectories.md`
   在验证时**仍是 untracked 状态（未 commit）**。按指示本次验证不做 commit，此处仅提示。

### 验证方的合规声明

- 未修改任何被验证的脚本或产物；`git status` 仅显示执行方遗留的 untracked 文件，无 modified 项。
- 未执行任何 git commit / push 或其他写远端操作。
- 本次仅新增本文件 `deepswe/data/trajectories/VERIFY.md`。
