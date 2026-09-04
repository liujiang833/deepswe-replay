#!/usr/bin/env python3
"""容器重放负载画像：从 replay.py 产出的 commands.jsonl 算出这次重放到底是什么负载。

输入是 `deepswe/replay_out/<trial>/commands.jsonl`（每行一条命令的 cgroup 计量），
输出是六段画像：意图分布 / CPU 归属 / IO / 内存 / 时间结构 / 与原始 trace 的对照。

设计约束（都是为了「每个数字都能复算」）：

1. **不猜**。分类只看 `cmd_stripped` 里真正被执行的程序名，不做正则关键字计数——
   heredoc 正文里全是 `pytest`/`sleep`/`>` 这类词，正则计数会把写文件的命令算成跑测试。
   所以先做一个 heredoc/引号感知的切分器（`split_statements`），把正文剥成 body，
   再对每个语句取程序名。
2. **一条命令一个标签**。指标是按命令采的（cgroup 窗口），没法拆到语句级，
   所以命令级标签 = 该命令所有语句里优先级最高的那个类别，优先级见 `PRIORITY`。
   `--audit` 会逐条打印 i / 类别 / 命中的类别集合 / 命令首行，便于人工抽查。
3. **计量口径照抄 replay.py**：user/system/usage_usec 是 cgroup `cpu.stat` 的窗口差值，
   rbytes/wbytes 是 `io.stat` 的窗口差值（块层，不是 read(2) 字节数），
   mem_peak 是窗口内 `memory.current` 的轮询最大值（含 page cache）。

用法：
    python3 deepswe/summarize_replay.py                      # 文本画像
    python3 deepswe/summarize_replay.py --audit              # 附逐条分类，供抽查
    python3 deepswe/summarize_replay.py --charts             # 另出 3 张 PNG
    python3 deepswe/summarize_replay.py --json out.json      # 机器可读快照
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_JSONL = HERE / "replay_out/gql-incremental-graphql-delivery__nnFNKRL/commands.jsonl"

# ---------------------------------------------------------------- 类别定义

READ, SEARCH, WRITE, TEST, VCS, OTHER = "读文件", "搜索", "写文件", "跑测试", "版本控制", "其他"
CATS = [TEST, WRITE, SEARCH, VCS, READ, OTHER]
# 命令级优先级：一条命令混了多种语句时，取排在前面的那一档。
# 依据：先取「有副作用 / 吃 CPU 的那件事」，纯查看类排最后。
# 唯一的细分是**语法校验**（`python3 -c "import ast; ast.parse(...)"` 这种一行守卫）：
# 它算 TEST，但排在「写文件」之后——因为 trace 里它几乎总是紧跟在一次编辑后面当护栏，
# 那条命令的正事是改文件，不是跑测试。真跑测试（pytest / 复现脚本 / 内联脚本）仍排第一。
SYNTAX_CHECK = "语法/编译校验"
RANKS = [(TEST, None), (WRITE, None), (TEST, SYNTAX_CHECK), (SEARCH, None),
         (VCS, None), (READ, None), (OTHER, None)]
PRIORITY = [TEST, WRITE, SEARCH, VCS, READ, OTHER]


def rank(pair):
    """(类别, 细类) -> 档位序号，越小越优先。"""
    cat, detail = pair
    if cat == TEST and detail == SYNTAX_CHECK:
        return RANKS.index((TEST, SYNTAX_CHECK))
    return RANKS.index((cat, None))

PROG_CAT = {
    # 读
    "cat": READ, "nl": READ, "head": READ, "tail": READ, "less": READ, "more": READ,
    "ls": READ, "wc": READ, "file": READ, "stat": READ, "du": READ, "tree": READ,
    "readlink": READ, "realpath": READ, "od": READ, "xxd": READ, "diff": READ, "sort": READ,
    "uniq": READ, "cut": READ, "tr": READ, "column": READ, "jq": READ,
    # 搜
    "grep": SEARCH, "egrep": SEARCH, "fgrep": SEARCH, "rg": SEARCH, "ag": SEARCH,
    "ack": SEARCH, "find": SEARCH, "locate": SEARCH, "awk": SEARCH, "xargs": SEARCH,
    # 写
    "tee": WRITE, "mkdir": WRITE, "rm": WRITE, "rmdir": WRITE, "mv": WRITE, "cp": WRITE,
    "touch": WRITE, "patch": WRITE, "chmod": WRITE, "chown": WRITE, "ln": WRITE,
    "truncate": WRITE, "install": WRITE,
    # 测/校验
    "pytest": TEST, "tox": TEST, "flake8": TEST, "mypy": TEST, "black": TEST,
    "isort": TEST, "pylint": TEST, "coverage": TEST,
    # 版本控制
    "git": VCS,
    # 其他
    "echo": OTHER, "printf": OTHER, "pwd": OTHER, "true": OTHER, "false": OTHER,
    "sleep": OTHER, "export": OTHER, "cd": OTHER, "which": OTHER, "type": OTHER,
    "pip": OTHER, "pip3": OTHER, "curl": OTHER, "wget": OTHER, "date": OTHER,
    "source": OTHER, ".": OTHER, "set": OTHER, "read": OTHER, "apt": OTHER,
    "apt-get": OTHER, "uname": OTHER, "whoami": OTHER, "id": OTHER, "env": OTHER,
}

# 语句头部可以直接丢掉、继续往后看真正程序名的词
DROP_AND_CONTINUE = {"if", "then", "else", "elif", "do", "while", "until", "!",
                     "time", "command", "exec", "nohup", "sudo", "env", "{", "("}
# 语句头部是这些词时，本语句不承载意图（循环的词表、块结束符等）
STOP_WORDS = {"for", "case", "esac", "fi", "done", "}", ")", ";;", "in", "elif;", "select"}

# python 内联脚本（`python3 -c` / `python3 - <<EOF`）正文的判据，**按下面的顺序**取第一个命中：
#   1 PY_WRITE  正文往文件里写           -> 写文件
#   2 PY_CHECK  ast.parse / compile()    -> 跑测试（语法校验）
#   3 PY_READ   inspect.getsource        -> 读文件（读库源码）
#   4 PY_SCAN   只读地打开文件 + 正则扫描 -> 搜索（脚本扫文件）
#   5 PY_RUN    构造对象/调用函数/assert -> 跑测试（内联脚本验证）
#   6 其余（只 import + print 属性、纯字符串算长度）-> 其他（接口探查）
# 注意不要用裸的 `.write(`：重放里有 aiohttp 复现脚本写的是 HTTP response，不是文件（i=270）
PY_WRITE = re.compile(r"""open\([^)]*['"][wax]\+?['"]|\.writelines\(|"""
                      r"""write_text\(|os\.(rename|replace|remove|unlink)|"""
                      r"""shutil\.(copy|move|rmtree)|\.mkdir\(""", re.S)
PY_CHECK = re.compile(r"ast\.parse|py_compile|compileall|(?<![\w.])compile\(", re.S)
PY_READ = re.compile(r"inspect\.get(source|file|members|signature)", re.S)
PY_SCAN = re.compile(r"\bopen\(", re.S)
PY_SCAN2 = re.compile(r"\bre\.(search|match|finditer|findall|sub|compile)\(|"
                      r"\.(search|findall|finditer|startswith|rstrip)\(", re.S)
# def/assert 必须在行首，否则会被字符串字面量里的 "def foo(" 误伤（i=416 就是这样）
PY_RUN = re.compile(r"^\s*\w+\s*=\s*[\w.]+\(|^\s*(async\s+)?def\s|"
                    r"^\s*assert\s|\bawait\s", re.M)

# ---------------------------------------------------------------- 切分器


def split_statements(cmd: str):
    """把一条 shell 命令切成语句列表，heredoc 正文单独收进 body，不参与后续词法。

    切分点：`&&` `||` `;` `|` 换行。引号内的这些字符不算切分点，heredoc 正文整段不扫描。
    返回 [{"text": …, "body": …, "piped": 是否由 `|` 接在上一段后面}, ...]
    """
    segs, cur, bodies = [], [], []
    piped = False         # 当前累积的这段是不是管道的下游
    pending = []          # 本行末尾待收正文的 heredoc: (delim, 是否 <<-)
    quote = None
    i, n = 0, len(cmd)

    def flush(next_piped=False):
        nonlocal cur, bodies, piped
        t = "".join(cur).strip()
        if t or bodies:
            segs.append({"text": t, "body": "\n".join(bodies), "piped": piped})
        cur, bodies = [], []
        piped = next_piped

    while i < n:
        c = cmd[i]
        if quote:                                     # 引号内：原样吞
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(cmd[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            i += 1; continue
        if c in "'\"":
            quote = c; cur.append(c); i += 1; continue
        if c == "\\" and i + 1 < n:
            cur.append(c); cur.append(cmd[i + 1]); i += 2; continue

        m = re.match(r'<<(-?)\s*(["\']?)([A-Za-z_][A-Za-z0-9_]*)\2', cmd[i:])
        if m:                                         # heredoc 开头，登记分隔符
            pending.append((m.group(3), m.group(1) == "-"))
            cur.append(m.group(0)); i += len(m.group(0)); continue

        if c == "\n":
            if pending:                               # 换行后紧跟正文，按分隔符逐段吃掉
                i += 1
                for delim, _strip in pending:
                    buf = []
                    while i < n:
                        j = cmd.find("\n", i)
                        line = cmd[i:] if j < 0 else cmd[i:j]
                        i = n if j < 0 else j + 1
                        if line.strip() == delim:
                            break
                        buf.append(line)
                    bodies.append("\n".join(buf))
                pending = []
            else:
                i += 1
            flush(); continue

        if cmd.startswith("&&", i) or cmd.startswith("||", i):
            flush(); i += 2; continue
        if c == "|":
            flush(next_piped=True); i += 1; continue
        if c == ";":
            flush(); i += 1; continue
        cur.append(c); i += 1

    flush()
    return segs


def unquote_tokens(text: str):
    """引号感知的粗分词：返回 (tokens, redirect_targets)。

    redirect_targets 只收引号外的 `>` / `>>` 目标，所以 awk '{if ($0 > 88)...}' 里的 `>`
    不会被误判成重定向。
    """
    toks, redirs = [], []
    cur, quote, i, n = [], None, 0, len(text)
    pending_redir = None

    def push():
        nonlocal cur, pending_redir
        if cur:
            t = "".join(cur)
            if pending_redir is not None:
                redirs.append(t); pending_redir = None
            else:
                toks.append(t)
            cur = []

    while i < n:
        c = text[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < n:
                cur.append(text[i + 1]); i += 2; continue
            else:
                cur.append(c)
            i += 1; continue
        if c in "'\"":
            quote = c; i += 1; continue
        if c == "\\" and i + 1 < n:
            cur.append(text[i + 1]); i += 2; continue
        if c.isspace():
            push(); i += 1; continue
        if c == ">":
            push()
            i += 2 if text.startswith(">>", i) else 1
            if i < n and text[i] == "&":               # >&2 之类不是写文件
                i += 1
                while i < n and not text[i].isspace():
                    i += 1
                continue
            pending_redir = True; continue
        if c == "<":
            push()
            while i < n and text[i] == "<":
                i += 1
            continue
        cur.append(c); i += 1
    push()
    return toks, redirs


def program_of(tokens):
    """跳过控制字、赋值、timeout/env 包装，取真正被执行的程序名。"""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in STOP_WORDS:
            return None
        if t in DROP_AND_CONTINUE:
            i += 1; continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", t):     # VAR=val 前缀
            i += 1; continue
        if t == "timeout":                                     # timeout [-k N] SECS prog...
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or
                                       re.fullmatch(r"\d+(\.\d+)?[smhd]?", tokens[i])):
                i += 1
            continue
        return t, tokens[i + 1:]
    return None


def classify_segment(seg):
    """单条语句 -> 类别。返回 (类别, 细类标签) 或 None（无意图，如 `done`）。"""
    toks, redirs = unquote_tokens(seg["text"])
    got = program_of(toks)
    if not got:
        return None
    prog, args = got
    prog = prog.rsplit("/", 1)[-1]
    body = seg["body"]
    writes_file = bool([r for r in redirs if not r.startswith("/dev/")])

    if prog in ("python", "python3", "python3.11", "python3.12"):
        joined = " ".join(args)
        if re.search(r"(^|\s)-m\s+pytest\b", joined) or args[:1] == ["-m"] and args[1:2] == ["pytest"]:
            return TEST, "pytest"
        if re.search(r"(^|\s)-m\s+(compileall|py_compile)\b", joined):
            return TEST, "语法/编译校验"
        if re.search(r"(^|\s)-m\s+pip\b", joined):
            return OTHER, "环境查询"
        code = body
        if "-c" in args:                                     # python3 -c "..." 的正文就是那个参数
            k = args.index("-c")
            code = code + "\n" + " ".join(args[k + 1:])
        if args and args[0].endswith(".py"):                 # python3 /tmp/test_repro_x.py
            return TEST, "复现脚本"
        if writes_file:
            return WRITE, "重定向写文件"
        if PY_WRITE.search(code):
            return WRITE, "python 改文件"
        if PY_CHECK.search(code):
            return TEST, "语法/编译校验"
        if PY_READ.search(code):
            return READ, "读库源码"
        if PY_SCAN.search(code) and PY_SCAN2.search(code):
            return SEARCH, "脚本扫文件"
        if PY_RUN.search(code):
            return TEST, "内联脚本验证"
        return OTHER, "接口探查"

    if prog == "sed":
        return (WRITE, "sed -i") if "-i" in args or any(a.startswith("-i") for a in args) \
            else (READ, "sed 取行段")
    if prog in ("cat", "tee", "printf", "echo") and writes_file:
        return WRITE, ("heredoc 写文件" if seg["body"] else "重定向写文件")
    if prog == "git":
        sub = args[0] if args else ""
        if sub in ("mv", "rm", "clean", "checkout", "restore", "apply", "reset", "stash"):
            return VCS, f"git {sub}（改工作区）"
        return VCS, f"git {sub}" if sub else VCS
    if prog in ("pip", "pip3"):
        return OTHER, "环境查询"
    if prog == "bash" or prog == "sh":
        return OTHER, "子 shell"

    cat = PROG_CAT.get(prog)
    if cat is None:
        return OTHER, f"未识别:{prog}"
    if cat == READ and writes_file:
        return WRITE, "重定向写文件"
    return cat, prog


# 管道头是这些程序时，它只是给下游喂文本，意图由下游决定
TRANSPARENT_HEADS = {"cat", "nl", "head", "tail", "sed 取行段", "ls", "echo",
                     "git diff", "git show", "git log"}
# 出现在管道下游时不算意图的「分页/查看」程序
VIEWER_DETAILS = {"cat", "nl", "head", "tail", "sed 取行段", "wc", "sort", "uniq",
                  "cut", "tr", "column", "jq", "less", "more", "echo"}


def classify_pipeline(pipe):
    """一条管道 -> (类别, 细类)。规则：**管道头决定意图**，
    因为下游多半只是 head/tail/grep 在过滤；唯一例外是管道头本身只是喂文本
    （cat/nl/sed -n/git diff…），这时看下游第一个非查看类的程序。"""
    res = [r for r in (classify_segment(s) for s in pipe) if r]
    if not res:
        return None
    head = res[0]
    if head[1] in TRANSPARENT_HEADS and len(res) > 1:
        rest = [r for r in res[1:] if r[1] not in VIEWER_DETAILS]
        if rest:
            return min(rest, key=rank)
    return head


def classify_command(cmd: str):
    """整条命令 -> (主类别, 命中的类别集合, (类别,细类) 列表, 主类别对应的细类)。"""
    pipes = []
    for seg in split_statements(cmd):
        if seg["piped"] and pipes:
            pipes[-1].append(seg)
        else:
            pipes.append([seg])
    pairs = [r for r in (classify_pipeline(p) for p in pipes) if r]
    if not pairs:
        return OTHER, set(), [], "空命令"
    hits = {c for c, _ in pairs}
    main, main_detail = min(pairs, key=rank)
    return main, hits, pairs, main_detail


# ---------------------------------------------------------------- 统计


def pct(x, tot):
    return 0.0 if not tot else 100.0 * x / tot


def q(sorted_vals, p):
    """线性插值分位数，避免依赖 numpy。"""
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def load(path):
    recs = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    for r in recs:
        r["cat"], r["cats"], r["pairs"], r["detail"] = classify_command(r["cmd_stripped"])
        r["details"] = [d for _, d in r["pairs"]]
        r["cpu_s"] = r["usage_usec"] / 1e6
        r["user_s"] = r["user_usec"] / 1e6
        r["sys_s"] = r["system_usec"] / 1e6
    return recs


SHORTEN = [(r"^timeout \d+ ", ""), (r"python3 -m pytest", "pytest"),
           (r"--cov-report=\S+", ""), (r"\s*>\s*/tmp/\S+.*$", ""), (r"\s*2>&1.*$", ""),
           (r"\s+", " ")]


def short_cmd(r, w=52):
    """图里用的短标签：砍掉 timeout/重定向/--cov-report 这些每条都一样的噪声，
    好让 15 条 pytest 之间真正的差别（--cov 的目标模块）露出来。"""
    s = r["cmd_stripped"].strip().splitlines()[0]
    for pat, rep in SHORTEN:
        s = re.sub(pat, rep, s)
    s = s.strip()
    return s[:w] + ("…" if len(s) > w else "")


def first_line(r, w=80):
    s = r["cmd_stripped"].strip().splitlines()[0] if r["cmd_stripped"].strip() else ""
    return s[:w]


def build(recs, cmd_timeout_s=30, cpu_quota=2):
    n = len(recs)
    tot_cpu = sum(r["cpu_s"] for r in recs)
    tot_user = sum(r["user_s"] for r in recs)
    tot_sys = sum(r["sys_s"] for r in recs)
    tot_wall = sum(r["wall_s"] for r in recs)

    by_cat = {}
    for c in CATS:
        g = [r for r in recs if r["cat"] == c]
        by_cat[c] = {
            "n": len(g), "n_pct": pct(len(g), n),
            "cpu_s": sum(r["cpu_s"] for r in g), "cpu_pct": pct(sum(r["cpu_s"] for r in g), tot_cpu),
            "wall_s": sum(r["wall_s"] for r in g), "wall_pct": pct(sum(r["wall_s"] for r in g), tot_wall),
            "user_s": sum(r["user_s"] for r in g), "sys_s": sum(r["sys_s"] for r in g),
            "mem_peak_max": max((r["mem_peak"] for r in g), default=0),
        }

    # 细类按「主类别对应的那条语句」归一，所以条数与 CPU 都是不重叠的划分
    detail = {}
    for r in recs:
        e = detail.setdefault(r["detail"], {"n": 0, "cpu_s": 0.0})
        e["n"] += 1
        e["cpu_s"] += r["cpu_s"]

    walls = sorted(r["wall_s"] for r in recs)
    mems = sorted(r["mem_peak"] for r in recs)
    par = sorted(r["cpu_s"] / r["wall_s"] if r["wall_s"] else 0 for r in recs)

    io_r = [r for r in recs if r["rbytes"]]
    io_w = [r for r in recs if r["wbytes"]]
    mism = [r for r in recs if r["rc"] != r["trace_rc"]]

    return {
        "n": n, "tot_cpu": tot_cpu, "tot_user": tot_user, "tot_sys": tot_sys,
        "tot_wall": tot_wall, "by_cat": by_cat, "detail": detail,
        "walls": walls, "mems": mems, "par": par,
        "io_r": io_r, "io_w": io_w, "mism": mism,
        "cpu_quota": cpu_quota, "cmd_timeout_s": cmd_timeout_s,
    }


# ---------------------------------------------------------------- 报告


def report(recs, S, audit=False):
    out = []
    P = out.append
    n, tc, tw = S["n"], S["tot_cpu"], S["tot_wall"]

    P("=" * 96)
    P(f"重放负载画像  n={n} 条命令   墙钟合计 {tw:.1f}s   CPU 合计 {tc:.1f}s "
      f"(user {S['tot_user']:.1f}s + sys {S['tot_sys']:.1f}s)")
    P("=" * 96)

    tiers = " > ".join(f"{c}" if d is None else f"{c}[{d}]" for c, d in RANKS)
    P(f"\n【1】命令意图分布（一条命令一个主类别，档位优先级：{tiers}）")
    P(f"{'类别':<8} {'条数':>5} {'条数占比':>9} {'CPU 秒':>10} {'CPU 占比':>9} "
      f"{'墙钟秒':>9} {'墙钟占比':>9} {'user/sys':>9}")
    for c in CATS:
        e = S["by_cat"][c]
        us = e["user_s"] / e["sys_s"] if e["sys_s"] else float("inf")
        P(f"{c:<8} {e['n']:>5} {e['n_pct']:>8.1f}% {e['cpu_s']:>10.1f} {e['cpu_pct']:>8.1f}% "
          f"{e['wall_s']:>9.1f} {e['wall_pct']:>8.1f}% {us:>9.1f}")
    P(f"{'合计':<8} {n:>5} {100.0:>8.1f}% {tc:>10.1f} {100.0:>8.1f}% {tw:>9.1f} {100.0:>8.1f}%")

    P("\n  细类（不重叠划分：每条命令按其主类别对应的那条语句归一类）")
    for d, e in sorted(S["detail"].items(), key=lambda kv: -kv[1]["cpu_s"])[:14]:
        P(f"    {d:<22} 命中 {e['n']:>4} 条   CPU {e['cpu_s']:>8.1f}s  ({pct(e['cpu_s'], tc):>5.1f}%)")

    multi = [r for r in recs if len(r["cats"]) > 1]
    P(f"\n  跨类别命令（一条里混了多种意图）：{len(multi)} 条 / {n} "
      f"({pct(len(multi), n):.1f}%)，已按上面的优先级归到单一主类别")

    P("\n【2】CPU 归属：top 15（usage_usec 降序）")
    P(f"{'i':>5} {'user_s':>8} {'sys_s':>7} {'wall_s':>7} {'u/s':>6} {'累计CPU%':>9}  命令前 80 字符")
    acc = 0.0
    for r in sorted(recs, key=lambda x: -x["cpu_s"])[:15]:
        acc += r["cpu_s"]
        us = r["user_s"] / r["sys_s"] if r["sys_s"] else float("inf")
        P(f"{r['i']:>5} {r['user_s']:>8.1f} {r['sys_s']:>7.1f} {r['wall_s']:>7.1f} {us:>6.1f} "
          f"{pct(acc, tc):>8.1f}%  {first_line(r)}")
    srt = sorted(recs, key=lambda x: -x["cpu_s"])
    for k in (15, 30, 50):
        P(f"  top{k:>3} 条 = {pct(sum(r['cpu_s'] for r in srt[:k]), tc):.1f}% CPU")
    npy = sum(1 for r in recs if r["cat"] == TEST)
    P(f"  user/sys 总比 = {S['tot_user'] / S['tot_sys']:.1f}  "
      f"（user {pct(S['tot_user'], tc):.1f}% / sys {pct(S['tot_sys'], tc):.1f}%）"
      f"；跑测试类 {npy} 条")

    P("\n【3】IO（cgroup io.stat 块层字节差值，不是 read(2)/write(2) 计数）")
    P(f"  rbytes 非零 {len(S['io_r'])}/{n} 条，合计 {sum(r['rbytes'] for r in S['io_r'])/1e6:.1f} MB")
    P(f"  wbytes 非零 {len(S['io_w'])}/{n} 条，合计 {sum(r['wbytes'] for r in S['io_w'])/1e6:.1f} MB")
    P("  rbytes 非零的命令（按 i 序，看首次触碰）：")
    for r in S["io_r"]:
        P(f"    i={r['i']:>4} r={r['rbytes']/1e6:>7.2f}MB w={r['wbytes']/1e6:>6.2f}MB "
          f"wall={r['wall_s']:>6.2f}s  {first_line(r, 62)}")
    for c in CATS:                                   # 哪些类别根本不产生块层读
        g = [r for r in recs if r["cat"] == c]
        nz = sum(1 for r in g if r["rbytes"])
        P(f"    {c:<8} {len(g):>4} 条中 rbytes 非零 {nz:>3} 条，wbytes 非零 "
          f"{sum(1 for r in g if r['wbytes']):>3} 条")
    top_r = sorted(S["io_r"], key=lambda x: -x["rbytes"])[:3]
    P(f"  读字节 top3 占总读的 {pct(sum(r['rbytes'] for r in top_r), sum(r['rbytes'] for r in recs)):.1f}%")
    P("  写字节 top 8：")
    for r in sorted(recs, key=lambda x: -x["wbytes"])[:8]:
        P(f"    i={r['i']:>4} w={r['wbytes']/1e6:>7.2f}MB  {first_line(r, 66)}")

    P("\n【4】内存（mem_peak = 命令窗口内 memory.current 轮询最大值，含 page cache）")
    m = S["mems"]
    for lbl, p in [("min", 0), ("p25", .25), ("中位", .5), ("p75", .75), ("p90", .9),
                   ("p99", .99), ("max", 1.0)]:
        P(f"    {lbl:>4} {q(m, p)/1e6:>9.1f} MB")
    P("  mem_peak top 8：")
    for r in sorted(recs, key=lambda x: -x["mem_peak"])[:8]:
        P(f"    i={r['i']:>4} {r['mem_peak']/1e6:>7.1f}MB  samples={r['mem_samples']:>5} "
          f"wall={r['wall_s']:>6.2f}s  {first_line(r, 52)}")
    P("  各类别 mem_peak 最大值：")
    for c in CATS:
        P(f"    {c:<8} {S['by_cat'][c]['mem_peak_max']/1e6:>7.1f} MB")
    rd = sorted(r["mem_peak"] for r in recs if r["cat"] == READ)
    P(f"  只读类命令（{len(rd)} 条 cat/nl/sed，进程 RSS 至多几 MB）的 mem_peak 中位数 = "
      f"{q(rd, .5)/1e6:.1f} MB —— 这就是口径问题：memory.current 是整个容器的，含 page cache")
    k = next(j for j, r in enumerate(recs) if r["detail"] == "pytest")   # 首次跑 pytest
    before = sorted(r["mem_peak"] for r in recs[:k])
    after = sorted(r["mem_peak"] for r in recs[k + 1:])
    P(f"  首次 pytest 在 i={recs[k]['i']}。之前 {k} 条 mem_peak 中位 {q(before, .5)/1e6:.1f} MB，"
      f"之后 {len(after)} 条中位 {q(after, .5)/1e6:.1f} MB、最小 {min(after)/1e6:.1f} MB")
    P(f"  → 缓存暖起来后再没跌回去，这 ~{min(after)/1e6:.0f} MB 是容器常驻底噪，不属于当前命令；"
      f"pytest 自身的增量约 {(max(r['mem_peak'] for r in recs) - min(after))/1e6:.0f} MB")
    lowsample = sum(1 for r in recs if r["mem_samples"] <= 5)
    P(f"  采样次数 <=5 的命令：{lowsample} 条（20ms 轮询，短命令只够采几个点）")

    P("\n【5】时间结构")
    w = S["walls"]
    for lbl, p in [("min", 0), ("p25", .25), ("中位", .5), ("p75", .75), ("p90", .9),
                   ("p95", .95), ("max", 1.0)]:
        P(f"    wall {lbl:>4} {q(w, p):>8.3f} s")
    fast = sum(1 for x in w if x < 1.0)
    slow = sum(1 for x in w if x >= 10.0)
    P(f"  wall < 1s：{fast} 条（{pct(fast, n):.1f}%），占墙钟 "
      f"{pct(sum(r['wall_s'] for r in recs if r['wall_s'] < 1), tw):.1f}%")
    P(f"  wall >= 10s：{slow} 条（{pct(slow, n):.1f}%），占墙钟 "
      f"{pct(sum(r['wall_s'] for r in recs if r['wall_s'] >= 10), tw):.1f}%")
    quota = S["cpu_quota"]
    P(f"  CPU 合计 / 墙钟合计 = {tc:.1f} / {tw:.1f} = {tc/tw:.3f} 核")
    P(f"  容器配额 {quota} 核 → 核·秒预算 {tw*quota:.0f}，实际用掉 {tc:.0f}，"
      f"利用率 {pct(tc, tw*quota):.1f}%，空转 {tw*quota-tc:.0f} 核·秒")
    idle = sum(max(0.0, r["wall_s"] - r["cpu_s"]) for r in recs)
    P(f"  单核口径的「CPU 没干活」墙钟 = Σmax(0, wall - cpu) = {idle:.1f}s "
      f"（占墙钟 {pct(idle, tw):.1f}%）")
    P(f"  单命令并行度 cpu/wall：中位 {q(S['par'], .5):.2f}，p90 {q(S['par'], .9):.2f}，"
      f"max {max(S['par']):.2f}")
    over1 = sum(1 for x in S["par"] if x > 1.05)
    P(f"  cpu/wall > 1.05（真正用到第二个核）的命令：{over1} 条")

    P("\n【6】与原始 trace 的对照（rc vs trace_rc）")
    P(f"  不一致 {len(S['mism'])}/{n} 条")
    kinds = {}
    for r in S["mism"]:
        k = ("重放超时(rc=124)，原始也超时(trace_rc=-1)" if r["trace_rc"] == -1
             else f"重放超时(rc=124)，原始成功(trace_rc={r['trace_rc']})" if r["timed_out"]
             else f"其他 rc={r['rc']} trace_rc={r['trace_rc']}")
        kinds.setdefault(k, []).append(r["i"])
    for k, v in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        P(f"    {len(v):>3} 条  {k}")
        P(f"          i = {v}")
    cov = [r for r in S["mism"] if "--cov" in r["cmd_stripped"]]
    P(f"  其中命令行带 --cov 的：{len(cov)} 条")
    P(f"  重放的单命令超时上限 = {S['cmd_timeout_s']}s；这些命令的 wall 落在 "
      f"[{min(r['wall_s'] for r in S['mism']):.2f}, {max(r['wall_s'] for r in S['mism']):.2f}]s")
    lost = sum(1 for r in recs if r["timed_out"])
    P(f"  被截断的命令共 {lost} 条，它们贡献了 "
      f"{pct(sum(r['cpu_s'] for r in recs if r['timed_out']), tc):.1f}% 的 CPU；"
      f"真实负载至少被少算这一截")

    if audit:
        P("\n【审计】逐条分类（i / 主类别 / 命中类别 / 细类 / 命令首行）")
        for r in recs:
            P(f"  {r['i']:>4} {r['cat']:<6} {'+'.join(sorted(r['cats'])):<24} "
              f"{r['detail'][:20]:<20} {first_line(r, 62)}")
    return "\n".join(out)


# ---------------------------------------------------------------- 图


def charts(recs, S, outdir: Path):
    """三张纯白底 PNG。调色板取 dataviz skill 的参考实例（references/palette.md）
    的分类槽位 1..8 原序：该顺序在 light 模式下已通过相邻对 CVD/常视 Delta E 门限。
    本机没有 node，跑不了 validate_palette.js，所以只**原样使用**已验证的槽位与顺序，
    不自造色；三张图都给每根条直接标数值（relief rule）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    fp = Path("/home/river/.local/share/fonts/WenQuanYiMicroHei.ttf")
    if fp.exists():
        font_manager.fontManager.addfont(str(fp))
        plt.rcParams["font.family"] = ["WenQuanYi Micro Hei"]
    plt.rcParams["axes.unicode_minus"] = False

    WHITE, INK, INK2, INK3 = "#ffffff", "#0b0b0b", "#52514e", "#8a8880"
    S1, S2 = "#2a78d6", "#eb6834"          # 分类槽位 1 / 2
    GRID = "#e6e5e1"

    def frame(ax):
        ax.set_facecolor(WHITE)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(GRID)
        ax.tick_params(colors=INK2, length=0)

    # ---- 图 1：意图分布，两口径 ----
    cats = [c for c in CATS if S["by_cat"][c]["n"] > 0]
    ns = [S["by_cat"][c]["n_pct"] for c in cats]
    cs = [S["by_cat"][c]["cpu_pct"] for c in cats]
    order = sorted(range(len(cats)), key=lambda k: -cs[k])
    cats = [cats[k] for k in order]; ns = [ns[k] for k in order]; cs = [cs[k] for k in order]

    fig, ax = plt.subplots(figsize=(10.2, 5.6), dpi=200)
    fig.patch.set_facecolor(WHITE); frame(ax)
    y = list(range(len(cats)))[::-1]
    h = 0.36
    b1 = ax.barh([v + h / 2 + 0.01 for v in y], ns, height=h, color=S1, label="条数占比")
    b2 = ax.barh([v - h / 2 - 0.01 for v in y], cs, height=h, color=S2, label="CPU 时间占比")
    for bars, vals, raw in ((b1, ns, [S["by_cat"][c]["n"] for c in cats]),
                            (b2, cs, [S["by_cat"][c]["cpu_s"] for c in cats])):
        for bar, v, rv in zip(bars, vals, raw):
            txt = f"{v:.1f}%  ({rv} 条)" if bars is b1 else f"{v:.1f}%  ({rv:.0f}s)"
            ax.text(v + 1.2, bar.get_y() + bar.get_height() / 2, txt,
                    va="center", ha="left", fontsize=9.5, color=INK)
    ax.set_yticks(y); ax.set_yticklabels(cats, fontsize=11, color=INK)
    ax.set_xlim(0, 128)                       # 留出条尾直接标注的位置，刻度仍只画到 100
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.set_xlabel("占比 %", color=INK2, fontsize=10)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
    leg = ax.legend(loc="lower right", frameon=False, fontsize=10)
    for t in leg.get_texts():
        t.set_color(INK)
    fig.text(0.5, 0.975, f"重放 {S['n']} 条命令的意图构成：按条数看是「读+搜」，按 CPU 看只有跑测试",
             ha="center", va="top", fontsize=13.5, color=INK)
    fig.text(0.5, 0.928,
             f"CPU = cgroup cpu.stat usage_usec 窗口差值，合计 {S['tot_cpu']:.0f}s"
             f"　·　一条命令归一个主类别，按 CPU 占比排序"
             f"（档位 跑测试>写文件>语法校验>搜索>版本控制>读文件>其他）",
             ha="center", va="top", fontsize=9, color=INK2)
    fig.subplots_adjust(top=0.85, left=0.12, right=0.97, bottom=0.12)
    p1 = outdir / "workload_intent_mix.png"
    fig.savefig(p1, facecolor=WHITE); plt.close(fig)

    # ---- 图 2：CPU top 15 ----
    top = sorted(recs, key=lambda x: -x["cpu_s"])[:15]
    fig, ax = plt.subplots(figsize=(11.4, 6.4), dpi=200)
    fig.patch.set_facecolor(WHITE); frame(ax)
    y = list(range(len(top)))[::-1]
    ax.barh(y, [r["user_s"] for r in top], height=0.62, color=S1, label="user")
    ax.barh(y, [r["sys_s"] for r in top], height=0.62, left=[r["user_s"] for r in top],
            color=S2, label="system", edgecolor=WHITE, linewidth=2)
    for yy, r in zip(y, top):
        ax.text(r["cpu_s"] + 0.4, yy, f"{r['cpu_s']:.1f}s  (wall {r['wall_s']:.1f}s)",
                va="center", ha="left", fontsize=9, color=INK)
    ax.set_yticks(y)
    ax.set_yticklabels([f"#{r['i']}  {short_cmd(r)}" for r in top], fontsize=8.2, color=INK)
    ax.set_xlim(0, max(r["cpu_s"] for r in top) * 1.30)
    ax.set_xlabel("CPU 秒", color=INK2, fontsize=10)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
    leg = ax.legend(loc="lower right", frameon=False, fontsize=10)
    for t in leg.get_texts():
        t.set_color(INK)
    share = pct(sum(r["cpu_s"] for r in top), S["tot_cpu"])
    fig.text(0.5, 0.975, f"CPU 归属：{len(top)} 条命令吃掉 {share:.0f}% 的 CPU，全部是 pytest",
             ha="center", va="top", fontsize=13.5, color=INK)
    fig.text(0.5, 0.932,
             f"user : system = {S['tot_user']/S['tot_sys']:.0f} : 1 → 纯用户态计算负载，"
             f"不是内核/系统调用密集型",
             ha="center", va="top", fontsize=9, color=INK2)
    fig.subplots_adjust(top=0.87, left=0.35, right=0.97, bottom=0.10)
    p2 = outdir / "workload_cpu_top15.png"
    fig.savefig(p2, facecolor=WHITE); plt.close(fig)

    # ---- 图 3：wall 分布（对数分箱）----
    fig, ax = plt.subplots(figsize=(10.2, 5.4), dpi=200)
    fig.patch.set_facecolor(WHITE); frame(ax)
    walls = [r["wall_s"] for r in recs]
    lo, hi = min(walls), max(walls)
    bins = [10 ** e for e in
            [math.log10(lo) + i * (math.log10(hi) - math.log10(lo)) / 28 for i in range(29)]]
    ax.hist(walls, bins=bins, color=S1, edgecolor=WHITE, linewidth=0.8)
    ax.set_xscale("log")
    ax.set_xlabel("单条命令墙钟（秒，对数轴）", color=INK2, fontsize=10)
    ax.set_ylabel("命令条数", color=INK2, fontsize=10)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8); ax.set_axisbelow(True)
    n_fast = sum(1 for x in walls if x < 1)
    n_slow = sum(1 for x in walls if x >= 10)
    w_fast = pct(sum(x for x in walls if x < 1), S["tot_wall"])
    w_slow = pct(sum(x for x in walls if x >= 10), S["tot_wall"])
    ax.annotate(f"{n_fast} 条 < 1s\n只占 {w_fast:.1f}% 墙钟", xy=(0.12, ax.get_ylim()[1] * 0.72),
                fontsize=10, color=INK, ha="center")
    ax.annotate(f"{n_slow} 条 ≥ 10s\n占 {w_slow:.1f}% 墙钟", xy=(16.0, ax.get_ylim()[1] * 0.30),
                fontsize=10, color=INK, ha="center")
    ax.axvline(S["cmd_timeout_s"], color=INK3, linewidth=1.2, linestyle="--")
    ax.text(S["cmd_timeout_s"] * 0.97, ax.get_ylim()[1] * 0.95,
            f"重放超时上限 {S['cmd_timeout_s']}s", rotation=90, ha="right", va="top",
            fontsize=8.5, color=INK2)
    fig.text(0.5, 0.975, "时间结构：双峰。绝大多数命令 <0.1s，墙钟几乎全被 20~30s 的测试跑吃掉",
             ha="center", va="top", fontsize=13.5, color=INK)
    fig.text(0.5, 0.930,
             f"墙钟合计 {S['tot_wall']:.0f}s，CPU 合计 {S['tot_cpu']:.0f}s → 平均并行度 "
             f"{S['tot_cpu']/S['tot_wall']:.2f} 核（配额 {S['cpu_quota']} 核，利用率 "
             f"{pct(S['tot_cpu'], S['tot_wall']*S['cpu_quota']):.0f}%）",
             ha="center", va="top", fontsize=9, color=INK2)
    fig.subplots_adjust(top=0.86, left=0.09, right=0.97, bottom=0.13)
    p3 = outdir / "workload_time_structure.png"
    fig.savefig(p3, facecolor=WHITE); plt.close(fig)
    return [p1, p2, p3]


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", default=str(DEFAULT_JSONL))
    ap.add_argument("--audit", action="store_true", help="逐条打印分类，供人工抽查")
    ap.add_argument("--charts", action="store_true", help="额外输出 3 张 PNG")
    ap.add_argument("--outdir", default=str(HERE))
    ap.add_argument("--json", dest="json_out", default=None, help="把画像存成 JSON")
    ap.add_argument("--cmd-timeout", type=int, default=30, help="重放时的单命令超时（画超时线）")
    ap.add_argument("--cpus", type=float, default=2, help="容器 CPU 配额")
    a = ap.parse_args()

    recs = load(a.jsonl)
    S = build(recs, a.cmd_timeout, a.cpus)
    print(report(recs, S, audit=a.audit))

    if a.charts:
        for p in charts(recs, S, Path(a.outdir)):
            print(f"\nwrote {p}")
    if a.json_out:
        blob = {
            "jsonl": str(a.jsonl), "n": S["n"],
            "totals": {"cpu_s": S["tot_cpu"], "user_s": S["tot_user"], "sys_s": S["tot_sys"],
                       "wall_s": S["tot_wall"]},
            "by_cat": S["by_cat"],
            "per_cmd": [{"i": r["i"], "cat": r["cat"], "details": r["details"],
                         "detail": r["detail"], "cpu_s": r["cpu_s"],
                         "wall_s": r["wall_s"]} for r in recs],
        }
        Path(a.json_out).write_text(json.dumps(blob, ensure_ascii=False, indent=1))
        print(f"\nwrote {a.json_out}")


if __name__ == "__main__":
    main()
