# DeepSWE 重放包 · 113 条

按 agent 的原始 trace 在容器里逐条重放命令，用
**「容器内 `git diff` 出来的 patch 与 agent 当初提交的 `model.patch` 逐字节相同」**
这一条硬标准，确认这台服务器的环境与原 harness 等价。

本文件就是操作说明，从解包到出结果一条路走完。
其余文档都是辅助：`RUNBOOK.md` 是深入排查用的参考手册，`INDEX.md` 是历史分析材料。

---

## 0. 前置条件

| 必须有 | 说明 |
|---|---|
| `docker` | 能起容器、能 build |
| `python3` | 3.8+，**只用标准库**，无需 pip install |
| **`mars-base` 基座镜像** | 所有 113 个镜像都 `FROM mars-base`。**基座不在，一条都建不了** |
| 能访问各包源 | 构建期要 clone github、取 npm/pypi/goproxy/crates |

基座拉不到就 `docker load` 一份进来。`preflight.sh` 会替你核对这几项。

⚠️ **重放期不需要外网、也不该有** —— 重放容器是 `--network=none` + 403 sinkhole，
刻意还原原 harness 的 `allow_internet=false`。要联网的只有构建期。

---

## 1. 解包与核对

```bash
tar xzf deepswe-replay-bundle-*.tar.gz && cd deepswe-replay-bundle
sha256sum -c SHA256SUMS      # 传输完整性
cat BUILD_INFO               # 这份包是哪个 commit 打的、各脚本的 sha256
```

## 2. 环境预检

```bash
bash preflight.sh
```

真起容器逐项验证能力，**有 ❌ 先解决再往下**。默认不要求 cgroup v2 可读
（那是 `--metrics` 才需要的），rootless docker / cgroup v1 的机器也能跑。

## 3. 建镜像 —— 最耗时的一步

包里**没有镜像**，带的是每个 task 的 `environment/Dockerfile`（配方）。
113 条 trial 对应 **113 个镜像**，一个 task 一条 trial 一个镜像。

```bash
bash check_sources.sh                # 先确认各包源在这台机上可达
bash build_arm.sh --list python      # 只看会怎么改写 Dockerfile，不构建
bash build_arm.sh python             # 真建
```

**指定建哪些** —— 语言名、trial 目录名前缀、`all` 都行，且可并列多个：

```bash
bash build_arm.sh typescript                      # 该语言全部 35 条
bash build_arm.sh true-myth                       # 前缀匹配，单条
bash build_arm.sh koota                           # 前缀匹配，一批（koota 有 5 条）
bash build_arm.sh true-myth koota-query vitest    # 并列多个
bash build_arm.sh all                             # 全建，按依赖从少到多排序
```

失败的条目会在结尾列出来，按同样的前缀单独重试即可。已存在的镜像会跳过
（要重建先 `docker rmi <tag>`）。

**内网环境的两个常用开关：**

```bash
bash build_arm.sh --proxy http://proxy:port python          # 构建期代理
bash build_arm.sh --ca-cert corp-ca.crt python              # 内网 TLS 中间人的 CA
```

CA 不知道从哪来就跑 `bash get_ca_cert.sh`（会抠出来并验证可用）；
不确定内网到底有没有做中间人就跑 `bash detect_mitm.sh`。

## 4. 重放

```bash
python3 run_batch.py --smoke 5 --skip-missing      # 冒烟：每条只跑前 5 条命令
python3 run_batch.py --skip-missing --keep-going   # 正式跑
```

- `--skip-missing` —— 镜像还没建好的自动跳过，而不是整批拒绝启动。**边建边跑靠它。**
- `--keep-going` —— 某条失败后继续跑剩下的（默认遇错即停）
- `--only python,go` —— 只跑指定语言

**「只跑哪几条」目前只能通过「只建哪几条的镜像」+ `--skip-missing` 间接实现** ——
`run_batch.py` 的 `--only` 只认语言，不认 trial 名。真要精确跑单条就直接调重放器：

```bash
python3 replay.py <trial 目录> <trial 目录>/task.json -o /tmp/one
```

## 5. 读结果

结果落在 `runs/<UTC 时间戳>/`：

| 文件 | 干什么用 |
|---|---|
| `SUMMARY.md` | 人看的汇总 |
| `summary.json` | 机器读的汇总 |
| `logs/<trial>.log` | 逐条实时输出，跑的过程中就能 `tail -f` |
| `<trial>/verdict.json` | 单条判定 |
| `<trial>/commands.jsonl` | per-command 指标 |
| `<trial>/replayed.patch` | 重放后从容器里 diff 出来的 patch |

**唯一的硬标准是 `patch_identical=true`。** `rc_match`、耗时这些都允许有出入
（已知有 4 类不可消除的差异），口径见 `RUNBOOK.md` §5。

---

## 规模与预算

| 语言 | trial 数 | 命令数 |
|---|---|---|
| typescript | 35 | 1620 |
| python | 34 | 1387 |
| go | 34 | 1039 |
| rust | 5 | 316 |
| javascript | 5 | 157 |
| **合计** | **113** | **4519** |

| | 预算 | 依据 |
|---|---|---|
| 建镜像 | **8~12 小时 / 25~35 GB** | python 144s、typescript ~280s 实测外推 |
| 重放 | **4~4.5 小时** | 2.35 s/命令实测 × ARM 1.4 折算 |

rust 那 5 条单独留时间：Dockerfile 里有 `cargo nextest run --no-run`，
是全部 113 个里唯一的真·编译步骤。

---

## 三个必须先知道的坑

**1. 这版包里没有任何已验证基线**

`<trial>/replay/verdict.json` 本来是「上一台机器的判定」，用于跨机对比。
**这一版一个都没有**（按要求去掉了 5 条已验证对照组）。`run_batch.py` 会对每条都打
「无基线」，`vs_baseline` 恒为 `None` —— 这是优雅降级，不会报错。

后果要心里有数：**某条失败时，无法区分「这个 task 有问题」和「整套流程有问题」。**
需要对照组就在开发机上重跑 `make_full_trials.py`（不加 `--no-verified`）重新打包，
会变回 118 条、镜像数仍是 113、零额外构建成本。

**2. 重建出来的镜像 ≠ 原 amd64 镜像**

`build_arm.sh` 会改写 Dockerfile（换基座、按架构改二进制下载 URL 等），
每条的改写清单落在 `build/<trial>/REWRITES.md`，**跑之前先读一眼**。
即使改写为零也仍有漂移：`pnpm install` 未加 `--frozen-lockfile`、
工具链是另一个架构编译的、基座是 `:latest` 不可复现。

**3. `--registry` 换源不是默认解法**

构建卡在 `pnpm install` 时容易想到换源，但开发机上的对照实验显示**换源反而慢 18%**
（官方源 132.4s vs 镜像源 156.6s，同仓库同 548 个包）。因为瓶颈是每请求的固定开销
（RTT + TLS 握手）而不是带宽，换目标主机解决不了。

**开之前先测**（必须带着构建用的同一套代理环境变量）：

```bash
for h in registry.npmjs.org registry.npmmirror.com; do
  echo "--- $h ---"
  for i in 1 2 3; do
    curl -sS -o /dev/null \
      -w "  tls=%{time_appconnect}  total=%{time_total}  %{speed_download}B/s\n" \
      "https://$h/minimatch/-/minimatch-9.0.5.tgz"
  done
done
```

两个 host 的 `total` 接近 → 瓶颈是代理本身，**换源白搭**；npmmirror 明显低才开：

```bash
bash build_arm.sh --registry https://registry.npmmirror.com typescript
```

---

## 出问题去哪查

| 症状 | 去处 |
|---|---|
| 预检报错 | `RUNBOOK.md` §2 |
| 拉不到镜像 / ECR 不通 | `RUNBOOK.md` §2b |
| `git clone` 报证书错误 | `RUNBOOK.md` §2b 证书一节 → `get_ca_cert.sh` |
| **构建**卡在 `pnpm install` | `RUNBOOK.md` §2b「取包慢:换镜像源」 |
| **重放**某条卡住不动 | `RUNBOOK.md` §6.5（多数是 trace 里本来就有 `sleep` 轮询） |
| 找不到 cgroup 目录 | `RUNBOOK.md` §6.1（或干脆别加 `--metrics`） |
| 结果怎么读、什么算通过 | `RUNBOOK.md` §5 |

⚠️ **已知问题**：宿主代理挂在 loopback（`127.0.0.1:xxxx`）时 `build_arm.sh` 会自动加
`--network host`，但在 **Docker Desktop + WSL2** 上这套 BuildKit **不兑现**该参数，
`git clone` 直接失败。原生 Linux Docker 未复现。绕法见 `RUNBOOK.md` §2b 代理一节。

---

## 包里的文件

**主线**（按用到的顺序）

```
README.md          本文件 —— 操作说明
preflight.sh       环境预检
check_sources.sh   各包源可达性检查
build_arm.sh       从本地基座重建 task 镜像
run_batch.py       批量重放 driver
replay.py          单条重放器（只用 python 标准库）
```

**辅助**

```
RUNBOOK.md         参考手册：每个坑的成因、判据、绕法。出问题查它
INDEX.md           上一轮 5 条跨语言验证的完整分析（历史材料，非本轮口径）
get_ca_cert.sh     内网 TLS 中间人：抠公司 CA
detect_mitm.sh     判定内网是否真的在做 TLS 中间人
BUILD_INFO         包的来源：commit / 打包时间 / 各脚本 sha256
SHA256SUMS         传输完整性校验
release.json       DeepSWE v1.1 release 描述（产物 URL 模板）
```

**数据**：113 个 `<trial>/` 目录，每个含

```
meta.json          语言 / 模型 / 镜像 / base_commit / 命令数
trajectory.json    原始 trace（命令逐字记录）
model.patch        agent 最终提交的 patch —— 保真度比对基准
task.json          任务定义（含 task.toml 与 environment/Dockerfile）
replay/            重放产物落在这里（本版包内为空）
```

> **bundle 必须在开发机上打好再拷过来，不能在服务器上 clone 仓库重打。**
> `trajectory.json` 与 `model.patch` 体量大且可复现，被 `.gitignore` 排除在版本库外，
> 新 clone 的仓库里没有这两个文件。
