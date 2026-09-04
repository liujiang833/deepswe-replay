# 非 Docker 执行重放：环境需求分析

**样例**：`gql-incremental-graphql-delivery__nnFNKRL`（python / gql / 438 条重放命令）
**镜像**：`public.ecr.aws/d3j8x8q7/swe-bench-202605:kh79vjbp8dv1pyk7t09zdb9xx9821628-v1.1`
（linux/amd64 单一 manifest，27 层 / 840 MB 压缩 / 2.65 GB 解压，Debian 12 + Python 3.12.12）

## 0. 结论先行

非 Docker 可行，但**不是"少了个守护进程"这么简单**。Docker 在这条链路里提供 9 项彼此独立的能力，
其中 **3 项是硬约束**，由本项目已验证的事实推出，不能靠"随便找个沙箱"绕过：

1. **PID 命名空间必须长驻，且 init 要能收养孤儿**——因为 mini-swe-agent 超时不杀容器内进程
   （见 EXEC_LOG Step 7），孤儿必须活着继续烧 CPU，否则重放的负载系统性偏低。
2. **`/tmp` 必须跨命令持久**——49 个 /tmp 路径、59 次跨 step 引用。
   每条命令新建 namespace + `--tmpfs /tmp` 的方案会直接破坏状态链，patch 硬校验必挂。
3. **cgroup 必须可写（限流）且可读（计量），并能归属到单条命令**——这是整个测量的地基。

## 1. Docker 提供的 9 项能力逐项拆解

| # | 能力 | Docker 怎么给的 | 非 Docker 需要什么 | 硬性 | 本机现状 |
|---|---|---|---|---|---|
| 1 | rootfs 组装 | 27 层 overlayfs 联合挂载 | 按序解包 27 层并处理 whiteout（`.wh.*` / opaque dir）；或自己挂 overlayfs | **硬** | overlay 在 `/proc/filesystems` |
| 2 | 挂载命名空间 | 自动 | `unshare -m`（rootless 需配 userns）或 root + `chroot` | **硬** | userns 可用（max=125518），无 AppArmor 限制 |
| 3 | PID 命名空间 + 孤儿收养 | PID 1 = `sleep infinity` | 长驻 `unshare --pid --fork` + 一个不退出的 init | **硬（见 §0.1）** | `unshare` 已装 |
| 4 | 网络隔离 | `--network=none` | `unshare -n`（空 netns） | **硬** | userns 内可建 netns |
| 5 | 资源限制 | `--cpus=2 --memory=8g --memory-swap=8g` | cgroup v2 写 `cpu.max` / `memory.max` / `memory.swap.max` | **硬** | user@1000.service 已下放 `cpu memory pids` |
| 6 | cgroup 计量 | `system.slice/docker-<id>.scope/` | 自建 cgroup 目录，读 `cpu.stat` / `io.stat` / `memory.current` | **硬** | 同上，rootless 可写 |
| 7 | 长驻会话 + 逐条进入 | `docker exec` | `nsenter --target <init pid>` 或 supervisor 从 FIFO 读命令 | **硬** | `nsenter` 随 util-linux |
| 8 | uid 0 身份 | 容器内 root | userns `--map-root-user`（uid 0 映射到宿主 uid）；多 uid 需 `/etc/subuid` + `newuidmap` | 中 | 单 uid 映射够用（trace 无 chown/useradd） |
| 9 | 可重置性 | `docker rm -f` + 新 `run` | overlayfs upper dir 清空，或删展平目录 | 中 | — |

## 2. 三档方案与各自的环境门槛

### 档 A：有 root（最省事）
```
解包 rootfs → unshare --mount --pid --net --fork --propagation private chroot <rootfs> /init
cgroup 直接写 /sys/fs/cgroup/<自建>/{cpu.max,memory.max}
逐条命令 nsenter --target <init pid> -m -p -n -- bash -lc "$cmd"
```
- **环境需求**：root / CAP_SYS_ADMIN；cgroup v2 unified；util-linux（unshare/nsenter）；tar
- **不需要**：userns、subuid、systemd、任何容器运行时
- 这是最少依赖的路径

### 档 B：rootless（本机可走）
```
systemd-run --user --scope -p CPUQuota=200% -p MemoryMax=8G -p MemorySwapMax=0 -- \
    unshare --user --map-root-user --mount --pid --net --fork /init
```
- **环境需求**（比档 A 多）：
  - `max_user_namespaces > 0`（本机 125518 ✅）
  - 无 AppArmor userns 限制（Ubuntu 24.04 默认开 `kernel.apparmor_restrict_unprivileged_userns=1`，
    **本机 WSL2 内核无此项 ✅**，但真机 Ubuntu 24.04 上是个坑）
  - **systemd 用户会话已下放 `cpu` 与 `memory` controller**——这是最容易缺的一条。
    很多发行版默认只下放 `memory pids`，`cpu` 需要在 `user@.service` 里配 `Delegate=cpu`。
    本机实测 `user@1000.service/cgroup.controllers` = `cpu memory pids` ✅
  - rootless overlayfs 需内核 ≥ 5.11 且在 userns 内；否则退化为展平目录（见 §3 磁盘代价）
- **不需要**：root

### 档 C：完全裸跑（不推荐，但成本最低）
在宿主上建 venv，把 gql 仓库 clone 到 `/app`，装 101 个包，直接跑命令。
- **放弃的东西**：网络隔离（trace 里 cmd #80 的 `pip install --dry-run` 失败**是负载的一部分**，
  裸跑会真的联网，行为改变）；`/app` 路径保真；uid 0 身份；资源限制（除非另配 cgroup）
- **仍然需要**：cgroup 做限流与计量（否则测不出东西）
- 只适合"我就想看看命令能不能跑通"，不适合做负载测量

## 3. 磁盘：overlayfs 是硬需求还是省钱项

以本地实测的压缩→解压比 3.15× 估算（840 MB → 2.65 GB）：

| 方案 | 压缩传输 | 落盘 |
|---|---|---|
| 每 task 独立展平目录 | 111 GB | **约 350 GB** |
| 共享层 + overlayfs / 硬链接去重 | 24.3 GB | **约 76 GB** |

113 个 task 共享 23 个 layer（750 MB 压缩），去重率 78%。
**所以 overlayfs（或至少解包时按 layer digest 做硬链接去重）在批量场景下接近硬需求**，
省下约 275 GB。单条 trace 调试则无所谓。

## 4. 容易被忽略的保真度细节

| 细节 | 原始 harness | 我们当前的 replay | 非 Docker 方案必须注意 |
|---|---|---|---|
| 解释器 | `bash -lc`（`docker.py:38`） | **`bash -c`**（`replay.py:258`，缺 `-l`） | `-l` 会 source `/etc/profile`，决定 PATH 里有没有 `/usr/local/bin`（python3 与 pytest 都在那）。Docker 下靠镜像 ENV 兜住了所以没出事；chroot/裸跑没有这层兜底，**必须补 `-l`** |
| 工作目录 | `-w /app` | `-w /app` | 每条命令自带 `cd /app &&` 前缀，但仍需保证起始 cwd 存在 |
| 超时语义 | 宿主杀 `docker exec` 客户端，**容器内进程存活** | 容器内 `timeout -k 5 30` 真杀 | 见 §0.1，非 Docker 方案要能"到点脱离不杀" |
| /proc /dev /sys | 自动挂 | 需显式挂 `--proc /proc --dev /dev` | 缺 /proc 会让 pytest / coverage 出奇怪错误 |
| 时区 / locale | 镜像内 /etc | chroot 后用的是 rootfs 的 /etc | 裸跑会串用宿主 /etc |

## 5. 本条 trace 对运行时的实际要求（决定裸跑可行性）

438 条命令的程序分布：`python3` 98、`grep` 71、`timeout` 51、`cat` 50、`sed` 50、`nl` 49、
`git` 31、`find` 11、`awk` 7。**零编译器调用、零架构探测、零二进制下载、零 SIMD**。
即除 Python 环境外只依赖标准 coreutils + git。
镜像里虽然装了 node 24 / go / rust 1.92 / gcc 12（来自 `mars-base` 多语言基座），
**这条 trace 一个都没用到**——所以若只为这一条 trace 裸跑，不需要复刻整个 2.65 GB 基座。

但注意这是 python task 的情况。go / rust / ts 的 task 会真的调编译器与包管理器，
裸跑要复刻的东西多得多，且更容易受宿主环境污染。

## 6. 需要进一步确认的点

- rootless overlayfs 在本机 5.15 WSL2 内核上是否真能挂（未实测）
- `nsenter` 进入 userns 拥有的 namespace 时的权限细节（未实测）
- 孤儿进程在自建 PID ns 里的收养行为是否与 Docker 一致（未实测）
- 展平解包时 whiteout 处理的正确性（27 层里是否真有 whiteout，未查）
