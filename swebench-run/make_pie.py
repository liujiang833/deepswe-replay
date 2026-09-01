#!/usr/bin/env python3
"""Pie of what the agent's shell operations actually were, `cd` prefixes excluded.

The raw analysis has 10 intents, six of them singletons -- a 10-slice pie with six
identical slivers is unreadable, so the intents are folded into 6 work phases
(<=6 segments is the limit for a readable part-to-whole). The full 10-way table
stays in OPERATIONS_astropy-12907.md as the detail view.

Palette: the validated categorical slots 1-6, assigned in fixed order to wedges
sorted large->small. Validated with the skill's validator for the ring's adjacent
pairs INCLUDING the wrap pair (slot 6 <-> slot 1), light and dark.
Light mode flags 3 slots under 3:1 contrast, so every wedge is directly labelled
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

# intent -> work phase
PHASES = [
    ("读代码 / 搜索", ["read_file", "list_dir", "search_files", "search_content"]),
    ("生成 / 检查 patch", ["make_patch", "vcs"]),
    ("跑测试", ["run_tests"]),
    ("复现 / 验证脚本", ["repro_script"]),
    ("改源码", ["edit_file"]),
    ("提交", ["submit"]),
]

THEME = {
    "light": dict(surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", ink3="#8a8880",
                  series=["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]),
    "dark":  dict(surface="#1a1a19", ink="#ffffff", ink2="#c3c2b7", ink3="#8a8880",
                  series=["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"]),
}


def build(res):
    counts = dict(res["intents"])
    rows = []
    for label, keys in PHASES:
        n = sum(counts.get(k, 0) for k in keys)
        if n:
            rows.append((label, n, keys))
    missing = set(counts) - {k for _, ks in PHASES for k in ks}
    assert not missing, f"unmapped intents: {missing}"
    assert sum(r[1] for r in rows) == res["n_real_ops"]
    return sorted(rows, key=lambda r: -r[1])


def draw(res, mode, out):
    t = THEME[mode]
    rows = build(res)
    total = sum(r[1] for r in rows)
    fig, ax = plt.subplots(figsize=(9.6, 6.9), dpi=200)
    fig.patch.set_facecolor(t["surface"])
    ax.set_facecolor(t["surface"])

    wedges, _ = ax.pie(
        [r[1] for r in rows],
        colors=t["series"][: len(rows)],
        startangle=90, counterclock=False,
        radius=1.0,
        wedgeprops=dict(edgecolor=t["surface"], linewidth=2.0),  # 2px surface gap
    )

    # ---- direct labels outside, with leader lines and collision spreading ----
    placed = []
    for i, (w, (label, n, _)) in enumerate(zip(wedges, rows)):
        ang = math.radians((w.theta1 + w.theta2) / 2)
        x, y = math.cos(ang), math.sin(ang)
        placed.append({"i": i, "x": x, "y": y, "ly": y * 1.28,
                       "side": 1 if x >= 0 else -1,
                       "text": f"{label}\n{n} 次 · {n/total*100:.0f}%"})
    # keep labels on the same side from overlapping
    for side in (1, -1):
        grp = sorted([p for p in placed if p["side"] == side], key=lambda p: p["ly"])
        for a, b in zip(grp, grp[1:]):
            if b["ly"] - a["ly"] < 0.36:
                b["ly"] = a["ly"] + 0.36
        shift = max(0.0, max((p["ly"] for p in grp), default=0) - 1.24)
        for p in grp:
            p["ly"] -= shift
    for p in placed:
        lx = p["side"] * 1.30
        ax.annotate(
            p["text"], xy=(p["x"] * 1.01, p["y"] * 1.01), xytext=(lx, p["ly"]),
            ha="left" if p["side"] > 0 else "right", va="center",
            fontsize=10.5, color=t["ink"], linespacing=1.45,
            arrowprops=dict(arrowstyle="-", color=t["ink3"], linewidth=0.9,
                            shrinkA=0, shrinkB=4,
                            connectionstyle="arc3,rad=0"),
        )

    ax.set_xlim(-2.30, 2.30)
    ax.set_ylim(-1.95, 1.55)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.text(0.5, 0.975, f"{res['instance_id']}：agent 的 {total} 个 shell 操作构成",
             ha="center", va="top", fontsize=14, color=t["ink"])
    fig.text(0.5, 0.928,
             f"已剥离 {res['n_cd_prefix']} 次 cd 前缀（每条 docker exec 都是新子 shell，必须重新 cd）"
             f"　·　{res['model']}　·　{res['n_steps']} 步 / ${res['instance_cost']:.4f}",
             ha="center", va="top", fontsize=9.5, color=t["ink2"])
    fig.text(0.5, 0.885,
             "读代码与搜索占 42%；真正修改源码的操作全程只有 1 次（5%）",
             ha="center", va="top", fontsize=10.5, color=t["ink"])

    fig.savefig(out, facecolor=t["surface"], bbox_inches="tight", pad_inches=0.28)
    plt.close(fig)
    print(f"wrote {out}")
    for label, n, _ in rows:
        print(f"   {label:18s} {n:>3}  {n/total*100:5.1f}%")


if __name__ == "__main__":
    res = json.loads(Path("traj_analysis.json").read_text())
    draw(res, "light", "op_mix_light.png")
    draw(res, "dark", "op_mix_dark.png")
