# Task Execution Log: 单条 trial 的 ARM topdown 采集脚本 + 自包含 tarball

**Date:** 2026-09-11
**Goal:** 在 baremetal ARM 服务器上重放 1 条 DeepSWE trial，同时在宿主侧按 cgroup 过滤
采 PMU 事件，算出 ARM L1 topdown 四象限；打成一个 scp 过去就能跑的 tarball。

## Problems to Solve

1. `docker exec` 的真实进程是 containerd-shim fork 的，不在 perf 的子进程树里，
   `perf stat -- cmd` 采不到 → 必须走 `perf stat -a -G <cgroup>`
2. 事件号各家核自己编号，不能硬编码进脚本 → 全部隔离到 `topdown.conf`
3. SLOTS 是四象限的公共分母，写死会在换机器时静默算错且不报错 → 必须运行时读 `caps/slots`
4. baremetal 上真正的失败模式不是 vPMU 透传，而是：事件号无效 / 计数器余量不够
   （watchdog 占一个）/ cgroup 路径推错（`-G` 路径错了不报错，只给 0）

## 口径与选型（协调层定下的，执行器按此实现）

- **用例选 `returns-validated-error-accumula__8JQj5gw`**（python / dry-python/returns）：
  5 条已验证里基线最全的一条 —— `patch_identical=true`、`n_replayed=98`、
  开发机 `elapsed_s=222.6`、`rc_match=95/98`（3 条是已知的 30s 超时）。
  且 python 镜像建得最快（~144s，vs typescript ~280s）
- **这一版不改 `replay.py`**，只在外面套 → 拿到的是整条 trial 的聚合值
- **不改 `make_bundle.sh`**：那是 113 条主包的路径，另写 `make_topdown_bundle.sh`

### 环境核查结论

- 113/113 条 `task.toml` 均为 `cpus=2 / memory_mb=8192`（上游定义，非 replay.py 默认）
- 本机 cgroup driver 实测是 **systemd**（`/sys/fs/cgroup/system.slice/docker-<64位>.scope`），
  与 `replay.py` 头部注释写的 cgroupfs 不符 —— 三级探测能兜住，但**该注释已过时**
- `--cpus=N` 是 CFS quota，容器内 `nproc` 仍报宿主 16；`--cpuset-cpus` 才会让 nproc 变 1。
  因此「改成 1 个 CPU」有两种截然不同的含义，且后者会让构建/测试并行度塌掉
- **改 cpus 会威胁硬标准**：2→1 会让贴着 30s 边界的命令翻成 rc=124，`rc_match` 掉，
  极端情况下改到 `patch_identical`。结论：profiling 单独跑一轮，不与保真度轮共用配置

### ⚠️ 一处已更正的数据口径

协调层最初给出的命令耗时分布（min 0.201 / median 0.600 / ≥1s 占 46.2% / 吃掉 96.2% 时间）
**是错的**：当时 glob 了 `runs/**/commands.jsonl`，把一个 8 条的冒烟 run 和一个 98 条的
**arm64 仿真 run**（541.3s，比 x86 原生慢 2.4 倍）混在一起算了，而真正的基线
`<trial>/replay/commands.jsonl` 不在 `runs/` 下，根本没被读到。正确口径：

| 数据源 | 条数 | min | median | ≥1s 占比 | 这些命令吃掉的时间 |
|---|---|---|---|---|---|
| x86 原生基线（进包的那份，222.5s） | 98 | 0.081s | 0.115s | **18.4%** | **93.0%** |
| arm64 仿真（541.3s，仅供参考） | 98 | 0.201s | 1.005s | 50.0% | 96.6% |

结论方向不变且更有利：后续做 per-command 归因时，门控在 `≥1s` 仍覆盖 93% 的时间，
而原生口径下只有 18 条命令够格 → perf attach 从 98 次砍到 18 次。
`TOPDOWN.md` 已把两组并列并标明来源。

## Steps Log

### Step 1: 读现有约定（make_bundle.sh / preflight.sh / build_arm.sh / replay.py）
- **Status:** success
- **Result location:** 无产物，结论落在下面各脚本的注释里
- **Success result:** 确认 replay.py 的容器名规则 `replay_<sanitize(trial)[:44]>_<PID>`、
  sidecar 名 `<主名>-sink`、cgroup 候选路径两种 driver；BUILD_INFO/SHA256SUMS 的约定

### Step 2: 写 6 个新文件
- **Status:** success
- **Result location:** `crosslang/{topdown.conf,probe_pmu.sh,topdown_trial.sh,topdown_parse.py,TOPDOWN.md,make_topdown_bundle.sh}`
- **Success result:** 61 / 497 / 363 / 465 / 378 / 117 行；未改动 `make_bundle.sh`

### Step 3: x86 上能跑的全部实跑验证
- **Status:** success
- **Result location:** 见下「实跑验证」
- **Success result:** 5 个真 bug 在验证中暴露并修掉（见「Key Findings」）

### Step 4: 真打一次包并解开核对
- **Status:** success
- **Result location:** `crosslang/deepswe-topdown-bundle-20260911.tar.gz`（280KB，解包 1.3MB，19 个文件）
- **Success result:** `sha256sum -c SHA256SUMS` 全匹配；19 个文件一个不少、`mini-swe-agent.txt` 没带进去

## 实跑验证（x86 上做了什么）

| 验证 | 方法 | 结果 |
|---|---|---|
| 语法 | `bash -n` × 4（含 `topdown.conf` 的 source），`python3 -m py_compile` | 全过 |
| 解析器正例 | 构造的假 perf JSON / CSV（SLOTS=8，四象限 25/5/20/50） | exit 0，两个自检 ✅ |
| 解析器反例·求和≠1 | 同上但 BackendBound 改 20% | exit 2，❌ 并反推「SLOTS 应约为 5.60」 |
| 解析器反例·复用 | `pcnt-running` 改 52.31% | exit 2，❌ 最低调度占比 |
| 解析器反例·事件无效 | `counter-value: <not supported>` | exit 1，指向 TRM 核对 |
| CSV 列序鲁棒性 | cgroup 列插在事件名前 / 后 / 完全没有 / 事件名原样回显成带逗号的整段 spec | 4 种全部正确解析 |
| 主脚本全流程 | 假 sysfs（`caps/slots`）+ 假 `perf`/`docker`/`sudo` + 假 `replay.py`，JSON 与 CSV 两条路各跑一遍 | 容器等待 → cgroup 推导 → perf 采集 → `wait` → 解析，全通，exit 0 |
| 主脚本失败路径 | 重放起不来 / cgroup 目录不存在 / 60s 等不到容器 | 都给出日志尾部与可执行的下一步，并杀掉后台重放 |
| 主脚本中断路径 | 采集中途 `kill -INT` | exit 130，replay.py 收到 SIGINT 走自己的 finally，无残留进程 |
| 探针全流程 | 假 sysfs + 假 `/proc/sys/kernel/nmi_watchdog` + 假内核 config | 正例 ✅9/❌0；异构核警告、watchdog 占用、余量不够、镜像不在本地、cgroup 找不到、SLOTS 写死冲突，逐个验过 |
| 打包 | 真打 + 解开 + `sha256sum -c` + 包内 `bash -n`/`py_compile` + `build_arm.sh --list` 能按前缀找到 trial | 全过 |
| 打包后布局 | 在解包目录里跑主脚本，确认 `./replay.py`（与 trial 同级）能被找到 | ✅ |
| **真 docker 行为** | 本机真起 `replay_..._99999` 和 `replay_..._99999-sink` 两个容器 | `name=^<全名>$` 精确过滤只命中主容器；**前缀过滤时 docker 先列出 `-sink`**，没有 `grep -v -- '-sink'` 就会拿到 sidecar；短 ID→64 位 ID 转换正确；`system.slice/docker-<64位>.scope` 存在而 `docker/<64位>` 不存在（本机 driver=systemd，证明两个候选路径都要试）；主容器与 sink 确为两个独立 cgroup |

## Key Findings（验证中暴露的真 bug，都已修）

1. **后台任务的 SIGINT 被置成 SIG_IGN。** 非交互 shell 在没开作业控制时，会把后台任务的
   SIGINT/SIGQUIT 设为 SIG_IGN，且被 exec 继承 —— python 里 `signal.getsignal(SIGINT)`
   直接就是 SIG_IGN。后果：`kill -INT $RPID` 毫无反应，清理白等 10s 再补 SIGKILL，而
   SIGKILL 不会让 replay.py 走 finally，主容器和 `-sink` 双双变孤儿。修法：起后台重放时
   `set -m` / `set +m` 包一下，让它拿到独立进程组 + 默认信号处置。
2. **`kill -0` 判不出僵尸。** 子进程退出后、被 `wait` 收尸前是僵尸，`kill -0` 照样成功，
   于是清理逻辑白等满 10 秒。修法：加 `alive()` 再读一次 `/proc/<pid>/stat` 的状态位。
3. **`source topdown.conf` 把探测结果冲掉了。** 探针第 2 节探出 PMU/SLOTS，第 3 节临用前
   又 source 了一遍，而配置里 `PMU=`/`SLOTS=` 正是推荐留空的 —— 两个空串把探测值盖掉，
   事件组拼成了没有 PMU 前缀的 `/event=0x0011,...`，**而且不报错**。修法：只 source 一次，
   配置原值另存 `CONF_*`。
4. **`pgrep -a perf` 把 iperf3 也匹了进来**（进程名子串正则），凭空报「有别的 perf 会话在
   抢计数器」。修法：锚定 `^perf`。
5. **`command -v perf` 成功不等于 perf 能用。** Debian/Ubuntu 的 `/usr/bin/perf` 是个按
   `uname -r` 找真身的 wrapper 脚本，包没装时 wrapper 还在，只有真跑才吐
   `perf not found for kernel ...`（本机就是这个情况）。修法：按 `perf --version` 的解析
   结果判，两个脚本都改了。

另外还有一类 `set -e` 陷阱：`[ cond ] && cmd` 写在语句位置，条件不成立时整条返回 1 会把
脚本打断（`SUDO="sudo"; [ "$(id -u)" = 0 ] && SUDO=""` 在非 root 下必然自杀）。已全部改成 `if`。

## 独立验证 agent 的结论（Rule 8）

9 项检查全 PASS（语法 / SLOTS 无硬编码 / 事件号只在 conf / 解析器两个自检 / tarball 完整性
/ `make_bundle.sh` 未改 / 中文且讲「为什么」/ TOPDOWN.md 覆盖 / 找 bug）。额外验了打包可复现
（除时间戳外全部 sha256 一致）、与 `replay.py` 的集成点对账（argparse、容器名推导规则、
输出目录约定三处完全一致）、`alive()` 对真僵尸进程的判定。

报了 7 条非阻塞问题，本轮修掉 4 条（剩 3 条见下）：

1. ✅ **可执行位不一致** —— 4 个新脚本是 644，`./probe_pmu.sh` 会 Permission denied。
   已 `chmod +x`，并在打包脚本里加 `chmod 755 "$ROOT"/*.sh "$ROOT"/*.py` 统一包内权限。
2. ✅ **`docker inspect` 失败时静默退出** —— `set -e` 下零输出直接退，与本脚本别处的详尽
   报错风格不符。已加显式报错 + 日志尾部。
3. ✅ **probe 对解析器 exit 1 的归因可能误导** —— SLOTS 未知导致的 1 会落进
   `<not supported>` 的话术。已拆出单独分支。
4. ✅ **docker name filter 是正则** —— sanitize 允许保留 `.`，而 `.` 在过滤器里是通配符。
   已把过滤用的那份 `.` 转义掉（当前 trial 名无点，属预防）。

**未修（判定为不值得动）：**
- `probe_pmu.sh` 的 `cleanup()` 无重入保护，INT trap 后 EXIT trap 会再跑一遍。
  `docker rm -f` 与 `rm -rf` 都幂等，无实际后果，只是与 `topdown_trial.sh` 的 `CLEANED` 不对称。
- CSV 里 cgroup 列排在事件名**之前**时，`topdown.json` 的 `cgroup` 字段为 `null`。
  纯元数据，不参与任何计算，四象限完全正确。
- （tmux 那条已在 TOPDOWN.md §5 补了一句说明，不算未修。）

## 到 ARM 机器上才能确认的部分

- 五个事件号在目标核上是否有效（本机没有 armv8 PMU，只能用假数据验流程）
- `caps/slots` 的真实值、`format/event` 是否为 `config:0-15`
- `perf stat -a -G <cgroup>` 在真 PMU + 真 docker cgroup driver 下能不能采到数
- 真实 perf 的 JSON/CSV 字段布局（解析器已对 4 种列序变体做了兼容，但没见过真样本）
- watchdog 到底占不占计数器、`dmesg` 报几个计数器
- 重建镜像上 `patch_identical` 是否仍成立（这是原本就开放的问题）

## Files Changed

- `crosslang/topdown.conf` - 新增
- `crosslang/probe_pmu.sh` - 新增
- `crosslang/topdown_trial.sh` - 新增
- `crosslang/topdown_parse.py` - 新增
- `crosslang/TOPDOWN.md` - 新增
- `crosslang/make_topdown_bundle.sh` - 新增（**没有动 `make_bundle.sh`**）
- `crosslang/deepswe-topdown-bundle-20260911.tar.gz` - 产物

**Commit:** pending（用户自行提交）

### Step 5: 独立验证（协调层另起的 verifier subagent，非执行器自带的那个）
- **Status:** success
- **Result location:** 本条目
- **Success result:** 8/8 PASS，**0 个阻塞项**，且**未改动任何文件**（收尾 `sha256sum -c` 仍 18/18）。
  重点验证项与结论：
  - 解析器四象限公式手算对账逐位吻合；`CPU_CYCLES=0` / 事件缺失 / `<not supported>` /
    非数字 / 空文件 / `SLOTS=abc` 六种边界**全部是明确报错 + 非零退出码，零 traceback**
  - 两个自检真的会拦：求和 1.30 → ❌ 且反推「SLOTS 应约为 10.40」，RC=2；
    占比 48.37% → ❌ 复用，RC=2；CSV 无占比列 → ❌ 不默认放行
  - **CSV 6 种列序变体全部取到同一组正确值**，含「cgroup 在第 0 列」与
    「perf 回显完整事件描述、逗号把字段打散」两种最刁钻的
  - `set -m` 与 `grep -v -- '-sink'` 两处经反证实验确认是**承重的**：
    去掉 `set -m`，后台 replay 的 SIGINT disposition 是 SIG_IGN，清理逻辑静默失效；
    去掉 `-sink` 过滤，docker ps 按创建时间倒序会让 `head -1` 拿到 sidecar，采出一堆空气
  - 失败路径实测无孤儿进程、无残留容器；中断时不会继续打印那份假的「两个自检都过」
- **非阻塞观察 3 条：** ① `sudo` 无前置检查，无 tty 场景下会白跑完整条重放才失败
  （TOPDOWN.md 已写明先跑 `sudo -v`）；② conf 删行触发 `set -u` 硬失败，提示不够友好；
  ③ `EV_EXTRA` 只校验事件名不校验事件号

### Step 6: 提交
- **Status:** success
- **Result location:** 见 git log

---

# 续：单条 → 批量（同日第二轮）

**Date:** 2026-09-11
**Goal:** 单条 trial 的 ARM topdown 采集已在目标机（**baremetal ARM，cgroup v1**）跑通，
现在扩到批量：`run_batch.py` 能批量采、汇总表能横向对比、包里带全部 113 条。

## Problems to Solve
1. `run_batch.py` 加 `--topdown`，且必须与 `--jobs > 1` 互斥并报错退出
2. 汇总表在**现有总表后面追加** topdown 列，并给一个按语言分组的横向小结
3. 打一个带全部 113 条 trial 的新包（现有的 `make_topdown_bundle.sh` 只打 1 条）
4. `TOPDOWN.md` 补批量流程、时间预算、为什么不能并发、跨 trial 比较的注意事项
5.（中途追加）`--per-lang N` 每种语言抽 N 条；再追加 `--pick median|heaviest|lightest`

## 关键设计决定（为什么这么做）

### 1. 复用 `topdown_trial.sh`，不在 `run_batch.py` 里重写 perf 逻辑
`--topdown` 打开时每条 trial 改调 `bash topdown_trial.sh <trial> -o <out> --cmd-timeout N
[--limit N] [--no-metrics]`，而不是直接调 `replay.py`。
理由：perf 那一套（`-G` 必须排在 `-e` 之后、cgroup v1 走 perf_event 独立层级、
兜底找容器必须排掉 `-sink`）这一轮已经栽过两次，两份实现只会跟着一起错。

代价是要给 `topdown_trial.sh` 补一个 `--cmd-timeout` 透传口 —— 批量入口一直给
`replay.py` 传 `--cmd-timeout 30`（对齐原 harness），传不下去的话同一批里 topdown
那几条就换了口径，而这种偏差在报告里完全看不出来。

### 2. `--topdown` × `--jobs>1` 报错退出（比 `--metrics` 那条更硬）
`--metrics` 并发抢的是**机器资源**，数字偏悲观但每条各有各的一份数据；
`--topdown` 并发抢的是**同一批物理计数器**（Neoverse 一般 6 个，watchdog 开着剩 5 个），
一轮要开 4~6 个事件且要求作为一个 `{}` 组同上同下 —— N ≥ 2 必然超，超了内核**不报错**，
直接复用。后果是每条 C5 自检全失败、分子分母来自不同时间窗口、而屏幕上每条都「跑完了」。
所以报错文案把这个机制整段讲出来，不是只说「不兼容」。

### 3. 单条 topdown 采废 ≠ trial 失败
`patch_identical`（保真度）与 C1~C5（数据可信度）**正交**。
但 `topdown_trial.sh` 最后只能吐**一个**退出码，把两件事压成了同一个数（自检没过是 2）。
→ 给它加了一份 `run_status.json`，把 `replay_rc` / `perf_rc` / `parse_rc` **分开记**；
`run_batch.py` 按 `replay_rc` 判 trial 成没成。
例外：**早于重放启动**的失败（事件号不合法、没 perf、等不到容器）不写这个文件，
那种情况 trial 确实没跑起来，按失败计 —— 这是有意的。

### 4. 分组小结**剔除自检没过的条**
C5 红了说明发生了复用，那组四象限的分子分母来自不同时间窗口，比值没有物理意义；
混进平均数只会污染整组，而平均值这个形式恰恰把「哪一条坏了」抹掉。
→ 只统计自检全过的条，被剔掉几条**单独成列**写出来（`n=2 剔除 3` 和 `n=2 剔除 0`
可信度完全不同，混在一个 n 里看不出来）。
一门语言若「采到了但全被剔掉」，仍保留一行 `n=0 剔除 k` —— 让它整个消失会被误读成「没跑」。

### 5. `--pick` 三策略：耗时 vs 数据干净度
topdown 采的是**整条 trial 的聚合值**，固定含容器启动 + 收尾 `git diff`。
trial 越轻这笔固定开销占比越大，四象限就越是在测「容器启动 + 解释器 import」。
实测（全量 113 条，五种语言各取 2 条，按 2.35s/命令 × ARM 1.4 折算）：

| --pick | 命令合计 | 粗估耗时 |
|---|---|---|
| median（默认） | 324 | 约 18 分钟 |
| heaviest | 1243 | 约 68 分钟 |
| lightest | 178 | 约 10 分钟 |

差 3.8 倍。这个权衡写进了 `--help` 和 `TOPDOWN.md`，不让用户自己猜。

## Steps Log

### Step 1: 读现状
- **Status:** success
- **Result location:** —
- **Success result:** 读完 run_batch.py / topdown_trial.sh / topdown_parse.py /
  topdown.conf / TOPDOWN.md / make_topdown_bundle.sh / make_bundle.sh，
  确认 `topdown.json` 的字段布局（`checks_run` 才是「真跑过哪几条」，
  `checks` 里混着数值和「没跑所以记 None」的项，不能拿 `checks` 整个去判）

### Step 2: topdown_trial.sh 加 --cmd-timeout + run_status.json
- **Status:** success
- **Result location:** `crosslang/topdown_trial.sh`

### Step 3: topdown_parse.py 加 event_codes
- **Status:** success
- **Result location:** `crosslang/topdown_parse.py`
- **Success result:** 批量跑完只剩一堆 topdown.json，而「数不对」最常见的根因就是
  事件号指错了 —— 光看计数值无从判断当时用的是 0x003a 还是别的，结果文件必须自证口径

### Step 4: run_batch.py 加 --topdown / --per-lang / --pick / --no-metrics
- **Status:** success
- **Result location:** `crosslang/run_batch.py`

### Step 5: make_topdown_bundle.sh 加 --trials-dir 全量模式
- **Status:** success
- **Result location:** `crosslang/deepswe-topdown-bundle-full-20260911.tar.gz`（4.4 MB，113 条）
- **Success result:** 全量模式下基线文件「有就带、没有不拦」（full_trials 那 113 条
  从没在开发机上重放过，硬要求会让全量包根本打不出来）；默认文件名多一段 `-full`
  与已有的 `-20260911.tar.gz` / `-20260911b.tar.gz` 区分开

### Step 6: TOPDOWN.md 加 §5.1 批量采集
- **Status:** success
- **Result location:** `crosslang/TOPDOWN.md`（+225 行）

### Step 7: 实跑验证（x86_64 / cgroup v2 / 无 ARM PMU）
- **Status:** success
- **Result location:** `/tmp/claude-1000/.../scratchpad/{regress,e2e,plang,tdrender,bundleverify}`
- **Success result:** 见下面「Key Findings」

## Key Findings（实跑验证结论）

- **`--topdown` × `--jobs 2` → 报错 + 退出码 1**，报错文案含完整机制说明
- **不加 `--topdown` 时老报告逐字节不变**：新旧 `write_summary` 各跑一遍，
  `SUMMARY.md` 1933/1933 字节、`summary.json` 3227/3227 字节，**逐字节一致**；
  另做了端到端 smoke 实跑（真起容器）对比，归一化后完全一致
- **新列渲染正确**（含混合情况）：全过 `✅` / 单条红 `❌C5` / 多条红 `❌C1,C5` /
  直接法求和红 `❌求和` / 没采到 `—` / 完全没 topdown.json `—`
- **分组小结算术手算对账通过**：python 两条 (.40,.10,.20,.30) 与 (.30,.06,.24,.40)
  → 均值 35.0/8.0/22.0/35.0%；「全部」三条的 BadSpec 均值 (.10+.06+.04)/3 = 6.7%、
  中位 6.0% —— 逐位吻合
- **单条 topdown 失败不判 trial 失败**：用 stub 模拟「重放成功、自检失败、脚本退出码 2」，
  `汇总 1/1 通过`、整体退出码 0、「校验」列 `❌C5`
- **参数透传正确**：stub 收到 `<trial> -o <out> --cmd-timeout 30 --limit 3 --no-metrics`
- **`--per-lang` 确定性**：同输入连跑 5 次选出同一批（含顺序）
- **三种 --pick 取法手算对账通过**（5 条 10/20/30/40/50：median→20,30；
  heaviest→40,50；lightest→10,20；同命令数按目录名升序兜底已验）
- **某语言 0 条可用 → 贡献 0 条、打印「跳过」、不报错**；可用数 < N 时明确打「可用的不够 N 条」
- **`--dry-run` 仍可用**（开/不开 topdown 都试过）
- **打包 → 解开 → `sha256sum -c` 466 个文件 0 失败 → 包内脚本语法全过 →
  113 条 trial 齐全（go×35 js×5 python×34 rust×5 ts×34）→ `run_batch.py` 在包里（755）**
- 包内 `run_batch.py --topdown --per-lang 2 --dry-run` 可独立跑通

## 只能到 ARM 机器上确认的（本机验不了）

- `perf stat -a -G` 在真 ARM PMU + cgroup **v1** 上能不能采到数（本机 v2、无 armv8 PMU）
- 真实 `topdown.json` 的四象限数值是否落在合理区间
- `--topdown` 批量下**串行**是否真的不触发复用（C5 全绿）
- 113 条串行的**真实耗时**（本文的数小时是按 x86 基线 2.35s/命令 × 1.4 折算的粗估）
- `--no-metrics` 在 v1 上是否确实绕开了 `sinkhole cgroup 初始化失败`
- 重建镜像上 `patch_identical` 是否仍成立（原本就开放的问题）

## Files Changed

- `crosslang/run_batch.py` - 加 `--topdown` / `--per-lang` / `--pick` / `--no-metrics` /
  `--topdown-script`；汇总表追加 5 列；按语言的横向小结；summary.json 加 `sampling` 与 `topdown` 块
- `crosslang/topdown_trial.sh` - 加 `--cmd-timeout` 透传；落 `run_status.json`（三个退出码分开记）
- `crosslang/topdown_parse.py` - `topdown.json` 加 `event_codes`
- `crosslang/make_topdown_bundle.sh` - 加 `--trials-dir` 全量模式；随包带 `run_batch.py`；
  BUILD_INFO 的 `kind` / `features` 区分两种包
- `crosslang/TOPDOWN.md` - 新增 §5.1 批量采集（流程 / 时间预算 / 为什么不能并发 /
  `--per-lang` `--pick` / 输出格式 / 采废不判失败）；§8 新增「跨 trial 比较的注意事项」；
  排障表 +5 行；文件清单加 `run_batch.py`
- `crosslang/deepswe-topdown-bundle-full-20260911.tar.gz` - 新产物（4.4 MB / 113 条 / 466 文件）

**Commit:** pending（用户明确要求不提交）

### Step 8: 独立验收（另起 verifier subagent）+ 返工
- **Status:** success
- **Result location:** 本条目；验收者的脚本在 `/tmp/acceptance-topdown/`
- **Success result:** 11 项里 **10 项通过、1 项不通过**，另有 2 条非阻塞观察。
  验收者独立做的几件超出要求的事：用 `fractions.Fraction` 精确重算分组小结（16 组全吻合）；
  逐字节对比覆盖 **10 个场景 × 3 份产物 = 30 项**（含「给了 `--per-lang` 但没真抽样」
  这个最容易漏的组合）；把 `make_topdown_bundle.sh --trials-dir` 重打一遍确认**可复现**
  （与出货包逐文件内容一致，仅 `built_utc` 与包名不同）；把 TOPDOWN.md §5.1 里的
  6 条命令行**在解开的包内原样实跑**，文档里 11 条 `run_batch.py` 命令行的参数逐个核对
- **不通过的一项（已修）：** `--pick heaviest` 的**并列兜底方向反了**。
  根因 `take = list(reversed(avail))[:n]` —— `avail` 是按 `(n_commands, 目录名)` 升序排的，
  整体 reverse 之后命令数确实降序了，但**并列项的目录名跟着变成降序**。
  三条都是 50 条命令时 `N=1` 取到 `ccc` 而不是 `aaa`。
  **危险之处在于它仍然是确定的**（不抖），跑起来一切正常、只是选错了人，
  而且与 median / lightest 的兜底方向不一致 —— 两批数据之间就此不可比，报告上看不出来。
  修法：`sorted(avail, key=lambda t: (-(t["n_commands"] or 0), t["name"]))[:n]`，
  命令数取负单独作主键，目录名才保持升序。
- **顺手补的一处（验收者列为「理论缺口，实践中不可达」）：**
  `collect_topdown` 现在要求四个象限**都是数**才算 available。
  否则会出现「available=True、自检全过、但某象限是 None」的条 —— 它既不进均值、
  又不算被剔掉，`可用 N = 进均值 X + 剔除 Y` 这个等式凭空少一条，而报告上看不出少在哪。
  修后实测闭合：可用 6 = 进均值 3 + 剔除 3
- **返工后重跑的验证：** 语法全过；老报告仍**逐字节一致**（1933/1933、3227/3227）；
  新列渲染与分组小结不变；`--topdown × --jobs 2` 仍 rc=1；采废不判失败仍 rc=0、
  重放失败仍 rc=1；`--dry-run` rc=0；重打包 **465/465 sha 全过、113 条齐、
  run_batch.py 与源同文件**，且包内 heaviest 并列兜底已是修复后的行为
- **验收者自报的两点（已确认无残留）：** ① 验收期间源文件被我改过（措辞润色），
  它已按最终版全部重跑；② 它的 `importlib` 测试在 `crosslang/__pycache__/` 留过一个
  `.pyc`，已自行删除，`git status` 无新增
