#!/usr/bin/env python3
"""Two pies of the same 19 shell operations: weighted by count, and by execution time.

`cd` prefixes are excluded (每条 docker exec 都是新子 shell，cd 是协议开销不是工作).

The raw analysis has 10 intents, six of them singletons -- a 10-slice pie with six
identical slivers is unreadable, so intents fold into 6 work phases (<=6 segments).
The full 10-way table stays in OPERATIONS_astropy-12907.md as the detail view.

Both charts use ONE fixed wedge order and a fixed phase->color map, because color
follows the entity and never its rank -- which also lets the reader compare the two
pies wedge-for-wedge. Palette = validated categorical slots 1-6; the ring's adjacent
pairs INCLUDING the wrap pair (slot 6 <-> slot 1) pass the skill's validator in light
mode. Three slots sit under 3:1 contrast there, so every wedge is directly labelled
(the relief rule) -- identity is carried by text, never by color alone.
"""
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager


font_manager.fontManager.addfont("/home/river/.local/share/fonts/WenQuanYiMicroHei.ttf")
plt.rcParams["font.family"] = ["WenQuanYi Micro Hei"]
plt.rcParams["axes.unicode_minus"] = False

SURFACE, INK, INK2, INK3 = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8880"

# fixed wedge order + fixed color per phase (validated as a ring, wrap included)
# 标注写的是命令形态，不是"是不是 Python"——pytest 本身就是个 Python console script
# (#!/opt/miniconda3/envs/testbed/bin/python)，跑测试同样是 Python 进程。
# 有意义的区分是代码谁写的：heredoc 是模型现场写的，pytest 跑的是仓库里现成的测试。
# 那三条 heredoc 之间再靠"正文写不写盘"和"相对改源码的位置"区分。
PHASES = [
    ("读代码 / 搜索",       "#2a78d6", ["read_file", "list_dir", "search_files", "search_content"]),
    ("生成 / 检查 patch",   "#eb6834", ["make_patch", "vcs"]),
    ("跑测试（pytest）",     "#1baf7a", ["run_tests"]),
    ("复现脚本（python heredoc）", "#eda100", ["repro_script"]),
    ("验证脚本（python heredoc）", "#4a3aa7", ["verify_script"]),
    ("改源码（python heredoc）",   "#e87ba4", ["edit_file"]),
    ("提交",                "#008300", ["submit"]),
]
# which phase a whole command belongs to, when it mixes intents
PRIORITY = ["submit", "run_tests", "edit_file", "repro_script", "verify_script",
            "make_patch", "vcs", "search_content", "search_files", "read_file", "list_dir"]
INTENT2PHASE = {k: name for name, _, keys in PHASES for k in keys}


def by_count(res):
    counts = dict(res["intents"])
    unmapped = set(counts) - set(INTENT2PHASE)
    assert not unmapped, f"unmapped intents: {unmapped}"
    vals = {name: sum(counts.get(k, 0) for k in keys) for name, _, keys in PHASES}
    assert sum(vals.values()) == res["n_real_ops"]
    return vals, "次", lambda v: f"{v:.0f} 次"


def command_phase(call: dict) -> str:
    """Phase of a whole command, from the intents the analyzer already assigned.

    Deliberately NOT a re-classification: repro vs verify is decided in
    analyze_traj by position relative to the first edit, and re-running
    classify() here would silently collapse the two back together.
    """
    intents = {op["intent"] for op in call["ops"]}
    for p in PRIORITY:
        if p in intents:
            return INTENT2PHASE[p]
    raise AssertionError(f"no phase for {call['command'][:60]!r} ({intents})")


def by_time(timing, res):
    rows, calls = timing["commands"], res["calls"]
    assert len(rows) == len(calls), (len(rows), len(calls))
    vals = {name: 0.0 for name, _, _ in PHASES}
    for row, call in zip(rows, calls):
        assert row["command"] == call["command"], f"order mismatch at #{row['n']}"
        vals[command_phase(call)] += row["seconds"]
    return vals, "秒", lambda v: f"{v:.2f} s"


def draw(vals, unit_fmt, out, title, subtitle, takeaway):
    rows = [(name, color, vals[name]) for name, color, _ in PHASES if vals[name] > 0]
    total = sum(r[2] for r in rows)
    fig, ax = plt.subplots(figsize=(9.6, 6.9), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    wedges, _ = ax.pie(
        [r[2] for r in rows], colors=[r[1] for r in rows],
        startangle=90, counterclock=False, radius=1.0,
        wedgeprops=dict(edgecolor=SURFACE, linewidth=2.0),   # 2px surface gap
    )

    placed = []
    for w, (label, _, v) in zip(wedges, rows):
        ang = math.radians((w.theta1 + w.theta2) / 2)
        x, y = math.cos(ang), math.sin(ang)
        placed.append({"x": x, "y": y, "ly": y * 1.28, "side": 1 if x >= 0 else -1,
                       "text": f"{label}\n{unit_fmt(v)} · {v/total*100:.0f}%"})
    for side in (1, -1):
        grp = sorted([p for p in placed if p["side"] == side], key=lambda p: p["ly"])
        for a, b in zip(grp, grp[1:]):
            if b["ly"] - a["ly"] < 0.34:
                b["ly"] = a["ly"] + 0.34
        shift = max(0.0, max((p["ly"] for p in grp), default=0) - 1.24)
        for p in grp:
            p["ly"] -= shift
    for p in placed:
        ax.annotate(p["text"], xy=(p["x"] * 1.01, p["y"] * 1.01),
                    xytext=(p["side"] * 1.30, p["ly"]),
                    ha="left" if p["side"] > 0 else "right", va="center",
                    fontsize=10.5, color=INK, linespacing=1.45,
                    arrowprops=dict(arrowstyle="-", color=INK3, linewidth=0.9,
                                    shrinkA=0, shrinkB=4, connectionstyle="arc3,rad=0"))

    ax.set_xlim(-2.30, 2.30); ax.set_ylim(-1.95, 1.55)
    ax.set_aspect("equal"); ax.axis("off")
    fig.text(0.5, 0.975, title, ha="center", va="top", fontsize=14, color=INK)
    fig.text(0.5, 0.928, subtitle, ha="center", va="top", fontsize=9.5, color=INK2)
    fig.text(0.5, 0.885, takeaway, ha="center", va="top", fontsize=10.5, color=INK)
    fig.savefig(out, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.28)
    plt.close(fig)
    print(f"wrote {out}")
    for name, _, v in rows:
        print(f"   {name:18s} {unit_fmt(v):>8}  {v/total*100:5.1f}%")


if __name__ == "__main__":
    res = json.loads(Path("traj_analysis.json").read_text())
    timing = json.loads(Path("exec_timing.json").read_text())

    vals, _, fmt = by_count(res)
    draw(vals, fmt, "op_mix_count.png",
         f"{res['instance_id']}：agent 的 {res['n_real_ops']} 个 shell 操作构成（按次数）",
         f"已剥离 {res['n_cd_prefix']} 次 cd 前缀（每条 docker exec 都是新子 shell，必须重新 cd）"
         f"　·　{res['model']}　·　{res['n_steps']} 步 / ${res['instance_cost']:.4f}",
         "读代码与搜索占 42%；模型自己写的 python 只有 3 段 heredoc：复现 / 验证 / 改源码各 1 次")

    vals, _, fmt = by_time(timing, res)
    draw(vals, fmt, "op_mix_time.png",
         f"{res['instance_id']}：同样 19 个操作，按容器内执行时间",
         f"14 条命令在同镜像中按原顺序重跑、取 3 次最小值，合计 {timing['total_seconds']:.2f}s"
         f"　·　{timing['image'].split('/')[-1]}",
         "按时间看结论翻转：pytest 独占 60%；同为 python heredoc，复现 1.95s 而改源码只要 0.26s")
