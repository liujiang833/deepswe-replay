# 不同 benchmark / 不同语言的环境需求

## 0. 先纠正一个直觉

"go 的 task 需要不同的运行时" —— **在 DeepSWE 内部不成立**。
113 个 task 共享同一个多语言基座，**一个镜像装齐 5 种语言的运行时**：

| | 版本 | 配套工具 |
|---|---|---|
| Python | 3.12.12 | pip 25.3 / uv 0.9.18 / poetry / pytest |
| Node | 24.12.0 | npm 11.6.2 / pnpm 10.26.1 / yarn 1.22.22 / bun |
| Go | 1.25.5 | — |
| Rust | 1.92.0 | cargo 1.92.0 |
| C/C++ | gcc/g++ 12.2.0 | make 4.3 |

**没有**：java / ruby / php / cmake / bazel / dotnet / swift。

实测：113 个镜像共有 **23 层 / 751 MB**，就是这个基座。
所以换语言不用换运行时，只需要那 751 MB 下一次。

## 1. 语言差异的真正所在：依赖缓存，不是运行时

各语言缓存落点（基座里已配好）：

```
GOMODCACHE  /root/go/pkg/mod          CARGO_HOME  /root/.cargo
npm cache   /root/.npm                pip         /usr/local/lib/python3.12/site-packages
```

**每个 task 镜像只预热自己那门语言的缓存。** 实测 gql（python task）：
site-packages 167 MB 是热的，`/root/go/pkg/mod` 与 `/root/.cargo/registry` **根本不存在**。

这直接决定了 per-task 层的重量（基座之上的独有层）：

| 语言 | task 数 | 独有层数 中位 | 独有大小 中位 | 最大 |
|---|---|---|---|---|
| rust | 5 | **7** | **370 MB** | 1393 MB (boa) |
| typescript | 35 | 4 | 141 MB | 1055 MB (prometheus) |
| javascript | 5 | 5 | 95 MB | 272 MB (katex) |
| python | 34 | 3 | 76 MB | 524 MB (skrub) |
| go | 34 | 4 | 64 MB | **1797 MB (goreleaser)** |

**rust 单 task 最重**（中位 370 MB），因为 Dockerfile 里有 `cargo fetch --locked`
再加 `cargo nextest run --no-run` 把编译产物一并烘进镜像。
go 的中位最轻但尾部最长（goreleaser 1797 MB，go module cache 撑起来的）。

**关键推论：依赖已经全部烘进镜像**（`go mod download` / `cargo fetch` / `npm install` / `pip install`
都在构建期跑完），所以重放时 `--network=none` 才成立。这不是巧合，是任务设计的一部分。

## 2. 对我们三条设计线的影响

### 2.1 离线包
- 基座 751 MB 只需传一次，之后每 task 只加中位 64–370 MB
- **按语言分批打包是合理的**：先出 python 34 个（中位 76 MB → 约 3 GB + 基座），
  rust 5 个单独出（中位 370 MB，且有 1.4 GB 的尾部）
- 尾部要单独处理：goreleaser 1797 MB、boa 1393 MB、prometheus 1055 MB 三个就占了 4.2 GB

### 2.2 无 Docker 路径 —— 这里有个语言相关的坑
语言运行时的 PATH 接线写在 **`/root/.bashrc`** 里：
```sh
. "$HOME/.cargo/env"                    # → /root/.cargo/bin  (rust)
export PATH="$PATH:/root/.local/bin"    # pipx
. "$HOME/.local/bin/env"
export BUN_INSTALL="$HOME/.bun"; export PATH="$BUN_INSTALL/bin:$PATH"   # bun (js/ts)
```
`/root/.bashrc` 只在**登录 shell** 下经 `/root/.profile` 被 source。
而 Debian 的 `/etc/profile` 对 root 会**无条件覆写** PATH 成
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`（无 cargo、无 bun）。

| | Docker 内 | chroot / 无 Docker |
|---|---|---|
| `bash -c`（我们 replay.py 现在用的） | 靠 image config Env 兜住，能找到 cargo/bun | **裸 PATH，rust/js task 找不到工具链** |
| `bash -lc`（harness `docker.py:38` 用的） | 正常 | **靠 .bashrc 自愈，正常** |

**所以 `bash -lc` 在无 Docker 路径下不是保真度细节，是能不能跑的问题。**
（python task 感觉不到，因为 python3/pytest 在 `/usr/local/bin`，两种 PATH 都有——
这也是为什么我们在 gql 上跑了三遍都没暴露这个问题。）

### 2.3 ARM
per-task 层是**依赖缓存**，全部架构相关，必须在 ARM 上重新构建，不能复用 amd64 的层。
只有 751 MB 基座能直接用 arm64 变体。构建代价按语言分化：
- python 34 个：pip 装 aarch64 wheel，快
- go 34 个：`go mod download` + `go install` 从源码编译，可移植但要编译
- rust 5 个：`cargo fetch` + `cargo nextest --no-run` **全量编译**，最慢；且 5 个都要改 nextest 的下载 URL
- ts/js 40 个：npm 装，注意 native module 可能要本地编译

## 3. 换一个 benchmark 时要问的问题（按重要性排序）

### 第 1 位：有没有可自动判定的保真度校验口径
DeepSWE 有 `pre_artifacts.sh` 明确定义提交物 = `git diff --binary <base> HEAD`，
所以重放后能逐字节比对。**没有等价物的 benchmark，重放数据无法自证，采集了也不知道对不对。**
这一条不满足，其余都不用谈。

### 第 2 位：trace 是不是逐字记录的 shell 命令
- mini-swe-agent 只有**一个 bash 工具**，命令逐字进 trace → 可开环重放
- 若 harness 带 `str_replace_editor` / 文件编辑 tool / 浏览器 tool，
  这些**不是 shell 命令**，重放要先做语义翻译，保真度大打折扣
- 观察是否被截断（DeepSWE 约 11K 字符）→ 影响弱校验能力

### 第 3 位：资源规格是否被声明且同质
- DeepSWE：113/113 全部 2 CPU / 8192 MB / 20480 MB / 0 GPU / 无网 / 无 MCP / verifier 独立环境，
  **完全同质**，所以时间可比
- SWE-bench（我们本地跑过的对照组）：harness 的 `containers.create` 调用里**未见** cpu/mem 限制参数，
  即不约束资源 → 不同机器上的负载不可比。（**此条只扫了一眼就被打断，待确认**）
- 规格不声明的 benchmark，必须自己定一套并说明，否则跨机数据没有意义

### 第 4 位：运行时供给方式
- **共享多语言基座**（DeepSWE）：去重率高（实测 78%），分包便宜
- **per-instance 环境**（SWE-bench 的 conda testbed）：每个实例自带环境，去重率低，包会大得多

### 第 5 位：环境形态
| 维度 | DeepSWE | 需要留意的其他形态 |
|---|---|---|
| 容器数 | 单容器 | 多容器 / docker-compose（带 DB、消息队列的 web 任务） |
| 网络 | 无网 | 需要内部网络互联，或需要外网（不可复现） |
| GPU | `gpus = 0` | ML 类 benchmark 需要 GPU，cgroup 计量方式完全不同 |
| GUI | 无 | 浏览器/桌面 agent 需要 X11/VNC，负载性质完全不同 |
| 特权 | 普通 | docker-in-docker、网络工具类任务需要 privileged |
| MCP | `mcp_servers = []` | 带 MCP server 的任务需要额外进程与端口 |

注意 `task.toml` 里 `gpus` 与 `mcp_servers` 这两个字段**存在但为空**——
说明 schema 本身预期支持这些形态，只是 v1.1 这批没用到。
换批次或换 benchmark 时要重新扫这两个字段。

## 4. 待确认

- SWE-bench harness 是否真的不设资源限制（`run_evaluation.py:116/133` 的 `containers.create` 参数未读完）
- ts/js 的 45 个 npm 安装里是否有需要本地编译的 native module
- 基座里没有 java/ruby/php —— 若后续批次引入这些语言，基座会变，去重率要重算
