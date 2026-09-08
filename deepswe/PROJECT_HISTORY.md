# PROJECT HISTORY

按时间倒序记录本项目的主要工作单元，用于长期上下文恢复。

## 2026-09-07: 重放流程可移植化 + 打包成服务器可跑的 bundle

**Goal:** 把容器重放流程做成一个可整包拷到服务器、开箱即跑的 bundle，
先在服务器上打通已验证过的 5 个镜像（crosslang 那 5 条 trial）。

**Steps:**
1. 摸清现状：`crosslang/` 已含 5 条 trial 的全部输入（trajectory / model.patch /
   task.json / meta.json）+ 基线判定，自包含、不依赖被 gitignore 的 `data/` — success
2. `replay.py` 可移植化（cgroup 三级探测 / 容器名唯一化 / 镜像预检 / host 记录）— success
3. 新增 `run_batch.py`（串行批量 + 基线对比 + 汇总）— success
4. 新增 `preflight.sh`（活体测试而非查版本号）— success
5. 新增 `make_bundle.sh` + `RUNBOOK.md` — success
6. 本机可验证部分全部验证（端到端保真度只能在服务器做）— success

**Key Findings:**
- **`replay.py` 在本机已经跑不起来了**：它硬编码 systemd driver 的
  `/sys/fs/cgroup/system.slice/docker-<id>.scope`，而本机 docker 的 cgroup driver
  是 `cgroupfs`，实际落点是 `/sys/fs/cgroup/docker/<id>` —— `is_dir()` 为 False，
  `Cgroup.__init__` 直接 raise。**这是换机器时第一个会炸的点**，也说明上次能跑通
  依赖了当时的 docker 配置。
- `/proc/<State.Pid>/cgroup` 这条最准的路**在 WSL / Docker Desktop 下不可用**
  （dockerd 在另一个 pid namespace，宿主 /proc 里没有那个 pid）。所以探测必须是
  「/proc → 已知 driver 候选 → 树搜索」三级，单靠任何一级都不够。
- 随包基线 `<trial>/replay/verdict.json` 是**最终口径**（`/bin/sh -c` + sinkhole403），
  而 `INDEX.md` 汇总表里的 rc 数字是**修正前**那一版（`bash -lc` + `--network=none`）。
  两者对不上是正常的（rust 72/76 vs 74/76、ts 58/59 vs 59/59、go 67/69 vs 66/69），
  已在 INDEX.md 和 RUNBOOK.md 里写明**跨机对比一律以 verdict.json 为准**。
- bundle 精简后只有 **616 KB**（解包 3.0 MB）：重放真正需要的只有 trajectory /
  model.patch / task.json / meta.json，agent 日志与 verifier 输出各约 1MB×5 用不到。

**Files Changed:**
- `deepswe/replay.py` - cgroup 三级探测替代硬编码；容器名带 PID + 存量检测；
  镜像默认不现拉；io.stat 缺失降级；verdict 增记 host。503 → 620 行
- `deepswe/crosslang/run_batch.py` - 新增，串行批量 driver + 基线对比 + 汇总
- `deepswe/crosslang/preflight.sh` - 新增，活体环境预检
- `deepswe/crosslang/make_bundle.sh` - 新增，自包含 tarball 打包
- `deepswe/crosslang/RUNBOOK.md` - 新增，服务器操作手册
- `deepswe/crosslang/INDEX.md` - 「已知风险」标注为已修 + 补充口径差异说明

**Commit:** pending

## 2026-09-04: 从 113 条 agent 轨迹提取运行期工具清单

**Goal:** 回答"重放这批负载到底需要哪些程序"，并在全量上复核
`NODOCKER_REQUIREMENTS.md` §5 那条基于单条 python trace 的结论。

**Steps:**
1. 写 `analyze_tools.py`（仅标准库）—— 自写 POSIX-ish 词法器 + 语法状态机解析
   4519 条 bash 原文 - success（0 条解析失败、0 条 unresolved）
2. 频次 / task 覆盖度双口径统计 + 分语言画像 + 六类分类 - success
3. 无 Docker 障碍点专项（绝对路径 / 特权 / 网络 / PATH / 重进程 / 后台进程）- success
4. 用本机 6 个 task 镜像实测 `bash -c` vs `bash -lc` 的 PATH 解析差异 - success
5. 逐条核对 31 次 egress 命令的 stdout 判定成败（含 6 条人工判定）- success
6. §5 复核并给出建议改写 - success
7. 输出改成逐字节可复现（消除 set 迭代顺序导致的 key 顺序抖动）- success

**Key Findings:**
- 115 个不同程序，top 18 覆盖 90% 调用；单 task 不同程序数中位数 17
- `python3` 是跨语言的**文件编辑引擎**（79/113 task，go 33/34、ts 32/35），
  不只是 python task 的运行时 —— 之前的文档没写这条
- **91/113 task（80.5%）写 `/app` 之外，且 100% 是 `/tmp`**；写系统目录 0 次
- 特权操作近乎为零：仅 2 task 各 1 次 `chmod`；0 次 apt-get/sudo/chown/mount
- `allow_internet=false` 实测是 **squid 代理返 403**，不是断网；31 次 egress 里 21 次被拦
- `go mod tidy`（8 go task）和 `pip install -e .` 的失败点在 **sumdb 校验 / 构建后端下载**，
  加 `GOPROXY=off GOSUMDB=off` 或 `--no-build-isolation` 就纯本地跑通（同 task 前后两步的对照）
- 裸 `bash -c` 下只有 `/root/.cargo/bin` 的工具和 `bunx` 解析不到 → **精确到 5 个 rust task**；
  `bun` / `uv` / `go` 都有 `/usr/local/bin` 兜底，不受影响（证伪了此前的假设）
- **§5 复核**：零架构探测 ✅ 成立、零成功下载 ✅ 成立、
  **零编译器调用 ❌ 不成立 —— 73/113 task（64.6%）真的调编译器**
- 附带发现：`prometheus-transactional-reload-status` 的 `task.toml` 语言标注错了
  （标 typescript，实际是 Go 仓库）；另有 1 条 trajectory 的 stdout 里泄漏了
  `VERTEXAI_CREDENTIALS` 服务账号私钥（`data/` 已 gitignore，但值得注意）

**Files Changed:**
- `analyze_tools.py` - 新增，全部分析逻辑（仅标准库，幂等，逐字节可复现）
- `TOOL_INVENTORY_RUNTIME.md` - 新增，结论文档（实测 / 推断分开标注）
- `data/tool_inventory.json` - 生成物，被 .gitignore 忽略，不入库

**Commit:** pending（按要求未提交）

## 2026-09-08: 全量 replay 用例集（118 条）+ 内网 CA 提取

**Goal:** 把服务器重放从 5 条样例扩到全量，目标跑通 go / python / javascript；
顺带把公司内网 TLS 中间人环境下的证书获取做成可复用工具。

**Steps:**
1. 摸清 `data/` 布局与语言归属，确认装配所需的最小字段集 - success
2. 发现 `TRAJECTORY_SELECTION.json` 选中的 113 条与 crosslang 那 5 条**不是同一批 trial** - success
3. 写 `make_full_trials.py` 装配 118 条（113 选中 + 5 已验证对照组）- success
4. 修 `build_arm.sh` 的「一语言一目录」覆盖 bug，改为按 trial 构建 - success
5. 给 `run_batch.py` 加 `--skip-missing`，支持边建边跑 - success
6. 新增 `detect_mitm.sh`：判定内网是否做 TLS 中间人 - success
7. 端到端验证：打包 → 解包 → 用新装配的 trial 实跑 replay - 见 EXEC_LOG

**Key Findings:**
- **replay.py 完全不读 meta.json**，只读 trial 目录 + task.json；task.json 里也只用
  `task.toml`（build_arm.sh 只用 `environment/Dockerfile`）。据此把 task.json 从
  30.4 MB 裁到 0.4 MB，顺带把参考解 `solution/solution.patch` 挡在包外
- **两批 trial 不同**：selection 取 claude-fable-5 优先，crosslang 那 5 条是更早一轮的跨模型取样。
  trial_name / 模型 / patch 字节全不同 → **113 条里一条已验证基线都没有**。
  对策是把 5 条带上当回归对照组（同 task 同镜像，零额外构建成本）→ 118 条
- `build_arm.sh` 的 `DIR_OF[$lang]="$dir"` 是覆盖式赋值，35 条 go 只留最后一条**且不报错**——
  这个 bug 在 5 条样例集上完全暴露不出来
- **镜像增量实测**：python task 镜像相对 `mars-base:arm64` 只 **+30 MB**，
  typescript **+560 MB**（`pnpm install` 把 devDeps 全装进去）。go/js/rust 未实测
- **构建耗时实测**（qemu 模拟 ARM）：python 144s；typescript ~280s
  （pnpm install 132s + 装报告器 59s + clone/gc 45s）。rust 是唯一有真·编译步骤的
  （`cargo nextest run --no-run`）
- **各工具的 CA 信任源不一致**：git/curl/go/cargo 读系统 bundle，
  **node/npm 只认内置 146 张根、python/pip 用 certifi**，两者都不读系统 bundle ——
  只跑 `update-ca-certificates` 会让 git clone 过而 npm/pip 照样失败
- **TLS 里证书在校验之前就明文送达客户端**，所以 `server certificate verification failed`
  的时候证书就在手上；`openssl s_client -showcerts` 照样拿得到（已用无关 CA 实测）
- 判定中间人最强的判据是**跨站点比较叶子证书的签发者**：中间人只有一张签名证书，
  它得给所有站点签，所以多站点签发者相同即是铁证（正常网络下应该五花八门）
- `CA:TRUE`（basicConstraints）才是「这张是不是 CA」的权威判据，不是靠链上的下标位置

**Files Changed:**
- `crosslang/make_full_trials.py` - 新增，data/ → trial 目录的装配器
- `crosslang/detect_mitm.sh` - 新增，TLS 中间人判定
- `crosslang/build_arm.sh` - 按 trial 构建；失败清单与单条重试
- `crosslang/run_batch.py` - `--skip-missing`；全量集下的输出裁剪
- `crosslang/make_bundle.sh` - `--trials-dir`
- `crosslang/RUNBOOK.md` - §7 重写为全量集操作说明 + 实测成本
- `EXEC_LOG_2026-09-08-full-replay-set.md` - 新增

**Commit:** 见下方 git log
