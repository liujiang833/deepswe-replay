# 服务器重放操作手册

把 5 条已验证的 trial 在服务器上重跑一遍，用 `patch_identical` 确认环境等价。

## 0. 一分钟版

```bash
tar xzf deepswe-replay-bundle-*.tar.gz && cd deepswe-replay-bundle
bash preflight.sh                  # 环境预检，有 ❌ 先解决
python3 run_batch.py --smoke 5     # 冒烟：每条只跑前 5 条命令，约 1 分钟
python3 run_batch.py               # 正式跑，约 15 分钟
```

结果落在 `runs/<UTC 时间戳>/`：`SUMMARY.md`（人看）、`summary.json`（机器读）、
`logs/*.log`（逐条实时输出）、`<trial>/{verdict.json,commands.jsonl,replayed.patch}`。

**唯一的通过标准是 5 条全部 `patch_identical=true`。** 其余数字都是参考。

---

## 1. 这个 bundle 里有什么

```
deepswe-replay-bundle/
├── replay.py          单条重放器（只用 python 标准库）
├── run_batch.py       批量 driver：串行跑全部、与基线对比、出汇总
├── preflight.sh       环境预检（真起容器验证每项能力）
├── RUNBOOK.md         本文件
├── INDEX.md           上一轮跨语言验证的完整分析（含 rc 不匹配逐条归因）
├── SHA256SUMS         传输完整性校验
└── <trial>/           5 条，每条含：
    ├── meta.json          语言 / 模型 / 镜像 / base_commit / 资源规格
    ├── trajectory.json    原始 trace（命令逐字记录）
    ├── model.patch        agent 最终提交的 patch —— 保真度比对基准
    ├── task.json          任务定义（replay.py 从里面读 task.toml）
    └── replay/verdict.json  上一台机器的基线判定，供跨机对比
```

解包后先核对完整性：`sha256sum -c SHA256SUMS`

> **bundle 必须在开发机上打好再拷过去，不能在服务器上 clone 仓库重打。**
> `trajectory.json` 与 `model.patch` 体量大且可复现，被 `.gitignore` 排除在版本库之外
> （见仓库 `.gitignore` 的 `deepswe/crosslang/*/trajectory.json` 等规则），
> 新 clone 出来的仓库里没有这两个文件。真丢了可以用
> `deepswe/fetch_trial_artifacts.py <trial_name>` 按 `release.json` 里的 URL 模板从
> CloudFront 重下（公开可取、无鉴权）。

## 2. 前置条件

| 项 | 要求 | 不满足会怎样 |
|---|---|---|
| docker | 能起容器，当前用户有权限 | 直接跑不了 |
| **cgroup v2** | `stat -fc %T /sys/fs/cgroup` = `cgroup2fs` | 性能指标采不到（`replay.py` 明确报错退出，不会静默写 0） |
| python3 | ≥3.8，标准库即可 | 跑不了 |
| 5 个镜像 | 已 `docker pull` 到本地 | `replay.py` 拒绝启动（默认不允许现拉，见下） |
| 磁盘 | ≥20 GB | 重放中途写满 |

镜像**必须预先拉好**。`replay.py` 默认拒绝在镜像缺失时启动，因为实测出口吞吐只有
**0.27 MB/s**，误触一个 ~800 MB 的镜像就是几十分钟。`preflight.sh` 和
`run_batch.py --dry-run` 都会把缺失的镜像连同 `docker pull` 命令一起列出来。

## 2b. 拉不到 registry 时(ECR / DockerHub 不通)

重放执行本身**零网络**,所以 registry 不通不影响跑,只影响"怎么把镜像弄到本地"。
三条路:

| 路 | 前提 | 代价 |
|---|---|---|
| A. `docker pull` | 能连 ECR | — |
| B. `docker save` \| zstd → 搬文件 → `docker load` | 能物理搬文件 | 5 个镜像共享基座,一次性打包约 690 MB(zstd);**分 5 次打会重复传 4 遍基座,涨到约 2.4 GB** |
| C. 从本地 mars-base 重建 | 能连各包源(npm/pypi/goproxy/crates) | 见 `build_arm.sh` |

### 路 C:重建(`check_sources.sh` + `build_arm.sh`)

```bash
bash check_sources.sh          # 先探源:几秒,决定哪几条建得成
bash build_arm.sh --list       # 看会做什么改写,不构建
bash build_arm.sh python       # 从依赖最少的开始
bash build_arm.sh all          # 全建(自动按 python→go→js→ts→rust 排序)
```

可行的前提(都已核实):`task.json` 里就带着 `environment/Dockerfile`;113 个 Dockerfile
**零 COPY / 零 ADD**,构建上下文可以是空目录;建完打上 `task.toml` 里原本的
`docker_image` tag,所以 `replay.py` 零改动。

各条需要的源:

| task | 需要 |
|---|---|
| python (returns) | github + pypi ← 依赖最少,且**唯一不需要装报告器**的 |
| go (actionlint) | github + proxy.golang.org + sum.golang.org |
| js (yjs) | github + npmjs(`npm ci`,锁定) |
| ts (true-myth) | github + npmjs(`pnpm install`,**未锁定**) |
| rust (fd) | github + crates.io + get.nexte.st + npmjs ← 最难,最后建 |

**ARM 上的一处硬改写**:`fd` 的 Dockerfile 写死 `get.nexte.st/${VER}/linux`,
那是 x86_64 产物。`build_arm.sh` 在基座是 arm64 时自动改成 `/linux-arm`。
(实测确认:`/linux` 8.2 MB、`/linux-arm` 6.7 MB 都存在,`/linux-arm64` 是 404。)

### ⚠️ 路 C 的保真度代价

**重建镜像 ≠ 原 amd64 镜像**,即使改写为零:

- 依赖版本会漂移:`pnpm install` 未加 `--frozen-lockfile`、`pip install` 未钉版本、
  `npm install -g` 只钉直接依赖。(例外:`cargo fetch --locked` 和 `npm ci` 是锁定的)
- 换架构后工具链、native 扩展、编译产物全部不同
- 基座本身是 `:latest`,不可复现

→ **`patch_identical` 在重建镜像上是待验证的开放问题**,不能因为它在原 amd64 镜像上
5/5 通过就假定这里也成立。它失败时,先分清是重放流程坏了,还是镜像本身就不一样——
`build/<lang>/REWRITES.md` 记着每条改写,是排查的起点。

## 3. 预检

```bash
bash preflight.sh
```

它不只查版本号，而是**真起一个容器**把 `replay.py` 依赖的每项能力跑一遍：

- cgroup v2 挂载、cpu/memory/io 控制器是否启用
- 容器 cgroup 目录能否定位（三级探测，见 §6.1）并读出 `cpu.stat`
- `--cpus=2 --memory=8192m` 是否真的生效（不生效跨机数字不可比）
- 镜像内有没有 `timeout` / `python3` / `git` / `sh`，`/app` 是不是 git 仓库
- `--network=container:` 共享 netns 能否用（403 sinkhole 的实现基础）
- `--network=none` 是否真的断网

有 ❌ 就先解决。⚠️ 可以跑，但要确认不影响你要的结论。

## 4. 跑

```bash
python3 run_batch.py --smoke 5      # 冒烟：确认容器能起、cgroup 能读、命令能跑
python3 run_batch.py                # 正式
python3 run_batch.py --only go,rust # 只跑指定语言
python3 run_batch.py --dry-run      # 只预检和排程
```

**串行是刻意的**：`replay.py` 采的是 cgroup 的 CPU/内存/IO，两条同时跑会互相争抢，
性能数字直接失去可比性。上一轮就因为中途并发，rust/ts/js 三条指标偏悲观、
只有 python/go 两条干净（`INDEX.md`「本轮的口径污染」）。

**冒烟模式不校验保真度**：只跑前 N 条命令，patch 天然不完整，`--smoke` 下汇总表的
「保真」列显示 `—(冒烟)`，不代表通过。

跑起来大约需要（基线机器，2 核限额下）：

| 语言 | 命令数 | 基线耗时 |
|---|---:|---:|
| python | 98 | 222.6s |
| go | 69 | 104.0s |
| rust | 76 | 295.6s |
| typescript | 59 | 68.6s |
| javascript | 65 | 171.3s |
| **合计** | **367** | **≈862s（14.4 分钟）** |

## 5. 怎么看结果

### 5.1 唯一的硬标准：`patch_identical`

容器内 `git diff --binary <base_commit> HEAD` 与随包的 `model.patch` **逐字节相等**。

5 条应当全为 `true`。**有一条 false 就说明环境与原始运行不等价，性能数字全部作废**，
先查那一条的 `logs/<trial>.log`。

### 5.2 允许有出入的：`rc_match`

退出码序列与 trace 的逐条比对。基线本身就不是满分，`INDEX.md`「rc 不匹配逐条归因」
把 10 条不匹配逐个查过，**没有一条是重放机制的缺陷**，来源有四类且不可消除：

| 类别 | 说明 | 换机器会不会变 |
|---|---|---|
| 双侧都超时 | trace 记 -1、重放记 124，行为一致 | 不变（`rc_match_semantic` 已算作匹配） |
| 单侧超时 | 命令本身贴着 30s 边界，两台机器分属两侧 | **会变**，机器越快这类越少 |
| 上游 flaky 测试 | go 那条是 Go map 迭代顺序随机（agent 自己写进 model.patch 的测试） | **每次都可能变** |
| dash 方言 | rust 有 2 条含 `time (...)`，dash 下就该失败 | 不变 |

所以 `rc_match` 比基线高或低几条都正常，`run_batch.py` 只报差异（`vs基线` 列的
`rc±N`），不判失败。

### 5.3 随包基线的口径

`<trial>/replay/verdict.json` 是**最终口径**：执行器 `/bin/sh -c`（dash）、
网络 `sinkhole403`。

⚠️ **`INDEX.md` 汇总表里的 rc 数字是修正前那一版**（`bash -lc` + `--network=none`），
与随包 `verdict.json` 对不上是正常的——INDEX.md 正文的「两处保真度修正」讲的就是这件事。
**跨机对比一律以 `verdict.json` 为准**，`run_batch.py` 读的也是它：

| 语言 | verdict.json（随包基线，新口径） | INDEX.md 表（旧口径） |
|---|---|---|
| python | 95/98（语义 97/98） | 95/98 |
| go | 66/69 | 67/69 |
| rust | 74/76（语义 75/76） | 72/76 |
| typescript | 59/59 | 58/59 |
| javascript | 65/65 | 65/65 |

### 5.4 性能数字

`elapsed_s` / `commands.jsonl` 里的 `usage_usec` / `mem_peak`。跨机比较前先确认：
两边都是 2 核限额、都串行、cgroup 口径相同（`verdict.json` 的 `host` 字段记了内核、
cgroup 目录与探测方式、CPU 数）。

## 6. 故障排查

### 6.1 `找不到容器 XXX 的 cgroup 目录`

cgroup 目录的位置**取决于 docker 的 cgroup driver 和是否 rootless**，各不相同。
`replay.py` 会三级探测并把试过的路径全部打出来：

1. `/proc/<容器 pid>/cgroup` —— 内核自己报告的，最准；但 dockerd 在别的 pid namespace
   时不可用（WSL2 / Docker Desktop 就是这种）
2. 已知 driver 候选：`/sys/fs/cgroup/docker/<id>`（cgroupfs）、
   `system.slice/docker-<id>.scope`（systemd）、rootless 的 `user.slice/...` 两种
3. 在 cgroup 树里按容器 ID 搜（自定义 `cgroup-parent` 时只剩这条）

三级全失败时报错会告诉你 `/sys/fs/cgroup` 的实际类型。若不是 `cgroup2fs`，
本流程的指标口径（`cpu.stat`/`memory.current`）不适用 cgroup v1，需要换机器或切 v2。

### 6.2 `镜像不在本地`

先 `docker pull`（报错信息里有现成命令）。确实想现拉就加 `--allow-pull`，
但注意 0.27 MB/s 的实测吞吐。

### 6.3 `发现同 trial 的存量重放容器`

有另一个重放进程在跑，或上次异常退出留了残骸。**这是刻意拦下来的**：旧版本容器名只按
trial 名推导，两个进程重放同一条会静默互删容器，先启动的那个从此每条命令都失败
（`INDEX.md`「已知风险」）。现在容器名带 PID，并在启动前检查同前缀存量。

确认无人在用后清理：
```bash
docker rm -f $(docker ps -aq --filter name=^replay_)
```

### 6.4 sinkhole 起不来

`replay.py` 会直接退出而不是静默降级。真跑不起来时用 `--net-mode=none` 退回旧口径，
但要知道代价：原 harness 的 `allow_internet=false` 是**代理立即回 403**，
`--network=none` 是**连不上挂着等**。对 npm/npx 这类带重试退避的工具，后者会把 30s
预算耗光，把秒级命令变成超时（ts 那条基线里就有实例）。

### 6.5 某条卡住不动

`logs/<trial>.log` 是实时写的，`tail -f` 看。单条命令最长 30s（容器内 `timeout -k 5 30`），
宿主侧还有 90s 兜底，正常不会真卡死。rust 那条基线里 agent 自己有大量 `sleep 25~29`
在等后台编译，看着像卡住但是正常的。

## 7. 扩到全量 113 条

本 bundle 只带了 5 条。扩量时：

- **镜像是主要成本**：113 个镜像完整下载 111 GB、层去重后 24.3 GB，按 0.27 MB/s
  实测吞吐串行拉取约 **25 小时**。提前预拉或换更快的出口。
- **重放本身**：5 条 367 条命令 862s，平均 2.63 s/cmd；113 条按每条 60~100 命令估，
  串行约 **5~7 小时**。
- 输入数据在 `deepswe/data/{trajectories,tasks}/`（被 gitignore，需另行同步）。
  `run_batch.py` 认的是「目录里有 meta.json + trajectory.json + model.patch + task.json」，
  按同样布局摆好即可复用。
- 时间预算的主导项不是语言，而是 **trace 里有没有 `sleep` 轮询模式**——
  rust 那条墙钟最长但 CPU 只有 0.30 核，容器大部分时间在空转。
