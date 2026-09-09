# Task Execution Log: typescript 镜像卡在 pnpm install → 换源开关

**Date:** 2026-09-09
**Goal:** 定位 typescript 镜像构建卡死的原因，并给 `build_arm.sh` 加一个保真度安全的换源开关。

## 起因

服务器上重建 typescript 镜像时卡在 `pnpm install`，最后一行日志
`resolved 548, downloaded 508`。

## Problems to Solve
1. 卡住的是哪一条 task，是镜像定义的问题还是环境的问题
2. 到底是「慢」还是「死锁」，判据是什么
3. 换源换在哪儿才既生效又不污染运行期镜像
4. 换源是否真的会更快

## 已确立的事实（本轮全部实测，非推断）

### 1. 卡住的是 `true-myth-iterable-collection-combinators`

`548` 是指纹。本机 9/8 那次成功构建的 `crosslang/build/typescript/build.log`
第 38 行就是 `Packages: +548`，且 TAG `kh74r2t7kdnt7h2efdk0hf5asx82zr0s-v1.1`
与 `data/tasks_extracted/true-myth-iterable-collection-combinators/task.toml`
里的 `docker_image` 逐字相同。

**同一个 Dockerfile 在本机 132s 建完过 → 问题不在镜像定义，在网络。**

`downloaded 508` 落在成功那次的 `486 → 509` 之间（t≈108s / 总 132s）。
剩余 40 个正好是尾部最大的一批：shiki 185K · oniguruma-to-es 269K ·
@shikijs/themes 169K · @vue/compiler-core 140K · regex 139K 等
（vitepress + shiki + vue 文档栈）。

### 2. 瓶颈是每请求固定开销，不是带宽

从成功那次的 29 条 `Tarball download average speed` WARN 反推耗时：

| 包 | size | 耗时 |
|---|---|---|
| `@nodelib/fs.stat` | 4 KiB | 8.0 s |
| `oniguruma-to-es` | 269 KiB | 5.6 s |

体积差 67 倍，耗时反而小的那个更久。29 条整体落在 1~11 s，均值 5.4 s，
**与 size 基本不相关**。带宽受限会呈现「小文件快、大文件慢」，这里没有。
29 条总量仅 2 MB。

→ 结论：延迟受限（RTT + TLS 握手，很可能是内网 TLS 中间人代理），不是带宽受限。

**限定**：pnpm 只在 <50 KiB/s 时打 WARN，所以这 29 条是 548 个里最慢的尾巴，
不代表全体。但当前卡住的位置正好在这条尾巴上。

**推论**：先前建议的「降低 network-concurrency 到 4~6」方向是反的 ——
那是带宽争抢的对策；延迟受限下应当**提高**并发让固定开销重叠。

### 3. 换源换在哪儿（三个工具三个地方）

| 工具 | 读什么 |
|---|---|
| `npm` | `.npmrc`（项目→用户→全局）+ `NPM_CONFIG_*` 环境变量；优先级 CLI > **env** > 项目 `.npmrc` |
| `pnpm` | 同上，复用 npm config 体系 |
| `corepack` | **只认 `COREPACK_NPM_REGISTRY` 环境变量，完全不读 `.npmrc`**（3 条 task 用 corepack：ofetch / query / valibot） |

基座 `mars-base:arm64` 里 `/root/.npmrc`、`/usr/etc/npmrc`、`/etc/npmrc`
**一个都不存在**（`npm config get globalconfig` → `/usr/etc/npmrc`，空位），
默认源 `https://registry.npmjs.org/` 来自内建默认。
→ **没有原值需要备份，撤掉变量就自动回默认。**

### 4. 注入途径：只有 `ARG` + `--build-arg` 是干净的

| 写法 | 构建期生效 | 留进 `Config.Env` |
|---|---|---|
| 宿主机 `export` | ❌ 空 | — |
| Dockerfile `ENV` | ✅ | ❌ 永久残留 |
| `ARG` + `--build-arg` | ✅ | ✅ 零残留 |

实测输出：

```
① 只在宿主 export，不传 --build-arg：  RUN 里看到的值 => [（空）]
② 裸 --build-arg NPM_CONFIG_REGISTRY：  RUN 里看到的值 => [https://registry.npmmirror.com]  ← 从宿主继承
③ --build-arg NPM_CONFIG_REGISTRY=<值>：RUN 里看到的值 => [https://explicit.example.com]
```

`ARG` 路线的运行期实测（普通 `docker run`，不带 build-arg）：

```
✅ Config.Env 里零残留
npm -> https://registry.npmjs.org/
COREPACK_NPM_REGISTRY -> (unset)
/root/.npmrc 不存在
```

**为什么代理不用写 ARG 而 registry 必须写**：docker 有预定义 build-arg 白名单
（`HTTP_PROXY`/`HTTPS_PROXY`/`FTP_PROXY`/`NO_PROXY`/`ALL_PROXY` 及小写），
`NPM_CONFIG_REGISTRY` 不在其中。实测：

```
HTTPS_PROXY(预定义,未声明ARG)  => [http://proxy.example:8080]   ← 通了
NPM_CONFIG_REGISTRY(未声明ARG) => [（空）]                       ← 传了也白传
```

### 5. 保真度

- **tarball 内容不会漂**：pnpm 按 lockfile 的 sha512 integrity 校验，
  npmmirror 是官方源全量同步，字节一致才过得去；不一致会直接报错，不静默。
- **解析可能会漂，但只影响一部分**：卡住这条 true-myth 的 log 第一行是
  `Lockfile is up to date, resolution step is skipped`，纯按 lockfile 取包，
  换源零风险。真会做全量 resolution 的是 `obsidian-linter` ×3
  （只有 `package-lock.json`、没有 `pnpm-lock.yaml`），需单独核对。
- 扫过 113 份 Dockerfile，只有 `ink-grid-box-layout` 提到 `.npmrc`，
  且是其仓库自带的 `package-lock=false`，不涉及 registry，
  **无 scoped registry 覆盖冲突**。

### 6. 未决

**换源到底会不会更快，没有验证。** 取决于那 5.4 s 花在哪：

- 情形 A：RTT + TLS 握手（到 npmjs.org 路由远）→ 域内镜像线性收益，换源有用
- 情形 B：公司代理自身开销（TLS 重签 + 内容扫描）→ 换目标域名毫无作用

判据（在服务器上、带构建同一套代理环境变量跑）：

```bash
for h in registry.npmjs.org registry.npmmirror.com; do
  echo "--- $h ---"
  for i in 1 2 3; do
    curl -sS -o /dev/null \
      -w "  connect=%{time_connect}  tls=%{time_appconnect}  ttfb=%{time_starttransfer}  total=%{time_total}  %{speed_download}B/s\n" \
      "https://$h/minimatch/-/minimatch-9.0.5.tgz"
  done
done
```

两个 host 的 `total` 差不多 → 情形 B，换源白搭。

## Steps Log

### Step 1: 定位 + 机制实测
- **Status:** success
- **Result location:** 本文件上方「已确立的事实」1~5 节
- **Success result:** 卡点定位到 true-myth；瓶颈判定为延迟受限；
  换源注入途径三选一实测完毕，确认 `ARG` 路线零残留

### Step 2: 给 build_arm.sh 加 `--registry`
- **Status:** in_progress
- **Result location:** `deepswe/crosslang/build_arm.sh`

### Step 3: 独立验证
- **Status:** success（静态 5/5 通过 + 真实端到端构建通过），但挖出 3 项与预期不符
- **Result location:** 见下

**通过的部分：**
- `bash -n` 通过；`ARG` 两行在三种组合（裸 / +`--ca-cert` / +`--insecure`）下位置都对：
  `FROM`(1) < `ARG`(12,13) < 第一条 `RUN`(15/18/17)
- `--build-arg` 确实传到了 `docker build`（用 PATH shim 截获命令行验证）
- 不带 `--registry` 时，生成的 Dockerfile 与改动前**逐字节相同**（与 `git show HEAD:` 对比）
- 真实构建 `regverify:t` 成功（309s），运行期纯净性全部通过：
  `Config.Env` 无残留、`npm config get registry` 回到官方源、四处 `.npmrc` 均不存在、
  **离线报错文本与对照镜像逐字节一致**
  （`ERR_PNPM_META_FETCH_FAIL … getaddrinfo EAI_AGAIN registry.npmjs.org`）
- 镜像源确实生效：日志里 tarball URL 全是 `registry.npmmirror.com`

**❗ 发现 1：换源实测更慢，与立项假设相反**

| | `pnpm install` | 低速 WARN | 速度区间 |
|---|---|---|---|
| 官方源 | **132.4 s** | 29 | 0~48 KiB/s |
| 镜像源 | **156.6 s**（+18%） | 38 | 3~47 KiB/s |

同仓库、同 548 个包。与本文档「延迟受限」的分析自洽——瓶颈不在目标主机。
非完美对照（官方源那次走代理、镜像源那次走直连，见发现 2），但**直连都更慢**，
说明镜像源在这台机器上确实无优势。
→ 已把 RUNBOOK 该节从「解法」改写为「测过确实有用才开的开关」，并把这组数字写进去。
**服务器上的结论可能不同，必须按 §2b 第三步先测。**

**❗ 发现 2：`build_arm.sh` 在 Docker Desktop + WSL2 上按现状跑不通（先于本次改动存在）**

宿主代理挂在 `127.0.0.1:7887` 时脚本自动加 `--network host`，但**这套 BuildKit 不兑现
`--network host`**，构建容器仍连不到宿主 loopback →
`fatal: unable to access …: Failed to connect to 127.0.0.1 port 7887`，`git clone` 即失败。
与 `--registry` 无关（不加该参数也一样）。→ 已写进 RUNBOOK §2b 代理一节作「已知问题」+ 绕法。

**❗ 发现 3：`node_modules/.modules.yaml` 会留下镜像站 URL**

镜像源建出来的镜像里该文件是 `default: https://registry.npmmirror.com/`，
官方源是 `.../registry.npmjs.org/` → **镜像内容字节不同**，且两条自检都查不到这里。
实测后果良性（pnpm 解析新包读自己的配置而非该文件，离线报错文本逐字节一致）。
→ 结论修正为：「**行为**不受污染」成立，「字节完全相同」**不**成立。已写进 REWRITES.md 漂移一节。

**次要（未修，均为先于本次改动存在的通病）：**
- `--registry` 不带值 → `$2: unbound variable`，与 `--base`/`--proxy`/`--ca-cert` 同款
- 已修：`--registry` 原本漏在脚本用法头注释里

### Step 5: 提交 + 打包
- **Status:** success
- **Result location:** commit `4f2451e`；`deepswe-replay-bundle-20260909.tar.gz`

### Step 6: 给 bundle 加 README（入口文档）
- **Status:** success（待独立验证）
- **Result location:** `deepswe/crosslang/README.md`（237 行），commit `d9cebba`

**动手时发现的真问题**：包里原本没有入口文件，解开后第一眼读到的是 `RUNBOOK.md`，
而它的 §0/§1 还是按上一版**5 条**那个 bundle 写的：

- 「把 5 条已验证的 trial 在服务器上重跑一遍」
- 「`python3 run_batch.py` 正式跑，约 15 分钟」
- 「唯一的通过标准是 5 条全部 `patch_identical=true`」
- 文件树里写 `<trial>/ 5 条`、`replay/verdict.json 上一台机器的基线判定`

这四条在 113 条这版**全都不成立**，而且恰好是任何人打开包最先读到的两节。

**处置**：
- 新建 `README.md` 作为**操作说明本身**（用户明确要求：README 指导如何使用 bundle，
  其余文档都是辅助）。结构是一条完整路径：前置条件 → 解包核对 → 预检 → 建镜像 →
  重放 → 读结果；外加选择性构建语法、规模与预算表、三个必须先知道的坑
  （无基线 / 重建镜像≠原镜像 / `--registry` 实测更慢）、故障速查表、文件清单
- `RUNBOOK.md` 删掉与 README 重复且过时的 §0/§1（51 行 → 26 行），换成
  「你想知道什么去第几节」的路标表；§2~§7 排查内容原样保留
- 又修掉 RUNBOOK 其余三处按 5 条写的地方：§5.1「5 条应当全为 true」→ 113 条
  并补「重建镜像下 `patch_identical` 失败不一定是流程坏了」；
  §4 命令示例补 `--skip-missing` 并说明它是目前「只跑哪几条」的唯一办法；
  §4 耗时表标注清楚是**已不在包内**的 5 条对照组的实测
- `make_bundle.sh` 的文件清单加 `README.md`

**顺带确认的一个能力缺口**（用户问「能不能指定跑哪几个」）：

| 脚本 | 阶段 | 选择粒度 |
|---|---|---|
| `make_full_trials.py` | 装配 | 只能按语言 `--only` |
| `build_arm.sh` | 建镜像 | **语言 / trial 目录名前缀 / all，可并列多个**（`build_arm.sh:88-97`）|
| `run_batch.py` | 重放 | **只能按语言**（`run_batch.py:123,147-149`）|

即构建端能精确到单条，重放端不能。当前只能靠「只建哪几条镜像」+ `--skip-missing`
间接实现，或直接调 `replay.py` 跑单条。**给 `run_batch.py` 加同款前缀匹配尚未做。**

### Step 7: 独立验证 README
- **Status:** in_progress
- **Result location:** 独立 agent，从 tarball 解包后逐条核对命令 / 数字 / § 引用

## 未做的待办（已向用户提出，未获选择）

1. **pnpm store 的 BuildKit cache mount** —— 省的是请求本身，不管瓶颈在 RTT
   还是在代理都有效；35 条 typescript 里 koota×4 / obsidian-linter×3 / happy-dom×2 /
   dynamodb-toolbox×2 同仓库。按本轮实测，**大概率比换源有用得多**
2. **`docker build` 加超时** —— `build_arm.sh` 的 `docker build` 无超时，
   113 个镜像通宵跑，一条挂死整批停摆
3. **给 `run_batch.py` 加 trial 前缀匹配** —— 与 `build_arm.sh` 的选择语法统一

### Step 4: 打包（113 条）
- **Status:** in_progress
- **Result location:** `deepswe/crosslang/full_trials/` + `make_bundle.sh` 产物

**范围变更（用户决定，2026-09-09）**：去掉 5 条已验证对照组，只打 113 条。

装配用 `make_full_trials.py --no-verified`（脚本本来就有这个开关）：

| 语言 | trial 数 | 命令数 |
|---|---|---|
| typescript | 35 | 1620 |
| python | 34 | 1387 |
| go | 34 | 1039 |
| rust | 5 | 316 |
| javascript | 5 | 157 |
| **合计** | **113** | **4519** |

113 条 / 113 个镜像 / 4519 条命令，一个 task 一条 trial 一个镜像，三者一一对应。
（原 118 条 = 113 + 5，那 5 条与已有 task 共享镜像，所以镜像数始终是 113。）

**已明确告知并被接受的代价**：113 条里**没有一条有已验证基线**——那 5 条是更早一轮
跨模型取样、本地已跑通且 patch 逐字节核对过的，两批 trial 不是同一次运行。
去掉之后 `run_batch.py` 每条都打「无基线」，`vs_baseline` 恒为 `None`，
**某条失败时无法区分「这个 task 有问题」和「整套流程有问题」**。
`run_batch.py` 本身对缺基线是优雅降级的（`cmp_baseline` 返回 `None`），不会报错。
要恢复：重跑 `make_full_trials.py` 不加 `--no-verified`。

**连带改的文档**（都是原本按 118 写的）：
- `RUNBOOK.md` §7 标题与 §7.1 全文、§7.3 重放时间（2818→2583 条命令、4891→4519）
- `RUNBOOK.md` §5.3 加了醒目提示：**当前包里一个 `verdict.json` 都没有**
- `RUNBOOK.md` §6.5 加交叉引用（重放卡住 ≠ 构建卡住）、§7.2 命令示例加 `--registry`
- `RUNBOOK.md` §2b 新增「取包慢:换镜像源 `--registry`」101 行
- `run_batch.py` 文档字符串、`make_bundle.sh` 用法注释
- `.gitignore` 补 `deepswe/crosslang/full_trials/`（原有规则只匹配一层深）

## 另有一处待办（本轮未做）

`build_arm.sh:311` 的 `docker build` **没有超时**。113 个镜像通宵跑，
一条挂死整批停摆。建议后续补 `timeout` + 失败清单重试。
