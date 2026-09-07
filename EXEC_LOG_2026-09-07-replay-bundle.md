# Task Execution Log: replay 脚本归档 + 服务器可移植化

**Date:** 2026-09-07
**Goal:** 把容器重放流程打包成一个可整包拷到服务器、开箱即跑的 bundle，
先在服务器上打通已验证过的 5 个镜像（crosslang 那 5 条 trial）。

## 背景与起点

`deepswe/crosslang/` 已经是 5 条 trial 的完整归档（commit 600637e 入库），
每条含 `trajectory.json` / `model.patch` / `task.json` / `meta.json` +
本机跑出来的 `replay/verdict.json` 基线。**输入数据自包含，不依赖被 gitignore 的 `data/`**——
这是可以直接打包的前提。

| 语言 | trial | 命令数 | 本机基线 |
|---|---|---:|---|
| python | `returns-validated-error-accumula__8JQj5gw` | 98 | patch_identical ✅ / rc 95(97)/98 |
| go | `actionlint-action-pinning-lint__23b2uyq` | 69 | patch_identical ✅ / rc 66/69 |
| rust | `fd-deterministic-multi-key-sorti__fK6jc93` | 76 | patch_identical ✅ / rc 72(73)/76 |
| typescript | `true-myth-iterable-collection-co__BBLS6Fy` | 59 | patch_identical ✅ / rc 58/59 |
| javascript | `yjs-map-conflict-detection__gSSidka` | 65 | patch_identical ✅ / rc 65/65 |

## Problems to Solve

1. **`replay.py` 不可移植**：cgroup 路径硬编码成 systemd driver 的
   `/sys/fs/cgroup/system.slice/docker-<id>.scope`，换台机器就 raise 退出
2. **没有批量 driver**：5 条要手敲 5 次，且结果无法与基线自动对比
3. **没有环境预检**：服务器上 cgroup driver / 镜像是否就位 / 磁盘空间，跑之前无从确认
4. **并发互杀**（`crosslang/INDEX.md` 已记录的已知风险）：容器名由 trial 名推导，
   同一条 trial 跑两个进程会静默互删容器
5. **镜像缺失会静默触发 pull**：实测出口带宽 0.27 MB/s，误触一次就是几十分钟
6. 没有打包脚本与服务器操作手册

## 关键实测（本机，2026-09-07）

| 项 | 值 |
|---|---|
| 内核 | 5.15.167.4-microsoft-standard-WSL2 |
| cgroup | v2（`cgroup2fs`） |
| **docker cgroup driver** | **`cgroupfs`**（不是 systemd） |
| 容器 cgroup 实际路径 | `/sys/fs/cgroup/docker/<full-id>` |
| `replay.py` 硬编码路径 | `/sys/fs/cgroup/system.slice/docker-<id>.scope` → **`is_dir()==False`** |
| `/proc/<State.Pid>/cgroup` | **不存在**（WSL 下 dockerd 在另一个 pid namespace） |
| `memory.peak` | 缺失（6.8+ 内核才有），轮询 `memory.current` 的方案仍必要 |
| 本机镜像 | 5 个 task 镜像**均不在本机**，端到端验证只能在服务器做 |

→ 结论：路径探测必须是「`/proc` 优先 → 已知 driver 候选 → 全盘搜兜底」三级，
且三级都要能失败得明确。单靠 `/proc` 或单靠候选列表都不够。

## Steps Log

### Step 1: replay.py 可移植化
- **Status:** success
- **Result location:** `deepswe/replay.py`（503 → 620 行）
- **改了什么（都不动指标口径，只动可移植性）：**
  1. **cgroup 三级探测**替代硬编码：`/proc/<pid>/cgroup` → 4 条已知 driver 候选
     （cgroupfs / systemd / rootless×2）→ cgroup 树搜索；全失败时把 `/sys/fs/cgroup`
     实际类型 + 容器 ID + State.Pid + 试过的路径一并抛出
  2. `io.stat` 缺失从「退出」降级为「rbytes/wbytes 记 0」（不影响保真度结论）
  3. 容器名改 `replay_<trial>_<pid>` + 启动前检测同前缀存量容器并拒绝启动
     （`--force` 越过）→ 消灭 INDEX.md 记的「并发互杀」
  4. 镜像不在本地时默认拒绝启动（`--allow-pull` 越过）
  5. verdict 增记 `host`（内核 / cgroup 目录与探测方式 / CPU 数 / 镜像）
- **实测验证：** 用 ubuntu:24.04 真起容器，探测到
  `/sys/fs/cgroup/docker/<id>`（方式=已知 driver 候选），sample 读出真实
  usage_usec/memory_current。**这正是旧版本会 raise 退出的场景。**

### Step 2: 批量 driver + 预检 + 打包 + 手册
- **Status:** success
- **Result location:** `deepswe/crosslang/{run_batch.py,preflight.sh,make_bundle.sh,RUNBOOK.md}`
- `run_batch.py`：扫 trial（判据是 4 个必需文件齐全，不靠目录名）→ 预检
  （docker / cgroup v2 / 镜像就位）→ 串行重放（实时输出 + 落日志）→ 与随包基线
  `replay/verdict.json` 对比 → 出 `SUMMARY.md` + `summary.json`。
  支持 `--only` / `--smoke N` / `--dry-run` / `--keep-going`。
- `preflight.sh`：**活体测试**而非查版本号——真起容器验证 cgroup 可定位可读、
  `--cpus/--memory` 真生效、镜像内有 timeout/python3/git/sh、`/app` 是 git 仓库、
  `--network=container:` 共享 netns 可用、`--network=none` 真断网。
- `make_bundle.sh`：产出自包含 tarball，默认精简（不带 agent 日志与 per-command 指标），
  `--full` 带全量；随包 SHA256SUMS。
- `RUNBOOK.md`：一分钟版 / 前置条件 / 判定口径 / 故障排查 / 全量扩展预算。

### Step 3: 本机可验证部分的验证
- **Status:** success（端到端保真度只能在服务器验，见下）
- **已验证：**
  | 项 | 结果 |
  |---|---|
  | cgroup 三级探测（真容器） | ✅ 探测到 `/sys/fs/cgroup/docker/<id>`，指标读出真实值 |
  | `run_batch.py --dry-run` | ✅ 5 条全识别，基线读出，镜像缺失逐个报出并给 pull 命令 |
  | `preflight.sh`（ubuntu 替身） | ✅ 18 通过 / 9 失败，失败项均为替身镜像本就没有的东西 |
  | 打包 → 解包 → SHA256SUMS | ✅ 全部匹配，616 KB / 解包 3.0 MB / 32 文件 |
  | 解包后自包含性 | ✅ `run_batch.py` 自动定位同级 `replay.py`；单条 CLI 解析出 99 条命令、读出 task.toml、容器名带 PID、镜像预检正确拦截 |
- **未验证（本机无 task 镜像，必须在服务器做）：** `patch_identical` 端到端保真度、
  实际重放耗时、sinkhole 在真镜像上的行为。

### Step 4: 归档与提交
- **Status:** success
- INDEX.md「已知风险」标注为已修，并补充「汇总表是修正前口径、verdict.json 才是
  最终口径」的说明（rust 72/76 vs 74/76、ts 58/59 vs 59/59、go 67/69 vs 66/69）
- PROJECT_HISTORY.md 已更新

## 交付物

拷到服务器的包：`bash deepswe/crosslang/make_bundle.sh` → 616 KB tar.gz，
解开后 `bash preflight.sh` → `python3 run_batch.py --smoke 5` → `python3 run_batch.py`。

## 服务器上的预期

- **唯一硬标准**：5 条全部 `patch_identical=true`
- `rc_match` 与基线差几条属正常（四类不可消除来源，RUNBOOK §5.2）
- 基线合计 862s / 367 条命令（python 222.6 / go 104.0 / rust 295.6 / ts 68.6 / js 171.3）


---

## 第二阶段：目标环境是 ARM + 拉不到 registry + 要代理

**起因**：用户澄清目标环境 —— ECR/DockerHub 不通、能访问 git、传文件极慢
（实测 400MB/2h ≈ 55 KB/s）、docker 可用、已传好 `mars-base:arm64`、访问外网要代理。

### 路线变更：从「搬 amd64 镜像」改为「在 ARM 上重建」

搬运不可行：5 个 task 镜像是 amd64，per-task 层是依赖缓存、全部架构相关，
ARM 上不能复用。而重建的下载走目标机自己的出口，**避开了 55 KB/s 那条链路**。

可行前提（均已核实）：`task.json` 里就带着 `environment/Dockerfile`；113 个 Dockerfile
零 COPY / 零 ADD，构建上下文可为空目录；建完打上 `task.toml` 里原本的 tag，
`replay.py` 零改动。

### 新增

| 文件 | 作用 |
|---|---|
| `check_sources.sh` | 探测构建期 12 个上游端点。打真实端点看 HTTP 码而非 ping（原环境的 403 就是 TCP 通、HTTP 拒） |
| `build_arm.sh` | 从本地 mars-base 重建。两处自动改写 + 代理支持 + 构建后自检 |

### 端到端实测（本机，用分发包里那份真 `mars-base:arm64`）

```
docker load → mars-base:arm64 (arm64 / 22 层 / 2.53 GB)
build_arm.sh python → 144s / 2427 MB
preflight → 15 项活体测试全过
完整重放 98 条命令 → patch_identical ✅ 63,009B 逐字节一致
```

**回答了原先标注的开放问题**：ARM 重建镜像上保真度成立。rc 91/98（基线 95/98），
少的 4 条全是超时类（7 条不匹配里 6 条是 pytest 撞 30s 墙，1 条 hypothesis 随机性）。
且这是在 qemu 模拟（比原生 ARM 更慢、超时更多）的不利条件下拿到的。

### 本阶段揪出的 5 个 bug

1. **cgroup 探测假阳性**（最危险）：探到 `/sys/fs/cgroup/init.scope` 却报"成功"。
   WSL 下 `.State.Pid` 撞上宿主另一个真实进程 → 静默采错数据。
   修：`/proc` 那级必须能在路径里认出容器 ID。
2. **`check_sources.sh` 假阴性**：状态码拼成 `200000`，6 个源被误报不通。
   根因是几 MB 的二进制端点撞 `--max-time`。修：`--range 0-0` 只取首字节。修后 12/12 全通。
3. **`replay.py` 漏了 `NO_PROXY`**：`~/.docker/config.json` 的 proxies 会被自动注入
   每个 `docker run`，其 NO_PROXY 会让部分域名绕过 403 sinkhole，报错文本与 trace 不符。
4. **`make_bundle.sh` 漏打新脚本**：`check_sources.sh` / `build_arm.sh` 没进包。
5. **`make_bundle.sh` 被 pipefail 打断**：`grep -v` 无匹配返回 1（正是"干净"的情况）。

### 代理（实测两条事实）

- **`docker build` 不继承 shell 的 `http_proxy`/`https_proxy`** —— 构建容器内是"未设置"，
  `git clone` 直接失败。必须显式 `--build-arg`。
- **预定义 build-arg 传的代理不会写进 image config 的 Env** —— 所以构建期用代理
  不污染运行期（原始 mars-base 的 Env 里本来也是零个 `*_proxy`）。构建后脚本再自检一次。

`build_arm.sh` 四个场景实测通过：环境变量继承 / loopback 自动 `--network=host` /
`user:pass@` 脱敏 / 未配代理时给提示。

### 交付

`deepswe/crosslang/deepswe-replay-bundle-20260907.tar.gz` — 628 KB / 35 文件。
随包 `BUILD_INFO`（打包时间、git commit、脚本 sha256 前 12 位）解决"手上这份是哪一版"。

**注意**：`mars-base:arm64` 与 task 镜像都不用传 —— 前者用户已传好，后者在服务器上重建。
