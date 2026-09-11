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
