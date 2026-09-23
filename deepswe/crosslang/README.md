# DeepSWE 重放包 · 113 条

按 agent 的原始 trace 在容器里逐条重放命令，用
**「容器内 `git diff` 出来的 patch 与 agent 当初提交的 `model.patch` 逐字节相同」**
这一条硬标准，确认这台服务器的环境与原 harness 等价。

本文件就是操作说明，从解包到出结果一条路走完。
其余文档都是辅助：`RUNBOOK.md` 是深入排查用的参考手册，`INDEX.md` 是历史分析材料。

**主流程 —— 顺序是有讲究的，别跳步：**

```
解包核对 → check_sources.sh → build_arm.sh（先建一批）→ preflight.sh → run_batch.py
   §1           §2                  §3                      §4            §5
```

- **探源为什么在最前**：`check_sources.sh` 几秒钟跑完，先告诉你哪几条建得成；
  而建镜像最慢的一条要几十分钟。先探再建，少走弯路。
- **预检为什么在建镜像之后**：`preflight.sh` 的价值不在查版本号，而在**真起一个容器**
  把 `replay.py` 依赖的每项能力跑一遍（netns 共享、资源限额、镜像内的 git/timeout…）。
  包里**不带镜像**，手里一个都没有时这段会整体跳过 —— 那次预检等于什么都没验。
  所以必须先建成至少一条再回头预检；`build_arm.sh` 跑完自己也提示「下一步：
  `bash preflight.sh`」。

---

## 0. 前置条件

| 必须有 | 说明 |
|---|---|
| `docker` | 能起容器、能 build |
| `python3` | 3.8+，**只用标准库**，无需 pip install |
| **`mars-base` 基座镜像** | 所有 113 个镜像都 `FROM mars-base`。**基座不在，一条都建不了** |
| 能访问各包源 | 构建期要 clone github、取 npm/pypi/goproxy/crates |

基座拉不到就 `docker load` 一份进来。核对「基座在不在、各包源通不通」的是
`check_sources.sh`（见 §2）——`preflight.sh` 不查这两样，它查的是容器能力。

⚠️ **重放期不需要外网、也不该有** —— 重放容器是 `--network=none` + 403 sinkhole，
刻意还原原 harness 的 `allow_internet=false`。要联网的只有构建期。

---

## 1. 解包与核对

```bash
tar xzf deepswe-replay-bundle-*.tar.gz && cd deepswe-replay-bundle
sha256sum -c SHA256SUMS      # 传输完整性
cat BUILD_INFO               # 这份包是哪个 commit 打的、各脚本的 sha256
```

## 2. 探包源

```bash
bash check_sources.sh
```

打各上游的**真实端点**看 HTTP 状态码——原环境的 `allow_internet=false` 就是靠代理返
403 实现的，TCP 通、DNS 通但 HTTP 被拒，所以光 ping 说明不了问题。顺带核对
**`mars-base` 基座是否已在本地**：它是 113 个 Dockerfile 的 `FROM`，不在的话一条都建不了。

某个源不通只挡掉用它的那几条，**不必等全绿才开工**；只有 github 不通才是真的没法开始。

## 3. 建镜像 —— 最耗时的一步

包里**没有镜像**，带的是每个 task 的 `environment/Dockerfile`（配方）。
113 条 trial 对应 **113 个镜像**，一个 task 一条 trial 一个镜像。

```bash
bash build_arm.sh --trials-dir full_trials --list python      # 只看会怎么改写 Dockerfile，不构建
bash build_arm.sh --trials-dir full_trials python             # 真建
```

⚠️ **`--trials-dir full_trials` 是仓库 clone 里的写法**（113 条在这个子目录）；
解包后的 bundle 里 trial 平铺在包根目录，**去掉这个参数**（给了会报「--trials-dir 不是目录」）。

**指定建哪些** —— 语言名、trial 目录名前缀、`all` 都行，且可并列多个：

```bash
bash build_arm.sh --trials-dir full_trials typescript                      # 该语言全部 34 条
bash build_arm.sh --trials-dir full_trials true-myth                       # 前缀匹配，单条
bash build_arm.sh --trials-dir full_trials koota                           # 前缀匹配，一批（koota 有 5 条）
bash build_arm.sh --trials-dir full_trials true-myth koota-query vitest    # 并列多个
bash build_arm.sh --trials-dir full_trials all                             # 全建，按依赖从少到多排序
```

失败的条目会在结尾列出来，按同样的前缀单独重试即可。已存在的镜像会跳过
（要重建先 `docker rmi <tag>`）。

**内网环境的两个常用开关：**

```bash
bash build_arm.sh --trials-dir full_trials --proxy http://proxy:port python    # 构建期代理
bash build_arm.sh --trials-dir full_trials --ca-cert corp-ca.crt python        # 内网 TLS 中间人的 CA
```

CA 不知道从哪来就跑 `bash get_ca_cert.sh`（会抠出来并验证可用）；
不确定内网到底有没有做中间人就跑 `bash detect_mitm.sh`。

## 4. 环境预检

```bash
bash preflight.sh                # 默认口径
bash preflight.sh --metrics      # 要采性能指标时才加：连 cgroup 一起验
```

真起容器逐项验证能力，**有 ❌ 先解决再往下**。默认不要求 cgroup v2 可读
（那是 `--metrics` 才需要的），rootless docker / cgroup v1 的机器也能跑——
`preflight.sh` 和 `run_batch.py` 在这一点上是同一个门控。

**镜像缺失只记警告、不算失败**：113 个不可能一次建齐，边建边跑本来就是设计好的流程
（`run_batch.py --skip-missing` 就是为它准备的）。但**一个镜像都没有时活体测试整段会
跳过**，等于什么都没验到 —— 这种情况先回上一步建成至少一条，再回来跑预检。

## 5. 重放

```bash
python3 run_batch.py --trials-dir full_trials --smoke 5 --skip-missing      # 冒烟：每条只跑前 5 条命令
python3 run_batch.py --trials-dir full_trials --skip-missing --keep-going   # 正式跑
```

- `--trials-dir full_trials` —— **仓库 clone 里必须给**：113 条在这个子目录，不给只扫 `crosslang/` 根下那几条。
  解包后的 bundle 里 trial 平铺在包根目录，去掉它（给了反而报「--trials-dir 不是目录」）
- `--skip-missing` —— 镜像还没建好的自动跳过，而不是整批拒绝启动。**边建边跑靠它。**
- `--keep-going` —— 某条失败后继续跑剩下的（默认遇错即停）
- `--only python,go` —— 只跑指定语言

**「只跑哪几条」目前只能通过「只建哪几条的镜像」+ `--skip-missing` 间接实现** ——
`run_batch.py` 的 `--only` 只认语言，不认 trial 名。真要精确跑单条就直接调重放器：

```bash
python3 ../replay.py full_trials/<trial> full_trials/<trial>/task.json -o /tmp/one
```

（这是仓库 clone 里在 `crosslang/` 下的写法，`replay.py` 在上一层。bundle 里 `replay.py` 和 trial 都在包根目录：
`python3 replay.py <trial> <trial>/task.json -o /tmp/one`。）

## 6. 读结果

结果落在 `runs/<UTC 时间戳>/`：

| 文件 | 干什么用 |
|---|---|
| `SUMMARY.md` | 人看的汇总 |
| `summary.json` | 机器读的汇总 |
| `run_cmd.txt` | 本轮实际运行命令、工作目录和记录时间，便于复现 |
| `logs/<trial>.log` | 逐条实时输出，跑的过程中就能 `tail -f` |
| `<trial>/verdict.json` | 单条判定 |
| `<trial>/commands.jsonl` | per-command 指标 |
| `<trial>/replayed.patch` | 重放后从容器里 diff 出来的 patch |
| `cmd_stats/SUMMARY.md` 等 | 命令类型 × 次数/耗时统计（per-benchmark / 语言 / 全体），一轮跑完自动出 |

命令类型统计只算「本轮成功跑起来」的 trial（判据同 `n_pass`，另外排除被 `--smoke` 截断的、
`commands.jsonl` 缺失或条数对不上的），排除了谁、为什么都写在 `cmd_stats/SUMMARY.md` 里。
自动那次失败了不影响重放结果，手动重跑 `python3 cmd_stats.py runs/<时间戳>`；口径见 `RUNBOOK.md` §5.5。

**唯一的硬标准是 `patch_identical=true`。** `rc_match`、耗时这些都允许有出入
（已知有 4 类不可消除的差异），口径见 `RUNBOOK.md` §5。

---

## 7. ARM Topdown 微架构分析

### 7.1 整条 trial 聚合 topdown

每条 trial 采一次 ARM L1 topdown 四象限（Retiring / BadSpec / FrontendBound / BackendBound），
覆盖整条 trial 的 PMU 计数。

```bash
# 先验 PMU 可用性
bash probe_pmu.sh

# 批量采（串行，跳过没镜像的）
python3 run_batch.py --trials-dir full_trials --topdown --skip-missing --no-metrics

# 每种语言抽 1 条先看横向
python3 run_batch.py --trials-dir full_trials --topdown --per-lang 1 --pick lightest --skip-missing --no-metrics
```

### 7.2 Per-step topdown（10ms interval + 事后按 step 归并）

加 `--per-step` 让 `topdown_trial.sh` 用 `perf stat -I 10`（每 10ms 一组精确计数），
事后按 step 时间窗口归并，得到 per-step 的四象限。

```bash
# 批量采集 per-step 数据
python3 run_batch.py --trials-dir full_trials --topdown --per-step \
       --skip-missing --no-metrics

# 每种语言抽 1 条快速验证
python3 run_batch.py --trials-dir full_trials --topdown --per-step \
       --per-lang 1 --pick lightest --skip-missing --no-metrics

# 单条 trial 手动跑
bash topdown_trial.sh full_trials/<trial> --per-step --no-metrics
```

每条 trial 产出 `<out>/<trial>/topdown/topdown_steps.json`，含每个 step 的四象限向量。

### 7.3 三级聚类分析

收集完 per-step 数据后，用 `topdown_cluster.py` 做三级聚类：

- **Level 1 per-trial**：每个 trial 内部独立聚类
- **Level 2 per-language**：同语言的 step 合并后聚类
- **Level 3 all-trials**：全部 step 合并后聚类

聚类方法：L∞ 贪心——每个 step 的四象限 `(Ret, Bad, FE, BE)` 作为 4 维向量，
按 cycles 降序处理，尝试加入已有 cluster（加入后任意两点任意单维差 < 阈值），
不行就新建，自然找到最少的 cluster 数。

```bash
# 三级全做（默认阈值 10%）
python3 topdown_cluster.py <out_dir>/

# 收紧阈值
python3 topdown_cluster.py <out_dir>/ --threshold 0.08

# 只做 all-trials 级
python3 topdown_cluster.py <out_dir>/ --level all

# 只看某语言
python3 topdown_cluster.py <out_dir>/ --only-lang go

# 排除某语言的 benchmark（例如与其他人的 Python 工作去重）
python3 topdown_cluster.py <out_dir>/ --exclude-lang python

# 一次排除多种语言（也可重复传入 --exclude-lang）
python3 topdown_cluster.py <out_dir>/ --exclude-lang python,javascript
```

`--exclude-lang` 会在聚类前过滤 benchmark，因此屏幕统计、JSON、各级 Excel、
`steps.xlsx` 和 `tool_summary.xlsx` 都不会包含被排除语言的数据。可选语言限定为
`python` / `go` / `rust` / `typescript` / `javascript`，传入其他值会在读取数据前报错退出。

输出 `topdown_cluster.json`，含三个层级各自的 cluster 列表，每个 cluster 记录：
steps 数 / wall_s / cycles / 占比 / cycles 加权四象限 / max_spread / 成员 trial 列表 / 代表命令。

---

## 规模与预算

| 语言 | trial 数 | 命令数 |
|---|---|---|
| go | 35 | 1075 |
| python | 34 | 1381 |
| typescript | 34 | 1590 |
| rust | 5 | 316 |
| javascript | 5 | 157 |
| **合计** | **113** | **4519** |

### 全量 113 条 benchmark × 语言对照

| trial_name | 语言 | 命令数 | task_id |
|---|---|---|---|
| adaptix-name-mapping-aliases__wtGF4BR | python | 53 | adaptix-name-mapping-aliases |
| aiomonitor-task-snapshots-diff__TAdGEJP | python | 25 | aiomonitor-task-snapshots-diff |
| bandit-incremental-cache-control__xCCpZfF | python | 26 | bandit-incremental-cache-control |
| bandit-interprocedural-taint-che__89L9Wju | python | 22 | bandit-interprocedural-taint-checks |
| bandit-structured-nosec-directiv__keYoMPc | python | 27 | bandit-structured-nosec-directives |
| cattrs-partial-structuring-recov__sdiJgDB | python | 19 | cattrs-partial-structuring-recovery |
| dateutil-rfc5545-timezone-intero__dFVN4cS | python | 47 | dateutil-rfc5545-timezone-interop |
| fastapi-deprecation-response-hea__jvw4ph9 | python | 24 | fastapi-deprecation-response-headers |
| fastapi-implicit-head-options__QQLdziE | python | 37 | fastapi-implicit-head-options |
| gql-incremental-graphql-delivery__caFVsA8 | python | 439 | gql-incremental-graphql-delivery |
| httpx-deterministic-cookie-store__mwrhyzw | python | 17 | httpx-deterministic-cookie-store |
| httpx-multipart-response-parsing__e6gDZHf | python | 14 | httpx-multipart-response-parsing |
| httpx-streaming-json-iteration__XgiLy6j | python | 10 | httpx-streaming-json-iteration |
| igel-persist-feature-schema__QGv8W5Q | python | 19 | igel-persist-feature-schema |
| ipython-session-bundle-replay__YMELQyX | python | 49 | ipython-session-bundle-replay |
| kombu-single-active-consumer-pri__vGRAfVv | python | 26 | kombu-single-active-consumer-priority |
| kombu-virtual-queue-dead-letteri__Hoa5EYu | python | 29 | kombu-virtual-queue-dead-lettering |
| langchain-request-coalescing__3thYcgr | python | 19 | langchain-request-coalescing |
| mashumaro-flattened-dataclass-fi__prskZor | python | 32 | mashumaro-flattened-dataclass-fields |
| mnamer-daemon-watch-lifecycle__yawTb7o | python | 21 | mnamer-daemon-watch-lifecycle |
| mobly-grouped-test-barriers__3JsWTSG | python | 22 | mobly-grouped-test-barriers |
| narwhals-rolling-window-suite__no6TBSa | python | 52 | narwhals-rolling-window-suite |
| numba-stencil-boundary-modes__3k4NEcP | python | 38 | numba-stencil-boundary-modes |
| psd-tools-blend-range-api__NWEBmgd | python | 23 | psd-tools-blend-range-api |
| pwntools-tube-multiplexing__ZBsXTH8 | python | 22 | pwntools-tube-multiplexing |
| python-statemachine-state-data-s__mwUxrAk | python | 40 | python-statemachine-state-data-scoping |
| returns-validated-error-accumula__sT7wC5g | python | 37 | returns-validated-error-accumulation |
| skrub-duration-encoding__Q2ZoPsT | python | 41 | skrub-duration-encoding |
| sqlfmt-create-table-ddl-formatti__LPd6nRg | python | 42 | sqlfmt-create-table-ddl-formatting |
| sqlite-utils-safe-import-checkpo__2Q6mWKW | python | 21 | sqlite-utils-safe-import-checkpoints |
| textual-kitty-key-phases__ceL6poB | python | 27 | textual-kitty-key-phases |
| textual-richlog-follow-state__nWk3rp2 | python | 19 | textual-richlog-follow-state |
| tomlkit-toml-table-converters__C3zxLJs | python | 13 | tomlkit-toml-table-converters |
| vulture-persistent-analysis-cach__nVmFENw | python | 29 | vulture-persistent-analysis-cache |
| abs-module-cache-flags__RiQqZb3 | go | 42 | abs-module-cache-flags |
| abs-stepped-slices__V4RCabW | go | 19 | abs-stepped-slices |
| actionlint-action-pinning-lint__f62DK3Z | go | 28 | actionlint-action-pinning-lint |
| anko-default-function-arguments__prQWdxC | go | 27 | anko-default-function-arguments |
| anko-typed-variable-bindings__5WDXhNz | go | 21 | anko-typed-variable-bindings |
| arcane-drift-detection-baselines__Eg9A7iC | go | 39 | arcane-drift-detection-baselines |
| dasel-html-document-format__svLPtbM | go | 21 | dasel-html-document-format |
| etree-xml-diff-patch__87hqQfM | go | 10 | etree-xml-diff-patch |
| expr-try-catch-errors__Goo2y6e | go | 45 | expr-try-catch-errors |
| geo-shapeindex-serialization__twUEtKk | go | 17 | geo-shapeindex-serialization |
| go-critic-doc-link-checker__HCp5QEM | go | 24 | go-critic-doc-link-checker |
| go-genai-streamed-function-args__pKZyyhT | go | 21 | go-genai-streamed-function-args |
| go-git-worktree-merge-conflicts__RZak54r | go | 30 | go-git-worktree-merge-conflicts |
| goreleaser-retry-publish-auditin__2SRQ5B6 | go | 31 | goreleaser-retry-publish-auditing |
| helm-array-merge-strategies__PqGYtp5 | go | 85 | helm-array-merge-strategies |
| helm-unified-manifest-stream__wD6dHmm | go | 42 | helm-unified-manifest-stream |
| kcp-go-multiplexed-kcp-streams__ESHk9j6 | go | 10 | kcp-go-multiplexed-kcp-streams |
| kgateway-consistent-hash-policy__8b6fELy | go | 47 | kgateway-consistent-hash-policy |
| onedump-dump-encryption-pipeline__occpiWG | go | 22 | onedump-dump-encryption-pipeline |
| opa-rego-rule-profiling__GwpVT2D | go | 20 | opa-rego-rule-profiling |
| opa-template-string-reconstructi__7gsVRR2 | go | 40 | opa-template-string-reconstruction |
| participle-grammar-conflict-anal__PicnMbY | go | 20 | participle-grammar-conflict-analysis |
| pebble-durability-wait-apis__b52qe5e | go | 44 | pebble-durability-wait-apis |
| prometheus-transactional-reload__zxrhV6T | go | 36 | prometheus-transactional-reload-status |
| prometheus-typed-label-sorting__xUbBphr | go | 16 | prometheus-typed-label-sorting |
| scc-bounded-memory-spilling__TjxRrcX | go | 23 | scc-bounded-memory-spilling |
| scriggo-method-declarations__953BwaT | go | 91 | scriggo-method-declarations |
| task-task-graph-export__7yRV3qm | go | 35 | task-task-graph-export |
| tengo-callable-instance-isolatio__HRRuV87 | go | 26 | tengo-callable-instance-isolation |
| tengo-destructuring-bindings__YFEjJAm | go | 50 | tengo-destructuring-bindings |
| termenv-preserve-ansi-resets__3gLqLDY | go | 17 | termenv-preserve-ansi-resets |
| updo-policy-alerting__9htVrva | go | 18 | updo-policy-alerting |
| wazero-multi-module-snapshots__G6CQSK5 | go | 10 | wazero-multi-module-snapshots |
| yaegi-go-embed-directives__4NhDvzY | go | 34 | yaegi-go-embed-directives |
| ytt-jsonpath-query-api__RYWCgis | go | 14 | ytt-jsonpath-query-api |
| boa-hierarchical-evaluation-canc__jmGsoPE | rust | 60 | boa-hierarchical-evaluation-cancellation |
| fd-deterministic-multi-key-sorti__tnQJDgA | rust | 23 | fd-deterministic-multi-key-sorting |
| oxvg-structural-selector-preserv__RujxJ4b | rust | 140 | oxvg-structural-selector-preservation |
| pest-character-class-coalescing__Xzpn5cx | rust | 37 | pest-character-class-coalescing |
| wasmi-trap-coredumps__87BtxHX | rust | 56 | wasmi-trap-coredumps |
| arktype-json-schema-refs-depende__LxqDbuL | typescript | 75 | arktype-json-schema-refs-dependencies |
| awilix-async-container-initializ__yTZ3pNw | typescript | 22 | awilix-async-container-initialization |
| clack-async-autocomplete-options__wT3ayLg | typescript | 21 | clack-async-autocomplete-options |
| claude-code-by-agents-recursive__hU9i6Zd | typescript | 19 | claude-code-by-agents-recursive-delegation |
| cliffy-config-file-parsing__fqsBa2P | typescript | 22 | cliffy-config-file-parsing |
| drizzle-orm-window-function-buil__LPZgzqP | typescript | 21 | drizzle-orm-window-function-builders |
| dynamodb-toolbox-conditional-att__jbHHSxk | typescript | 59 | dynamodb-toolbox-conditional-attribute-requirements |
| dynamodb-toolbox-lazy-recursive__RfCwzxE | typescript | 92 | dynamodb-toolbox-lazy-recursive-schemas |
| effect-sse-httpapi-streaming__cWrUjkR | typescript | 174 | effect-sse-httpapi-streaming |
| eicrud-keyset-pagination-cursor__HwJLCCX | typescript | 35 | eicrud-keyset-pagination-cursor |
| happy-dom-abort-pending-body-rea__wJwHMUw | typescript | 31 | happy-dom-abort-pending-body-reads |
| happy-dom-deterministic-intersec__YiQFxph | typescript | 16 | happy-dom-deterministic-intersectionobserver |
| ink-grid-box-layout__byspTz2 | typescript | 41 | ink-grid-box-layout |
| kea-atomic-signal-selectors__Tjngtum | typescript | 28 | kea-atomic-signal-selectors |
| koota-composite-trait-aspects__SHJswD4 | typescript | 94 | koota-composite-trait-aspects |
| koota-deferred-mutation-buffer__EDVbxCw | typescript | 61 | koota-deferred-mutation-buffer |
| koota-entity-snapshot-rollback__wrEzQ8W | typescript | 23 | koota-entity-snapshot-rollback |
| koota-pair-relation-tracking__DQMatVZ | typescript | 56 | koota-pair-relation-tracking |
| koota-query-predicates__eQvCM4F | typescript | 31 | koota-query-predicates |
| kysely-window-grouping-helpers__8E2VJHH | typescript | 40 | kysely-window-grouping-helpers |
| meriyah-explicit-resource-declar__imBZ9iy | typescript | 113 | meriyah-explicit-resource-declarations |
| obsidian-linter-auto-table-of-co__HbwSKY7 | typescript | 50 | obsidian-linter-auto-table-of-contents |
| obsidian-linter-link-format-conv__Jz3j6p2 | typescript | 30 | obsidian-linter-link-format-conversion |
| obsidian-linter-scoped-ignore-ma__49j959X | typescript | 23 | obsidian-linter-scoped-ignore-markers |
| ofetch-per-origin-circuit-breake__HDZ7sS3 | typescript | 14 | ofetch-per-origin-circuit-breaker |
| optique-conditional-option-depen__MbK6FSg | typescript | 125 | optique-conditional-option-dependencies |
| query-persist-restored-query-sta__9hJcn9K | typescript | 34 | query-persist-restored-query-state |
| quill-shared-toolbar-focus__jPYaizC | typescript | 42 | quill-shared-toolbar-focus |
| sql-formatter-bigquery-pipe-form__QjWxfnT | typescript | 27 | sql-formatter-bigquery-pipe-formatting |
| superjson-error-stack-serializat__DNvPvUJ | typescript | 53 | superjson-error-stack-serialization |
| true-myth-iterable-collection-co__zVw8vPV | typescript | 23 | true-myth-iterable-collection-combinators |
| ts-pattern-match-each__hdvbo33 | typescript | 11 | ts-pattern-match-each |
| valibot-recursive-schema-composi__SUQiK76 | typescript | 46 | valibot-recursive-schema-composition |
| vitest-duration-sharding__3XHkvAD | typescript | 38 | vitest-duration-sharding |
| csstree-shorthand-expansion-comp__XhiQVFb | javascript | 31 | csstree-shorthand-expansion-compression |
| katex-multicolumn-array-spans__ytP7ccc | javascript | 29 | katex-multicolumn-array-spans |
| testem-bail-on-test-failure__zFp2HSU | javascript | 44 | testem-bail-on-test-failure |
| testem-per-launcher-reports__Ud2ShPN | javascript | 21 | testem-per-launcher-reports |
| yjs-map-conflict-detection__cAHUYjt | javascript | 32 | yjs-map-conflict-detection |

⚠️ **上游 `task.toml` 里有 3 个 task 的语言标错了**，本包已修正（`make_full_trials.py`
里有一张带证据的修正表，运行时会打印修正了哪几条）：

| task | 上游标注 | 实际 | 判据 |
|---|---|---|---|
| `prometheus-transactional-reload-status` | typescript | **go** | Dockerfile 只有 `go mod download`/`go install`，零 npm |
| `httpx-deterministic-cookie-store` | typescript | **python** | `pip install` |
| `koota-entity-snapshot-rollback` | python | **typescript** | `pnpm install` |

这不是洁癖：`build_arm.sh` 按语言展开构建目标、`run_batch.py --only` 按语言过滤，
标错会让你 `build_arm.sh python` 时莫名撞进一个跑 `pnpm install` 的 Node 仓库。

> 4519 是 **trace 里的命令总数**，含每条 trial 末尾那条哨兵命令
> （`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`，原始运行里就没执行过，
> `replay.py` 的 `load_trace` 标 `sentinel=True` 后跳过）。
> 一条 trial 一条哨兵，所以**实跑 4519 − 113 = 4406 条**。下面的时间预算按 4519 估，
> 偏保守。

| | 预算 | 依据 |
|---|---|---|
| 建镜像 | **8~12 小时 / 25~35 GB** | python 144s、typescript ~280s 实测外推 |
| 重放 | **4~4.5 小时** | 2.35 s/命令实测 × ARM 1.4 折算 |

rust 那 5 条单独留时间：Dockerfile 里有 `cargo nextest run --no-run`，把测试二进制
整个编译一遍，是全部 113 个里**最重的编译步骤**——但不是唯一一个：`pest` 另有
`cargo build --package pest_bootstrap`，`eicrud` 有两处 `npm run compile`（tsc），
`goreleaser` 有 `go build ./...`。

---

## 三个必须先知道的坑

**1. 这版包里没有任何已验证基线**

`<trial>/replay/verdict.json` 本来是「上一台机器的判定」，用于跨机对比。
**这一版一个都没有**（按要求去掉了 5 条已验证对照组）。`run_batch.py` 会对每条都打
「无基线」，`vs_baseline` 恒为 `None` —— 这是优雅降级，不会报错。

后果要心里有数：**某条失败时，无法区分「这个 task 有问题」和「整套流程有问题」。**
需要对照组就在开发机上重跑 `make_full_trials.py`（不加 `--no-verified`）重新打包，
会变回 118 条、镜像数仍是 113、零额外构建成本。

**2. 重建出来的镜像 ≠ 原 amd64 镜像**

`build_arm.sh` 会改写 Dockerfile（换基座、按架构改二进制下载 URL 等），
每条的改写清单落在 `build/<trial>/REWRITES.md`，**跑之前先读一眼**。
即使改写为零也仍有漂移：`pnpm install` 未加 `--frozen-lockfile`、
工具链是另一个架构编译的、基座是 `:latest` 不可复现。

**3. `--registry` 换源不是默认解法**

构建卡在 `pnpm install` 时容易想到换源，但开发机上的对照实验显示**换源反而慢 18%**
（官方源 132.4s vs 镜像源 156.6s，同仓库同 548 个包）。因为瓶颈是每请求的固定开销
（RTT + TLS 握手）而不是带宽，换目标主机解决不了。

**开之前先测**（必须带着构建用的同一套代理环境变量）：

```bash
for h in registry.npmjs.org registry.npmmirror.com; do
  echo "--- $h ---"
  for i in 1 2 3; do
    curl -sS -o /dev/null \
      -w "  tls=%{time_appconnect}  total=%{time_total}  %{speed_download}B/s\n" \
      "https://$h/minimatch/-/minimatch-9.0.5.tgz"
  done
done
```

两个 host 的 `total` 接近 → 瓶颈是代理本身，**换源白搭**；npmmirror 明显低才开：

```bash
bash build_arm.sh --trials-dir full_trials --registry https://registry.npmmirror.com typescript
```

---

## 出问题去哪查

| 症状 | 去处 |
|---|---|
| 预检报错 | `RUNBOOK.md` §3 |
| 拉不到镜像 / ECR 不通 | `RUNBOOK.md` §2b |
| `git clone` 报证书错误 | `RUNBOOK.md` §2b 证书一节 → `get_ca_cert.sh` |
| **构建**卡在 `pnpm install` | `RUNBOOK.md` §2b「取包慢:换镜像源」 |
| **重放**某条卡住不动 | `RUNBOOK.md` §6.5（多数是 trace 里本来就有 `sleep` 轮询） |
| 找不到 cgroup 目录 | `RUNBOOK.md` §6.1（或干脆别加 `--metrics`） |
| 结果怎么读、什么算通过 | `RUNBOOK.md` §5 |

⚠️ **已知问题**：宿主代理挂在 loopback（`127.0.0.1:xxxx`）时 `build_arm.sh` 会自动加
`--network host`，但在 **Docker Desktop + WSL2** 上这套 BuildKit **不兑现**该参数，
`git clone` 直接失败。原生 Linux Docker 未复现。绕法见 `RUNBOOK.md` §2b 代理一节。

---

## 包里的文件

**主线**（按用到的顺序）

```
README.md          本文件 —— 操作说明
check_sources.sh   各包源可达性检查 + 基座在不在（建之前跑）
build_arm.sh       从本地基座重建 task 镜像
preflight.sh       环境预检（**建出至少一个镜像之后**再跑，见 §4）
run_batch.py       批量重放 driver
replay.py          单条重放器（只用 python 标准库）
cmd_stats.py       命令类型 × 次数/耗时统计（run_batch 收尾自动调，也可手动跑）
summarize_replay.py  命令分类器（cmd_stats.py 要 import 它）
topdown_trial.sh   单条 trial 的 ARM topdown 采集（perf stat -a -G，支持 --per-step）
topdown_parse.py   把 perf stat 输出算成 ARM L1 topdown 四象限 + 自检
topdown_steps.py   per-step topdown：解析 perf stat -I interval，按 step 归并
topdown_cluster.py 三级聚类（per-trial / per-language / all-trials），L∞ 贪心
topdown.conf       PMU 事件号 + SLOTS 配置
probe_pmu.sh       PMU 可用性探测（采 topdown 之前先跑）
```

**辅助**

```
RUNBOOK.md         参考手册：每个坑的成因、判据、绕法。出问题查它
INDEX.md           上一轮 5 条跨语言验证的完整分析（历史材料，非本轮口径）
get_ca_cert.sh     内网 TLS 中间人：抠公司 CA
detect_mitm.sh     判定内网是否真的在做 TLS 中间人
BUILD_INFO         包的来源：commit / 打包时间 / 各脚本 sha256
SHA256SUMS         传输完整性校验
release.json       DeepSWE v1.1 release 描述（产物 URL 模板）
```

**数据**：113 个 `<trial>/` 目录，每个含

```
meta.json          语言 / 模型 / 镜像 / base_commit / 命令数
trajectory.json    原始 trace（命令逐字记录）
model.patch        agent 最终提交的 patch —— 保真度比对基准
task.json          任务定义（含 task.toml 与 environment/Dockerfile）
replay/            重放产物落在这里（本版包内为空）
```

> **clone 仓库也能直接跑全量。** `full_trials/` 下 113 条的 `trajectory.json` 与 `model.patch` 已入库，
> clone 完在 `crosslang/` 下带 `--trials-dir full_trials` 即可（见 §3、§5）。
> `.gitignore` 只排除 `crosslang/` 根下那几条旧 trial 的这两个文件。
