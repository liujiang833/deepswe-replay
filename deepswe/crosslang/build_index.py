#!/usr/bin/env python3
"""汇总 deepswe/crosslang/ 下五条跨语言重放的结果，生成 INDEX.md。

输入（每条 trial 目录下）：
    meta.json          归档元数据（语言 / 模型 / 步数 / 镜像）
    trajectory.json    原始 trace
    replay/verdict.json, replay/commands.jsonl   replay.py 产物

用法:
    python3 deepswe/crosslang/build_index.py
"""

import collections
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
ORDER = ["python", "go", "rust", "typescript", "javascript"]

# 各语言"构建/测试工具链"关键词，用于统计命令构成差异
TOOLCHAIN = [
    "cargo", "rustc", "cargo nextest", "clippy",
    "go test", "go build", "go vet", "go run", "gofmt",
    "pnpm", "npm", "npx", "node", "tsc", "vitest", "jest",
    "pytest", "python3", "python -", "mypy", "ruff", "make",
]


# 2026-09-04 实测（scratchpad/pull.log + registry manifest 统计），无法从归档里重算，故内联
# (语言, 起止, 耗时秒, 完整下载MB, 去重后增量MB)
PULL_COST = [
    ("python",     "10:41:55–10:42:35", 40,  762, 11),
    ("go",         "10:42:40–10:44:08", 88,  779, 28),
    ("rust",       "10:44:13–10:59:12", 899, 1013, 263),
    ("typescript", "10:59:17–11:07:42", 505, 867, 117),
    ("javascript", "11:07:47–11:12:58", 311, 833, 82),
]


def strip_cd(c):
    return re.sub(r"^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*cd\s+\S+\s*&&\s*", "", c.strip())


def trace_cmds(trial_dir):
    """重放产物还没有时，退化成从 trace 直接取命令，保证命令构成一节始终有内容。"""
    d = json.loads((trial_dir / "trajectory.json").read_text())
    out = []
    for s in d["steps"]:
        for tc in s.get("tool_calls") or []:
            out.append({"cmd": tc["arguments"].get("command", "")})
    return out


def load(trial_dir):
    meta = json.loads((trial_dir / "meta.json").read_text())
    rep = trial_dir / "replay"
    v = json.loads((rep / "verdict.json").read_text()) if (rep / "verdict.json").exists() else None
    recs = []
    if (rep / "commands.jsonl").exists():
        recs = [json.loads(l) for l in (rep / "commands.jsonl").read_text().splitlines() if l.strip()]
    return meta, v, (recs or trace_cmds(trial_dir))


# heredoc 正文是**被写入文件的源码**，不是被执行的命令。不剥离会把源码里的
# 关键词计成工具链调用——实测 go 的 `make([]string, 0)`（Go 内建函数）被算成 3 次
# `make` 命令调用，rust/typescript 各 3 次则来自 "makes" 之类的词内子串。
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?\n.*?\n\s*\1(?=\s|$)", re.S)


def strip_heredocs(c):
    prev = None
    while prev != c:
        prev = c
        c = HEREDOC.sub("<<HEREDOC_BODY_STRIPPED", c, count=1)
    return c


def toolchain_mix(recs):
    kw = collections.Counter()
    for r in recs:
        c = strip_heredocs(r["cmd"])
        for t in TOOLCHAIN:
            # 右边界不能省：否则 "makes" 命中 make、"mypyc" 命中 mypy
            pat = r"(?<![\w/.-])" + re.escape(t) + (r"(?![\w-])" if t[-1].isalnum() else "")
            if re.search(pat, c):
                kw[t] += 1
    return kw


COMPILE_RE = re.compile(
    r"\b(go\s+(?:test|build|run|vet)|cargo\s+(?:build|test|check|clippy)|rustc"
    r"|tsc\b|npm\s+run\s+(?:build|dist|types)|rollup|esbuild|webpack|vite\s+build)\b")
TEST_RE = re.compile(
    r"\b(pytest|go\s+test|cargo\s+test|cargo\s+nextest|vitest|jest|mocha|npm\s+(?:run\s+)?test|node\s+tests?/)\b")


def trace_timeouts(trial_dir):
    """trace 侧被**原 harness** 30s 打死的命令（observation 写明 timed out after）。"""
    d = json.loads((trial_dir / "trajectory.json").read_text())
    out, idx = [], 0
    for st in d["steps"]:
        rs = (st.get("observation") or {}).get("results") or []
        for ti, tc in enumerate(st.get("tool_calls") or []):
            c = rs[ti]["content"] if ti < len(rs) else ""
            if "timed out after" in c:
                out.append((idx, tc["arguments"].get("command", "")))
            idx += 1
    return out


def adaptations(recs):
    """agent 为绕开 30s 墙而采取的手法计数。"""
    nohup = sum(1 for r in recs if "nohup" in strip_heredocs(r["cmd"]))
    slp = sum(1 for r in recs if re.search(r"\bsleep\s+\d+", strip_heredocs(r["cmd"])))
    tmo = sum(1 for r in recs if re.search(r"\btimeout\s+\d+", strip_heredocs(r["cmd"])))
    return nohup, slp, tmo


def classify(cmd):
    c = strip_heredocs(cmd)
    k = []
    if COMPILE_RE.search(c):
        k.append("编译")
    if TEST_RE.search(c):
        k.append("测试")
    return "+".join(k) or "其他"


def head_tokens(recs, n=8):
    cnt = collections.Counter()
    for r in recs:
        s = strip_cd(r["cmd"]).split()
        cnt[s[0] if s else ""] += 1
    return cnt.most_common(n)


def main():
    rows, dirs = [], {}
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        if not (d / "meta.json").exists():
            continue
        r = load(d)
        rows.append(r)
        dirs[r[0]["language"]] = d
    rows.sort(key=lambda x: ORDER.index(x[0]["language"]) if x[0]["language"] in ORDER else 99)

    L = []
    A = L.append
    A("# 跨语言容器重放验证（crosslang）\n")
    A("`deepswe/replay.py` 按 agent trace 在长驻容器内串行重放命令，并以")
    A("「容器内 `git diff --binary <base_commit_hash> HEAD` 与下载到的 `model.patch` 逐字节比对」")
    A("作为保真度硬校验。本目录把该流程从 1 条 python task 扩到 5 种语言各 1 条。\n")

    A("## 选样口径\n")
    A("- 每种语言（python / go / rust / typescript / javascript）各取 1 条 trial，人工指定，非随机抽样。")
    A("- 五条都来自 DeepSWE release `v1.1`，产物经公开 CloudFront 直取（无鉴权）。")
    A("- 五条分属 **5 个不同模型**（见下表）——因此语言之间的差异里混着模型差异，")
    A("  本轮只用于验证「重放流程是否跨语言成立」，不能拿来做语言间的负载横向对比。")
    A("- 资源上限全部为 `cpus=2 / memory_mb=8192 / allow_internet=false`，与 `task.toml` 对齐。\n")

    A("## 目录结构\n")
    A("```")
    A("deepswe/crosslang/")
    A("├── INDEX.md                  本文件")
    A("├── build_index.py            汇总脚本（重跑即可再生成本文件）")
    A("├── release.json              DeepSWE v1.1 release 描述（产物 URL 模板）")
    A("└── <trial_name>/")
    A("    ├── meta.json             语言 / 模型 / 步数 / 镜像引用 / token 与成本")
    A("    ├── trajectory.json       原始 trace（ATIF v1.7）")
    A("    ├── model.patch           agent 最终提交的 patch（保真度比对基准）")
    A("    ├── task.json             任务定义副本（拷自 deepswe/data/tasks/<task_id>.json）")
    A("    ├── mini-swe-agent.txt    agent 原始日志")
    A("    ├── test-stdout.txt       verifier 输出")
    A("    └── replay/               重放产物")
    A("        ├── verdict.json      判定（patch_identical / rc_match / timing_divergences）")
    A("        ├── commands.jsonl    per-command 指标（wall / cpu / io / mem_peak）")
    A("        └── replayed.patch    重放后从容器里 diff 出来的 patch")
    A("```\n")

    A("## 五条 trial 元数据\n")
    A("| 语言 | trial_name | task_id | 仓库 | 模型 | steps | 命令数 | model.patch |")
    A("|---|---|---|---|---|---|---|---|")
    for meta, v, recs in rows:
        A(f"| {meta['language']} | `{meta['trial_name']}` | `{meta['task_id']}` | "
          f"{meta.get('repo','-')} | `{meta['agent']['model_name']}` | {meta['steps_total']} | "
          f"{meta['n_commands']} | {meta['model_patch_bytes']:,}B |")
    A("")
    A("镜像（同一 base `public.ecr.aws/x8v8d7g8/mars-base:latest`，各 task 一个成品 tag）：\n")
    A("| 语言 | docker_image | base_commit_hash |")
    A("|---|---|---|")
    for meta, v, recs in rows:
        im = meta["image"]
        A(f"| {meta['language']} | `{im['docker_image']}` | `{im['base_commit_hash'][:12]}` |")
    A("")

    A("## 镜像拉取成本（2026-09-04 实测，串行拉取）\n")
    A("五个镜像串行拉取，重试策略为指数退避（10s → 20s → … 上限 300s，最多 6 次）。")
    A("本机已有一个同批次镜像 `…kh79vjbp8dv1…-v1.1`，作为层复用的基线。\n")
    A("| 语言 | 起止 | 耗时 | 完整下载 | 去重后实际下载 | 重试次数 |")
    A("|---|---|---|---|---|---|")
    for lang, span, sec, full, inc in PULL_COST:
        A(f"| {lang} | {span} | {sec}s | {full} MB | **{inc} MB** | 0 |")
    A("")
    A("- **合计 500 MB / 1,871s（31 分钟）**，与预估的「增量约 500 MB」一致。")
    A("- 有效吞吐 **≈0.27 MB/s**（约 2.2 Mbit/s），五条之间高度一致（0.23–0.32 MB/s）：")
    A("  耗时几乎完全由下载字节线性决定，解包不是瓶颈。")
    A("- **全程 0 次 ECR 限流**：没有出现 `toomanyrequests` / 429 / retry，5 个镜像全部 attempt=1 一次成功。")
    A("  退避重试逻辑保留，但本轮未被触发。")
    A("- 全量 113 个 task 镜像的预算（只取 manifest 统计、未真拉）：")
    A("  各镜像完整下载求和 **111 GB**，层去重后 **24.3 GB**；首个镜像 840 MB（公共基座），")
    A("  之后每个平均增量 **210 MB**。按实测 0.27 MB/s 推算，全量串行拉取约 **25 小时**——")
    A("  这是批量化的主要时间成本，建议提前预拉或换更快的出口。\n")

    rows_x = [(m, v, r, dirs[m["language"]]) for (m, v, r) in rows]

    A("## 重放结果汇总\n")
    A("| 语言 | patch_identical | rc_match | rc_match_semantic | timing_divergences | 命令数 | 总耗时 | CPU 总量 | s/cmd |")
    A("|---|---|---|---|---|---|---|---|---|")
    for meta, v, recs in rows:
        if not v:
            A(f"| {meta['language']} | (未跑) | - | - | - | - | - | - | - |")
            continue
        cpu = sum(r.get("usage_usec", 0) for r in recs) / 1e6
        n = v["n_replayed"]
        A(f"| {meta['language']} | {'✅ 是' if v.get('patch_identical') else '❌ 否'} | "
          f"{v['rc_match']} | {v['rc_match_semantic']} | {len(v['timing_divergences'])} | "
          f"{n} | {v['elapsed_s']:.0f}s | {cpu:.0f}s | {v['elapsed_s']/max(n,1):.2f} |")
    A("")
    A("- `patch_identical`：容器内 `git diff --binary <base> HEAD` 与 `model.patch` 逐字节相等。")
    A("- `rc_match`：重放退出码与 trace 记录严格逐条相等。")
    A("- `rc_match_semantic`：额外把「trace 侧 -1（被原 harness 打死）↔ 重放侧超时」算作匹配。")
    A("- `timing_divergences`：只有一侧超时的条数（重放机与原机速度差导致，预期存在）。")
    A("- CPU 总量 = 各命令 cgroup `cpu.stat/usage_usec` 差值之和（2 核上限，可 > 墙钟的 1 倍）。\n")

    A("## 执行器口径：`bash -c` → `bash -lc`\n")
    A("本轮把 `replay.py` 里 `docker exec … bash -c <cmd>` 改成 `bash -lc`，对齐原 harness")
    A("（mini-swe-agent `environments/docker.py:38`：`interpreter = [\"bash\", \"-lc\"]`）。")
    A("在镜像里实测两种口径的差别如下：\n")
    A("```")
    A("PATH(-c)  = /root/go/bin:/root/.cargo/bin:/root/.local/bin:/root/.rye/shims:/root/.bun/bin:")
    A("            /usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    A("PATH(-lc) = /root/.bun/bin:/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:")
    A("            /usr/bin:/sbin:/bin:/root/.local/bin:/root/.local/bin")
    A("```\n")
    A("- 机制：`-lc` 是登录 shell，`/etc/profile` 会**整体覆写** PATH（`PATH=\"/usr/local/sbin:…\"`），")
    A("  随后 `/root/.profile` 再 `. \"$HOME/.cargo/env\"` 和 pipx 的 `.local/bin` 把两条路径加回来。")
    A("- **结论与预期相反**：`cargo` / `rustc` 在两种口径下都能解析到 `/root/.cargo/bin`——")
    A("  因为 mars-base 已经把它烘进了镜像的 ENV PATH，rust 并不是差异暴露点。")
    A("- `-lc` 真正的行为差异是**丢掉了 `/root/go/bin` 和 `/root/.rye/shims`**")
    A("  （它们只存在于 docker ENV，被 `/etc/profile` 覆写掉了）。`/root/go/bin` 里是 go 镜像")
    A("  额外装的 `go-ctrf-json-reporter`。已核对：五条 trace 里**没有任何命令**用到这两个目录下的")
    A("  可执行文件（`go` / `gofmt` 本体在 `/usr/local/bin`，`cargo-nextest` 在 `/usr/local/bin`），")
    A("  所以本轮改动对命令解析**无实际影响**；但它是原 harness 的真实口径，改了更保真。")
    A("- 另已验证 `-lc` **不改变工作目录**（`docker exec -w /app` 下 `pwd` 仍是 `/app`），")
    A("  `/root/.profile` 里没有 `cd`，相对路径命令不受影响。\n")

    A("## 已知风险\n")
    A("- `replay.py` 启动时会 `docker rm -f <容器名>`，容器名由 trial 目录名推导")
    A("  （`replay_<trial_name>`，截断到 60 字符）。**两个进程同时重放同一条 trial 会静默互杀**——")
    A("  后启动的那个会把先启动的容器删掉，先启动的那条从此每条命令都失败。")
    A("  批量化时必须保证同一 trial 只有一个重放进程。\n")

    A("## 命令构成差异（工具链关键词命中次数）\n")
    all_kw = []
    for meta, v, recs in rows:
        all_kw.append((meta["language"], toolchain_mix(recs)))
    keys = [k for k in TOOLCHAIN if any(kw.get(k) for _, kw in all_kw)]
    A("| 工具 | " + " | ".join(l for l, _ in all_kw) + " |")
    A("|---" * (len(all_kw) + 1) + "|")
    for k in keys:
        A(f"| `{k}` | " + " | ".join(str(kw.get(k, "")) for _, kw in all_kw) + " |")
    A("")
    A("首 token 分布（剥掉 `cd X &&` 前缀后）：\n")
    for meta, v, recs in rows:
        A(f"- **{meta['language']}**：" + ", ".join(f"`{t}`×{c}" for t, c in head_tokens(recs)))
    A("")

    A("## 30s 超时归因\n")
    A("原 harness 与本重放都用 30s 单命令超时。分别统计**原始机器上**（trace 侧，未受任何干扰）")
    A("和**重放机器上**被砍掉的命令：\n")
    A("| 语言 | trace 侧超时 | 重放侧超时 | 只有一侧超时 | trace 侧超时命令形态 |")
    A("|---|---|---|---|---|")
    for meta, v, recs, tdir in rows_x:
        tt = trace_timeouts(tdir)
        rt = [r for r in recs if r.get("timed_out")] if v else []
        kinds = ", ".join(sorted({classify(c) for _, c in tt})) or "—"
        div = len(v["timing_divergences"]) if v else "-"
        A(f"| {meta['language']} | {len(tt)} | {len(rt) if v else '-'} | {div} | {kinds} |")
    A("")
    A("- **「30s 对编译型语言系统性偏紧」只成立一半**：go / rust 的 trace 侧超时全是编译类")
    A("  （go 是 `go test ./...` 全包测试，rust 是 `cargo build`），而 typescript / javascript")
    A("  **原始机器上零超时**——它们没有 AOT 编译期，`npx vitest` / `node` 起步即跑。")
    A("- python 也撞了 2 次，但不是编译，是 `tests/test_laws.py` 这类性质测试本身跑得久。")
    A("- 判据：`patch_identical` 不受超时影响的前提是**被砍的命令不改文件**。本轮被砍的")
    A("  全部是只读的构建/测试命令，所以保真度没有因超时受损。")
    A("- 表格两处需要看注解，别照字面读：")
    A("  rust 的「其他」是 `sleep 45; tail -30 /tmp/build.log`——agent 自己的轮询等待，45>30 必被砍，")
    A("  两侧行为一致；typescript 的重放侧超时**不是机器慢**，是网络语义差异（见下 B 节）。\n")

    A("### 批量化的时间预算\n")
    A("按「独占重放」口径，只有 python / go 两条是干净的；rust / ts / js 三条受并发影响偏悲观。")
    A("另有一条早前的独立基线：python `gql-incremental-graphql-delivery__nnFNKRL`，438 条命令 / 981.5s")
    A("= **2.24 s/cmd**，与本轮 python 的 2.48 s/cmd 互相印证。\n")
    A("- **每命令耗时没有出现「编译型语言数量级更贵」的现象**：go 反而最快（1.63 s/cmd），")
    A("  因为 go 的增量编译有 build cache，且 trace 里多数命令是 `sed`/`grep` 这类瞬时命令。")
    A("- rust 最慢（3.93 s/cmd）**主要不是编译慢，而是 agent 自己 `sleep 25~29` 在等**——")
    A("  rust 的 CPU 总量只有 91s / 298s 墙钟（0.30x），容器大部分时间在空转。")
    A("  这是 30s 上限**倒逼出来的**开销：真正的 `cargo build` 反而只占其中一小段。")
    A("- 因此时间预算的主导项不是语言，而是 **trace 里有没有 `sleep` 轮询模式**。")
    A("- 粗略预算：5 条共 367 条命令、合计 965s 墙钟，**平均 2.63 s/cmd**。")
    A("  按全量 113 条 task、每条 60~100 条命令估算，串行重放约需 **5~7 小时**（不含拉镜像的 25 小时）。\n")

    A("### agent 对 30s 墙的适应行为\n")
    A("| 语言 | `nohup` 后台 | `sleep N` | `timeout N` | 策略 |")
    A("|---|---|---|---|---|")
    STRAT = {
        "python": "两手都用：16 条 `timeout 120~300` 包装（**无效**，外层 30s 更小）+ 3 次 nohup 后台",
        "go": "无适应——只撞过 1 次，之后改用 `go test .` 单包而非 `./...`",
        "rust": "**全面后台化**：`(nohup cargo … &); sleep 25~29; tail` —— sleep 取值贴着 30s 上限调",
        "typescript": "无需适应（零超时）",
        "javascript": "无需适应（零超时）",
    }
    for meta, v, recs, tdir in rows_x:
        n, sl, tm = adaptations(recs)
        A(f"| {meta['language']} | {n} | {sl} | {tm} | {STRAT.get(meta['language'], '')} |")
    A("")
    A("rust 的 `sleep` 取值序列很能说明问题：i=29 用 25s、i=30 试 45s **立刻被砍**、i=31 退回 25s，")
    A("此后固定在 25/28/29。这是 agent 在**反复试探 30s 上限**。副作用是 rust 这条 trace 的重放")
    A("对机器速度敏感——`sleep 28` 窗口内 cargo 是否编译完，决定 `tail` 抓到什么。\n")

    A("## rc 不匹配逐条归因\n")
    A("五条全部 `patch_identical = 是`，但 rc 序列合计有 10 条不匹配。逐条查过，**没有一条是重放机制的缺陷**：\n")
    A("| 类别 | 条数 | 语言 | 说明 |")
    A("|---|---|---|---|")
    A("| A 双侧都超时 | 4 | python×2, go×1, rust×1 | trace 侧 -1、重放侧 124，行为一致，`rc_match_semantic` 已计为匹配 |")
    A("| B 网络语义差异 | 1 | typescript | 见下「网络」一节 |")
    A("| C shell 方言差异 | 2 | rust | 见下「执行器 shell」一节 |")
    A("| D 上游 flaky 测试 | 1 | go | 见下「flaky」一节 |")
    A("| E 单侧超时（机器快慢） | 2 | python×1, rust×1 | 命令本身贴着 30s 边界，两台机器分属两侧 |")
    A("")

    A("### B. 网络：原 harness 是「403 代理」，重放是「无网卡」\n")
    A("typescript i=34 是唯一一条 `replay_timeout_only`，且反差极大——")
    A("**原机器上该 step 间隔上界只有 4.3s，重放却整整跑满 30s 被砍**。原因不是机器慢：\n")
    A("```")
    A("trace 侧 observation:  npm error code E403")
    A("                       npm error 403 Forbidden - GET https://registry.npmjs.org/tsx   → rc=0，秒回")
    A("重放侧:                stdout 0 字节，wall 30.09s，rc=124                              → 被 timeout 砍")
    A("```")
    A("原 harness 的 `allow_internet=false` 是用**主动拒绝的代理**实现的（立刻回 403）；")
    A("`replay.py` 用的是 `--network=none`（连不上，**挂着等**）。对 `npx`/`npm` 这类自带重试退避的")
    A("工具，后者会把 30s 预算耗光。对比证据：go i=18 用 `urllib.request` 直连，`--network=none` 下")
    A("DNS 立即失败（`Errno -3 Temporary failure in name resolution`），0.21s 返回、rc 与 trace 一致。")
    A("**结论：差异只在「失败得快不快」，而 npm 系工具会把它放大成超时。**")
    A("批量化前建议把 `--network=none` 换成一个立即拒绝的 sinkhole 代理，与原 harness 对齐。\n")

    A("### C. 执行器 shell：证据指向 `sh`(dash)，不是 `bash -lc`\n")
    A("rust i=53 / i=56 两条，trace 侧 rc=2 且 observation 是：\n")
    A("```")
    A("/bin/sh: 1: Syntax error: word unexpected (expecting \")\")")
    A("```")
    A("这是 **dash 的报错格式**，说明原 harness 是用 `/bin/sh` 跑的。两条命令都含 `time (...)`（bash 专有）。")
    A("重放用 `bash -lc`，两条都正常执行、rc=0——**重放比原环境更宽松，把原本失败的命令跑成功了**。\n")
    A("核查过的旁证：")
    A("- 五条 trace 里**真正的 bashism 只有这 2 条**，且都失败；没有任何 bashism 在 trace 侧成功过。")
    A("  （python 里看似有 `[[` / `arr=(`，逐条查是 `Callable[[_FirstType], …]`、`@validated(exceptions=(…,))`")
    A("  这类 **Python 源码**，不是 shell 语法。）")
    A("- 五个镜像的 `/bin/sh` 全部指向 `/usr/bin/dash`，且 `time (echo x)` 在五个镜像里一律 FAIL。")
    A("- 所以 `bash -c` → `bash -lc` 这次改动虽然对齐了当前版本的 mini-swe-agent，")
    A("  **但对 v1.1 这批 trace 未必是更保真的选择**——`sh -c` 才能复现 dash 的失败。")
    A("  影响面很小（10 条不匹配里占 2 条，且都只碰 `/tmp` 与只读 perf 检查，不改仓库，")
    A("  所以 `patch_identical` 没受影响），但批量化前值得定一个口径。\n")

    A("### D. go 的 flaky 测试（Go map 迭代顺序）\n")
    A("go i=66 是最后的总验证命令，trace rc=0、重放 rc=1，且**只跑了 0.97s**（不是超时）。根因：\n")
    A("```")
    A("--- FAIL: TestRuleActionPinningDisabledAndDefault")
    A("    rule_action_pinning_test.go:67: first error should identify a reusable workflow:")
    A("    \"step action \\\"actions/checkout@v1\\\" is not pinned to semver…\"")
    A("```")
    A("测试断言 `messages[0]` 必须是 reusable workflow 的报错，而 actionlint 的 AST 里")
    A("`Jobs map[string]*Job` 是 **Go map**，Visitor `for _, j := range n.Jobs` 的遍历顺序**每次随机**。")
    A("workflow 里 `reusable` 与 `actions` 两个 job 谁先被访问是掷硬币——")
    A("trace 那次 `reusable` 先，重放这次 `actions` 先。\n")
    A("**这是 agent 自己写进 model.patch 的 flaky 测试，不是重放缺陷。** 含义：批量化时 `rc_match`")
    A("存在一个不可消除的下限，部分 mismatch 来自被复现代码自身的不确定性。`patch_identical` 不受影响")
    A("（源码字节一致，只是跑出来的结果不同）。\n")

    A("## 本轮的口径污染（必须标注）\n")
    A("串行前提在中途被打破，**性能指标（总耗时 / CPU / s/cmd）分两档可信度**：\n")
    A("| 语言 | 起止 | 并发情况 | 性能指标可信度 |")
    A("|---|---|---|---|")
    A("| python | 11:13:07–11:17:04 | **独占** | 干净 |")
    A("| go | 11:17:04–11:18:54 | **独占** | 干净 |")
    A("| rust | 11:18:45–11:23:5x | 前 3 分钟独占，之后与 ts+js 三路并发 | 偏悲观，仅供参考 |")
    A("| typescript | 11:21:56–11:23:39 | 与 rust+js 三路并发 | 偏悲观，仅供参考 |")
    A("| javascript | 11:21:59–11:28:2x | 与 rust+ts 三路并发 | 偏悲观，仅供参考 |")
    A("")
    A("- 缓解因素：容器都限 `--cpus=2`，宿主 16 核，三路并发时实测各容器仍能吃满自己的配额")
    A("  （`docker stats` 实测 rust 208% / ts 162% / js 97%，宿主 loadavg 3.93），")
    A("  所以 CPU 维度基本没被饿到；主要残留风险是磁盘 IO 与内存带宽争抢。")
    A("- 另有一个无关负载全程占约 1–2 核（`jcore/SuperScalarModel` 的 `gfsim` CI 自测）。")
    A("- **`patch_identical` 的结论不受影响**：五条全为真，而争抢只会让条件更苛刻。\n")

    A("## 最重的命令（按重放墙钟）\n")
    for meta, v, recs in rows:
        if not v:
            A(f"**{meta['language']}** — 重放未跑，暂无数据\n")
            continue
        A(f"**{meta['language']}**\n")
        A("| wall_s | cpu_s | rc | 命令 |")
        A("|---|---|---|---|")
        for r in sorted(recs, key=lambda r: -r["wall_s"])[:5]:
            c = strip_cd(r["cmd"]).splitlines()[0][:90].replace("|", "\\|")
            A(f"| {r['wall_s']:.1f} | {r['usage_usec']/1e6:.1f} | {r['rc']} | `{c}` |")
        A("")

    (ROOT / "INDEX.md").write_text("\n".join(L))
    print(f"-> {ROOT / 'INDEX.md'} ({len(L)} 行)")


if __name__ == "__main__":
    main()
