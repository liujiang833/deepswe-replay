# PROJECT HISTORY

按时间倒序记录本项目的主要工作单元，用于长期上下文恢复。

## 2026-09-09: 重放包缺陷修复 —— 14 条已定位问题（含 3 条阻断级）

**Goal:** 让「没有上下文的人拿着 tarball 照 README 操作」这条路在新服务器上真能走通。

**Steps:**
1. 改之前先核事实：扫 113 份 Dockerfile 的真实上游主机、实算哨兵命令数、
   curl 验 deno aarch64 资产 - success
2. 三条阻断级：README 主流程重排、`preflight.sh` 加 `--metrics` 门控 +
   镜像缺失降 warn、`run_batch.py` 逗号→空格 - success
3. 事实性错误 6 条（脚本名 / 章节号 / 5 条口径 / `build/<trial>` 路径 /
   patch_identical 结论 / ARM 硬改写数） - success
4. ARM deno 改写落地到 `build_arm.sh`，并用真实 Dockerfile 文本验证正则 - success
5. `check_sources.sh` 补 4 个探测点、全文改 113 口径 - success
6. 自检：6 个 `.sh` 过 `bash -n`、3 个 `.py` 过 `py_compile`，
   `check_sources.sh` 实跑 17/17 通，`preflight.sh` 两种模式实跑正常 - success

**Key Findings:**
- **预检与建镜像的顺序原本是反的**：README 让人先跑 `preflight.sh`，而它对每个缺失
  镜像打一个 FAIL，新机器 113 个全缺 → 必然 `exit 1`；更糟的是无镜像时活体测试整段
  `warn` 跳过，**预检真正的价值（起容器验能力）被架空**。`build_arm.sh` 自己的结尾
  提示反而是对的（先建后检）
- **`--metrics` 门控只有 `preflight.sh` 没对齐**：`run_batch.py:86` 早有 `need_cgroup`
  门控，README 也按「默认不要求 cgroup v2」写，唯独 `preflight.sh` 四处无条件硬失败
  → rootless docker / cgroup v1 的机器会被挡在门外
- **`build_arm.sh` 只认空格分隔的并列目标**，`run_batch.py` 的 `--only` 才吃逗号；
  原先打印的 `build_arm.sh a,b,c` 照抄必报「认不出目标」
- **ARM 硬改写漏了一条**：`cliffy` 写死 `deno-x86_64-...zip`。装错架构的二进制
  **下载时不报错**，拖到下一句 `RUN deno cache` 才 `exec format error`，极难认
- **`check_sources.sh` 全绿也可能建不成**：漏探 `deb.nodesource.com`、
  `repo.mongodb.org`/`www.mongodb.org`（eicrud）、`jsr.io` 与 github release 下载域（cliffy）
- **命令数 4519 含 113 条哨兵**（一 trial 一条，`load_trace` 标 `sentinel=True` 后跳过），
  实跑 **4406** 条
- **`patch_identical` 对这 113 条仍是开放问题**：2026-09-07 那次实测只覆盖另外 5 条
  对照组（ARM 重建 + qemu），RUNBOOK 原先写成「已成立」，与 `build_arm.sh` 三处
  「待验证的开放问题」直接矛盾 —— 已改成讲清适用范围，其余三处保持不动

**Files Changed:**
- `crosslang/README.md` - 主流程重排（解包→探源→建镜像→预检→重放）并写明为什么；
  前置条件改指 `check_sources.sh`；故障表 §2→§3；编译步骤措辞；4519/4406 口径注
- `crosslang/preflight.sh` - 新增 `--metrics`（cgroup 两段受其门控）；镜像缺失 bad→warn
  且零镜像时明确提示先建；磁盘/结尾提示改 113 口径并补 `--skip-missing`
- `crosslang/check_sources.sh` - 补 4 个探测点（共 17）；全文 5 条→113 条口径
- `crosslang/build_arm.sh` - 新增 deno x86_64→aarch64 改写；注释 118→113
- `crosslang/run_batch.py` - 缺镜像提示的 `","` → `" "`
- `crosslang/RUNBOOK.md` - §2 前置条件表、§2b 路 B / 源表 / ARM 改写两处、§3 预检、
  路 C 保真度结论、`build/<lang>`→`build/<trial>`、§7.1/§7.3 口径
- `EXEC_LOG_2026-09-09-bundle-fixes.md` - 新增

**Commit:** 未提交（按要求保留在工作区）

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

## 2026-09-09: 换源开关 `--registry` + 全量集缩到 113 条

**Goal:** 服务器上 typescript 镜像卡在 `pnpm install`（`resolved 548, downloaded 508`），
定位原因并给 `build_arm.sh` 加一个保真度安全的换源开关；同时把打包范围改成只要 113 条。

**Steps:**
1. 定位卡点 - success：`548` 是指纹，本机 9/8 那次成功日志里 `Packages: +548`、
   TAG 也与 `true-myth-iterable-collection-combinators` 逐字相同。**同一 Dockerfile 本机 132s 建完过**
2. 判定瓶颈 - success：从 29 条低速 WARN 反推耗时，与 size 无关 → 延迟受限而非带宽受限
3. 实测换源的三条注入途径 - success：只有 `ARG` + `--build-arg` 既生效又零残留
4. 执行 agent 改 `build_arm.sh` - success
5. 独立 agent 验证（含真实端到端构建）- success，但推翻了立项假设（见下）
6. 按要求去掉 5 条对照组，全量集 118 → 113 - success
7. 连带修正所有按 118 写的文档与脚本注释 - success

**Key Findings:**
- **瓶颈是每请求固定开销，不是带宽**：`@nodelib/fs.stat` 4 KiB 用了 8.0 s，
  `oniguruma-to-es` 269 KiB 只用 5.6 s；29 条整体均值 5.4 s，与 size 不相关。
  推论：`network-concurrency` 该**调高**而非调低（早先的相反建议已更正）
- **❗换源实测更慢**：同仓库同 548 包，官方源 132.4s / 镜像源 156.6s（+18%），
  低速 WARN 29 → 38。与延迟受限分析自洽——瓶颈不在目标主机，换主机没用。
  → RUNBOOK 该节已从「解法」改写成「测过确实有用才开的开关」，并附了 A/B 测法
- **三个工具三个配置源**：npm/pnpm 读 `NPM_CONFIG_REGISTRY`，
  **corepack 只读 `COREPACK_NPM_REGISTRY`、完全不读 `.npmrc`**（ofetch/query/valibot 三条会踩）
- **注入途径只有一条是干净的**：宿主 `export` 不传递；`ENV` 永久进 `Config.Env`；
  只有 `ARG` + `--build-arg` 构建期生效且零残留。且 `NPM_CONFIG_REGISTRY`
  **不在** docker 预定义 build-arg 白名单里（白名单只有 `*_PROXY`），必须显式写 `ARG`
- **❗`build_arm.sh` 在 Docker Desktop + WSL2 上按现状跑不通**（先于本次改动存在）：
  代理在 loopback 时脚本自动加 `--network host`，但这套 BuildKit **不兑现**该参数，
  `git clone` 直接失败。原生 Linux Docker 未复现
- **`node_modules/.modules.yaml` 会留下镜像站 URL**：镜像内容与官方源建出来的字节不同，
  两条自检都查不到。实测后果良性（离线报错文本逐字节一致）→
  结论修正为「**行为**不受污染」成立、「字节完全相同」不成立
- **全量集 113 条 / 113 个镜像 / 4519 条命令**，一 task 一 trial 一镜像。
  去掉的 5 条对照组是**唯一已验证基线**，去掉后 `run_batch.py` 全部显示「无基线」
  （优雅降级，不报错），某条失败时无法区分是 task 的问题还是流程的问题

**Files Changed:**
- `crosslang/build_arm.sh` - 新增 `--registry` / `DEEPSWE_NPM_REGISTRY`；插 `ARG` 两行；
  两条构建后自检（Env 残留 + 运行期 `npm config get registry` 实测）；REWRITES.md 漂移两条
- `crosslang/RUNBOOK.md` - 新增 §2b「取包慢:换镜像源」（含 A/B 实测数据与测法）；
  §2b 代理一节加 `--network host` 已知问题；§5.3 加「包里已无 verdict.json」提示；
  §6.5/§7.2 交叉引用；§7 全章按 113 重写
- `crosslang/run_batch.py`、`crosslang/make_bundle.sh` - 118 → 113 的注释修正
- `.gitignore` - 补 `deepswe/crosslang/full_trials/`（原规则只匹配一层深）
- `EXEC_LOG_2026-09-09-npm-registry.md` - 新增

**Commit:** 见 git log

## 2026-09-09（续）: bundle 可用性修复 + 重放并发 + Go 取模块开关

**Goal:** 让「没有上下文的人照 README 在新服务器上操作」真能走通；把重放从串行改成可并发
（当前阶段目标是先跑通而非收集性能数据）；解决 go 取模块被 RST 的问题。

**Steps:**
1. 给 bundle 加 `README.md` 作为入口（原本没有，解开第一眼是按 5 条那版写的 RUNBOOK）- success
2. 独立验证 README，挖出 3 个阻断级问题 - success
3. 修 14 条缺陷（含 ARM deno 架构改写）- success，独立复核 14/14
4. `run_batch.py` 加 `--jobs` 并发 + 批次心跳 - success
5. 独立验证并发，挖出「日志非实时」「Ctrl-C 启动窗口留孤儿容器」两条 - success
6. 修上述两条 + trial 内进度节流 - success
7. `build_arm.sh` 加 `--goproxy` / `--gosumdb` / `--godebug` - success
8. 端到端保真度回归（真实容器，改动前后 4 轮对照）- success

**Key Findings:**
- **README 把预检排在建镜像之前是错的**：`preflight.sh` 对每个缺失镜像打 `bad`，
  新机器 113 个全缺 → `exit 1`；且无镜像时活体测试整段跳过。正确顺序是先建后检
  （`build_arm.sh` 自己结尾的提示本来就是「下一步 preflight.sh」）
- **两个脚本的目标分隔符相反**：`build_arm.sh` 吃空格、`run_batch.py --only` 吃逗号。
  `run_batch.py` 打印的建镜像命令用逗号拼，照抄必失败
- **`logs/<trial>.log` 不是实时的**（`open(log,"w")` 块缓冲）——而并发模式下它是唯一
  排查通道。实测：整条 10 分钟的 trial 全程 0 字节，进程关闭才落盘。
  **两层都要修**：父侧 `buffering=1` + 子进程 `PYTHONUNBUFFERED=1`，缺一不可
- **串行模式号称的「实时刷屏」也是假的**：真 pty 实测，旧版 14s 内第一行在 +10.2s
  才出现（子进程退出时一次性吐出），新版 +0.2s
- **Ctrl-C 在容器启动窗口会留孤儿**：`replay.py` 的 `try:` 在 594 行，而主容器 530 行
  就起来了。而且**扩到「`sh(run)` 返回之后」仍然漏** —— `docker run -d` 在调用返回前
  就已在 daemon 里建好容器，必须扩到 `sh(run)` **之前**（容器名提前算好，`rm -f` 对
  半路被杀的 `docker run` 依然有效）
- **`i % N` 的进度打印覆盖不到卡死**：卡住时恰恰是 `i` 不增长。必须再加一条
  **按时间**的下限（30s 无输出就强制打一行，含卡在哪条命令、多久）
- **并发的真正上限不是 CPU 是内存**：`--cpus` 是 CFS 配额，超配只变慢；`--memory` 超配
  会 OOM kill，而被 OOM 的容器表现成「命令莫名失败」，极难与真实 task 失败区分
- **并发超过 `核数÷2` 会制造假失败**：命令被拖慢撞 `timeout -k 5 30`，若被砍的命令改了
  文件则 `patch_identical` 假失败。`INDEX.md` 已记录上一轮 rust/ts/js 三条受并发污染
- **Go 取模块用 HTTP/2（实测 `resp.Proto = HTTP/2.0`），wget 用 1.1** —— 一度以为是
  中间设备掐 h2，但**用户反馈 rust 能正常编译**（cargo 同样走 h2 且在同一容器内），
  证伪了该假设。指向 `proxy.golang.org` 域名被单独阻断
- **保真度回归结论**：改动前后 `patch_identical` 完全一致，rc 不匹配的命令下标集合也
  逐个相同。typescript 那条的 `false` 是**本机既有损伤**（改动前的 HEAD 代码同样复现）：
  宿主比基线机慢约 6 倍，12 条连续 `npx` 全部撞 30s 超时，`prettier --write` 被砍导致
  生成的 `.d.ts` 格式不同、差 630 字节

**Files Changed:**
- `crosslang/README.md` - 新增，bundle 的操作说明（主文档）
- `crosslang/RUNBOOK.md` - 改为参考手册；删与 README 重复且过时的 §0/§1
- `crosslang/build_arm.sh` - `--goproxy`/`--gosumdb`/`--godebug`；deno 架构改写；用法头
- `crosslang/run_batch.py` - `--jobs`/`-j auto`、心跳、日志行缓冲、汇总记未跑条数
- `crosslang/preflight.sh` - `--metrics` 门控 cgroup 检查；镜像缺失降为 warn；口径 113
- `crosslang/check_sources.sh` - 补探 nodesource/mongodb/jsr.io；口径 113
- `replay.py` - try/finally 扩到容器创建之前；`ProgressTicker`（30s 静默下限）
- `EXEC_LOG_2026-09-09-bundle-fixes.md` - 新增

**Commit:** 见 git log

## 2026-09-11: ARM topdown 采集包（单条 trial）

**Goal:** 在 baremetal ARM 服务器上重放 1 条 trial，同时宿主侧按 cgroup 过滤采 PMU 事件，
算出 ARM L1 topdown 四象限；打成自包含 tarball。事件号完全解耦到配置文件，脚本不硬编码。

**Steps:**
1. 读现有约定（`make_bundle.sh` / `preflight.sh` / `build_arm.sh` / `replay.py`）- 完成
2. 新增 6 个文件：`topdown.conf` / `probe_pmu.sh` / `topdown_trial.sh` / `topdown_parse.py`
   / `TOPDOWN.md` / `make_topdown_bundle.sh`（**未改 `make_bundle.sh`**，113 条主包路径不冒险）- 完成
3. x86 上能跑的全部实跑验证（语法 / 解析器正反例 / 假 sysfs+假 perf 的全流程 / 失败路径
   / 中断路径 / 真打包解开核对）- 完成，暴露并修掉 5 个真 bug
4. 打包产出 `crosslang/deepswe-topdown-bundle-20260911.tar.gz`（280KB / 19 文件）- 完成

**Key Findings:**
- **`docker exec` 采不到**：真实进程是 containerd-shim fork 的，不在 perf 子进程树里。
  只能 `perf stat -a -G <cgroup>` 系统级采样 + cgroup 过滤。采集窗口用
  `-- tail --pid=<replay PID> -f /dev/null` 划定，无竞态、不用折腾信号
- **`-G` 路径推错时 perf 不报错，只给一串 0** —— 这是整套里最隐蔽的失败模式，所以
  `probe_pmu.sh` 必须做活体验证（真起容器、真烧 CPU、真采一次）而不是查配置
- **SLOTS 绝不能硬编码**：它是四象限的公共分母（V1=8 / N2=5），写死后换机器会让四个比值
  一起按同一比例静默偏移，每一项看着都还正常。唯一兜底是「四象限求和 ≈ 1」自检
- **baremetal 上的头号风险是计数器余量，不是 vPMU**：NMI/hardlockup watchdog（perf 版）
  常驻占一个通用计数器，6 变 5，而 L1 正好要 5 个 —— 加一个 `EV_EXTRA` 就必然复用，
  而复用的现象与「事件号写错」一模一样，极易把排查带偏
- **后台任务的 SIGINT 被 shell 置成 SIG_IGN**（非交互 + 无作业控制，且被 exec 继承）：
  `kill -INT` 对后台 replay.py 完全无效，只能靠 `set -m` 让它拿到独立进程组
- **`kill -0` 判不出僵尸**，清理逻辑会白等满超时再补 SIGKILL
- **`command -v perf` 成功 ≠ perf 能用**：Debian/Ubuntu 的 `/usr/bin/perf` 是按 `uname -r`
  找真身的 wrapper，包没装时它照样在（本机 WSL2 就是这个情况）
- **perf CSV 加 `-G` 后多一列 cgroup，各版本插入位置不一致** → 解析一律按事件的 `name=`
  匹配再按「形状」取值，列号一次都不用；`-j` 的输出是逐行 JSON 对象，不是数组
- **这一版是整条 trial 的聚合值**，覆盖容器启动 + 98 条命令 + 收尾 git diff；
  sidecar 独立 cgroup 天然滤掉；per-command 归因是后续工作
- **PMU 数字跨架构不可比**：判定环境等价的仍然只有 `patch_identical`

**Files Changed:**
- `crosslang/topdown.conf` - 新增，唯一需要用户改的文件（PMU/SLOTS/5 个事件号/EV_EXTRA/输出格式）
- `crosslang/probe_pmu.sh` - 新增，目标机第一件事：事件号有效性 + 计数器余量 + 活体验证 `-G`
- `crosslang/topdown_trial.sh` - 新增，采集主脚本
- `crosslang/topdown_parse.py` - 新增，perf 输出 → 四象限 + 两个自检 + `topdown.json`（纯标准库）
- `crosslang/TOPDOWN.md` - 新增，包的入口文档
- `crosslang/make_topdown_bundle.sh` - 新增，打包脚本（与 `make_bundle.sh` 并列，互不影响）
- `crosslang/EXEC_LOG_2026-09-11-topdown.md` - 新增，本轮执行日志

**Commit:** pending
