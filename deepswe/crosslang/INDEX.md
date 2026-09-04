# 跨语言容器重放验证（crosslang）

`deepswe/replay.py` 按 agent trace 在长驻容器内串行重放命令，并以
「容器内 `git diff --binary <base_commit_hash> HEAD` 与下载到的 `model.patch` 逐字节比对」
作为保真度硬校验。本目录把该流程从 1 条 python task 扩到 5 种语言各 1 条。

## 选样口径

- 每种语言（python / go / rust / typescript / javascript）各取 1 条 trial，人工指定，非随机抽样。
- 五条都来自 DeepSWE release `v1.1`，产物经公开 CloudFront 直取（无鉴权）。
- 五条分属 **5 个不同模型**（见下表）——因此语言之间的差异里混着模型差异，
  本轮只用于验证「重放流程是否跨语言成立」，不能拿来做语言间的负载横向对比。
- 资源上限全部为 `cpus=2 / memory_mb=8192 / allow_internet=false`，与 `task.toml` 对齐。

## 目录结构

```
deepswe/crosslang/
├── INDEX.md                  本文件
├── build_index.py            汇总脚本（重跑即可再生成本文件）
├── release.json              DeepSWE v1.1 release 描述（产物 URL 模板）
└── <trial_name>/
    ├── meta.json             语言 / 模型 / 步数 / 镜像引用 / token 与成本
    ├── trajectory.json       原始 trace（ATIF v1.7）
    ├── model.patch           agent 最终提交的 patch（保真度比对基准）
    ├── task.json             任务定义副本（拷自 deepswe/data/tasks/<task_id>.json）
    ├── mini-swe-agent.txt    agent 原始日志
    ├── test-stdout.txt       verifier 输出
    └── replay/               重放产物
        ├── verdict.json      判定（patch_identical / rc_match / timing_divergences）
        ├── commands.jsonl    per-command 指标（wall / cpu / io / mem_peak）
        └── replayed.patch    重放后从容器里 diff 出来的 patch
```

## 五条 trial 元数据

| 语言 | trial_name | task_id | 仓库 | 模型 | steps | 命令数 | model.patch |
|---|---|---|---|---|---|---|---|
| python | `returns-validated-error-accumula__8JQj5gw` | `returns-validated-error-accumulation` | dry-python/returns | `moonshot/vellise-0716` | 72 | 99 | 63,009B |
| go | `actionlint-action-pinning-lint__23b2uyq` | `actionlint-action-pinning-lint` | rhysd/actionlint | `openai/gpt-5.6-luna` | 72 | 70 | 31,161B |
| rust | `fd-deterministic-multi-key-sorti__fK6jc93` | `fd-deterministic-multi-key-sorting` | sharkdp/fd | `vertex_ai/claude-opus-5` | 74 | 77 | 42,279B |
| typescript | `true-myth-iterable-collection-co__BBLS6Fy` | `true-myth-iterable-collection-combinators` | true-myth/true-myth | `anthropic/claude-opus-4-8` | 62 | 60 | 39,714B |
| javascript | `yjs-map-conflict-detection__gSSidka` | `yjs-map-conflict-detection` | yjs/yjs | `openai/gpt-5.5` | 68 | 66 | 32,899B |

镜像（同一 base `public.ecr.aws/x8v8d7g8/mars-base:latest`，各 task 一个成品 tag）：

| 语言 | docker_image | base_commit_hash |
|---|---|---|
| python | `public.ecr.aws/d3j8x8q7/swe-bench-202605:kh754n098chwgtm24jakheqsw5833ec6-v1.1` | `41607fae1289` |
| go | `public.ecr.aws/d3j8x8q7/swe-bench-202605:kh79dnvkvq8j9bs22ededmsc79823akj-v1.1` | `0bdc95715fa5` |
| rust | `public.ecr.aws/d3j8x8q7/swe-bench-202605:kh79s1ny2ab454f8caet44rv5n82za06-v1.1` | `227883606023` |
| typescript | `public.ecr.aws/d3j8x8q7/swe-bench-202605:kh74r2t7kdnt7h2efdk0hf5asx82zr0s-v1.1` | `d8fbebc75de4` |
| javascript | `public.ecr.aws/d3j8x8q7/swe-bench-202605:kh7fwz4nedevfex8ssk2p8xbt9836scp-v1.1` | `7795050a749b` |

## 镜像拉取成本（2026-09-04 实测，串行拉取）

五个镜像串行拉取，重试策略为指数退避（10s → 20s → … 上限 300s，最多 6 次）。
本机已有一个同批次镜像 `…kh79vjbp8dv1…-v1.1`，作为层复用的基线。

| 语言 | 起止 | 耗时 | 完整下载 | 去重后实际下载 | 重试次数 |
|---|---|---|---|---|---|
| python | 10:41:55–10:42:35 | 40s | 762 MB | **11 MB** | 0 |
| go | 10:42:40–10:44:08 | 88s | 779 MB | **28 MB** | 0 |
| rust | 10:44:13–10:59:12 | 899s | 1013 MB | **263 MB** | 0 |
| typescript | 10:59:17–11:07:42 | 505s | 867 MB | **117 MB** | 0 |
| javascript | 11:07:47–11:12:58 | 311s | 833 MB | **82 MB** | 0 |

- **合计 500 MB / 1,871s（31 分钟）**，与预估的「增量约 500 MB」一致。
- 有效吞吐 **≈0.27 MB/s**（约 2.2 Mbit/s），五条之间高度一致（0.23–0.32 MB/s）：
  耗时几乎完全由下载字节线性决定，解包不是瓶颈。
- **全程 0 次 ECR 限流**：没有出现 `toomanyrequests` / 429 / retry，5 个镜像全部 attempt=1 一次成功。
  退避重试逻辑保留，但本轮未被触发。
- 全量 113 个 task 镜像的预算（只取 manifest 统计、未真拉）：
  各镜像完整下载求和 **111 GB**，层去重后 **24.3 GB**；首个镜像 840 MB（公共基座），
  之后每个平均增量 **210 MB**。按实测 0.27 MB/s 推算，全量串行拉取约 **25 小时**——
  这是批量化的主要时间成本，建议提前预拉或换更快的出口。

## 重放结果汇总

| 语言 | patch_identical | rc_match | rc_match_semantic | timing_divergences | 命令数 | 总耗时 | CPU 总量 | s/cmd |
|---|---|---|---|---|---|---|---|---|
| python | ✅ 是 | 95/98 | 97/98 | 1 | 98 | 243s | 330s | 2.48 |
| go | ✅ 是 | 67/69 | 68/69 | 0 | 69 | 112s | 179s | 1.63 |
| rust | ✅ 是 | 72/76 | 73/76 | 1 | 76 | 298s | 91s | 3.93 |
| typescript | ✅ 是 | 58/59 | 58/59 | 1 | 59 | 103s | 120s | 1.75 |
| javascript | ✅ 是 | 65/65 | 65/65 | 0 | 65 | 208s | 268s | 3.20 |

- `patch_identical`：容器内 `git diff --binary <base> HEAD` 与 `model.patch` 逐字节相等。
- `rc_match`：重放退出码与 trace 记录严格逐条相等。
- `rc_match_semantic`：额外把「trace 侧 -1（被原 harness 打死）↔ 重放侧超时」算作匹配。
- `timing_divergences`：只有一侧超时的条数（重放机与原机速度差导致，预期存在）。
- CPU 总量 = 各命令 cgroup `cpu.stat/usage_usec` 差值之和（2 核上限，可 > 墙钟的 1 倍）。

## 执行器口径：`bash -c` → `bash -lc`

本轮把 `replay.py` 里 `docker exec … bash -c <cmd>` 改成 `bash -lc`，对齐原 harness
（mini-swe-agent `environments/docker.py:38`：`interpreter = ["bash", "-lc"]`）。
在镜像里实测两种口径的差别如下：

```
PATH(-c)  = /root/go/bin:/root/.cargo/bin:/root/.local/bin:/root/.rye/shims:/root/.bun/bin:
            /usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PATH(-lc) = /root/.bun/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:
            /usr/bin:/sbin:/bin:/root/.local/bin:/root/.local/bin
```

- 机制：`-lc` 是登录 shell，`/etc/profile` 会**整体覆写** PATH（`PATH="/usr/local/sbin:…"`），
  随后 `/root/.profile` 再 `. "$HOME/.cargo/env"` 和 pipx 的 `.local/bin` 把两条路径加回来。
- **结论与预期相反**：`cargo` / `rustc` 在两种口径下都能解析到 `/root/.cargo/bin`——
  因为 mars-base 已经把它烘进了镜像的 ENV PATH，rust 并不是差异暴露点。
- `-lc` 真正的行为差异是**丢掉了 `/root/go/bin` 和 `/root/.rye/shims`**
  （它们只存在于 docker ENV，被 `/etc/profile` 覆写掉了）。`/root/go/bin` 里是 go 镜像
  额外装的 `go-ctrf-json-reporter`。已核对：五条 trace 里**没有任何命令**用到这两个目录下的
  可执行文件（`go` / `gofmt` 本体在 `/usr/local/bin`，`cargo-nextest` 在 `/usr/local/bin`），
  所以本轮改动对命令解析**无实际影响**；但它是原 harness 的真实口径，改了更保真。
- 另已验证 `-lc` **不改变工作目录**（`docker exec -w /app` 下 `pwd` 仍是 `/app`），
  `/root/.profile` 里没有 `cd`，相对路径命令不受影响。

## 已知风险

- `replay.py` 启动时会 `docker rm -f <容器名>`，容器名由 trial 目录名推导
  （`replay_<trial_name>`，截断到 60 字符）。**两个进程同时重放同一条 trial 会静默互杀**——
  后启动的那个会把先启动的容器删掉，先启动的那条从此每条命令都失败。
  批量化时必须保证同一 trial 只有一个重放进程。

## 命令构成差异（工具链关键词命中次数）

| 工具 | python | go | rust | typescript | javascript |
|---|---|---|---|---|---|
| `cargo` |  |  | 13 |  |  |
| `clippy` |  |  | 2 |  |  |
| `go test` |  | 19 |  |  |  |
| `go vet` |  | 2 |  |  |  |
| `go run` |  | 4 |  |  |  |
| `gofmt` |  | 19 |  |  |  |
| `npm` |  |  |  |  | 8 |
| `npx` |  |  |  | 14 | 8 |
| `node` |  |  |  | 1 | 19 |
| `tsc` |  |  |  | 2 | 5 |
| `vitest` |  |  |  | 9 |  |
| `pytest` | 26 |  |  |  |  |
| `python3` |  | 17 | 12 | 7 |  |
| `python -` | 43 |  |  |  | 16 |
| `mypy` | 2 |  |  |  |  |

首 token 分布（剥掉 `cd X &&` 前缀后）：

- **python**：`cat`×25, `python`×19, `timeout`×13, `git`×9, `sed`×7, `grep`×7, `ls`×6, `head`×3
- **go**：`sed`×18, `python3`×16, `git`×9, `grep`×9, `cat`×5, `go`×4, `nl`×3, `pwd`×1
- **rust**：`sed`×13, `python3`×11, `grep`×9, `ls`×6, `cat`×6, `git`×6, `(nohup`×5, `rm`×5
- **typescript**：`cd`×57, `ls`×2
- **javascript**：`nl`×22, `python`×11, `node`×10, `git`×6, `grep`×4, `cat`×4, `npm`×2, `npx`×2

## 30s 超时归因

原 harness 与本重放都用 30s 单命令超时。分别统计**原始机器上**（trace 侧，未受任何干扰）
和**重放机器上**被砍掉的命令：

| 语言 | trace 侧超时 | 重放侧超时 | 只有一侧超时 | trace 侧超时命令形态 |
|---|---|---|---|---|
| python | 2 | 3 | 1 | 测试 |
| go | 1 | 1 | 0 | 编译+测试 |
| rust | 2 | 1 | 1 | 其他, 编译 |
| typescript | 0 | 1 | 1 | — |
| javascript | 0 | 0 | 0 | — |

- **「30s 对编译型语言系统性偏紧」只成立一半**：go / rust 的 trace 侧超时全是编译类
  （go 是 `go test ./...` 全包测试，rust 是 `cargo build`），而 typescript / javascript
  **原始机器上零超时**——它们没有 AOT 编译期，`npx vitest` / `node` 起步即跑。
- python 也撞了 2 次，但不是编译，是 `tests/test_laws.py` 这类性质测试本身跑得久。
- 判据：`patch_identical` 不受超时影响的前提是**被砍的命令不改文件**。本轮被砍的
  全部是只读的构建/测试命令，所以保真度没有因超时受损。
- 表格两处需要看注解，别照字面读：
  rust 的「其他」是 `sleep 45; tail -30 /tmp/build.log`——agent 自己的轮询等待，45>30 必被砍，
  两侧行为一致；typescript 的重放侧超时**不是机器慢**，是网络语义差异（见下 B 节）。

### 批量化的时间预算

按「独占重放」口径，只有 python / go 两条是干净的；rust / ts / js 三条受并发影响偏悲观。
另有一条早前的独立基线：python `gql-incremental-graphql-delivery__nnFNKRL`，438 条命令 / 981.5s
= **2.24 s/cmd**，与本轮 python 的 2.48 s/cmd 互相印证。

- **每命令耗时没有出现「编译型语言数量级更贵」的现象**：go 反而最快（1.63 s/cmd），
  因为 go 的增量编译有 build cache，且 trace 里多数命令是 `sed`/`grep` 这类瞬时命令。
- rust 最慢（3.93 s/cmd）**主要不是编译慢，而是 agent 自己 `sleep 25~29` 在等**——
  rust 的 CPU 总量只有 91s / 298s 墙钟（0.30x），容器大部分时间在空转。
  这是 30s 上限**倒逼出来的**开销：真正的 `cargo build` 反而只占其中一小段。
- 因此时间预算的主导项不是语言，而是 **trace 里有没有 `sleep` 轮询模式**。
- 粗略预算：5 条共 367 条命令、合计 965s 墙钟，**平均 2.63 s/cmd**。
  按全量 113 条 task、每条 60~100 条命令估算，串行重放约需 **5~7 小时**（不含拉镜像的 25 小时）。

### agent 对 30s 墙的适应行为

| 语言 | `nohup` 后台 | `sleep N` | `timeout N` | 策略 |
|---|---|---|---|---|
| python | 3 | 2 | 16 | 两手都用：16 条 `timeout 120~300` 包装（**无效**，外层 30s 更小）+ 3 次 nohup 后台 |
| go | 0 | 0 | 0 | 无适应——只撞过 1 次，之后改用 `go test .` 单包而非 `./...` |
| rust | 8 | 10 | 0 | **全面后台化**：`(nohup cargo … &); sleep 25~29; tail` —— sleep 取值贴着 30s 上限调 |
| typescript | 0 | 0 | 0 | 无需适应（零超时） |
| javascript | 0 | 0 | 0 | 无需适应（零超时） |

rust 的 `sleep` 取值序列很能说明问题：i=29 用 25s、i=30 试 45s **立刻被砍**、i=31 退回 25s，
此后固定在 25/28/29。这是 agent 在**反复试探 30s 上限**。副作用是 rust 这条 trace 的重放
对机器速度敏感——`sleep 28` 窗口内 cargo 是否编译完，决定 `tail` 抓到什么。

## rc 不匹配逐条归因

五条全部 `patch_identical = 是`，但 rc 序列合计有 10 条不匹配。逐条查过，**没有一条是重放机制的缺陷**：

| 类别 | 条数 | 语言 | 说明 |
|---|---|---|---|
| A 双侧都超时 | 4 | python×2, go×1, rust×1 | trace 侧 -1、重放侧 124，行为一致，`rc_match_semantic` 已计为匹配 |
| B 网络语义差异 | 1 | typescript | 见下「网络」一节 |
| C shell 方言差异 | 2 | rust | 见下「执行器 shell」一节 |
| D 上游 flaky 测试 | 1 | go | 见下「flaky」一节 |
| E 单侧超时（机器快慢） | 2 | python×1, rust×1 | 命令本身贴着 30s 边界，两台机器分属两侧 |

### B. 网络：原 harness 是「403 代理」，重放是「无网卡」

typescript i=34 是唯一一条 `replay_timeout_only`，且反差极大——
**原机器上该 step 间隔上界只有 4.3s，重放却整整跑满 30s 被砍**。原因不是机器慢：

```
trace 侧 observation:  npm error code E403
                       npm error 403 Forbidden - GET https://registry.npmjs.org/tsx   → rc=0，秒回
重放侧:                stdout 0 字节，wall 30.09s，rc=124                              → 被 timeout 砍
```
原 harness 的 `allow_internet=false` 是用**主动拒绝的代理**实现的（立刻回 403）；
`replay.py` 用的是 `--network=none`（连不上，**挂着等**）。对 `npx`/`npm` 这类自带重试退避的
工具，后者会把 30s 预算耗光。对比证据：go i=18 用 `urllib.request` 直连，`--network=none` 下
DNS 立即失败（`Errno -3 Temporary failure in name resolution`），0.21s 返回、rc 与 trace 一致。
**结论：差异只在「失败得快不快」，而 npm 系工具会把它放大成超时。**
批量化前建议把 `--network=none` 换成一个立即拒绝的 sinkhole 代理，与原 harness 对齐。

### C. 执行器 shell：证据指向 `sh`(dash)，不是 `bash -lc`

rust i=53 / i=56 两条，trace 侧 rc=2 且 observation 是：

```
/bin/sh: 1: Syntax error: word unexpected (expecting ")")
```
这是 **dash 的报错格式**，说明原 harness 是用 `/bin/sh` 跑的。两条命令都含 `time (...)`（bash 专有）。
重放用 `bash -lc`，两条都正常执行、rc=0——**重放比原环境更宽松，把原本失败的命令跑成功了**。

核查过的旁证：
- 五条 trace 里**真正的 bashism 只有这 2 条**，且都失败；没有任何 bashism 在 trace 侧成功过。
  （python 里看似有 `[[` / `arr=(`，逐条查是 `Callable[[_FirstType], …]`、`@validated(exceptions=(…,))`
  这类 **Python 源码**，不是 shell 语法。）
- 五个镜像的 `/bin/sh` 全部指向 `/usr/bin/dash`，且 `time (echo x)` 在五个镜像里一律 FAIL。
- 所以 `bash -c` → `bash -lc` 这次改动虽然对齐了当前版本的 mini-swe-agent，
  **但对 v1.1 这批 trace 未必是更保真的选择**——`sh -c` 才能复现 dash 的失败。
  影响面很小（10 条不匹配里占 2 条，且都只碰 `/tmp` 与只读 perf 检查，不改仓库，
  所以 `patch_identical` 没受影响），但批量化前值得定一个口径。

### D. go 的 flaky 测试（Go map 迭代顺序）

go i=66 是最后的总验证命令，trace rc=0、重放 rc=1，且**只跑了 0.97s**（不是超时）。根因：

```
--- FAIL: TestRuleActionPinningDisabledAndDefault
    rule_action_pinning_test.go:67: first error should identify a reusable workflow:
    "step action \"actions/checkout@v1\" is not pinned to semver…"
```
测试断言 `messages[0]` 必须是 reusable workflow 的报错，而 actionlint 的 AST 里
`Jobs map[string]*Job` 是 **Go map**，Visitor `for _, j := range n.Jobs` 的遍历顺序**每次随机**。
workflow 里 `reusable` 与 `actions` 两个 job 谁先被访问是掷硬币——
trace 那次 `reusable` 先，重放这次 `actions` 先。

**这是 agent 自己写进 model.patch 的 flaky 测试，不是重放缺陷。** 含义：批量化时 `rc_match`
存在一个不可消除的下限，部分 mismatch 来自被复现代码自身的不确定性。`patch_identical` 不受影响
（源码字节一致，只是跑出来的结果不同）。

## 本轮的口径污染（必须标注）

串行前提在中途被打破，**性能指标（总耗时 / CPU / s/cmd）分两档可信度**：

| 语言 | 起止 | 并发情况 | 性能指标可信度 |
|---|---|---|---|
| python | 11:13:07–11:17:04 | **独占** | 干净 |
| go | 11:17:04–11:18:54 | **独占** | 干净 |
| rust | 11:18:45–11:23:5x | 前 3 分钟独占，之后与 ts+js 三路并发 | 偏悲观，仅供参考 |
| typescript | 11:21:56–11:23:39 | 与 rust+js 三路并发 | 偏悲观，仅供参考 |
| javascript | 11:21:59–11:28:2x | 与 rust+ts 三路并发 | 偏悲观，仅供参考 |

- 缓解因素：容器都限 `--cpus=2`，宿主 16 核，三路并发时实测各容器仍能吃满自己的配额
  （`docker stats` 实测 rust 208% / ts 162% / js 97%，宿主 loadavg 3.93），
  所以 CPU 维度基本没被饿到；主要残留风险是磁盘 IO 与内存带宽争抢。
- 另有一个无关负载全程占约 1–2 核（`jcore/SuperScalarModel` 的 `gfsim` CI 自测）。
- **`patch_identical` 的结论不受影响**：五条全为真，而争抢只会让条件更苛刻。

## 最重的命令（按重放墙钟）

**python**

| wall_s | cpu_s | rc | 命令 |
|---|---|---|---|
| 30.1 | 29.1 | 124 | `timeout 280 python -m pytest tests/ -o addopts="" -q --ignore=tests/test_laws.py --ignore=` |
| 30.1 | 29.0 | 124 | `timeout 300 python -m pytest tests/test_laws.py -o addopts="" -q 2>&1 \| tail -5` |
| 30.1 | 29.1 | 124 | `timeout 280 python -m pytest tests/test_laws.py -o addopts="" -q 2>&1 \| tail -3` |
| 29.0 | 56.2 | 0 | `timeout 120 python -m pytest tests/test_contrib/ -o addopts="" -q --ignore=tests/test_cont` |
| 25.1 | 36.4 | 0 | `sleep 25; tail -3 /tmp/final_run.txt` |

**go**

| wall_s | cpu_s | rc | 命令 |
|---|---|---|---|
| 30.1 | 50.6 | 124 | `python3 - <<'PY'` |
| 6.7 | 11.1 | 0 | `pkgs=$(go list ./... \| grep -v '^github.com/rhysd/actionlint/scripts/generate-'); echo "$p` |
| 4.9 | 9.4 | 0 | `go vet ./... >/tmp/vet.out 2>&1; rc=$?; echo RC=$rc; cat /tmp/vet.out; exit $rc` |
| 4.7 | 7.5 | 1 | `sed -n '140,155p' command.go; python3 - <<'PY'` |
| 4.6 | 7.8 | 1 | `python3 - <<'PY'` |

**rust**

| wall_s | cpu_s | rc | 命令 |
|---|---|---|---|
| 30.1 | 0.0 | 124 | `sleep 45; tail -30 /tmp/build.log` |
| 29.5 | 11.2 | 0 | `python3 - <<'PY'` |
| 29.1 | 5.3 | 0 | `(nohup cargo test > /tmp/final.log 2>&1 &); sleep 29; grep -E "^test result\|^error\|warning` |
| 29.1 | 10.2 | 0 | `(nohup cargo test > /tmp/test3.log 2>&1 &); sleep 29; grep -E "^test result\|error" /tmp/te` |
| 28.1 | 6.0 | 0 | `python3 - <<'PY'` |

**typescript**

| wall_s | cpu_s | rc | 命令 |
|---|---|---|---|
| 30.1 | 2.5 | 124 | `cd "$(git rev-parse --show-toplevel)" && cp /tmp/runtime.mjs ./runtime_check.mts && sed -i` |
| 10.4 | 16.4 | 0 | `cd "$(git rev-parse --show-toplevel)" && npx vitest run test/repro.test.ts 2>&1 \| tail -40` |
| 8.3 | 15.2 | 0 | `cd "$(git rev-parse --show-toplevel)" && npx vitest run 2>&1 \| tail -40` |
| 7.9 | 13.4 | 0 | `cd "$(git rev-parse --show-toplevel)" && npx tsc --noEmit --project ts/test.tsconfig.json ` |
| 7.8 | 14.5 | 0 | `cd "$(git rev-parse --show-toplevel)" && npx vitest run --coverage.enabled=false 2>&1 \| ta` |

**javascript**

| wall_s | cpu_s | rc | 命令 |
|---|---|---|---|
| 26.5 | 30.2 | 0 | `node tests/index.js --repetition-time 1` |
| 26.0 | 30.9 | 0 | `node tests/index.js --repetition-time 1 >/tmp/ytests2.log && tail -5 /tmp/ytests2.log` |
| 24.2 | 28.7 | 0 | `node tmp/check-map-conflicts.mjs && node tests/index.js --repetition-time 1` |
| 23.7 | 27.8 | 0 | `node tests/index.js --repetition-time 1 >/tmp/ytests3.log && tail -5 /tmp/ytests3.log` |
| 22.7 | 25.9 | 0 | `node tests/index.js --repetition-time 1 >/tmp/ytests.log && tail -5 /tmp/ytests.log` |
