# 服务器重放参考手册

**操作步骤看 `README.md`，本文件是参考手册** —— 每个坑的成因、判据、绕法，
以及各项数字是怎么测出来的。按需跳读，不用通读。

| 你想知道 | 去 |
|---|---|
| 怎么一步步跑完 | **`README.md`** |
| 前置条件的细节 | §2 |
| 拉不到镜像怎么办（重建路线） | §2b |
| 代理 / 证书 / 换源 | §2b 的三小节 |
| 预检在检什么 | §3 |
| 怎么跑、有哪些开关 | §4 |
| 结果怎么读、什么算通过 | §5 |
| 出错了 | §6 |
| 全量集的构成与成本 | §7 |

硬标准只有一条：**`patch_identical=true`**。其余数字都是参考，
允许有出入的部分见 §5.2。

默认**不采 cgroup 性能指标**（打通阶段用不上）。这顺带去掉了「必须 cgroup v2 且
宿主侧目录可读」这条硬约束——rootless docker、受限容器、cgroup v1 的机器都能跑。
要采时加 `--metrics`。

---

## 2. 前置条件

| 项 | 要求 | 不满足会怎样 |
|---|---|---|
| docker | 能起容器，当前用户有权限 | 直接跑不了 |
| cgroup v2 | 仅 `--metrics` 时需要 | 不采指标就完全不碰 cgroup；采时缺它 `replay.py` 明确报错退出，不会静默写 0 |
| python3 | ≥3.8，标准库即可 | 跑不了 |
| 113 个镜像 | 本轮 ECR 拉不到，走 `build_arm.sh` 从本地基座**重建**（见 §2b 路 C） | 缺的那条跑不了；不加 `--skip-missing` 时整批拒绝启动 |
| 磁盘 | ≥20 GB | 重放中途写满 |

镜像**必须预先备好**——本轮是用 `build_arm.sh` 重建的，不是 `docker pull`（ECR 拉不到，
见 §2b）。`replay.py` 默认拒绝在镜像缺失时启动，因为实测出口吞吐只有 **0.27 MB/s**，
误触一个 ~800 MB 的镜像就是几十分钟。113 个镜像不可能一次建齐，所以批量跑一律带
`run_batch.py --skip-missing`（缺的自动跳过而不是整批拒绝启动）；`preflight.sh` 和
`run_batch.py --dry-run` 都会把还缺哪些列出来。

## 2b. 拉不到 registry 时(ECR / DockerHub 不通)

重放执行本身**零网络**,所以 registry 不通不影响跑,只影响"怎么把镜像弄到本地"。
三条路:

| 路 | 前提 | 代价 |
|---|---|---|
| A. `docker pull` | 能连 ECR | — |
| B. `docker save` \| zstd → 搬文件 → `docker load` | 能物理搬文件 | 所有镜像共享同一个基座,所以**必须把要搬的 tag 一次性 `docker save`**——分批打会把基座重复传 N 遍。上一轮 5 个镜像实测:一次打 690 MB(zstd),分 5 次打涨到约 2.4 GB。113 个的体量没实测过 |
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

各条需要的源。⚠️ **下表只是 5 个例子,不是全集**——它是上一轮那 5 条对照组的样子,
留在这里是为了说明「不同语言的依赖面差多少」。全量 113 条的上游主机集合更大:
除了下面这些,还有 `deb.debian.org`(装系统包的 5 条)、`deb.nodesource.com` 与
`repo.mongodb.org` / `www.mongodb.org`(eicrud 那条)、`jsr.io` 与 github 的 release
下载域(cliffy 那条)。**以实际探测为准:`check_sources.sh` 已经把这些全打一遍。**

| task(示例) | 需要 |
|---|---|
| python (returns) | github + pypi ← 依赖最少,且**唯一不需要装报告器**的 |
| go (actionlint) | github + proxy.golang.org + sum.golang.org |
| js (yjs) | github + npmjs(`npm ci`,锁定) |
| ts (true-myth) | github + npmjs(`pnpm install`,**未锁定**) |
| rust (fd) | github + crates.io + get.nexte.st + npmjs ← 最难,最后建 |

**ARM 上的两处硬改写**,`build_arm.sh` 在基座是 arm64 时自动做掉:

1. **cargo-nextest**——**5 条 rust 全部**(boa / fd / oxvg / pest / wasmi)的 Dockerfile
   都写死 `get.nexte.st/${VER}/linux`,那是 x86_64 产物,自动改成 `/linux-arm`。
   (实测确认:`/linux` 8.2 MB、`/linux-arm` 6.7 MB 都存在,`/linux-arm64` 是 404。)
2. **deno**——`cliffy` 那条写死
   `.../deno/releases/download/v2.0.0/deno-x86_64-unknown-linux-gnu.zip`,
   自动改成 `deno-aarch64-...`(2026-09-09 实测该资产在 v2.0.0 存在,HTTP 206)。
   这条尤其要紧:装错架构的 deno **不会在下载时报错**,要拖到紧接着的
   `RUN deno cache` 才 `exec format error`,不知道的话很难认。

### 代理:构建期要,运行期绝不能有

**`docker build` 不继承 shell 里 export 的 `http_proxy`/`https_proxy`。** 实测过:
shell 里设了,构建容器内仍是"未设置",`git clone` 直接失败。必须显式 `--build-arg`。

`build_arm.sh` 会自动从环境变量读并显式传:

```bash
export HTTPS_PROXY=http://proxy:port NO_PROXY=localhost,127.0.0.1
bash build_arm.sh python
# 或者： bash build_arm.sh --proxy http://proxy:port python
```

三个细节:

1. **用的是 docker 预定义 build-arg**(`HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 及小写),
   无需在 Dockerfile 里声明 `ARG` 就能注入,而且**不会写进 image config 的 Env**——
   实测确认过。所以重建镜像依旧干净,运行期不受污染
   (原始 mars-base 的 Env 里本来也是零个 `*_proxy`)。构建完脚本还会再自检一次,
   有残留就判失败。
2. **代理挂在 loopback 上时自动加 `--network=host`**:构建容器内的 `127.0.0.1`
   是它自己,连不到宿主的代理。`--build-network` 可覆盖。

   ⚠️ **已知问题(2026-09-09 实测)**:在 Docker Desktop + WSL2 这套 BuildKit 上
   **`--network host` 不被兑现**,构建容器仍然连不到宿主 loopback,表现为
   `fatal: unable to access …: Failed to connect to 127.0.0.1 port 7887`,
   `git clone` 那一步直接失败——也就是说**这种环境下 `build_arm.sh` 按现状是跑不通的**。
   绕法:若构建容器本来就能直连外网,把代理相关参数全去掉再跑;
   否则把代理换成一个非 loopback 地址(宿主在 docker 网桥上的 IP,或局域网地址)。
   原生 Linux Docker 上未复现此问题。
3. 日志里代理的 `user:pass@` 会被抹成 `***@`。

**运行期不需要代理**,也不该有:重放容器是 `--network=none` + 403 sinkhole。
唯一的隐患是 `~/.docker/config.json` 里的 `proxies` 段——docker 会把它**自动注入
每个 `docker run`**。`replay.py` 已显式覆盖 `HTTP(S)_PROXY` 和 `NO_PROXY` 指向
sinkhole 压住它;`preflight.sh` 也会检查并提示。若重放时外连报错不是 403 而是
连接失败,先查这里。

### 证书:公司内网做 TLS 中间人时

症状是 `git clone` 报 `server certificate verification failed`。公司代理用内网 CA
重签了所有 HTTPS,宿主机通常已被 IT 装好这张 CA(所以宿主上 curl 是通的),
**但容器里没有**。

**第一步:把 CA 抠出来**

```bash
bash get_ca_cert.sh          # → ./corp-ca.crt
```

两条路都试:从宿主系统信任库里挑本地额外添加的 CA(最可靠,就是 IT 装的那张);
拿不到就从一次真实 TLS 握手抓证书链。产出后会**用它重试一次 HTTPS 验证是否真的可用**。

**第二步:装进镜像**

```bash
bash build_arm.sh --ca-cert corp-ca.crt python
```

装 CA 的逻辑分三块,**缺一不可**——各工具的信任源并不一致(实测):

| 工具 | 信任源 | 靠什么生效 |
|---|---|---|
| git / curl / go / cargo | `/etc/ssl/certs/ca-certificates.crt` | `update-ca-certificates` |
| **node / npm / pnpm** | 内置 146 张根证书,**不读系统 bundle** | `NODE_EXTRA_CA_CERTS` |
| **python / pip** | certifi 自带 `cacert.pem`,**不读系统 bundle** | `PIP_CERT` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` |

所以只跑 `update-ca-certificates` 的话,**git clone 会过,npm 和 pip 照样失败**。
`build_arm.sh` 生成的 Dockerfile 三块都插:

```dockerfile
COPY corp-ca.crt /usr/local/share/ca-certificates/corp-ca.crt
RUN update-ca-certificates                      # 1 added, 0 removed
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=... PIP_CERT=... REQUESTS_CA_BUNDLE=... CARGO_HTTP_CAINFO=...
```

那几个 ENV 会留在镜像里,但值指向系统 bundle——是「信任库更全」,不是「不再校验」,
与下面 `--insecure` 的残留性质完全不同。

**退路:`--insecure`**

```bash
bash build_arm.sh --insecure python
```

关掉 git/curl/pip/npm/go 的证书校验,并在构建末尾**还原**(用文件配置而非 ENV,
就是为了能还原;ENV 进了 image config 就删不掉)。构建后脚本会真起一个容器复核
是否还原干净。

**但 cargo 没有 insecure 开关**,只认 `CARGO_HTTP_CAINFO` 指向的 CA 文件——
所以 rust 那条无论如何都得走 `--ca-cert`。

### 取包慢:换镜像源 `--registry`

症状是 `pnpm install` / `npm install` 挂在那里不动,日志停在
`Progress: resolved 548, downloaded 508` 这种行上。

> 🚨 **先读这段再决定要不要换源。2026-09-09 在开发机上做过一次对照实验,
> 结论是换源更慢:**
>
> | | `pnpm install` 耗时 | 低速 WARN 条数 | 速度区间 |
> |---|---|---|---|
> | 官方源 `registry.npmjs.org` | **132.4 s** | 29 | 0~48 KiB/s |
> | 镜像源 `registry.npmmirror.com` | **156.6 s**（+18%） | 38 | 3~47 KiB/s |
>
> 同一个仓库(true-myth)、同样 548 个包。两边撞的是同一堵墙。
> 这与下面「第二步」的延迟受限分析一致——**瓶颈不在目标主机,换主机自然没用**。
>
> ⚠️ 这不是完美对照:官方源那次走公司代理,镜像源那次因为代理故障(见 §2b 代理一节
> 的已知问题)走的直连。直连都还更慢,说明镜像源在这台机器上确实没优势。
>
> **所以 `--registry` 是一个「测过确实有用才开」的开关,不是默认解法。**
> 换机器、换网络结论可能不同——务必按下面第三步先测。

**第一步:先判「慢」还是「死」,别急着改东西**

pnpm 的 `Progress:` 行**只在计数器变化时才打**(实测:时间戳 18.47/27.70/32.19/33.56
明显不等间隔)。所以:

```bash
tail -f build/<trial>/build.log      # 超过 5~8 分钟一行不动,才是真挂了
cat /proc/net/dev; sleep 60; cat /proc/net/dev   # 更硬:RX 还涨不涨
```

RX 还在涨(哪怕几十 KB/min)就是在爬,等着就行。

**第二步:判瓶颈是带宽还是延迟**

从 `Tarball download average speed` 的 WARN 行反推每个包的耗时(`size ÷ speed`)。
2026-09-09 从 true-myth 那份成功日志的 29 条 WARN 里算出来:

| 包 | size | 耗时 |
|---|---|---|
| `@nodelib/fs.stat` | 4 KiB | 8.0 s |
| `oniguruma-to-es` | 269 KiB | 5.6 s |

体积差 67 倍、耗时反而小的那个更久;29 条整体落在 1~11 s,均值 5.4 s,**与 size 无关**。
带宽受限会呈现「小文件快、大文件慢」,这里没有。→ **延迟受限**(RTT + TLS 握手)。

推论:这种情况下 `network-concurrency` 应当**调高**而不是调低——调低是带宽争抢的对策。

**第三步:换源之前先测它值不值**

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

**必须带着构建用的同一套代理环境变量跑。** 两个 host 的 `total` 差不多 → 瓶颈是
公司代理自身(TLS 重签 + 内容扫描),换目标域名毫无作用,**换源白搭**;npmmirror
明显低 → 换源有用。

**用法**

```bash
bash build_arm.sh --registry https://registry.npmmirror.com typescript
# 或者： export DEEPSWE_NPM_REGISTRY=https://registry.npmmirror.com
```

**换在哪儿:三个工具三个地方**(实测)

| 工具 | 读什么 |
|---|---|
| `npm` | `.npmrc`(项目→用户→全局)+ `NPM_CONFIG_*` 环境变量;优先级 CLI > **env** > 项目 `.npmrc` |
| `pnpm` | 同上,复用 npm 的 config 体系 |
| **`corepack`** | **只认 `COREPACK_NPM_REGISTRY` 环境变量,完全不读 `.npmrc`** |

corepack 那条不是细节:`ofetch` / `query` / `valibot` 三条 task 用 corepack 引导 pnpm,
只设 `.npmrc` 的话它们照样走官方源。

基座里 `/root/.npmrc`、`/usr/etc/npmrc`、`/etc/npmrc` **一个都不存在**,默认源
`https://registry.npmjs.org/` 来自内建默认——所以**没有原值需要备份还原**。

**为什么用 `ARG` 而不是 `ENV` 或 `.npmrc`**(三种写法实测)

| 写法 | 构建期生效 | 留进镜像 `Config.Env` |
|---|---|---|
| 宿主机 `export` | ❌ 空 | — |
| Dockerfile `ENV` | ✅ | ❌ **永久残留** |
| `ARG` + `--build-arg` | ✅ | ✅ **零残留** |

和代理不同,`NPM_CONFIG_REGISTRY` **不在** docker 的预定义 build-arg 白名单里
(白名单只有 `HTTP_PROXY`/`HTTPS_PROXY`/`FTP_PROXY`/`NO_PROXY`/`ALL_PROXY` 及小写),
所以必须往生成的 Dockerfile 里插两行 `ARG` 声明,光传 `--build-arg` 是空值。
`build_arm.sh` 把它们插在 `FROM` 之后、第一条 `RUN` 之前。

写 `/app/.npmrc` 也不行——会弄脏工作区,撞死构建期那道 `git status --porcelain` 断言。

**构建后的自检**(`--registry` 生效时自动跑)

1. `Config.Env` 里不得有 `NPM_CONFIG_REGISTRY` / `COREPACK_NPM_REGISTRY` 残留
2. 真起一个容器跑 `npm config get registry`,必须已回到 `registry.npmjs.org`

**保真度代价**

- **tarball 内容不会漂**:pnpm 按 lockfile 的 sha512 integrity 校验,npmmirror 是
  官方源全量同步,字节一致才过得去;不一致会直接报错,不会静默。
- **解析可能会漂,但只影响一部分 task**:有 lockfile 的会打印
  `Lockfile is up to date, resolution step is skipped`,纯按 lockfile 取包,零风险。
  真会做全量 resolution 的是 `obsidian-linter` 那 3 条(只有 `package-lock.json`、
  没有 `pnpm-lock.yaml`),镜像源同步延迟理论上可能解到不同版本,**建议单独核对**。
- 扫过 113 份 Dockerfile,只有 `ink-grid-box-layout` 提到 `.npmrc`,且是其仓库自带的
  `package-lock=false`,不涉及 registry,**无 scoped registry 覆盖冲突**。

### ⚠️ 路 C 的保真度代价

**重建镜像 ≠ 原 amd64 镜像**,即使改写为零:

- 依赖版本会漂移:`pnpm install` 未加 `--frozen-lockfile`、`pip install` 未钉版本、
  `npm install -g` 只钉直接依赖。(例外:`cargo fetch --locked` 和 `npm ci` 是锁定的)
- 换架构后工具链、native 扩展、编译产物全部不同
- 基座本身是 `:latest`,不可复现

→ **对本轮这 113 条,它仍然是开放问题。**

2026-09-07 做过一次实测,但**覆盖面只有另外 5 条**:上一轮跨语言验证的对照组
(returns / actionlint / yjs / true-myth / fd),条件是**基座 arm64 重建 + qemu 模拟执行**。
在那 5 条上、那个条件下,结论是**成立**的。下表是其中 python(returns)那条的数字:

| | 那次(ARM 重建 + qemu 模拟) | 基线(amd64 原镜像) |
|---|---|---|
| `patch_identical` | **✅ 63,009B 逐字节一致** | ✅ 63,009B |
| `rc_match` | 91/98(语义 93/98) | 95/98(语义 97/98) |
| 耗时 | 541.3s | 222.6s(×2.43) |

rc 少 4 条**全部是超时类**:7 条不匹配里 6 条是 `pytest` 撞 30s 墙(qemu 下慢 2.43 倍,
边界命令被多砍几条),剩 1 条是 hypothesis 的随机性。这正是 §5.2「单侧超时(机器快慢)」
那一类,与镜像重建无关。

**那次还是在比原生 ARM 更不利的条件下成立的**——qemu 模拟更慢、超时更多,被砍的命令
依然都不改文件,所以 patch 没受影响。真机上只会更稳。

**但这结论外推不到本轮的 113 条**:换了 task、换了仓库、换了依赖树,
`pnpm install` 未锁版本这类漂移会不会咬人是**逐条**的事,5 条通过不代表 113 条通过
(两批 trial 连模型和 `model.patch` 都不是同一批,见 §7.1)。所以 `build_arm.sh`
(脚本头、每条 `REWRITES.md`、构建结束提示)和 `README.md` 一律按「待验证的开放问题」
措辞——那是对的,**本轮跑出来的 113 个 `patch_identical` 正是在回答这个问题**。

它失败时仍按老办法排查:先分清是重放流程坏了,还是镜像本身不一样——
`build/<trial>/REWRITES.md` 记着每条改写,是起点(目录名是 **trial 目录名**,
不是语言名:同一种语言有几十条,各有各的改写清单)。

## 3. 预检

```bash
bash preflight.sh                # 默认口径
bash preflight.sh --metrics      # 要采性能指标时才加
bash preflight.sh <镜像>         # 指定拿哪个镜像做活体测试
```

**必须先有镜像再跑它**——它不只查版本号，而是**真起一个容器**把 `replay.py` 依赖的
每项能力跑一遍。包里不带镜像，手里一个都没有时这段整体跳过，那次预检等于什么都没验，
所以顺序是 `build_arm.sh` → `preflight.sh`（`README.md` 的主流程图就是这个顺序）。

默认查：

- `--cpus=2 --memory=8192m` 是否真的生效（不生效跨机数字不可比）
- 镜像内有没有 `timeout` / `python3` / `git` / `sh`，`/app` 是不是 git 仓库
- `--network=container:` 共享 netns 能否用（403 sinkhole 的实现基础）
- `--network=none` 是否真的断网

加 `--metrics` 才查（与 `run_batch.py` 的 `need_cgroup` 是同一个门控，不采指标就
根本不碰 cgroup，所以默认连查都不查，更不会因此判失败）：

- cgroup v2 挂载、cpu/memory/io 控制器是否启用
- 容器 cgroup 目录能否定位（三级探测，见 §6.1）并读出 `cpu.stat`

有 ❌ 就先解决。⚠️ 可以跑，但要确认不影响你要的结论。**镜像缺失只记 ⚠️ 不记 ❌**：
边建边跑是设计好的流程（见 §7.2），不该因为 113 个没建齐就让预检 `exit 1`。

## 4. 跑

```bash
python3 run_batch.py --smoke 5 --skip-missing      # 冒烟：确认容器能起、命令能跑
python3 run_batch.py --skip-missing --keep-going   # 正式
python3 run_batch.py --only go,rust                # 只跑指定语言
python3 run_batch.py --dry-run                     # 只预检和排程
```

**全量集下 `--skip-missing` 基本是必须的**：113 个镜像不可能一次建齐，不加它
只要有一个镜像缺失整批就拒绝启动。另外它也是目前「只跑哪几条」的唯一办法——
`--only` 只认语言、不认 trial 名，所以做法是**只建那几条的镜像**再靠它跳过其余。

**串行是刻意的**：`replay.py` 采的是 cgroup 的 CPU/内存/IO，两条同时跑会互相争抢，
性能数字直接失去可比性。上一轮就因为中途并发，rust/ts/js 三条指标偏悲观、
只有 python/go 两条干净（`INDEX.md`「本轮的口径污染」）。

**冒烟模式不校验保真度**：只跑前 N 条命令，patch 天然不完整，`--smoke` 下汇总表的
「保真」列显示 `—(冒烟)`，不代表通过。

下表是**那 5 条对照组**在基线机器（2 核限额）上的实测。**本版包里已经没有这 5 条**，
列在这里只作单位成本参考——全量 113 条的估算见 §7.3。

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

113 条应当全为 `true`。**有一条 false 就说明环境与原始运行不等价，性能数字全部作废**，
先查那一条的 `logs/<trial>.log`。

⚠️ 但注意：这批镜像是 `build_arm.sh` 在服务器上重建的，不是原 amd64 镜像
（见 §2b 末尾）。`patch_identical` 在重建镜像上失败，**不一定是重放流程坏了**，
也可能是依赖漂移或架构差异。判断前先读该条的 `build/<trial>/REWRITES.md`。

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

🚫 **当前这版包（113 条）里一个 `verdict.json` 都没有**——基线只存在于 `crosslang/`
下那 5 条对照组上，而它们已按要求从包里去掉（见 §7.1）。所以下面这张表描述的是
**曾经的**随包基线，现在包里对不到；`run_batch.py` 每条都会打「无基线」，
`vs_baseline` 恒为 `None`。要恢复就重跑 `make_full_trials.py`（不加 `--no-verified`）。

以下口径在恢复对照组后仍然适用：

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

### 5.5 命令类型统计：`cmd_stats.py`

回答「这一轮里各类命令跑了多少次、花了多少时间」，分 per-benchmark / per-language / 全体三层。

**什么时候跑**：`run_batch.py` 一轮跑完（写好 `summary.json` 之后）会自动调一次，
结果落 `<输出目录>/cmd_stats/`，屏幕上只多一行 `命令统计 …/SUMMARY.md（纳入 N 条 / 排除 M 条）`。
`--dry-run` 不调；统计失败只打一行 `⚠️ 命令统计失败（不影响本批结果）…；手动重跑 …`，
**不改变 run_batch 的退出码**。手动跑：

```bash
python3 cmd_stats.py                          # 取 runs/ 下最新一轮（summary.json 的 mtime 最新，不按目录名）
python3 cmd_stats.py runs/<批次>               # collect + aggregate，输出 runs/<批次>/cmd_stats/
python3 cmd_stats.py runs/<批次> -o /tmp/stats # 指定输出目录
python3 cmd_stats.py collect runs/<批次> [-o OUT]   # 只做第一步
python3 cmd_stats.py aggregate runs/<批次>/cmd_stats # 只做第二步（只读 per_benchmark/*.json）
```

退出码：0 正常（含「一条都没纳入」）；1 自检不过；2 输入不对（没有 summary.json 等）。
依赖同目录（包里）或上一级目录（仓库里）的 `summarize_replay.py`，只用 python 标准库。

**产物**

| 文件 | 内容 |
|---|---|
| `SUMMARY.md` | 人读：纳入/排除清单与原因、全体 / 按语言 / 按 benchmark 的主要表格 |
| `manifest.json` | collect 的清单：纳入谁、排除谁（逐条原因）、分类器路径与 sha256 |
| `per_benchmark/<trial>.json` | meta + **逐条命令记录**（i / 主类别 / 细类 / program / wall_s / rc / timed_out / usage_usec）+ 分组表 |
| `per_language/<lang>.json`、`all.json` | 汇聚后的分组表，另带 `n_benchmarks` 与每个 key 的 `n_benchmarks_with_key` |
| `per_benchmark.csv`、`per_language.csv`、`all.csv` | 三层各一份长表（一行 = 一个范围里一个维度的一个 key），UTF-8 带 BOM，Excel 直接开 |

**纳入口径** —— 只统计「本轮成功跑起来」的 benchmark，四条同时满足：

1. `exit_code == 0` 且（冒烟模式 或 `patch_identical is True`）—— 与 `run_batch.py` 算 `n_pass` 的式子逐字相同；
2. 没被截断：`summary.json` 的 `options.smoke == 0`，且 `verdict.json` 里
   `n_replayed + n_skipped_sentinel == n_cmds_trace`（`replay.py --limit` 会让左边变小）。
   冒烟批次在第 1 条里算「通过」，但在这一条被排除 —— 所以 `--smoke` 批次统计出来是空的，这是对的；
3. `<trial>/commands.jsonl` 存在，每行都是合法 JSON 且带 `i / rc / timed_out / wall_s / cmd_stripped`；
4. 条数 == `verdict.n_replayed`。

不满足的进 `manifest.json` 的 `excluded`，**所有**不满足的原因都列出来（不止第一条）。
不在 `summary.json` 里的目录（比如上一轮残留的 trial 目录）一律无视。

**命令类型的两个维度**（一条命令一个标签，都取「决定主类别的那条语句」，
分类器是 `summarize_replay.classify_command_full`）：

- **主类别 / 细类**：跑测试 > 写文件 > 语法校验 > 搜索 > 版本控制 > 读文件 > 其他（优先级）。
  跨语言的口径：测试、编译/构建、类型检查、lint/格式化都归「跑测试」，细类是工具名
  （`go test` / `cargo check` / `vitest` / `tsc` / `gofmt`）；装包、查版本归「其他 › 环境查询」；
  跑脚本文件（`node x.mjs` / `tsx x.ts` / `bun run x.ts`）归「跑测试 › 复现脚本」，与 `python3 x.py` 同口径
  （`go run` / `cargo run` 细类就是它自己）；`node -e` / `node <<EOF` 与 python 内联脚本同一套判据；
  以路径或变量调用的未知程序（`/tmp/abs`、`$FD`）归「跑测试 › 运行本地程序」；
  `npx` / `pnpm exec` / `uv run` / `bash -c '…'` 看里面那个程序。
- **program**：
  - 一般程序取 basename；`python3*` → `python`，`pip3` → `pip`；
  - `python -m X` → `python -m X`；
  - git / go / cargo / npm / pnpm / yarn / bun / deno / pip / uv / poetry / rustup 带第一个位置参数
    （跳过选项，带值选项连值跳过）：`go test`、`git status`、`pnpm test`（`pnpm -F core test` 也是它）；
  - npx / bunx / uvx 带被启动的程序：`npx vitest`；
  - run / run-script / exec / x / dlx / `go tool` 再带一段脚本名或程序名：`npm run build`、`uv run pytest`；
    那一段像路径（含 `/` 或脚本扩展名）就不带：`bun run src/t.ts` → `bun run`。

**指标口径**

- `count_pct` / `wall_pct` 的分母是同一范围（这条 benchmark / 这门语言 / 全体）的总条数、总 wall_s；
- `mean_s / median_s / p90_s / max_s` 是单条命令的 wall_s。**分位数每一层都用合并后的逐条记录重算**
  （线性插值），不是下层统计值的平均；
- `cpu_s` = Σ`usage_usec`/1e6。组内**有任何一条没采**（`--no-metrics`，默认就是不采）就记 `null`
  并给出 `n_cpu_null`，不记 0、也不做部分求和；
- `n_timed_out` = replay 侧 `timed_out`（rc 124/137）；`n_rc_nonzero` 含超时。
- ⚠️ 一条命令的 wall_s 只是它自己那次 `docker exec` 的时长。agent 常把构建丢后台
  （`nohup cargo test … &` 然后下一条 `sleep 25; tail log`），后台进程的耗时会记在后面那几条
  `sleep` / `tail` 上，而分类记在发起的那一条上。

**自检**（不过就退出码 1，不出结果）：每个 benchmark 的各维度分组必须不重不漏；
aggregate 读回 per_benchmark 文件时，用逐条记录重算的分组表必须与文件里存的一致；
language 层逐 key 的 count / wall_s 必须等于该语言各 benchmark 之和，all 层必须等于各语言之和。

**分类器改动的验收材料**在 `classify_coverage/`：`python_regression.txt`（改动前后对 python jsonl 的
默认输出逐字 diff）、`COVERAGE.md`（full_trials 113 条 trace 按语言落进「其他」的比例与 top 程序，
改动前 vs 改动后），`bash classify_coverage/reproduce.sh` 可复现。
端到端自检：`python3 cmd_stats_selftest.py`（需要本机有 crosslang/*/replay/ 下的基线 jsonl）。

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

### 6.1b cgroup 探测出的路径看着不对（只在 `--metrics` 下相关）

若日志里 cgroup 目录是 `/sys/fs/cgroup/init.scope` 之类**不含容器 ID**的路径，
那是采错对象了——读的是别的进程的指标。

WSL 上实测过这个假阳性：`docker inspect .State.Pid` 给的是 dockerd 所在 namespace
里的编号，拿到宿主 `/proc` 下恰好对上了另一个真实进程，于是「读成功」却读的是
`init.scope`。**静默采错比报错危险**，所以三级探测的第一级现在会用容器 ID 复核路径，
认不出就当没读到、继续试候选路径。

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

**注意这一节说的是「重放」卡住。「构建」卡住是另一回事**——`pnpm install` / `npm install`
停在 `Progress: resolved N, downloaded M` 上不动,判定与对策见 §2b 的
「取包慢:换镜像源 `--registry`」。

## 7. 全量集（113 条）

### 7.1 它是什么

`make_full_trials.py` 把 `deepswe/data/` 里的下载态数据装配成 replay 能直接吃的布局：

```bash
python3 make_full_trials.py --no-verified        # → ./full_trials/，113 条
python3 make_full_trials.py --only go,python,javascript
bash make_bundle.sh --trials-dir full_trials     # → tar.gz
```

**113 条**全部来自 `TRAJECTORY_SELECTION.json`——每个 task 取 claude-fable-5 优先的
那次 pass，一个 task 一条，一个 task 一个镜像，**三者一一对应**。

| 语言 | trial 数 | 命令数 |
|---|---|---|
| go | 35 | 1075 |
| python | 34 | 1381 |
| typescript | 34 | 1590 |
| rust | 5 | 316 |
| javascript | 5 | 157 |
| **合计** | **113** | **4519** |

**语言归类做过修正**：上游 `task.toml` 有 3 个标错（`prometheus-transactional-reload-status`
ts→go、`httpx-deterministic-cookie-store` ts→python、`koota-entity-snapshot-rollback`
python→ts），`make_full_trials.py` 里有一张带证据的修正表，且**修正表自身会自校验**——
上游若自行修了导致"原值"对不上，脚本直接报错退出而不是静默跳过。修正后的分布与
2026-09-04 工具清单调研的独立推导一致。

命令数是 **trace 里的总数**，含每条 trial 末尾那条哨兵（`replay.py` 的 `load_trace`
标 `sentinel=True` 后跳过，原始运行里本来也没执行）。一条 trial 一条，
所以**实跑 4519 − 113 = 4406 条**；下面 §7.3 的时间估算按 4519 算，偏保守。

⚠️ **包里没有任何本地已验证基线。** `make_full_trials.py` 默认会额外并入 `crosslang/`
下那 5 条更早一轮跨模型取样、**本地已跑通且 patch 逐字节核对过**的 trial 当回归对照组
（同 task 同镜像，零额外构建成本）；2026-09-09 按要求用 `--no-verified` 去掉了。

代价要知道：那 113 条**没有一条有已验证基线**（两批 trial 不是同一次运行，模型与
`model.patch` 都不同）。所以 `run_batch.py` 会对每条都显示「无基线」，
`vs_baseline` 恒为 `None`；**某条失败时无法区分「这个 task 有问题」和「整套流程有问题」**。
要恢复对照组：重跑 `make_full_trials.py`（不加 `--no-verified`）即可，会变回 118 条 /
仍是 113 个镜像。

### 7.2 边建边跑

113 个镜像不可能一次建齐，所以流程是分段的：

```bash
# 建一批（语言名会展开成该语言的全部 trial）
bash build_arm.sh --ca-cert corp-ca.crt python

# 取包慢到卡住时加镜像源（先按 §2b 测过确实有用再加）
bash build_arm.sh --ca-cert corp-ca.crt --registry https://registry.npmmirror.com typescript

# 跑已经建好的那些，镜像没建好的自动跳过而不是拒绝启动
python3 run_batch.py --skip-missing --keep-going
```

`--skip-missing` 是全量集的关键开关。不加它，只要有一个镜像缺失整批就拒绝启动。

`build_arm.sh` 失败的条目会在结尾列出来，可以按 trial 目录名前缀单独重试：

```bash
bash build_arm.sh --ca-cert corp-ca.crt abs-module-cache-flags
```

### 7.3 成本（实测 + 外推，标注了哪些是估的）

**镜像磁盘**——每个 task 镜像只在共享基座上加几层，实测：

| | 体积 | 相对 mars-base 的增量 |
|---|---|---|
| `mars-base:arm64` | 5.28 GB | — （只占一份） |
| python task 镜像 | 5.31 GB | **+30 MB**（实测） |
| typescript task 镜像 | 5.84 GB | **+560 MB**（实测） |
| go / javascript / rust | — | 未实测 |

python 那种纯下载的增量极小，typescript 因为 `pnpm install` 把 devDeps 整个装进去所以
大得多。**go/js/rust 的增量我没实测过，别拿 python 的数去推**。粗估全量 113 个约
25~35 GB，其中 typescript 那 35 条是大头。

**构建时间**——两个实测点（qemu 模拟 ARM，网络无代理）：

| | 耗时 | 大头 |
|---|---|---|
| python | 144s | `pip install -e` |
| typescript | ~280s | `pnpm install` 132s + 装报告器 59s + clone/gc 45s |

按每条 3~8 分钟估，**73 个镜像（go+python+javascript）约 5~8 小时**，全量 113 个
约 8~12 小时。rust 那 5 条要单独留时间：它的 Dockerfile 里有
`cargo nextest run --no-run`，把测试二进制整个编译一遍，是**最重的编译步骤**——
但不是唯一一个：`pest` 另有 `cargo build --package pest_bootstrap`，`eicrud` 有两处
`npm run compile`（tsc），`goreleaser` 有 `go build ./...`。

**重放时间**——单位成本来自 5 条对照组的实测（367 条命令 862.1s，即 **2.35 s/命令**）。
这 5 条虽已不在包内，实测值依然是目前唯一的实测依据。按 ARM ×1.4 折算
约 3.3 s/命令：

- go + python + javascript（2613 条命令）≈ **2.5 小时**
- 全量 113 条（4519 条命令）≈ **4~4.5 小时**

注意主导项不是语言而是 **trace 里有没有 `sleep` 轮询**——rust 那条墙钟最长但 CPU 只有
0.30 核，容器大部分时间在空转。

### 7.4 包里带了什么、没带什么

`task.json` 被裁剪到只剩 `task.toml` 与 `environment/Dockerfile` 两个文件——replay.py
只读前者（取 `docker_image` / `base_commit_hash` / `cpus` / `memory_mb` /
`allow_internet`），build_arm.sh 只读后者。完整包 30.4 MB → 裁剪后 0.4 MB，顺带把参考解
`solution/solution.patch` 挡在包外：它对重放毫无用处，不该出现在服务器上。

整包 27 MB 解包体积 / **4.8 MB 压缩**。按 55 KB/s 的传输速率约 25 分钟。

输入数据在 `deepswe/data/{trajectories,tasks}/` 与 `deepswe/data/build_env.json`
（被 gitignore，需另行同步）。`make_full_trials.py` 需要这三者才能装配。
