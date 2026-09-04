# 两个工程问题：容器内多 step 执行 / ARM 镜像预构建

---

# Q1. 如何让多个 step 在 Docker 内执行

## 1.1 核心机制：长驻容器 + 串行 exec

```bash
docker run -d --name replay_<trial> \
    --cpus=2 --memory=8192m --memory-swap=8192m \   # 对齐 task.toml，113/113 都是 2C/8G
    --network=none \                                 # 对齐 allow_internet=false
    <task_image> sleep infinity

for cmd in commands:                                 # 严格按 trace 顺序，串行
    docker exec -w /app <name> bash -lc "$cmd"
```

**不能一条命令一个 `docker run`。** 实测该 trace 有 49 个 `/tmp` 路径、**59 次跨 step 引用**
（如 `git commit -F /tmp/commit_message.txt` 引用的是几个 step 之前写的文件）。
每条命令新建容器会让这些引用全部落空，patch 硬校验必挂。

## 1.2 状态模型：什么跨 step 保留

| 状态 | 跨 step | 说明 |
|---|---|---|
| 文件系统（含 `/tmp`） | **保留** | 容器 rootfs 持续存在，这是多 step 能工作的根本 |
| 后台进程 / 孤儿进程 | **保留** | 见 §1.4，这是保真度的关键 |
| page cache | **保留** | 影响测量：前约 85 条命令是 warm-up，跨机对比必须统一预热 |
| 当前工作目录 (cwd) | **不保留** | 每条 exec 是新进程 |
| 环境变量 | **不保留** | 同上 |

cwd 不保留正是为什么 trace 里 **418/439 条命令自带 `cd /app &&` 前缀**——
模型的 system prompt 明确告诉它 "Directory or environment variable changes are not persistent.
Every action is executed in a new subshell."

## 1.3 解释器必须是 `bash -lc`，不是 `bash -c`

harness 侧 `environments/docker.py:38`：`interpreter: list[str] = ["bash", "-lc"]`。
`-l` 会 source `/etc/profile`，决定 PATH 里有没有 `/usr/local/bin`（python3 与 pytest 都装在那）。

**我们当前的 `replay.py:258` 用的是 `bash -c`，缺 `-l`。** Docker 下靠镜像 ENV 兜住了没出事，
但这是一处与 harness 的真实偏差，且在 chroot / 裸跑场景下没有兜底。**待修**。

## 1.4 超时与孤儿：要复现就不能在容器内杀

`DockerEnvironment.execute` 用的是普通 `subprocess.run(cmd, timeout=30)`，
`cmd = ["docker","exec",...]`——**超时杀的是宿主上的 docker exec 客户端，容器内进程照跑**。
（同包的 `LocalEnvironment._run` 显式 `killpg`，上游只给 local 修了，没给 docker 修。）

两次实测（探针容器，测完即删）：
- `sha1sum /dev/zero`：超时后 5s 仍在跑，%CPU=96.6，cgroup 3s 增量 = 98.9% 单核
- `while true; do echo spam; done`：**同样存活**，%CPU=97.2。管道断裂**没有**触发 SIGPIPE

所以三种超时策略的取舍：

| 策略 | 实现 | 保真度 |
|---|---|---|
| 容器内 `timeout -k 5 30`（当前） | 真杀干净 | ❌ 负载**低于**原始，无孤儿争抢 |
| 宿主 `subprocess.run(timeout=30)` | 只杀 exec 客户端 | ✅ 与 harness 逐行一致 |
| 条件式：`trace_rc==-1` 走宿主超时，其余给足宽限 | 混合 | ✅ 最优：rc 序列全对齐 + 不制造原始没有的孤儿 |

**推荐第三种。** 副作用：孤儿的 CPU 会被记到后续命令的 cgroup 差分里——
这是物理真实（原始运行就这样），但受污染的窗口需显式标记。

## 1.5 exec 往返开销：实测约 70ms，占比 3%

438 条命令 `wall_s`：最小 **0.0702s**、p5 0.0737s、中位 0.0863s。
最轻的命令（`pwd` 之类）几乎全是 exec 往返开销，即约 **70ms/条**。
438 × 70ms ≈ **31s**，占总墙钟 981s 的 **3.2%**。

## 1.6 备选：容器内 driver

把命令清单送进容器（`docker cp` 或构建期 `COPY`），由容器内一个 driver 串行执行。

| | 宿主驱动 + 逐条 exec（当前） | 容器内 driver |
|---|---|---|
| exec 开销 | 70ms/条，共 3.2% | 无 |
| per-command cgroup 归因 | 干净 | driver 自身 CPU 混入被测 cgroup |
| "到点脱离不杀" | 直接可实现 | 要自己实现进程组管理，更容易做错 |
| 宿主依赖 | 需 docker CLI | 只需把清单塞进去 |
| 与 harness 语义一致性 | 逐行一致 | 需自行复刻 |

**结论：为了测量保真，留在宿主驱动。** 3.2% 的开销买的是干净的单命令归因和与 harness 一致的超时语义，
值得。容器内 driver 只在"要把负载打包发给别人跑、且不关心单命令归因"时才划算。

---

# Q2. 如何预构建 ARM 镜像

## 2.1 现状

- 113 个 task 镜像**全部是 linux/amd64 单一 manifest**，无多架构索引 → ARM 上没有现成镜像
- 但基座 `public.ecr.aws/x8v8d7g8/mars-base:latest` **是多架构的**：
  - amd64 `sha256:9c024d5dc46f…` 22 层 / 750 MB
  - arm64 `sha256:8d4d973c9937…` 22 层 / 705 MB
- 所以路径是**按 task Dockerfile 在 ARM 上重建**，不是找现成镜像

## 2.2 基座漂移已量化：只差 layer 0

把今天的 mars-base amd64 与 gql task 镜像逐层比对：

```
#0   ae4ce04d0e1c  28.2MB   ≠≠   c25bee1cbcbb  29.1MB      ← Debian 基础 rootfs 层
#1..#21                     逐位相同（21 层 / 约 721 MB）    ← node/go/rust/gcc/python 工具链
```

**漂移只发生在 OS 基础层，工具链一层没动。** 意味着：
- 重建拿不到当初那个 layer 0（而且 ARM 上本来就是不同的二进制），这部分差异不可避免
- 但决定行为的工具链是同一套，风险可控

## 2.3 Dockerfile 需要改的只有 6/113

扫描全部 113 个 `environment/Dockerfile`：

| 模式 | 命中 | 语言分布 |
|---|---|---|
| URL 里写死 linux/amd64/x86_64 | **6** | rust 5, typescript 1 |
| 构建期编译 go build/install | 36 | go 34, rust 1, ts 1 |
| 构建期编译 cargo --no-run | 5 | rust 5 |
| npm/pnpm/yarn install | 45 | ts 33, rust 5, js 5, python 2 |
| pip install | 32 | python 31, ts 1 |

需要改的 URL 只有两类（各有官方 ARM 版，一行替换）：

| 现状 | ARM 替换 | 影响 task |
|---|---|---|
| `https://get.nexte.st/${NEXTEST_VERSION}/linux` | `.../linux-arm` | 5 个 rust |
| `deno-x86_64-unknown-linux-gnu.zip` | `deno-aarch64-unknown-linux-gnu.zip` | 1 个 ts |

（`nodejs.org/dist/…linux-x64.tar.xz` 出现 5 次，但全在**注释掉的 fallback** 里，不执行。）

**其余 107/113 零改动。**

## 2.4 依赖侧：以 gql 为例，15/15 原生扩展包有 aarch64 wheel

镜像内 99 个包，15 个带 `.so`。按锁定版本逐个查 PyPI：

```
aiohttp 3.13.5 / cffi 2.0.0 / charset-normalizer 3.4.4 / coverage 7.14.1 /
cryptography 46.0.3 / dulwich 0.21.7 / frozenlist 1.8.0 / msgpack 1.1.2 /
multidict 6.7.1 / propcache 0.5.2 / PyYAML 6.0.3 / RapidFuzz 3.14.3 /
websockets 15.0.1 / wrapt 2.2.1 / yarl 1.24.2
```
**15/15 都有 cp312 aarch64 manylinux wheel，一个都不用编译。**

## 2.5 构建方式三选一

| 方式 | 命令 | 适用 |
|---|---|---|
| **原生 ARM 机器**（推荐） | 在 Graviton / Ampere / Apple Silicon VM 上直接 `docker build` | 全部 113 个。go/rust/npm 的构建期编译按原生速度跑 |
| buildx + 远程 ARM builder | `docker buildx create --name arm --platform linux/arm64 ssh://user@arm-host` | 想在 x86 上驱动、ARM 上真跑，兼顾两者 |
| buildx + QEMU 模拟 | `docker buildx build --platform linux/arm64` | 只适合 python task。go 36 个 / rust 5 个有构建期编译，QEMU 下会慢一个量级 |

## 2.6 钉死策略（对抗版本漂移）

风险不在架构，在**浮动版本**。三处要钉：

1. **基座**：`FROM public.ecr.aws/x8v8d7g8/mars-base@sha256:8d4d973c9937…`（arm64 digest），
   不用 `:latest`
2. **仓库代码**：Dockerfile 已用 `ARG BASE_SHA=<commit>`，本来就确定
3. **依赖**：`pip install -e ".[test]"` 是构建期解析。
   用 amd64 镜像导出的 `pip freeze`（99 行）转成 constraints 文件钉死：
   ```
   docker run --rm <amd64_image> pip list --format=freeze > constraints-amd64.txt
   # Dockerfile 里改成
   RUN pip install -e ".[test]" -c /tmp/constraints-amd64.txt
   ```
   go / rust 侧对应的是 `go.sum` 与 `Cargo.lock`（`cargo fetch --locked` 已经锁了）

## 2.7 验证：怎么确认 ARM 镜像"等价"

按强度排序：

1. **依赖集比对**：arm64 镜像的 `pip freeze` / `go list -m all` / `cargo tree` 与 amd64 逐行比对
2. **gold solution 验证**：在 arm64 镜像里打 `solution/solution.patch`，跑 `tests/test.sh`，
   确认 f2p 与 p2p 与榜单记录一致（这是任务定义自带的判据，最有力）
3. **trace 重放硬校验**：跑一条该 task 的 trace，`git diff base..HEAD` 与 model.patch 逐字节比对
4. **rc 序列比对**：注意 ARM 与 x86 速度不同，`timeout N` 的边界会移动，
   这类分歧要按 §1.4 的条件超时口径处理，不能当作架构问题

## 2.8 成本估算

- 传输：arm64 基座 705 MB（一次）+ 每 task 独有层。amd64 侧实测去重后 24.3 GB / 113 个，
  ARM 侧量级相当
- 构建时间：python task 几分钟；go task 含 `go mod download` + `go install`；
  rust task 含 `cargo nextest --no-run` 全量编译，单个可能十几分钟。
  原生 ARM 上 113 个估 10–20 小时，可并行
- 存放：自建 registry，或 `docker save` 成 tar 分发

## 2.9 未验证项

- 本机无 ARM 硬件、无 qemu-user binfmt handler（`/proc/sys/fs/binfmt_misc/` 只有 WSLInterop），
  以上均为静态分析结论，未实机构建
- `get.nexte.st/<ver>/linux-arm` 与 deno aarch64 包的实际可用性未验证
- ts/js 的 45 个 npm 安装里是否有需要本地编译的 native module，未逐个查
