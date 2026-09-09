# Task Execution Log: crosslang 重放包缺陷修复（14 条）
**Date:** 2026-09-09
**Goal:** 修掉 `deepswe/crosslang/` 重放工具包里已定位的 14 条缺陷，
让「没有上下文的人照 README 操作」这条路在新服务器上真能走通。

## Problems to Solve
1. 阻断：README 把 `preflight.sh` 排在建镜像之前，新机器 113 个镜像全缺 → `exit 1`；
   且无镜像时活体测试整段跳过，预检的价值被架空
2. 阻断：README 承诺「默认不要求 cgroup v2」，但 `preflight.sh` 是无条件硬失败
3. 阻断：`run_batch.py` 打印的 `build_arm.sh` 命令用逗号拼语言名，照抄必失败
4-8. 事实性错误：脚本名、章节号、5 条/113 条口径、`build/<lang>` 路径、
   「patch_identical 已成立」与 build_arm.sh 结论相反
9. ARM 硬改写漏了 cliffy 的 deno x86_64 → 该 task 在 ARM 上必然失败
10. `check_sources.sh` 漏探 nodesource / mongodb / jsr.io / github release 下载域
11-14. 措辞口径：5 条 → 113 条、缺 `--skip-missing`、118 → 113、编译步骤/命令数不准

## Steps Log

### Step 1: 事实核对（改之前先验）
- **Status:** success
- **Result location:** 本文件「Key Findings」
- **Success result:** 扫全部 113 份 Dockerfile 得到真实上游主机集合；
  用 `replay.py.load_trace` 实算出哨兵数；curl 验证 deno aarch64 资产存在

### Step 2: 三条阻断级
- **Status:** success
- **Result location:** `crosslang/README.md`、`crosslang/preflight.sh`、`crosslang/run_batch.py`
- **Success result:** README 主流程重排为 §1 解包 → §2 探源 → §3 建镜像 → §4 预检 → §5 重放
  并写明为什么；`preflight.sh` 加 `--metrics` 门控、镜像缺失降为 warn；
  `run_batch.py:193` `","` → `" "`（`--only` 那处的逗号保持不动）

### Step 3: 事实性错误 + ARM deno 改写
- **Status:** success
- **Result location:** `crosslang/RUNBOOK.md`、`crosslang/build_arm.sh`
- **Success result:** build_arm.sh 新增第 3 条改写（deno x86_64 → aarch64）；
  RUNBOOK §2/§2b/§3/路 C/§7 全部按 113 条口径与「对 113 条仍是开放问题」重写

### Step 4: 口径/措辞 + 自检
- **Status:** success
- **Result location:** `crosslang/check_sources.sh`、各文件
- **Success result:** check_sources 补 4 个探测点（共 17 个，实跑 17/17 通）；
  `bash -n` 6 个 .sh 全过、`python3 -m py_compile` 3 个 .py 全过；
  `preflight.sh` / `preflight.sh --metrics` / `run_batch.py --dry-run` 实跑正常

## Key Findings
- **全量 113 份 Dockerfile 的真实上游主机**（`https?://` 扫描，去掉注释行）：
  `github.com`(113)、`get.nexte.st`(5 条 rust 全部)、`deb.nodesource.com`(1)、
  `www.mongodb.org`/`repo.mongodb.org`(1)。`nodejs.org` 那 5 处**全在注释里**，不是真依赖
- **命令数 4519 含 113 条哨兵**：用 `replay.py.load_trace` 实算 = 4519 总 / 113 sentinel /
  **4406 实跑**，与「一条 trial 一条哨兵」吻合
- **deno 那条尤其阴**：装错架构的二进制**下载时不报错**，拖到下一句 `RUN deno cache`
  才 `exec format error`。正则实测只命中 cliffy 一条、只命中一处；amd64 基座下不改写
- **README 说的「唯一的真·编译步骤」不成立**：除 `cargo nextest --no-run` 外还有
  `pest` 的 `cargo build --package pest_bootstrap`、`eicrud` 的两处 `npm run compile`、
  `goreleaser` 的 `go build ./...`
- **`--metrics` 门控口径**：`run_batch.py:86` 早就有 `need_cgroup` 门控，
  `preflight.sh` 是唯一没对齐的一处 —— 对齐后 rootless / cgroup v1 的机器不再被挡

## Files Changed
- `crosslang/README.md` - 主流程重排 + 前置条件指向 check_sources.sh + 故障表 §2→§3
  + 编译步骤措辞 + 4519/4406 口径注
- `crosslang/preflight.sh` - `--metrics` 参数与 cgroup 两段门控；镜像缺失 bad→warn
  且零镜像时明确提示先建；磁盘与结尾命令改 113 口径并补 `--skip-missing`
- `crosslang/check_sources.sh` - 补 jsr.io / nodesource / mongodb×2 / github release 下载域；
  全文改 113 条口径
- `crosslang/build_arm.sh` - 新增 deno x86_64→aarch64 改写；注释 118→113
- `crosslang/run_batch.py` - 缺镜像提示改空格分隔
- `crosslang/RUNBOOK.md` - §2 前置条件表、§2b 路 B/源表/ARM 改写、§3 预检、
  路 C 保真度结论、`build/<trial>` 路径、§7.1/§7.3 口径

**Commit:** 未提交（按要求）
