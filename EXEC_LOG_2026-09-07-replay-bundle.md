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
