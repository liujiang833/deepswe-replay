# ARM topdown 采集包

在 ARM 服务器上重放 DeepSWE trial，同时在**宿主侧**按 cgroup 过滤采 PMU 事件，
算出 ARM L1 topdown 四象限（Retiring / BadSpec / FrontendBound / BackendBound）。

本文件就是操作说明，从解包到出结果一条路走完。

**包有两种，`cat BUILD_INFO` 的 `kind` 一行可辨：**

| kind | 带几条 trial | 入口 | 用途 |
|---|---|---|---|
| `topdown` | 1 条（带 x86 基线对照） | `topdown_trial.sh` | 先把采集链路在目标机上跑通 |
| `topdown-full` | 全量 113 条（多数无基线） | `run_batch.py --topdown` | 链路通了之后批量采，做横向对比 |

两种包的脚本是同一套，差别只在带几条 trial、以及全量包多带一个 `run_batch.py`。

**主流程 —— 顺序是有讲究的，别跳步：**

```
解包核对 → probe_pmu.sh → 填 topdown.conf → build_arm.sh → topdown_trial.sh   单条，§5
   §1          §2              §3               §4        ↘ run_batch.py --topdown  批量，§5.1
```

- **探针为什么必须在最前**：它一次性确认三件事 ——
  **① 事件号在这颗核上真的有效、格式也对**（PMUv3 只有一小部分事件号是架构必需的，
  其余各家核自己编号；而漏写 `0x` 前缀会让 perf 按十进制读成别的事件还不报错）；
  **② 通用计数器余量够**（watchdog 会常驻占掉一个，6 个变 5 个，而我们要开 4~6 个）；
  **③ cgroup 路径推导对**（`-G` 路径错了不报错，只给一串 0）。
  这三样任何一样不对，后面建镜像那几分钟和整条重放（开发机上 222s，ARM 上只会更久）
  都是白做。`probe_pmu.sh` 几十秒跑完，先把这三个问题问清楚。
- **建镜像为什么排在填配置之后**：不为什么，纯粹是建镜像最慢，让它在后台跑的同时
  你正好可以按 TRM 核对事件号。两件事互不依赖。
- **批量为什么不是「并发跑快一点」**：`--topdown` **强制串行**，`--jobs > 1` 会被直接
  拒绝启动。理由是计数器竞争，不是「怕慢」—— 详见 §5.1。全量 113 条串行是**数小时**
  级别，所以实际做法是 `--skip-missing` 只跑已建好的镜像、或 `--per-lang N` 每种语言
  抽几条，而不是硬跑满。

---

## 0. 前置条件

| 必须有 | 说明 |
|---|---|
| **ARM 服务器**，Neoverse V1/N2 级 | slot-based TMA。本包的公式与事件号是 ARM PMUv3 的 |
| **baremetal**（本包的前提） | 不是虚机，所以不存在 vPMU 透传问题。真跑在虚机上的话，先自行确认 hypervisor 开了 vPMU |
| **通用计数器余量 ≥ 4**（直接法 ≥ 5） | 要开几个取决于后端口径，见 §3。NMI/hardlockup watchdog 会占掉一个，`probe_pmu.sh` 会替你算这笔账 |
| `perf` | `perf stat -a -G` 要能用。`sudo` 权限，或 `kernel.perf_event_paranoid ≤ 0` |
| `docker` | 能起容器、能 build。**cgroup v1 / v2 都支持**，但 v1 上有一条已知限制，见下 |
| `python3` | 3.8+，**只用标准库**，无需 pip install |
| **`mars-base` 基座镜像** | 这条 trial 的镜像 `FROM mars-base`。基座不在就建不了 |

⚠️ **重放期不需要外网、也不该有** —— 重放容器是 `--network=none` + 403 sinkhole，
刻意还原原 harness 的 `allow_internet=false`。要联网的只有构建期。

### ⚠️ cgroup v1 机器上的已知限制

先看一眼自己在哪一档：`stat -fc %T /sys/fs/cgroup` —— `cgroup2fs` 是 v2，
`tmpfs` 是 **v1**（`probe_pmu.sh` 和 `topdown_trial.sh` 第一屏也都会直接告诉你）。

v1 上有两件事跟着变，**都已经在脚本里处理掉了**，但你要知道它们是同一个根因：

| | v2（统一层级） | v1（多层级） |
|---|---|---|
| `perf -G` 的路径 | 相对 `/sys/fs/cgroup/` | 相对 **`/sys/fs/cgroup/perf_event/`**（perf 用 perf_event 这个独立层级） |
| `replay.py` 的 per-command cgroup 指标 | 可用 | ❌ **不可用**（它只认 v2 的 `cpu.stat` / `memory.current`） |

- 路径这件事脚本已经改成**直接读 `/proc/<容器主进程 pid>/cgroup`**，不猜 docker 的
  cgroup driver，v1/v2 各取各的那一行，取完还验一次目录真的在。
- `replay.py` 那套指标在 v1 上等于要重写（`cpuacct.usage` / `memory.usage_in_bytes` /
  `blkio.*` 分散在不同层级），**不在这一版范围内**。v1 上它会在启动时报
  `sinkhole cgroup 初始化失败` 把整轮采集卡住 —— **加 `--no-metrics` 绕过**：

  ```bash
  bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw --no-metrics
  ```

  **这不影响 topdown 结果，也不影响 `patch_identical`。** PMU 是宿主侧 `perf -G` 采的，
  完全不经过 `replay.py` 那套指标；受影响的只有 `commands.jsonl` 里
  `usage_usec` / `mem_peak` 那几列（记成 `null`）。

> 这两个报错（`sinkhole cgroup 初始化失败` 和
> `no access to cgroup /sys/fs/cgroup/perf_event/...`）看着毫不相干，
> **根因是同一个：这台机器是 cgroup v1**。所以脚本把 cgroup 版本放在第一屏。

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
  `sudo perf stat -a -e "{事件组}" -G <cgroup>` 采 3 秒（注意 `-G` 在 `-e` **之后**，
  见 §5 步骤 5）→ 检查这几件事：

  | 检查 | ❌ 意味着 |
  |---|---|
  | 计数非零且不是 `<not supported>` | **事件号在这颗核上无效** —— 号写错了，或这颗核不实现这个事件。逐个对照目标核 TRM |
  | 事件号格式是 `0x…` | 写 `11` 会被 perf 按**十进制**读成 `0xb`，数到别的事件还不报错。探针会当场拒绝 |
  | 每个事件的调度占比 = 100% | 发生**计数器复用**，事件组没生效，比值不可信 |
  | 那组四象限自检（**跑哪几条取决于后端口径**，见 §3 / §6） | 直接法：求和 ≈ 1；残差法：C1~C5（+ 可选的 X） |

  > ⚠️ 探针的活体验证在残差法下**不会**、也不该打出「四象限求和 ≈ 1，通过」——
  > 残差法下那是恒等式。它会改口说「C1~C5 全过」，并且在没开 X 交叉校验时
  > 额外 ⚠️ 一句「这组抓不到 SLOTS 偏大」。

  还有一节**计数器余量**的账（baremetal 上最隐蔽的失败模式）：

  - `kernel.nmi_watchdog` 非 0，且内核用的是 `CONFIG_HARDLOCKUP_DETECTOR_PERF`
    （而不是 buddy / arch 那几种不吃 PMU 的实现）时，watchdog 会**常驻占用一个通用计数器**，
    6 个变 5 个。本套要开 4~6 个（看口径，见 §3），卡在边界上 —— 再加一个就可能复用。
    探针会提示 `sudo sysctl kernel.nmi_watchdog=0` 临时腾出来，**采完记得
    `sudo sysctl kernel.nmi_watchdog=1` 改回去**（它是死锁检测，长期关着等于少一层保护）。
  - `pgrep -a '^perf'`：同机还有别的 perf 会话在跑的话会来抢计数器。
  - 最后把「通用计数器总数 / watchdog 占用 / 实际可用 / 本次要开的事件数」并排打出来，
    余量够不够一眼可见。

  > 为什么这笔账值得单独算：**复用不会报错**，它只会让自检红（直接法是「求和 ≈ 1」，
  > 残差法是 C1 / C4），而那个现象跟「事件号写错了」一模一样。不先把余量的账算清楚，
  > 很容易一路去抠 TRM 事件号，方向全错。

**为什么必须活体验证，而不是查一查配置就算了**：`perf -G` 在 cgroup 路径推错的时候
**不报错**，它只是安安静静给你一串 0。看起来像「这段负载没跑」，实际是过滤器根本没命中。
所以只能真采一次、真看数。

探针**不猜** docker 的 cgroup driver：它读容器主进程的 `/proc/<pid>/cgroup`，
v2 取 `0::/…` 那一行、v1 取 `perf_event` 那一行（v1 上控制器常常 co-mount 成
`cpuset,perf_event` 这种逗号列表，所以是按边界正则匹配的），去掉前导 `/` 就是
`-G` 要的相对路径，再验一次对应目录真的存在。推不出来会把 `/proc/<pid>/cgroup`
**原文**打出来 —— 那是排查这件事的唯一线索。

> 这里原先是「猜两条常见路径 + `find` 兜底」，**在 cgroup v1 上会静默推错**：
> v1 的 `/sys/fs/cgroup/` 下是 `blkio/`、`memory/`、`perf_event/` 等并列的控制器目录，
> 每个下面都有 `docker/<id>`，`find -name "*<id>*"` 会命中其中随便一个（字母序大概率是
> `blkio`），于是路径变成 `blkio/docker/<id>`，perf 再拼到自己的 perf_event 挂载点下 →
> 一个根本不存在的路径。报错长这样，完全看不出是推导选错了控制器目录：
> ```
> no access to cgroup /sys/fs/cgroup/perf_event/blkio/docker/<id>
> ```

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
  > 但整套数都是错的，而且**不会报任何错**。兜底是自检，可那时你已经采完一整轮了。
  >
  > **而兜底能力取决于后端口径：**
  >
  > | | SLOTS 偏小 | SLOTS 偏大 |
  > |---|---|---|
  > | 直接法 | 求和 > 1 ❌ | 求和 < 1 ❌ |
  > | 残差法 | C1 ❌（还会反推出最小自洽 SLOTS） | **抓不到** ← 只有 X 交叉校验能补 |
  >
  > 直接法实测（把 SLOTS 从 8 写成 5）：
  > ```
  > ❌ 求和  四象限求和 1.6000，超出 1 ± 0.03
  >    反推：若事件号无误，SLOTS 应约为 8.00
  > ```
  > 残差法实测（同一组数，同样把 8 写成 5）：
  > ```
  > ❌ C1  残差非负失败：Retiring+BadSpec+FrontendBound = 1.6000 > 1
  >        → BackendBound = -0.6000，是个**负数**，物理上不可能。
  >    反推最小自洽 SLOTS = (OP_SPEC + STALL_SLOT_FE) / CPU_CYCLES = 8.0000
  > ```

- 事件号，默认值是 **ARM PMUv3 架构定义值**，多数 Neoverse 核直接适用，
  但**请按目标核 TRM 核对**：

  | 配置键 | 默认 | 事件 | 用途 | 能不能留空 |
  |---|---|---|---|---|
  | `EV_CPU_CYCLES` | `0x0011` | CPU_CYCLES | 公共分母 | ❌ 必填 |
  | `EV_OP_RETIRED` | `0x003a` | OP_RETIRED | Retiring | ❌ 必填 |
  | `EV_OP_SPEC` | `0x003b` | OP_SPEC | BadSpec = OP_SPEC − OP_RETIRED | ❌ 必填 |
  | `EV_STALL_SLOT_FE` | `0x003d` | STALL_SLOT_FRONTEND | FrontendBound | ❌ 必填 |
  | `EV_STALL_SLOT_BE` | *（空）* | STALL_SLOT_BACKEND | BackendBound | ✅ **留空 = 残差法** |
  | `EV_STALL_SLOT` | *（空）* | STALL_SLOT（总停顿） | 只做 X 交叉校验 | ✅ 留空 = 不跑 X |

  > ⚠️ **事件号必须写成 `0x` 开头的十六进制。** perf 的 `event=` 按 **C 风格**解析数字 ——
  > 写 `11`，perf 读到的是**十进制 11**，也就是事件 `0xb`（BR_MIS_PRED），
  > 而你想要的 CPU_CYCLES 是 `0x11`。这个错误 perf **完全不报错**：`0xb` 是合法事件，
  > 照样有数，只是数的是别的东西。`topdown_trial.sh` / `probe_pmu.sh` 会在启动时
  > 逐个校验（含 `EV_EXTRA` 里的号），不合格当场拒绝：
  > ```
  > ❌ EV_CPU_CYCLES=11 —— 事件号必须写成 0x 开头的十六进制（如 0x0011 / 0x11）
  > ```

### 后端口径：直接法 or 残差法 —— 这一节是新的，必须读

`EV_STALL_SLOT_BE` **允许留空**：

| | 填了（直接法） | 留空（残差法） |
|---|---|---|
| BackendBound | `STALL_SLOT_BACKEND / (CPU_CYCLES × SLOTS)` | `1 − (Retiring + BadSpec + FrontendBound)` |
| 主事件个数 | 5 | 4 |
| 「四象限求和 ≈ 1」 | **真校验** | **恒等式，不是校验** |
| 跑哪些自检 | 求和 + C2~C5 | C1~C5 |

**为什么会有残差法**：不是所有核都实现了独立的 `STALL_SLOT_BACKEND` 计数器。
没实现的核上填了它，perf 会给 `<not supported>`，而且事件整组用 `{}` 包着，
**组里一个事件无效整组都开不起来**，另外四个也跟着采不到。这种核只能走残差法。
留空**不是降级用法，是这颗核的正确用法**。

> ⚠️⚠️ **残差法的代价：「四象限求和 ≈ 1」这条自检直接失效。**
> 残差法的 BackendBound 是拿 1 减出来的，求和**恒等于 1**，永远是个漂亮数字。
> 而那条自检原本是防「SLOTS 取错 / 事件号写错」的主要安全网 —— 在残差法下它退化成
> 恒真式，判别力为零。**继续给它打一个绿色 ✅ 比不检查更糟，那是假的安全感。**
> 所以 `topdown_parse.py` 在残差法下**不给求和打 ✅**（它照打这个数，但紧跟一句
> 说明它恒等于 1、不构成校验），改跑 C1~C5，见 §6。

- `EV_STALL_SLOT=`（STALL_SLOT，架构值 `0x003f`，总停顿 slot 数）—— **可选，但强烈建议填**。

  填了它，解析器会**独立**算一个 backend 出来跟上面的结果对账：

  ```
  BE_indep = (STALL_SLOT − STALL_SLOT_FRONTEND) / (CPU_CYCLES × SLOTS)
  ```

  差超过 `0.03` 报 ❌。这是残差法下**唯一**一条不依赖「1 减出来」那个恒等式的校验，
  也是**唯一**能抓到「SLOTS 偏大」的校验。代价是多占一个通用计数器。
  目标核没实现 `0x003f` 就留空 —— 不影响四象限，只是少一条校验（而解析器会把
  「你现在没有任何一条校验能抓 SLOTS 偏大」这句话明确打给你看）。

- `EV_EXTRA=` —— 可选的 L2 下钻事件，格式 `"名字=0x00xx 名字2=0x00yy"`。
  加事件不用改代码，只打印原始计数，不参与四象限。

  > ⚠️ **总事件数不能超过实际可用的通用计数器个数。** 这一轮要开几个：
  >
  > ```
  > 主事件      残差法 4 个 / 直接法 5 个
  > + EV_STALL_SLOT   填了 +1
  > + EV_EXTRA        每条 +1
  > ```
  >
  > Neoverse 一般 6 个通用计数器，但 **watchdog 开着时只剩 5 个**。所以典型组合是：
  >
  > | 组合 | 事件数 | watchdog 开着（5 个可用） |
  > |---|---|---|
  > | 残差法，不开 X | 4 | 还能加 1 条 `EV_EXTRA` |
  > | 残差法 + X | 5 | **正好占满**，`EV_EXTRA` 加不了 |
  > | 直接法，不开 X | 5 | **正好占满** |
  > | 直接法 + X | 6 | ❌ 必然复用，先关 watchdog |
  >
  > 超了 perf 会开始**复用**：每个事件只在一部分时间真在计数，其余靠外推，
  > 调度占比掉到 100% 以下（C5 会红），四象限跟着偏。宁可分两轮采。
  > 本机的余量账看 `probe_pmu.sh` 的「计数器余量」一节 —— 它按你实际配的口径算。

- `PERF_OUTPUT=auto` —— `auto` 按 perf 版本探测（≥ 5.17 用 `-j` 出 JSON，否则 `-x,` 出 CSV）。
  排查时可强制写 `json` / `csv`。

配置文件同时要能被 bash `source` 和 python 逐行解析，所以格式是纯 `KEY=VALUE`：
等号两边不留空格，带空格的值用引号包住，注释用 `#`，不要写 shell 展开。

---

## 4. 建镜像 —— 最耗时的一步

包里**没有镜像**，带的是 task 的 `environment/Dockerfile`（配方）。

**单条包**只有 1 条 trial：

```
returns-validated-error-accumula__8JQj5gw     # python，仓库 dry-python/returns
```

```bash
bash check_sources.sh                  # 先探上游源通不通（几秒）
bash build_arm.sh --list returns-validated   # 只看会怎么改写 Dockerfile，不构建
bash build_arm.sh returns-validated          # 真建
```

**全量包**（`kind = topdown-full`）有 113 条，一条一个镜像，不可能一次全建好 ——
也**不需要**全建好。`build_arm.sh` 的目标既可以是语言名，也可以是 trial 名前缀，
并列多个目标用**空格**分隔（不是逗号，逗号会被当成一个目标名直接报「认不出目标」）：

```bash
bash check_sources.sh                  # 先探上游源
bash build_arm.sh --list python        # 只看会怎么改写，不构建
bash build_arm.sh python               # 建 python 那一批
bash build_arm.sh python go            # 两种语言一起建（空格分隔）
bash build_arm.sh returns-validated    # 只建某一条（按 trial 名前缀）
```

建到哪算哪，然后用 `run_batch.py --skip-missing` 跑已经建好的那部分，边建边跑 ——
见下面 §5.1。

⚠️ 重建出来的镜像**不等于**原 amd64 镜像。`patch_identical` 在重建镜像上是否仍然成立，
本身就是这轮要测的东西之一。内网 TLS 中间人的处理见 `get_ca_cert.sh` / `detect_mitm.sh`，
用法与主包一致（`build_arm.sh --proxy` / `--ca-cert` / `--registry` …）。

---

## 5. 采集

```bash
bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw
bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw -o /data/td
bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw --limit 5      # 冒烟
bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw --no-metrics   # 见下
```

> **`--no-metrics`**：`replay.py` 启动时报 `sinkhole cgroup 初始化失败` 的话加上它。
> 它关掉的是 `replay.py` **自己那套 cgroup 指标**（`usage_usec` / `mem_peak`，靠直接读
> 容器的 cgroup 文件拿），和本脚本在宿主侧用 `perf -G` 采 PMU 是**两条完全独立的路** ——
> 关掉不影响四象限一个数，只是 `commands.jsonl` 里那几列记成 `null`。
> **cgroup 指标采不到 ≠ topdown 采不到**，别因为前者放弃整轮采集。

> `perf stat -a` 要 root。脚本会自动加 `sudo`；**先跑一次 `sudo -v`** 把密码缓存起来，
> 免得采集跑到一半卡在密码提示上（那时重放已经在跑了）。
>
> 这条 trial 在开发机上跑 222s，ARM 上只会更久。**远程跑建议放进 tmux / screen。**
> （实测 bash 在未捕获 SIGHUP 时仍会执行 EXIT trap，所以 SSH 掉线会走 cleanup 正常
> 收尾、不会留孤儿容器 —— 但那一轮的数据也就没了，重跑一遍不如一开始就挂上。）

### 它做了什么

1. `source topdown.conf`，定下 PMU（空则探测）与 SLOTS（空则读 `caps/slots`）
2. 后台起 `python3 replay.py <trial> <trial>/task.json -o <outdir>`（带 `--no-metrics` 时透传过去），记下 PID
3. 轮询等主容器出现（最多 60s）。容器名是
   `replay_<trial名 sanitize 后前 44 字符>_<replay.py 自己的 PID>`，而 replay.py 是我们
   fork 的，所以这个名字**可以精确算出来**，不用去 `docker ps` 里猜（机器上可能同时有
   别人的重放在跑）。兜底才按 `^replay_` 前缀找，且**必须排掉 `-sink`** ——
   那是 403 sinkhole sidecar，独立容器、独立 cgroup，采它等于采了个空气
4. 读容器主进程的 `/proc/<pid>/cgroup` 推 cgroup 相对路径（**不猜 docker driver**；
   v2 取 `0::/…` 行，v1 取 `perf_event` 行 → 路径相对 `/sys/fs/cgroup/perf_event/`），
   并验证对应目录真的存在。相对路径和**绝对路径**都会打印出来
5. `sudo perf stat -a <-j|-x,> -o <file> -e "{事件组}" -G "$CG" -- tail --pid=<replay PID> -f /dev/null`

   > ⚠️ **`-G` 必须排在 `-e` 后面**，顺序反了 perf 直接拒绝启动，只吐一句
   > `must define events before cgroups`。perf 的 `parse_cgroups()` 在解析 `-G` 时会检查
   > evlist 是不是空的，空就报这句退出；man perf-stat 的原话是 cgroup
   > "always refer to events defined earlier on the command line" —— `-G` 是**按位置**
   > 绑到它前面那些 `-e` 上的，不是一个全局开关。报错信息里完全没提「参数顺序」，
   > 不翻 man page 很难联想到，所以别把这个顺序「顺手整理」成看着更顺眼的样子。
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
    ├── run_status.json         # 三个退出码分开记：replay_rc / perf_rc / parse_rc
    └── topdown.json            # 解析结果，机读（含 backend_method / checks_run / 每条自检的过没过）
```

> `run_status.json` 为什么要单独存在：本脚本最后只能吐**一个**退出码，而它把两件
> 正交的事压成了一个数 —— `replay_rc` 回答「这条 trial 重放成没成」（保真度那条线），
> `parse_rc` 回答「四象限的自检过没过」（数据可信度，2 = 数拿到了但自检没过）。
> 调用方（`run_batch.py`）看到一个非零退出码分不清是哪一种，而把 trial 判成失败是
> **错的**：topdown 采废不影响 `git diff` 逐字节相等这个结论。

单独重新解析一份已有的 perf 输出（不用重跑）：

```bash
python3 topdown_parse.py <outdir>/<trial名>/topdown/perf.json --slots 8
```

**要一次跑多条 trial 就别写 for 循环**，用 `run_batch.py --topdown` —— 见 §5.1。

---

## 5.1 批量采集 —— `run_batch.py --topdown`

链路在单条上跑通之后，用 `run_batch.py` 批量采。**不要自己写 for 循环去调
`topdown_trial.sh`** —— `run_batch.py --topdown` 干的就是这件事，外加排程、跳过没建好
镜像的、收三个退出码、汇总四象限。

```bash
# 标准姿势：只跑已建好镜像的那些，关掉 replay.py 自己那套 cgroup 指标
python3 run_batch.py --topdown --skip-missing --no-metrics

# 先探路：每种语言抽 2 条，十几分钟出一版横向对比
python3 run_batch.py --topdown --per-lang 2 --no-metrics

# 只看某几种语言
python3 run_batch.py --topdown --per-lang 2 --only python,go --no-metrics

# 不真跑，只看这轮会选中哪些、资源账多少
python3 run_batch.py --topdown --per-lang 2 --dry-run
```

完整批量流程（承接前面的 §1~§4）：

```
probe_pmu.sh  →  填 topdown.conf  →  build_arm.sh <语言或前缀>  →  run_batch.py --topdown --skip-missing --no-metrics
   §2                §3                      §4                              §5.1
```

> **`--no-metrics` 在 cgroup v1 的机器上是必须的**，不是可选项。`replay.py` 那套
> per-command 指标只认 v2 的 `cpu.stat` / `memory.current`，v1 上它会在启动时报
> `sinkhole cgroup 初始化失败`，**整条采集起不来**。`run_batch.py` 默认就不采指标
> （等价于 `--no-metrics`），显式写出来只是让命令行自己说明白。它**不影响 topdown
> 一个数**，也不影响 `patch_identical` —— PMU 是宿主侧 `perf -G` 采的，两条路互不相干。

### ⚠️ 为什么 `--topdown` 不能并发 —— 这条是硬的

`--topdown` 与 `--jobs > 1` **互斥，并且直接报错退出**，不是警告后继续。

这和 `--metrics` 那条互斥**机制不同，也更硬**：

| | `--metrics` 并发 | `--topdown` 并发 |
|---|---|---|
| 抢的是什么 | **机器资源**：CPU 配额、内存、磁盘队列 | **同一批物理计数器**（PMU 上的通用计数器，全机共享） |
| 后果 | 数字被挤压得偏悲观，但每条 trial 各有各的一份数据 | 触发**复用**，每个事件只在一部分时间窗口里真计数，其余靠外推 |
| 还能用吗 | 不能横向比，但数本身是真的 | **整批作废** |

算笔账：PMU 上的通用计数器 Neoverse 一般 **6 个**，NMI watchdog 开着只剩 **5 个**。
而一轮 topdown 要开 **4~6 个**事件（残差法 4 / 直接法 5 / 再加 `EV_STALL_SLOT` 6），
还要求它们作为一个 `{}` 组被内核**同时上、同时下**。并发 N 条就是 N 个
`perf stat -a` 会话同时要这批计数器，总需求 ≈ N × 4~6 —— 只要 N ≥ 2 就一定超。

超了内核**不报错**，它会复用。表现是：

- 每条 trial 的 **C5（最低调度占比 > 99.9%）自检全部失败**；
- 四象限的分子和分母来自**不同的时间窗口**，比值不再有物理意义；
- 而屏幕上每条都「跑完了」，四个数也都规规矩矩落在 0~1 之间。

所以 `run_batch.py` 在这里选择**报错退出**：一批看着像真的假数据，比跑不起来危险得多。

### 时间预算 —— 必须先算，再决定跑多少

`--topdown` 强制串行，所以整批耗时 ≈ **各条耗时之和**，没有并发可以摊。

以包里那条 python trial 的 x86 基线为参照：**98 条命令 / 222.6s**，约 **2.3s/命令**。
ARM 上只会更慢（重建镜像、不同微架构），按 **×1.4** 粗估：

| 跑法 | 条数 | 命令合计 | 粗估耗时 |
|---|---|---|---|
| 全量 113 条 | 113 | 约 8000+ | **数小时**（4~8h 量级，取决于机器） |
| `--per-lang 2 --pick median`（默认） | 10 | 324 | **约 18 分钟** |
| `--per-lang 2 --pick heaviest` | 10 | 1243 | **约 68 分钟** |
| `--per-lang 2 --pick lightest` | 10 | 178 | 约 10 分钟 |

**结论：不要一上来就跑满 113 条。** 实际做法是

1. `--per-lang 2`（默认 median）先出一版横向对比，十几分钟；
2. 觉得某几种语言值得细看，再 `--only <语言> --skip-missing` 跑那一批；
3. 真要全量，挂 tmux / screen 过夜。

> 跑之前先 `sudo -v` 把密码缓存起来 —— `perf stat -a` 要 root，串行批次跑几小时，
> 中途卡在密码提示上时前面的容器已经起来了。

### `--per-lang N` / `--pick` —— 每种语言抽 N 条

```bash
python3 run_batch.py --topdown --per-lang 2 --no-metrics                  # 默认 median
python3 run_batch.py --topdown --per-lang 2 --pick heaviest --no-metrics  # 数据更干净，慢 3.8 倍
python3 run_batch.py --topdown --per-lang 1 --pick lightest --no-metrics  # 只验链路通不通
```

选取规则：

1. 按 `meta.json` 的 `language` 分组；
2. **只在镜像已建好的里面挑**（和 `--skip-missing` 是同一个判断，两个同时给不冲突）；
3. 组内按 `n_commands` 排序后按策略取 N 条，**同命令数按 trial 目录名升序兜底** ——
   同样的输入必须选出同一批，否则两次跑的结果没法比；
4. 某语言一条可用的都没有 → **贡献 0 条，不报错也不中断**（那正是「这门语言的镜像还没建」）。

三种策略的权衡 —— **这是耗时与数据干净度的取舍，不是好坏之分**：

| `--pick` | 取哪几条 | 快慢 | 数据干净度 |
|---|---|---|---|
| `median`（默认） | 命令数**正中间**的 N 条 | 快（全量各取 2 条约 18 分钟） | 一般 |
| `heaviest` | 命令数**最多**的 N 条 | 慢 3.8 倍（约 68 分钟） | **最好** |
| `lightest` | 命令数**最少**的 N 条 | 最快（约 10 分钟） | 最差 |

> **为什么轻的 trial 数据更脏**：topdown 采的是**整条 trial 的聚合值**，里面固定含
> **容器启动**和收尾的 `git diff` 保真校验。trial 越轻，这笔固定开销在聚合值里占的比重
> 越大，四象限就越是在描述「容器启动 + 解释器 import 长什么样」，而不是这个仓库的
> workload 长什么样。想要更干净的 workload 画像就用 `--pick heaviest`，代价是慢几倍。
> **真正干净的对比要等 per-command 归因那一版**，这一版只能在这两头之间选。

开跑前会把选取结果摊开，用的哪种策略、每条多少命令都在里面：

```
选取      --per-lang 2 --pick median（按命令数取中位，同数按名字）
  python      3/34 已建镜像 → 取 2 条
                <trial-a>                                     (26 条命令)
                <trial-b>                                     (26 条命令)
  go          1/35 已建镜像 → 取 1 条（可用的不够 2 条）
                <trial-c>                                     (28 条命令)
  typescript  0/34 已建镜像 → 跳过
  合计 3 条
```

`SUMMARY.md` 和 `summary.json` 里都会记下这轮是抽样跑的（`--per-lang` 的值、`--pick`
的值、各语言实际取了几条、总共多少条可用）—— 抽样批次和全量批次的报告长得一模一样，
不留痕的话事后会被当成全量结论去引用。

### 批量的输出

```
runs/<UTC 时间戳>/                      # 或 -o 指定的目录
├── SUMMARY.md                          # 人读：总表 + topdown 横向小结 + 逐条
├── summary.json                        # 机读：每条带完整 topdown 结果
├── logs/<trial>.log                    # 每条的 topdown_trial.sh 全部输出
└── <trial名>/
    ├── verdict.json                    # 重放判定（patch_identical 等）
    ├── commands.jsonl
    └── topdown/
        ├── perf.json | perf.csv        # perf 原始输出
        ├── perf.stderr
        ├── replay.log
        ├── run_status.json             # 三个退出码分开记，见下
        └── topdown.json                # 四象限 + 逐条自检
```

`SUMMARY.md` 的总表**在原有各列后面追加**五列（不另起一张表 —— 这张表就是拿来横向
对比不同 benchmark 的，拆开就得来回对着行名找）：

```
| 语言 | 退出 | 保真 | rc_match | rc_语义 | 耗时s | vs基线 | Retiring | BadSpec | FE | BE | 校验 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| python | 0 | ✅ | 95/98 | 97/98 | 240.1 | — | 31.2% | 8.1% | 14.7% | 46.0% | ✅ |
| go     | 0 | ✅ | 66/69 | 68/69 | 110.4 | — | 28.0% | 6.2% | 19.1% | 46.7% | ❌C5 |
| rust   | 0 | ✅ | 74/76 | 75/76 | 300.2 | — | —      | —     | —     | —     | — |
```

- 四个比值是百分比、保留一位小数；
- **「校验」列写明是哪一条红了**（`❌C1` / `❌C5` / `❌C1,C5` / `❌求和` / `❌X`），
  不是笼统一个 ❌ —— C1 红了是「SLOTS 偏小或事件号错」，C5 红了是「计数器复用」，
  两者的排查方向毫无交集，只写 ❌ 等于把诊断信息扔掉；
- 没采到写 `—`；
- **不加 `--topdown` 时这五列完全不出现**，老报告能逐字节 diff。

后面还有一张按语言分组的**横向小结**（各象限的均值 / 中位数）。注意它只统计
**自检也全过**的那些条：C5 红了的那组数分子分母来自不同时间窗口，混进平均值只会
污染整组，而平均值这个形式恰恰把「哪一条坏了」抹掉。被剔掉几条会单独成列写出来。

### ⚠️ 单条 topdown 采废，不算这条 trial 失败

这两件事**正交**，报告里也分开记：

| | 判据 | 归谁管 |
|---|---|---|
| **保真度** | `patch_identical` —— 容器内 `git diff --binary` 与 `model.patch` 逐字节相等 | `replay.py` → `verdict.json` |
| **数据可信度** | C1~C5（+ X）自检 | `topdown_parse.py` → `topdown.json`，退出码 2 |

`topdown_trial.sh` 最后只能吐**一个**退出码，它把这两件事压成了同一个数。所以它额外落
一份 `run_status.json`，把三个退出码分开记：

```json
{ "trial": "...", "replay_rc": 0, "perf_rc": 0, "parse_rc": 2 }
```

`run_batch.py` 按 `replay_rc` 判这条 trial 成没成 —— **某条 topdown 采废了，照常记录该条
的重放结论，topdown 那几列标 `—` 或 `❌Cx`**。逐条那一节会写清楚原因和三个退出码的分解。

（例外：**早于重放启动**的失败 —— 事件号不合法、没 perf、等不到容器 —— 不会写
`run_status.json`。那种情况这条 trial 确实没跑起来，按失败计，这是有意的。）

---

---

## 6. 结果怎么读

四象限公式（分母一律是 `CPU_CYCLES × SLOTS`）：

```
Retiring      = OP_RETIRED             / (CPU_CYCLES × SLOTS)
BadSpec       = (OP_SPEC − OP_RETIRED) / (CPU_CYCLES × SLOTS)
FrontendBound = STALL_SLOT_FRONTEND    / (CPU_CYCLES × SLOTS)

BackendBound  = STALL_SLOT_BACKEND     / (CPU_CYCLES × SLOTS)     ← 直接法
              = 1 − (Retiring + BadSpec + FrontendBound)          ← 残差法
```

**解析器第一屏就会打出这轮用的是哪种口径**，不用猜：

```
 后端口径   残差法（EV_STALL_SLOT_BE 留空）  BackendBound = 1 − (Retiring + BadSpec + FrontendBound)
            ⚠️ 残差法下「四象限求和 ≈ 1」是恒等式，**不是校验**；改跑 C1~C5
 交叉校验   EV_STALL_SLOT=0x003f → 跑 X 交叉校验
```

### 自检清单

| 自检 | 判据 | 直接法 | 残差法 | 它能抓什么 |
|---|---|---|---|---|
| **求和** | 四象限求和在 `1 ± 0.03` 内 | ✅ 跑 | **不跑**（恒等式） | SLOTS 偏大/偏小、任一事件号指错、复用。会**反推 SLOTS 应约为多少** |
| **C1** 残差非负 | `Retiring + BadSpec + FrontendBound ≤ 1` | 不跑¹ | ✅ 跑 | SLOTS **偏小**、事件号指错、复用。会**反推最小自洽 SLOTS** |
| **C2** 投机 ≥ 退休 | `OP_SPEC ≥ OP_RETIRED` | ✅ | ✅ | `EV_OP_SPEC` / `EV_OP_RETIRED` 写错或写反。**与 SLOTS 无关**（分母约掉了） |
| **C3** 退休率封顶 | `OP_RETIRED / CPU_CYCLES ≤ SLOTS` | ✅ | ✅ | SLOTS **偏小** 或 `EV_CPU_CYCLES` / `EV_OP_RETIRED` 指错。**不碰前端/后端事件** |
| **C4** 取值域 | 四个象限都在 `[0, 1]` | ✅ | ✅ | 任何让某一项出界的口径错误 |
| **C5** 复用 | 最低调度占比 `> 99.9%` | ✅ | ✅ | 计数器复用（事件开多了 / watchdog 占着 / 别人在抢） |
| **X** 交叉校验 | `\|BE_indep − BackendBound\| ≤ 0.03` | 填了 `EV_STALL_SLOT` 才跑 | 同左 | **唯一能抓「SLOTS 偏大」的一条**，也是残差法下唯一真正独立的校验 |

¹ 直接法下 `R+BS+FE > 1` 同样是错的，但那正是求和自检要抓的现象，不必由 C1 重复报一遍。

不等式类自检（C1 / C3 / C4）带 `0.005` 的越界容差 —— 硬件定义上它们严格成立，
这点容差只为容忍计数器起停那一丁点偏斜，不是放水（真出错时偏离量是几个百分点起步）。

### ⚠️ 残差法下查不出来的错误 —— 诚实清单

残差法把「求和」这条主安全网变成了恒等式，C1~C5 补回来的**不是全部**。下面这些，
**在残差法且没开 X 交叉校验时，解析器一个都抓不到，屏幕上仍然是一片 ✅**：

| 抓不到的错 | 为什么抓不到 | 怎么办 |
|---|---|---|
| **SLOTS 偏大**（真值 8 填了 16） | 分母放大只让残差里的 BackendBound 跟着变大，C1/C3/C4 反而更宽松 | **填 `EV_STALL_SLOT` 开 X**；或让 `SLOTS=` 留空让脚本读 `caps/slots` |
| `EV_STALL_SLOT_FE` 指到了一个**偏小**的事件 | 少掉的那部分被残差原封不动地算进 BackendBound，四项仍然自洽 | 填 `EV_STALL_SLOT` 开 X（X 用的就是 FE，对不上就红） |
| 后端本身的一切细节 | 残差法压根没测后端，BackendBound 是「剩下的都算它」 | 只能上直接法或 X |
| `EV_OP_SPEC` / `EV_OP_RETIRED` **同向**偏小 | 比例关系不变，C2/C3 都过，缺的量进了 BackendBound | 无（这类错只有求和/X 抓得到） |
| 采集窗口里混进了别的负载 | 任何口径都抓不到，PMU 只看 cgroup | 靠 `-G` 过滤本身，以及 §8 的覆盖范围说明 |

实测这个盲区长什么样（真值 SLOTS=8，配置里写成 16，残差法不开 X）：

```
  Retiring        20.00%       ← 真值应是 40.00%
  BackendBound    70.00%  ← 残差   ← 真值应是 40.00%
  ✅ 5 条自检都过，这组四象限可用。
     ⚠️ 但请记住：残差法 + 没有 X 交叉校验 = 「SLOTS 偏大」这类错查不出来。
```

同一组数把 `EV_STALL_SLOT=0x003f` 填上：

```
  ❌ X   交叉校验失败：独立算的 BackendBound = 0.2000，残差法算的 = 0.7000，差 0.5000 > 0.03
```

**所以：目标核只要实现了 `STALL_SLOT`（`0x003f`），就一定要填上它。**
这不是锦上添花，它是残差法下唯一能把「数看着正常其实全错」这件事翻出来的东西。

### 退出码

| 码 | 谁给的 | 什么意思 |
|---|---|---|
| `0` | — | 采集完成，自检全过 |
| `1` | `topdown_trial.sh` | 起不来：事件号格式不合法 / 必需事件留空 / 没 perf / 没 PMU / 读不到 SLOTS / 等不到容器 / cgroup 路径推不出来 |
| `1` | `topdown_parse.py` | 数没拿到：perf 输出空、缺事件、`<not supported>`、SLOTS 未知、计数全 0 |
| `2` | `topdown_parse.py` | **数拿到了但自检没过** —— 上面清单里任何一条 ❌。这组四象限不要用 |
| `130` / `143` | `topdown_trial.sh` | 收到 SIGINT / SIGTERM，已清理 |
| 其他 | `replay.py` | 重放本身失败，原样透出（重放退出码优先于解析退出码） |

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
- **干净的跨 trial 对比**。原因见紧接着的下一节。
- **cgroup v1 机器上 `replay.py` 的 per-command cgroup 指标**（`usage_usec` / `mem_peak`）。
  它只认 v2 的 `cpu.stat` / `memory.current`，v1 上要重写成
  `cpuacct.usage` / `memory.usage_in_bytes` / `blkio.*` 三套分散在不同层级的读法 ——
  不在这一版范围内。v1 上加 `--no-metrics` 绕过即可，**topdown 四象限和
  `patch_identical` 都不受影响**（详见 §0）。

### ⚠️ 跨 trial 比较的注意事项 —— 批量结果读之前先看这一节

`run_batch.py --topdown` 会给出一张按语言分组的横向小结。它有用，但**不是纯 workload
对比**，下面三件事必须一起带着看：

1. **这是整条 trial 的聚合值。** 采集窗口从主容器出现到 `replay.py` 退出，里面含
   容器启动、全部重放命令、以及收尾的 `git diff` 保真校验。没有任何一段被剔掉。
2. **里面有一笔固定开销。** 容器启动 + 收尾 `git diff` 的量级跟 trial 大小基本无关，
   是每条都要交的「入场费」。于是**trial 越轻，它在聚合值里占的比重越大** ——
   一条 23 条命令的 trial 和一条 439 条命令的 trial，四象限里「容器启动长什么样」
   的成分完全不是一个量级。
3. **各 trial 的命令数差异极大。** 全量 113 条里从 10 条到 439 条都有，相差 40 倍。
   所以两条 trial 的四象限之差，**既可能是 workload 真的不同，也可能只是一条更轻**。

怎么用：

- 横向小结里的差异，先回上面那张总表看这几条各有多少命令（`--per-lang` 的选取打印
  和 `SUMMARY.md` 的抽样行都带命令数）。命令数量级相近时，横向比才比较有意义。
- 想让 workload 的成分更突出，用 `--pick heaviest`（见 §5.1），代价是慢几倍。
- **不同 `--pick` 策略选出的批次之间不可直接比较** —— median 那批天生更轻。
  `summary.json` 的 `sampling.pick` 记着这轮用的是哪种。
- 真正干净的对比得等 per-command 归因那一版，那时才能把容器启动和收尾单独摘出来。

另外，`patch_identical` 与 topdown 自检是**正交**的两件事（见 §5.1 最后一节）：
某条 topdown 采废不代表这条 trial 的重放结论不可信，反过来也一样。

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
| `perf stderr` 里是 `must define events before cgroups` | **参数顺序**问题：`-G` 排到了 `-e` 前面 | 脚本里已经是对的顺序（`-e … -G …`）。看到这条说明有人把顺序改回去了，或者你在手敲 perf 命令。详见 §5 步骤 5 |
| `replay.py` 报 `sinkhole cgroup 初始化失败`，整轮起不来 | **多半是这台机器是 cgroup v1**（`stat -fc %T /sys/fs/cgroup` = `tmpfs`）—— `replay.py` 那套指标只认 v2 的 `cpu.stat` / `memory.current` | **加 `--no-metrics`**：`bash topdown_trial.sh <trial> --no-metrics`。它只关掉 `usage_usec` / `mem_peak` 那几列，**对 topdown 结果和 `patch_identical` 都没有任何影响** —— PMU 是宿主侧 `perf -G` 采的，两条路互不相干。v1 下的 per-command cgroup 指标是这一版的**已知限制**，见 §0 |
| perf 报 `no access to cgroup /sys/fs/cgroup/perf_event/…` | 同上：这台机器是 **cgroup v1**，而路径推成了别的控制器目录（老版本脚本的 `find` 兜底会命中 `blkio/…`） | 本版已改成读 `/proc/<pid>/cgroup` 直接取 perf_event 那一行。仍然报的话看脚本打印的 `/proc/<pid>/cgroup` 原文：没有 `perf_event` 那一行 = 内核没挂载 perf_event 控制器（`ls /sys/fs/cgroup/` 确认） |
| 某个事件是 `<not supported>` | **这个事件号在这颗核上无效** | 号写错了，或这颗核不实现它。逐个对照**目标核 TRM** 的 PMU 事件表改 `topdown.conf` —— 组里只要有一个不对，整组 `{}` 都开不起来。**如果不支持的正好是 `STALL_SLOT_BACKEND`，把 `EV_STALL_SLOT_BE` 清空改走残差法**（见 §3） |
| `❌ EV_xxx=11 —— 事件号必须写成 0x 开头的十六进制` | 漏写 `0x` 前缀 | perf 会把 `11` 按**十进制**读成 `0xb`，数到别的事件还不报错。改成 `0x11` 并按 TRM 核对 |
| `/sys/bus/event_source/devices/` 下没有 `armv8*` | 内核没注册 ARM PMU 驱动 | baremetal 上一般是 DT/ACPI 没描述 PMU 中断（固件/DTB），或内核没编 `CONFIG_ARM_PMUV3`。`sudo dmesg \| grep -iE 'pmu\|perfevents'` |
| 调度占比 < 100%（复用） | ① watchdog 占着一个计数器 ② `EV_EXTRA` 开多了 ③ 有别的 perf 会话在抢 | `cat /proc/sys/kernel/nmi_watchdog` 非 0 → `sudo sysctl kernel.nmi_watchdog=0`（**采完改回 1**）；清空/减少 `EV_EXTRA` 分两轮采；`pgrep -a '^perf'` 看有没有别人在跑。余量账看 `probe_pmu.sh` 第 3 节 |
| 四象限求和 ≠ 1（**只有直接法会报这条**） | ① SLOTS 不对（最常见）② 某个事件号指错了 ③ 复用 | 先看 C5 排除 ③；再看解析器「反推 SLOTS 应约为 N」那一行，对照 TRM；都对就逐个事件号核 TRM |
| `❌ C1 残差非负失败`（残差法） | 同上三种 | 看它反推出来的「最小自洽 SLOTS」：**接近某个整数**（8 = V1，5 = N2）多半是 SLOTS 取错；离整数很远更可能是事件号指错或复用 |
| `❌ C2 OP_SPEC < OP_RETIRED` | `EV_OP_SPEC` / `EV_OP_RETIRED` 写错或写反 | 这条和 SLOTS 无关（分母约掉了），所以它红 = 事件号问题，别去动 SLOTS |
| `❌ C3 OP_RETIRED/CPU_CYCLES > SLOTS` | SLOTS 偏小，或 `EV_CPU_CYCLES` / `EV_OP_RETIRED` 指错 | 它只用到这三个量，不碰前端/后端事件，可以据此缩小范围 |
| `❌ X 交叉校验失败` | ① SLOTS 不对（**含偏大**）② `EV_STALL_SLOT` / `EV_STALL_SLOT_FE` 指错 ③ 复用 | 残差法下这是判别力最强的一条，红了就别用这组数 |
| 残差法下一切 ✅ 但数看着不对 | **可能真的抓不到** —— 见 §6「诚实清单」 | 填 `EV_STALL_SLOT=0x003f` 开 X 交叉校验再采一次 |
| 计数全是 0，但不是 `<not supported>` | cgroup 路径推错，`-G` 滤空了 | **`perf -G` 路径不对时不报错，只给 0**。核对脚本打印的 cgroup 相对路径是不是那个容器；docker 的 cgroup driver 是 systemd 还是 cgroupfs 决定落点（`docker info -f '{{.CgroupDriver}}'`），rootless 则落在 `user.slice` 下 |
| 「60s 内没等到重放容器」 | 镜像不在本地 / 存量容器占着名字 | 看脚本打出来的 `replay.log` 尾部。`bash build_arm.sh returns-validated` 建镜像；`docker ps -a --filter name=^replay_` 清存量 |
| 采到的数偏小，而且容器明明在跑 | 采到 `-sink` 上去了 | sidecar 是独立 cgroup。脚本已经排掉 `-sink`，如果是手工敲的 perf 命令，检查用的是不是主容器的 ID |
| `perf` 命令在，但一跑就报 `perf not found for kernel ...` | Debian/Ubuntu 的 `/usr/bin/perf` 是个按 `uname -r` 找真身的 **wrapper 脚本** | 装 `linux-tools-$(uname -r)`；云镜像上这个具体版本常不在源里，退一步装 `linux-tools-generic` |
| 采集跑到一半卡住不动 | `sudo` 在等密码 | 先 `sudo -v`。串行批次跑几小时，这一条在批量下尤其要命 |
| `run_batch.py` 报「`--topdown` 与 `--jobs N` 互斥，拒绝启动」 | 并发下多个 `perf stat -a` 抢同一批物理计数器 | 去掉 `--jobs`（或写 `--jobs 1`）。这条**不能绕**，绕过去采到的是一批 C5 全红的废数据。要快就用 `--per-lang N` 少跑几条，见 §5.1 |
| `run_batch.py --topdown` 报「需要 topdown_trial.sh，没找到」 | 解开的是**主重放包**（`deepswe-replay-bundle-*`），它不带 topdown 那一套 | 用 topdown 包：`kind = topdown-full` 的那个（`cat BUILD_INFO`） |
| 批量跑完，某几条的 topdown 列是 `—` | 那几条采废了（perf 没起来 / 等不到容器 / 解析没数） | 看 `SUMMARY.md` 逐条那一节里该条的「不可用」原因和三个退出码的分解，再看 `<trial>/topdown/perf.stderr`。**这不影响该条的 `patch_identical`**，两者正交 |
| 批量跑完，几乎每条都 `❌C5` | 计数器复用 —— 多半是同时还有别的 perf 会话，或 watchdog 占着 | `pgrep -a '^perf'` 看有没有别人在跑；`cat /proc/sys/kernel/nmi_watchdog` 非 0 就 `sudo sysctl kernel.nmi_watchdog=0`（**采完改回 1**）。确认没并发跑 `run_batch.py` 的第二个实例 |
| 异构核（多个 `armv8*` PMU） | 事件只在一簇核上打开，进程跑到别的簇就采不到 | 用 `docker --cpuset-cpus` 把容器钉在一簇上；或每个 PMU 各采一轮自己合并（本版不支持） |

---

## 10. 文件清单

| 文件 | 干什么 |
|---|---|
| `TOPDOWN.md` | 本文件，入口 |
| `topdown.conf` | **唯一需要你改的文件**：PMU / SLOTS / 事件号（含后端口径开关 `EV_STALL_SLOT_BE` 与可选的 `EV_STALL_SLOT`）/ EV_EXTRA / 输出格式 |
| `probe_pmu.sh` | 目标机第一件事：验事件号有效性 + 计数器余量 + 活体验证 `-G` |
| `topdown_trial.sh` | 采集主脚本（单条）。落 `topdown.json` 与 `run_status.json` |
| `run_batch.py` | **批量入口**（只在 `kind = topdown-full` 的包里）：`--topdown` 逐条调 `topdown_trial.sh`，汇总四象限。`--per-lang N` / `--pick` 抽样。**perf 逻辑不在这里，全在 `topdown_trial.sh`** |
| `topdown_parse.py` | 解析 perf 输出 → 四象限 + 自检（直接法：求和 + C2~C5；残差法：C1~C5；填了 `EV_STALL_SLOT` 再加 X）+ `topdown.json`（只用标准库） |
| `replay.py` | 重放引擎（与主包同一份） |
| `build_arm.sh` | 从 `mars-base` 重建 task 镜像 |
| `check_sources.sh` | 构建期上游源连通性探测 |
| `preflight.sh` | 重放环境预检（cgroup / docker / netns 活体测试） |
| `get_ca_cert.sh` / `detect_mitm.sh` | 内网 TLS 中间人：检测 + 取 CA |
| `<trial名>/` | trial 目录。单条包只有 1 条且 `replay/` 下带 x86 基线对照；全量包 113 条、多数没有基线（那 113 条从没在开发机上重放过，属预期） |
| `BUILD_INFO` / `SHA256SUMS` | 这份包的来源与全量指纹 |
