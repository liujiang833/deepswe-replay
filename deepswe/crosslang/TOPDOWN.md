# ARM topdown 采集包 · 单条 trial

在 ARM 服务器上重放 **1 条** DeepSWE trial，同时在**宿主侧**按 cgroup 过滤采 PMU 事件，
算出 ARM L1 topdown 四象限（Retiring / BadSpec / FrontendBound / BackendBound）。

本文件就是操作说明，从解包到出结果一条路走完。

**主流程 —— 顺序是有讲究的，别跳步：**

```
解包核对 → probe_pmu.sh → 填 topdown.conf → build_arm.sh → topdown_trial.sh
   §1          §2              §3               §4             §5
```

- **探针为什么必须在最前**：它一次性确认三件事 ——
  **① 五个事件号在这颗核上真的有效**（PMUv3 只有一小部分事件号是架构必需的，其余各家核
  自己编号）；**② 通用计数器余量够**（watchdog 会常驻占掉一个，6 个变 5 个，而我们正好
  要 5 个）；**③ cgroup 路径推导对**（`-G` 路径错了不报错，只给一串 0）。
  这三样任何一样不对，后面建镜像那几分钟和整条重放（开发机上 222s，ARM 上只会更久）
  都是白做。`probe_pmu.sh` 几十秒跑完，先把这三个问题问清楚。
- **建镜像为什么排在填配置之后**：不为什么，纯粹是建镜像最慢，让它在后台跑的同时
  你正好可以按 TRM 核对事件号。两件事互不依赖。

---

## 0. 前置条件

| 必须有 | 说明 |
|---|---|
| **ARM 服务器**，Neoverse V1/N2 级 | slot-based TMA。本包的公式与事件号是 ARM PMUv3 的 |
| **baremetal**（本包的前提） | 不是虚机，所以不存在 vPMU 透传问题。真跑在虚机上的话，先自行确认 hypervisor 开了 vPMU |
| **通用计数器余量 ≥ 5** | NMI/hardlockup watchdog 会占掉一个，`probe_pmu.sh` 会替你算这笔账 |
| `perf` | `perf stat -a -G` 要能用。`sudo` 权限，或 `kernel.perf_event_paranoid ≤ 0` |
| `docker` | cgroup v2；能起容器、能 build |
| `python3` | 3.8+，**只用标准库**，无需 pip install |
| **`mars-base` 基座镜像** | 这条 trial 的镜像 `FROM mars-base`。基座不在就建不了 |

⚠️ **重放期不需要外网、也不该有** —— 重放容器是 `--network=none` + 403 sinkhole，
刻意还原原 harness 的 `allow_internet=false`。要联网的只有构建期。

---

## 1. 解包与核对

```bash
tar xzf deepswe-topdown-bundle-*.tar.gz && cd deepswe-topdown-bundle
sha256sum -c SHA256SUMS      # 传输完整性
cat BUILD_INFO               # 这份包是哪个 commit 打的、各脚本的 sha256
```

---

## 2. 探 PMU —— 第一件事

```bash
bash probe_pmu.sh                 # 默认用 ubuntu:24.04 做活体测试
bash probe_pmu.sh <本地已有镜像>   # 没网拉不到 ubuntu 时用这个
bash probe_pmu.sh --emit-conf     # 顺带打印可粘进 topdown.conf 的片段
```

只读 + 起一个临时容器（结束必清，^C 也清）。**目标机是 baremetal，所以它验的不是
「vPMU 有没有透传」，而是事件号有效性、计数器余量、cgroup 路径这三样。** 它输出：

- PMU 名、`caps/slots`、`format/event`、通用计数器个数、perf 版本、`perf_event_paranoid`
- **列出所有 `armv8*` PMU**。多于一个 = 异构核（big.LITTLE / 多簇），事件必须按 PMU
  分别打开，这一版脚本不支持，得先用 `--cpuset-cpus` 把容器钉在一簇上
- **活体验证 `-G` 真的能在容器上算出数**：起容器 → 容器内烧一个核 → 宿主侧
  `sudo perf stat -a -G <cgroup>` 采 3 秒 → 检查三件事：

  | 检查 | ❌ 意味着 |
  |---|---|
  | 计数非零且不是 `<not supported>` | **事件号在这颗核上无效** —— 号写错了，或这颗核不实现这个事件。逐个对照目标核 TRM |
  | 每个事件的调度占比 = 100% | 发生**计数器复用**，事件组没生效，比值不可信 |
  | `(ss_fe + ss_be + op_spec) / (cycles × SLOTS) ≈ 1`（容差 3%） | SLOTS 或事件号不对 |

  还有一节**计数器余量**的账（baremetal 上最隐蔽的失败模式）：

  - `kernel.nmi_watchdog` 非 0，且内核用的是 `CONFIG_HARDLOCKUP_DETECTOR_PERF`
    （而不是 buddy / arch 那几种不吃 PMU 的实现）时，watchdog 会**常驻占用一个通用计数器**，
    6 个变 5 个。我们正好要 5 个，卡在边界上 —— 再往 `EV_EXTRA` 里加一个就必然复用。
    探针会提示 `sudo sysctl kernel.nmi_watchdog=0` 临时腾出来，**采完记得
    `sudo sysctl kernel.nmi_watchdog=1` 改回去**（它是死锁检测，长期关着等于少一层保护）。
  - `pgrep -a '^perf'`：同机还有别的 perf 会话在跑的话会来抢计数器。
  - 最后把「通用计数器总数 / watchdog 占用 / 实际可用 / 本次要开的事件数」并排打出来，
    余量够不够一眼可见。

  > 为什么这笔账值得单独算：**复用不会报错**，它只会让「四象限求和 ≈ 1」这条自检失败，
  > 而那个现象跟「事件号写错了」一模一样。不先把余量的账算清楚，很容易一路去抠 TRM
  > 事件号，方向全错。

**为什么必须活体验证，而不是查一查配置就算了**：`perf -G` 在 cgroup 路径推错的时候
**不报错**，它只是安安静静给你一串 0。看起来像「这段负载没跑」，实际是过滤器根本没命中。
所以只能真采一次、真看数。

探针试的 cgroup 路径依次是
`system.slice/docker-<完整64位ID>.scope`（systemd driver）和
`docker/<完整ID>`（cgroupfs driver），都不中就在 cgroup 树里 `find` 一次；
全失败会把**实际试过的路径**原样打出来。

---

## 3. 填 `topdown.conf`

整套脚本里**只有这一个文件需要你改**，事件号一行都没写进脚本。

```bash
vi topdown.conf
```

- `PMU=` —— **留空**表示自动探测（`ls /sys/bus/event_source/devices/ | grep -m1 armv8`）。
  只有异构核或自动选错时才手填。
- `SLOTS=` —— **强烈建议留空**。留空 = 运行时从
  `/sys/bus/event_source/devices/$PMU/caps/slots` 读。

  > ⚠️ 这是整个包里最危险的一个值。SLOTS 是四象限的**公共分母**（V1=8、N2=5、
  > 别的核又不一样）。写死一个常数之后换机器、甚至同机换到另一簇核，四个比值会
  > **一起按同一比例静默偏移** —— 每一项看着都还在 0~1 之间，像是「这台机后端压力大点」，
  > 但整套数都是错的，而且**不会报任何错**。唯一兜底是下面那条「求和 ≈ 1」自检，
  > 可那时你已经采完一整轮了。
  >
  > 实测长这样（把 SLOTS 从 8 写成 5）：
  > ```
  > ❌ 四象限求和 1.6000，超出 1 ± 0.03
  >    反推：若事件号无误，SLOTS 应约为 8.00
  > ```

- 五个 L1 必需事件，默认值是 **ARM PMUv3 架构定义值**，多数 Neoverse 核直接适用，
  但**请按目标核 TRM 核对**：

  | 配置键 | 默认 | 事件 | 用途 |
  |---|---|---|---|
  | `EV_CPU_CYCLES` | `0x0011` | CPU_CYCLES | 公共分母 |
  | `EV_OP_RETIRED` | `0x003a` | OP_RETIRED | Retiring |
  | `EV_OP_SPEC` | `0x003b` | OP_SPEC | BadSpec = OP_SPEC − OP_RETIRED |
  | `EV_STALL_SLOT_FE` | `0x003d` | STALL_SLOT_FRONTEND | FrontendBound |
  | `EV_STALL_SLOT_BE` | `0x003e` | STALL_SLOT_BACKEND | BackendBound |

- `EV_EXTRA=` —— 可选的 L2 下钻事件，格式 `"名字=0x00xx 名字2=0x00yy"`。
  加事件不用改代码，只打印原始计数，不参与四象限。

  > ⚠️ **总事件数（5 + EV_EXTRA 条数）不能超过实际可用的通用计数器个数。**
  > Neoverse 一般 6 个，但 **watchdog 开着时只剩 5 个** —— 正好被五个 L1 必需事件占满，
  > 此时 `EV_EXTRA` 一个都加不了。超了 perf 会开始**复用**：每个事件只在一部分时间真在
  > 计数，其余靠外推，调度占比掉到 100% 以下，四象限跟着偏。宁可分两轮采。
  > 本机的余量账看 `probe_pmu.sh` 的「计数器余量」一节。

- `PERF_OUTPUT=auto` —— `auto` 按 perf 版本探测（≥ 5.17 用 `-j` 出 JSON，否则 `-x,` 出 CSV）。
  排查时可强制写 `json` / `csv`。

配置文件同时要能被 bash `source` 和 python 逐行解析，所以格式是纯 `KEY=VALUE`：
等号两边不留空格，带空格的值用引号包住，注释用 `#`，不要写 shell 展开。

---

## 4. 建镜像 —— 最耗时的一步

包里**没有镜像**，带的是 task 的 `environment/Dockerfile`（配方）。本包只有 1 条 trial：

```
returns-validated-error-accumula__8JQj5gw     # python，仓库 dry-python/returns
```

```bash
bash check_sources.sh                  # 先探上游源通不通（几秒）
bash build_arm.sh --list returns-validated   # 只看会怎么改写 Dockerfile，不构建
bash build_arm.sh returns-validated          # 真建
```

⚠️ 重建出来的镜像**不等于**原 amd64 镜像。`patch_identical` 在重建镜像上是否仍然成立，
本身就是这轮要测的东西之一。内网 TLS 中间人的处理见 `get_ca_cert.sh` / `detect_mitm.sh`，
用法与主包一致（`build_arm.sh --proxy` / `--ca-cert` / `--registry` …）。

---

## 5. 采集

```bash
bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw
bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw -o /data/td
bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw --limit 5   # 冒烟
```

> `perf stat -a` 要 root。脚本会自动加 `sudo`；**先跑一次 `sudo -v`** 把密码缓存起来，
> 免得采集跑到一半卡在密码提示上（那时重放已经在跑了）。
>
> 这条 trial 在开发机上跑 222s，ARM 上只会更久。**远程跑建议放进 tmux / screen。**
> （实测 bash 在未捕获 SIGHUP 时仍会执行 EXIT trap，所以 SSH 掉线会走 cleanup 正常
> 收尾、不会留孤儿容器 —— 但那一轮的数据也就没了，重跑一遍不如一开始就挂上。）

### 它做了什么

1. `source topdown.conf`，定下 PMU（空则探测）与 SLOTS（空则读 `caps/slots`）
2. 后台起 `python3 replay.py <trial> <trial>/task.json -o <outdir>`，记下 PID
3. 轮询等主容器出现（最多 60s）。容器名是
   `replay_<trial名 sanitize 后前 44 字符>_<replay.py 自己的 PID>`，而 replay.py 是我们
   fork 的，所以这个名字**可以精确算出来**，不用去 `docker ps` 里猜（机器上可能同时有
   别人的重放在跑）。兜底才按 `^replay_` 前缀找，且**必须排掉 `-sink`** ——
   那是 403 sinkhole sidecar，独立容器、独立 cgroup，采它等于采了个空气
4. 由**完整 64 位容器 ID** 推 cgroup 相对路径（systemd / cgroupfs 两种 driver 都试）
5. `sudo perf stat -a -G "$CG" <-j|-x,> -o <file> -e "{事件组}" -- tail --pid=<replay PID> -f /dev/null`
6. `wait` 收重放退出码
7. 调 `topdown_parse.py` 打印结果，同时落 `topdown.json`

### 三个设计点

**为什么是 `-a -G <cgroup>`，不是 `perf stat -- <命令>`。**
`replay.py` 是用 `docker exec` 一条条重放的，而 `docker exec` 的真实进程是
**containerd-shim fork 出来的**，根本不在这个 perf 的子进程树里。
`perf stat -- docker exec ...` 采到的只有 docker 客户端那点 RPC 开销，被测负载一个周期
都进不来。只能走系统级采样 + cgroup 过滤。

**为什么用 `tail --pid` 当 perf 的子进程。**
采集窗口 = perf 的生命周期。给 perf 挂一个「跟着 replay 一起活」的空壳子进程，
replay 一退出 tail 就退出，perf 跟着收尾打印。比起「后台起 perf 再找准时机发信号」，
这条路没有竞态，也不用管信号语义。`tail` 自己那点开销不进统计 —— 它不在容器 cgroup 里。

**为什么事件整组用 `{}` 包住。**
保证这几个事件被内核当作**一个调度单元**同时上、同时下。否则它们各自落在不同的时间窗口里
计数，四象限的比值就没有意义了。组开不下时 perf 会复用，`topdown_parse.py` 的第二条自检
就是抓这个。

### 输出

```
<outdir>/<trial名>/
├── verdict.json                # replay.py 的判定（patch_identical 等）
├── commands.jsonl              # 逐条命令的耗时/资源
└── topdown/
    ├── perf.json | perf.csv    # perf 原始输出
    ├── perf.stderr
    ├── replay.log              # 重放全部输出，出问题先看它
    └── topdown.json            # 解析结果，机读
```

单独重新解析一份已有的 perf 输出（不用重跑）：

```bash
python3 topdown_parse.py <outdir>/<trial名>/topdown/perf.json --slots 8
```

---

## 6. 结果怎么读

四象限公式（分母一律是 `CPU_CYCLES × SLOTS`）：

```
Retiring      = OP_RETIRED          / (CPU_CYCLES × SLOTS)
BadSpec       = (OP_SPEC − OP_RETIRED) / (CPU_CYCLES × SLOTS)
FrontendBound = STALL_SLOT_FRONTEND / (CPU_CYCLES × SLOTS)
BackendBound  = STALL_SLOT_BACKEND  / (CPU_CYCLES × SLOTS)
```

**两个自检必须都是 ✅，否则那组数不要用：**

| 自检 | 判据 | ❌ 说明什么 |
|---|---|---|
| 四象限求和 | 在 `1 ± 0.03` 内 | 四象限按定义正好铺满全部 slot。不等于 1 只有三种原因：SLOTS 不对（最常见）、某个事件号指错了、发生了复用。解析器会**反推出 SLOTS 应该是多少**，直接对照 TRM |
| 最低调度占比 | `> 99.9%` | 低于它 = 计数器复用，每个事件只在一部分时间真在计数，其余靠外推 |

解析上的两个坑（已经在 `topdown_parse.py` 里处理掉了，这里记下来是为了别再踩）：

- **CSV 的列序不可靠。** 加了 `-G` 之后 perf 的 CSV 会**多出一列 cgroup**，而且各版本
  插在事件名前还是后并不一致。按列号取值在一台机器上能跑通，换一台就静默读错列 ——
  读到的还是个数字，不会报错。所以解析器一律**按事件的 `name=` 匹配**再按「形状」
  找计数值和调度占比，列号一次都不用。
- **JSON 是逐行 JSON 对象，不是一个数组。** `perf stat -j` 每个事件吐一行独立的 `{...}`，
  整文件 `json.load()` 直接报错，必须逐行解析。

---

## 7. 基线对照

这条 trial 在**开发机（x86_64，原生 amd64 镜像）**上的已知结果：

| 项 | 值 |
|---|---|
| `patch_identical` | **true** |
| `n_replayed` | 98（trace 共 99 条，跳过 1 条哨兵） |
| `elapsed_s` | 222.6 |
| `rc_match` | 95/98（语义 97/98） |

3 条不匹配全是**已知的 30s 超时**：`i=31` 与 `i=57` 属 semantic match（原 trace 里也超时了），
只有 `i=50` 是真背离（原 trace rc=0，重放撞上 30s 上限）。

包里带了 `<trial>/replay/verdict.json` 和 `<trial>/replay/commands.jsonl` 作对照，
`topdown_trial.sh` 跑完也会把新的 `verdict.json` 关键字段直接打出来。

### ⚠️ PMU 数字跨架构不可比

ARM 上采到的 cycles / IPC / 四象限，和原 amd64 harness **不构成任何对照关系**：
指令集不同、微架构不同、slot 模型不同、连「一条指令」的含义都不同。
**PMU 数据只能同机纵向用**（改一个参数前后对比、不同 trial 之间对比）。

判定「这台机器的环境是否与原 harness 等价」的，**始终只有 `patch_identical`**。
PMU 是用来回答「时间花在哪」的，不是用来回答「环境对不对」的。

---

## 8. 这一版覆盖什么、不覆盖什么

**覆盖：整条 trial 的聚合 topdown。** 窗口从「主容器出现」到「replay.py 退出」，
里面包含容器启动、全部 98 条命令、以及收尾的 `git diff` 保真校验。

**不覆盖：**

- **sidecar 的开销**。403 sinkhole sidecar（`<容器名>-sink`）是独立容器、独立 cgroup，
  `-G` 天然把它滤掉了，不用额外处理。
- **per-command 归因**。这一版给不出「哪条命令 Backend 高」，只有一个总数。
  那是后续工作（思路：按命令边界切窗口，或者在 replay.py 里对每条命令单独开关 perf）。

### 短命令的数据有效性 —— 读数之前先看这一节

这条 trial 的命令**绝大多数极短**，而短命令里**进程启动和动态链接的开销占了大头** ——
`timeout` 起一个进程、`/bin/sh -c` 再起一个、动态链接器再解析一遍符号，
**这些全都发生在容器 cgroup 里，会被原样算进 topdown**。也就是说，
四象限里有相当一部分反映的是「进程启动长什么样」，而不是「这个仓库的测试负载长什么样」。

实测（同一条 trial，两组数都能用下面的一行命令复算）：

| | n | min | median | `wall_s ≥ 1s` 的条数占比 | 这些条占掉的时间 |
|---|---|---|---|---|---|
| 开发机 x86_64 原生（包里带的基线，222.5s） | 98 | 0.081s | 0.115s | 18.4% | 93.0% |
| 开发机上的 arm64 仿真重放（541.3s） | 98 | 0.201s | 1.005s | 50.0% | 96.6% |
| 同上，剔掉 6 条撞 30s 超时的 | 92 | 0.201s | 0.597s | 46.7% | 94.9% |

arm64 那一组里**最快的一条也要 0.201s，最快的 `cat` 是 0.237s** —— 一个 `cat` 单文件
本身耗不了 0.2s，这 0.2s 基本就是启动开销的地板。

**怎么用这个事实：**

- `wall_s ≥ 1s` 的那批命令（不到一半的条数）吃掉了 **95% 左右**的时间。
  整条 trial 的聚合 topdown **主要反映的就是这批长命令**（基本都是 pytest），
  短命令虽然条数多，但对加权结果的影响有限。
- 但「有限」不等于「没有」。要把启动开销彻底剥掉，得等 per-command 归因那一版。
- 想自己复算：

  ```bash
  python3 - <<'PY'
  import json, statistics
  rows = [json.loads(l) for l in open("<trial>/replay/commands.jsonl")]
  w = sorted(r["wall_s"] for r in rows); tot = sum(w); ge1 = [x for x in w if x >= 1]
  print(f"n={len(w)} min={min(w):.3f} median={statistics.median(w):.3f} "
        f">=1s: {len(ge1)}条({100*len(ge1)/len(w):.1f}%) 占时{100*sum(ge1)/tot:.1f}%")
  PY
  ```

---

## 9. 出问题查这里

| 现象 | 多半是 | 怎么办 |
|---|---|---|
| 某个事件是 `<not supported>` | **这个事件号在这颗核上无效** | 号写错了，或这颗核不实现它。逐个对照**目标核 TRM** 的 PMU 事件表改 `topdown.conf` —— 五个里只要有一个不对，整组 `{}` 都开不起来 |
| `/sys/bus/event_source/devices/` 下没有 `armv8*` | 内核没注册 ARM PMU 驱动 | baremetal 上一般是 DT/ACPI 没描述 PMU 中断（固件/DTB），或内核没编 `CONFIG_ARM_PMUV3`。`sudo dmesg \| grep -iE 'pmu\|perfevents'` |
| 调度占比 < 100%（复用） | ① watchdog 占着一个计数器 ② `EV_EXTRA` 开多了 ③ 有别的 perf 会话在抢 | `cat /proc/sys/kernel/nmi_watchdog` 非 0 → `sudo sysctl kernel.nmi_watchdog=0`（**采完改回 1**）；清空/减少 `EV_EXTRA` 分两轮采；`pgrep -a '^perf'` 看有没有别人在跑。余量账看 `probe_pmu.sh` 第 3 节 |
| 四象限求和 ≠ 1 | ① SLOTS 不对（最常见）② 某个事件号指错了 ③ 复用 | 先看占比自检排除 ③；再看解析器「反推 SLOTS 应约为 N」那一行，对照 TRM；都对就逐个事件号核 TRM |
| 计数全是 0，但不是 `<not supported>` | cgroup 路径推错，`-G` 滤空了 | **`perf -G` 路径不对时不报错，只给 0**。核对脚本打印的 cgroup 相对路径是不是那个容器；docker 的 cgroup driver 是 systemd 还是 cgroupfs 决定落点（`docker info -f '{{.CgroupDriver}}'`），rootless 则落在 `user.slice` 下 |
| 「60s 内没等到重放容器」 | 镜像不在本地 / 存量容器占着名字 | 看脚本打出来的 `replay.log` 尾部。`bash build_arm.sh returns-validated` 建镜像；`docker ps -a --filter name=^replay_` 清存量 |
| 采到的数偏小，而且容器明明在跑 | 采到 `-sink` 上去了 | sidecar 是独立 cgroup。脚本已经排掉 `-sink`，如果是手工敲的 perf 命令，检查用的是不是主容器的 ID |
| `perf` 命令在，但一跑就报 `perf not found for kernel ...` | Debian/Ubuntu 的 `/usr/bin/perf` 是个按 `uname -r` 找真身的 **wrapper 脚本** | 装 `linux-tools-$(uname -r)`；云镜像上这个具体版本常不在源里，退一步装 `linux-tools-generic` |
| 采集跑到一半卡住不动 | `sudo` 在等密码 | 先 `sudo -v` |
| 异构核（多个 `armv8*` PMU） | 事件只在一簇核上打开，进程跑到别的簇就采不到 | 用 `docker --cpuset-cpus` 把容器钉在一簇上；或每个 PMU 各采一轮自己合并（本版不支持） |

---

## 10. 文件清单

| 文件 | 干什么 |
|---|---|
| `TOPDOWN.md` | 本文件，入口 |
| `topdown.conf` | **唯一需要你改的文件**：PMU / SLOTS / 五个事件号 / EV_EXTRA / 输出格式 |
| `probe_pmu.sh` | 目标机第一件事：验事件号有效性 + 计数器余量 + 活体验证 `-G` |
| `topdown_trial.sh` | 采集主脚本 |
| `topdown_parse.py` | 解析 perf 输出 → 四象限 + 两个自检 + `topdown.json`（只用标准库） |
| `replay.py` | 重放引擎（与主包同一份） |
| `build_arm.sh` | 从 `mars-base` 重建 task 镜像 |
| `check_sources.sh` | 构建期上游源连通性探测 |
| `preflight.sh` | 重放环境预检（cgroup / docker / netns 活体测试） |
| `get_ca_cert.sh` / `detect_mitm.sh` | 内网 TLS 中间人：检测 + 取 CA |
| `returns-validated-error-accumula__8JQj5gw/` | 唯一一条 trial。`replay/` 下是 x86 基线对照 |
| `BUILD_INFO` / `SHA256SUMS` | 这份包的来源与全量指纹 |
