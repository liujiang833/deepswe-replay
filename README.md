# deepswe-replay

按 agent 的原始 trace 在容器里逐条重放命令，用
**「容器内 `git diff` 出来的 patch 与 agent 当初提交的 `model.patch` 逐字节相同」**
这一条硬标准，确认一台服务器的环境与原 harness 等价；并在重放的同时，
在宿主侧按 cgroup 过滤采 ARM PMU 事件，算出 L1 topdown 四象限。

全量 113 条 trial（go 35 / python 34 / typescript 34 / rust 5 / javascript 5，
合计 4519 条命令）的 trace 与 patch **都在仓库里**，clone 完即可开跑。

---

## 我该看哪份文档

| 想干什么 | 看这个 |
|---|---|
| 只做重放，确认环境等价 | [`deepswe/crosslang/README.md`](deepswe/crosslang/README.md) |
| 采 ARM topdown | [`deepswe/crosslang/TOPDOWN.md`](deepswe/crosslang/TOPDOWN.md) |
| 出了问题查成因与绕法 | [`deepswe/crosslang/RUNBOOK.md`](deepswe/crosslang/RUNBOOK.md) |
| 这个项目一路是怎么来的 | [`deepswe/PROJECT_HISTORY.md`](deepswe/PROJECT_HISTORY.md) |

---

## 最短路径（ARM 服务器上采 topdown）

```bash
git clone https://github.com/liujiang833/deepswe-replay.git
cd deepswe-replay/deepswe/crosslang
```

### 0. 前置条件

| 必须有 | 说明 |
|---|---|
| `docker` | 能起容器、能 build |
| `python3` | 3.8+，**只用标准库**，无需 pip install |
| **`mars-base` 基座镜像** | 所有 113 个 Dockerfile 都 `FROM mars-base`。基座不在，一条都建不了 |
| `perf` | 采 topdown 才需要；且**必须能跑**（Debian/Ubuntu 的 `/usr/bin/perf` 是按内核版本找真身的 wrapper，包没装时它照样在） |

### 1. 填事件号

```bash
cp topdown.conf.example topdown.conf 2>/dev/null || true   # 没有 example 就直接改 topdown.conf
vi topdown.conf
```

只需要改事件号那几行，**`PMU` 和 `SLOTS` 留空**让脚本运行时现读：

```bash
PMU=                      # 留空 = 自动探测 armv8*
SLOTS=                    # 留空 = 从 caps/slots 现读（强烈建议留空）
EV_CPU_CYCLES=0x0011      # 以下按目标核 TRM 核对，必须带 0x 前缀
EV_OP_RETIRED=0x003a
EV_OP_SPEC=0x003b
EV_STALL_SLOT_FE=0x003d
EV_STALL_SLOT_BE=         # 核没实现独立 backend 计数器就留空 → 走残差法
EV_STALL_SLOT=0x003f      # 实现了就填 —— 残差法下唯一能抓住「SLOTS 错了」的校验
```

> ⚠️ 事件号**必须带 `0x`**。写 `11` 会被 perf 当十进制读成 `0xb`，数的是另一个事件，
> 而且不报错。脚本现在会拒绝没有 `0x` 前缀的值。

### 2. 探针（几秒，别跳）

```bash
bash probe_pmu.sh
```

验的是事件号在这颗核上有没有效、通用计数器余量够不够（NMI watchdog 可能占一个）、
cgroup 路径推导对不对。**`perf -G` 的路径推错时不会报错，只会给你一串 0** ——
这是整套里最隐蔽的失败模式，所以这一步是活体验证（真起容器、真烧 CPU、真采一次）。

### 3. 链路自检（~15 秒）

```bash
bash topdown_selftest.sh
```

不跑重放，只用一个烧 CPU 的临时容器把
`SLOTS 解析 → 事件组拼装 → cgroup 推导 → perf stat -a -G → topdown_parse.py`
这条链路走一遍。被测的是纯整数死循环，**四象限可预判**（应为高 Retiring、
低 FrontendBound），于是「数字对不对」变成了一个能判断的问题。

不确定 SLOTS 该是几就换着试，不用改配置：

```bash
bash topdown_selftest.sh --slots 8
bash topdown_selftest.sh --slots 5
```

### 4. 建镜像（最耗时）

```bash
bash check_sources.sh                       # 先探各上游通不通、基座在不在
bash build_arm.sh --trials-dir full_trials python    # 按语言建
bash build_arm.sh --trials-dir full_trials koota     # 或按 trial 名前缀
```

内网做了 TLS 中间人的话：`bash detect_mitm.sh` → `bash get_ca_cert.sh` →
`build_arm.sh --ca-cert corp-ca.crt …`。**rust 那几条必须走 `--ca-cert`**，
cargo 没有 insecure 开关。

全量 113 条预算 8~12 小时 / 25~35 GB。不必等全建完，下一步的 `--skip-missing`
会自动跳过没建好的，边建边跑。

### 5. 采集

```bash
sudo -v
tmux new -s topdown                          # 全量要几小时，ssh 断了就白跑

sudo -E python3 run_batch.py --trials-dir full_trials --topdown \
     --skip-missing --no-metrics --keep-going
```

先加 `--dry-run` 看一眼待跑几条、资源账对不对。

想先快速拿一版横向数据，不跑满：

```bash
... --per-lang 2                 # 每种语言取 2 条（默认按命令数取中位，约 18 分钟）
... --per-lang 2 --pick heaviest # 取最重的，信噪比更好但慢 3.8 倍
```

#### 每个 flag 为什么必须

| flag | 理由 |
|---|---|
| `--trials-dir full_trials` | 全量 113 条在这个子目录；不给的话只扫 `crosslang/` 根下那几条 |
| `--topdown` | 不加就只是普通重放，不采 PMU |
| `--skip-missing` | 没建镜像的自动跳过，而不是整批拒绝启动 |
| `--no-metrics` | **cgroup v1 的机器上必须加** —— `replay.py` 的指标代码只认 v2，不加起不来 |
| `--keep-going` | 默认遇错即停。几小时的批次里一条失败就全停 |
| `sudo -E` | `perf stat -a` 要 root。`sudo` 凭据 15 分钟过期，长批次跑到一半会静默失败 |

**不要加 `-j` / `--jobs`** —— 会直接拒绝启动。多个 `perf stat -a` 竞争同一批物理
计数器，复用之后每条的「无复用」自检全失败，整批数据作废。

### 6. 读结果

结果落在 `runs/<UTC 时间戳>/`：

| 文件 | 干什么用 |
|---|---|
| `SUMMARY.md` | 人看的汇总。原有总表后面追加 `Retiring / BadSpec / FE / BE / 校验` 五列，另有按语言分组的横向小结 |
| `summary.json` | 机器读的汇总 |
| `<trial>/verdict.json` | 单条重放判定 |
| `<trial>/topdown/topdown.json` | 单条 topdown 结果 + 逐条自检 |
| `logs/<trial>.log` | 逐条实时输出，跑的过程中就能 `tail -f` |

**重放的唯一硬标准是 `patch_identical=true`。** topdown 的自检是**另一件事** ——
它判的是这组 PMU 数可不可信，和环境等价与否正交。某条 topdown 采废了不影响
这条 trial 的重放结论。

退出码：`0` 成功 / `1` 环境或参数问题 / **`2` 数采到了但不可信**（自检没过）。

---

## 两个必须先知道的坑

**1. PMU 数字跨架构不可比。** ARM 上采到的 cycles / IPC / 四象限，和原 amd64
harness 的运行**不构成任何对照关系** —— 事件语义、流水线、cache 层级全不一样。
PMU 只能同机纵向用（找热点、比不同 build）。判定「这台机器与原 harness 等价」的，
仍然只有 `patch_identical`。

**2. 这一版是整条 trial 的聚合值。** 覆盖容器启动 + 全部重放命令 + 收尾 `git diff`，
而各 trial 的命令数从 10 条到 439 条不等，固定开销占比因此差很多。跨 trial 横向比
要带上这个背景，不是纯 workload 对比。per-command 归因是后续工作。

---

## 出问题去哪查

| 症状 | 去处 |
|---|---|
| `must define events before cgroups` | perf 的 `-G` 必须排在 `-e` **之后** |
| `no access to cgroup /sys/fs/cgroup/perf_event/…` | 机器是 cgroup v1，路径要走 perf_event 独立层级（脚本已处理，见 `TOPDOWN.md`） |
| `sinkhole cgroup 初始化失败` | 同样是 cgroup v1 —— 加 `--no-metrics`，不影响 topdown |
| 四象限全是 `—`、没有 `topdown.json` | 采集没跑到解析这一步。看 `<trial>/topdown/perf.stderr` 和日志里的 `perf 退出码` |
| 「目录里没有合规的 trial」 | 忘了 `--trials-dir full_trials` |
| 其余 | `RUNBOOK.md`（重放）/ `TOPDOWN.md` §排查表（采集） |
