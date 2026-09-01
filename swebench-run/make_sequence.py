#!/usr/bin/env python3
"""Sequence diagram: how one SWE-bench task reaches mini-SWE-agent, and how the
agent talks to the LLM and to bash.

Every arrow is grounded in source read for this run:
  swebench/inference/mini_swe_agent.py   build_command / run
  minisweagent/run/benchmarks/swebench.py  process_instance / get_sb_environment
  minisweagent/agents/default.py         run / step / query / execute_actions
  minisweagent/models/litellm_model.py   query / _parse_actions
  minisweagent/environments/docker.py    _start_container / execute / _check_finished
"""
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
from matplotlib import font_manager

font_manager.fontManager.addfont("/home/river/.local/share/fonts/WenQuanYiMicroHei.ttf")
CJK = "WenQuanYi Micro Hei"
MONO = "DejaVu Sans Mono"
HAS_CJK = re.compile(r"[　-鿿＀-￯]")

SURFACE, INK, INK2, MUTED, ACCENT = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8880", "#2a78d6"
LIFELINE, FRAME = "#c9c8c0", "#1baf7a"

PARTS = [
    ("swebench infer", "swebench/cli/infer.py"),
    ("mini runner", "run/benchmarks/swebench.py"),
    ("DefaultAgent", "agents/default.py"),
    ("LitellmModel", "→ api.deepseek.com"),
    ("DockerEnvironment", "→ 容器 /testbed"),
]

# (kind, src, dst, lines)   kind: call | ret | self | note
MSGS = [
    ("call", 0, 1, ["subprocess.call：python -m minisweagent.run.benchmarks.swebench",
                    "--subset SWE-bench/SWE-bench_Verified --split test",
                    "-c <mini>/config/benchmarks/swebench.yaml  -m deepseek/deepseek-v4-pro"]),
    ("self", 1, 1, ["load_dataset() → filter_instances(--filter astropy__astropy-12907)",
                    "1 个 instance；task = instance['problem_statement']"]),
    ("call", 1, 4, ["get_sb_environment(config, instance)"]),
    ("self", 4, 4, ["docker run -d --name minisweagent-<uuid8> -w /testbed --rm \\",
                    "  swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest sleep 2h"]),
    ("ret",  4, 1, ["container id"]),
    ("call", 1, 2, ["DefaultAgent(model, env, step_limit=250, cost_limit=$3)",
                    ".run(task)"]),
    ("self", 2, 2, ["messages = [ system_template ,  instance_template(task) ]",
                    "题面是 agent 唯一输入：看不到 gold patch / test_patch / F2P 列表"]),

    ("loopstart", 2, 4, ["loop　每步一轮，直到 messages[-1].role == 'exit'　（本例 12 步）"]),
    ("call", 2, 3, ["model.query(messages)　　# 先查 step_limit / cost_limit"]),
    ("self", 3, 3, ["litellm.completion(model, messages, tools=[BASH_TOOL],",
                    "  drop_params=True, parallel_tool_calls=True)"]),
    ("ret",  3, 2, ["tool_calls=[ bash(command=\"cd /testbed && …\") ]　+ usage/cost",
                    "只有一个工具 bash，没有 read/write/edit"]),
    ("call", 2, 4, ["env.execute({'command': …})　　一轮可有多个 action，串行执行"]),
    ("self", 4, 4, ["docker exec -w /testbed -e BASH_ENV=/root/.bashrc -e PAGER=cat \\",
                    "  <cid> bash -c \"<command>\"　　60s 超时；每条都是新子 shell",
                    "所以 cd / export 不跨命令保留 → 命令都以 cd /testbed && 开头"]),
    ("ret",  4, 2, ["<returncode>0</returncode><output>…</output>　　>10000 字符则头尾各留 5000"]),
    ("self", 2, 2, ["append role='tool' 观察消息 → save(traj)（每步落盘）"]),
    ("loopend", 2, 4, []),

    ("note", 4, 4, ["_check_finished()：输出首行 == COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                    "且 returncode == 0  →  raise Submitted(submission = 其余行 = patch)"]),
    ("ret",  4, 2, ["Submitted　→　messages[-1].role = 'exit'，循环结束"]),
    ("ret",  2, 1, ["{ exit_status: 'Submitted',  submission: <patch 文本> }"]),
    ("self", 1, 1, ["写 <instance_id>.traj.json（全量 messages + info）",
                    "写 preds.json[iid] = {model_name_or_path, instance_id, model_patch}"]),
    ("ret",  1, 0, ["exit 0"]),
]

LINE_H = 0.30
GAP = 0.26            # clear space between one message's ink and the next's
LOOP_HEADER = 0.52


def extents(kind, lines):
    """How far a message's ink reaches above and below its own y."""
    k = len(lines)
    if kind in ("call", "ret"):
        return LINE_H * k + 0.22, 0.10          # labels sit ABOVE the arrow
    if kind in ("self", "note"):
        return 0.10, LINE_H * k + 0.20          # text hangs BELOW the anchor
    return 0.0, 0.0


def text(ax, x, y, s, size=8.6, color=INK, ha="center", va="center",
         weight="normal", mask=False):
    fam = CJK if HAS_CJK.search(s) else MONO
    bbox = dict(facecolor=SURFACE, edgecolor="none", pad=1.4) if mask else None
    return ax.text(x, y, s, fontsize=size, color=color, ha=ha, va=va,
                   family=fam, fontweight=weight, zorder=6, bbox=bbox)


def est_width(line: str) -> float:
    """Rough data-unit width of a label line (CJK glyphs are ~2x an ASCII one)."""
    return sum(0.155 if HAS_CJK.search(c) else 0.078 for c in line)


def main():
    n = len(PARTS)
    xs = [1.0 + i * 3.55 for i in range(n)]
    W = xs[-1] + 1.6

    # ---- pre-compute vertical layout -----------------------------------
    ys, y, prev_below = [], -0.56, 0.0
    for kind, _s, _d, lines in MSGS:
        if kind == "loopstart":
            y -= prev_below + GAP + LOOP_HEADER
            ys.append(y); prev_below = 0.10
            continue
        if kind == "loopend":
            y -= prev_below + GAP
            ys.append(y); prev_below = 0.0
            continue
        above, below = extents(kind, lines)
        y -= prev_below + GAP + above
        ys.append(y)
        prev_below = below
    bottom = y - prev_below - 0.70

    fig, ax = plt.subplots(figsize=(W * 0.83, (-bottom + 2.3) * 0.44), dpi=170)
    fig.patch.set_facecolor(SURFACE); ax.set_facecolor(SURFACE)

    # ---- participants ---------------------------------------------------
    for x, (name, sub) in zip(xs, PARTS):
        ax.add_patch(FancyBboxPatch((x - 1.58, -0.52), 3.16, 0.92,
                                    boxstyle="round,pad=0.02,rounding_size=0.09",
                                    linewidth=1.2, edgecolor=ACCENT,
                                    facecolor="#eef4fc", zorder=4))
        text(ax, x, -0.02, name, size=9.6, weight="bold")
        text(ax, x, -0.33, sub, size=7.6, color=INK2)
        ax.plot([x, x], [-0.56, bottom], color=LIFELINE, lw=1.0,
                ls=(0, (3, 3)), zorder=1)

    # ---- loop frame ------------------------------------------------------
    li = next(i for i, m in enumerate(MSGS) if m[0] == "loopstart")
    lo = next(i for i, m in enumerate(MSGS) if m[0] == "loopend")
    top, bot = ys[li] + LOOP_HEADER, ys[lo]
    ax.add_patch(Rectangle((xs[2] - 1.85, bot), (xs[4] + 1.9) - (xs[2] - 1.85), top - bot,
                           linewidth=1.15, edgecolor=FRAME, facecolor="#1baf7a0d", zorder=2))
    ax.add_patch(Rectangle((xs[2] - 1.85, top - 0.34), 0.86, 0.34,
                           linewidth=1.15, edgecolor=FRAME, facecolor="#e3f5ee", zorder=3))
    text(ax, xs[2] - 1.42, top - 0.17, "loop", size=8.4, color="#0d6b4b", weight="bold")
    text(ax, xs[2] - 0.88, top - 0.17, MSGS[li][3][0].split("　", 1)[1], size=8.4, color=INK2, ha="left")

    # ---- messages --------------------------------------------------------
    notes = []
    for (kind, s, d, lines), y in zip(MSGS, ys):
        if kind in ("loopstart", "loopend"):
            continue
        if kind in ("self", "note"):
            x = xs[s]
            h = LINE_H * len(lines) + 0.12
            if kind == "self":
                ax.add_patch(FancyArrowPatch((x, y + 0.06), (x, y - h + 0.02),
                                             connectionstyle="bar,fraction=-0.28",
                                             arrowstyle="-|>", mutation_scale=9,
                                             color=INK2, lw=1.1, zorder=5))
                lx, ha = x + 0.62, "left"
            else:
                lx, ha = x - est_width(max(lines, key=est_width)) - 0.28, "left"
            arts = []
            for i, ln in enumerate(lines):
                arts.append(text(ax, lx, y - 0.10 - i * LINE_H, ln, ha=ha, size=8.3,
                                 color=INK if i == 0 else INK2, mask=(kind == "self")))
            if kind == "note":
                notes.append(arts)
        else:
            x0, x1 = xs[s], xs[d]
            solid = kind == "call"
            ax.add_patch(FancyArrowPatch((x0, y), (x1, y), arrowstyle="-|>",
                                         mutation_scale=11, color=INK if solid else INK2,
                                         lw=1.3 if solid else 1.0,
                                         linestyle="-" if solid else (0, (5, 3)),
                                         zorder=5, shrinkA=0, shrinkB=0))
            mid = (x0 + x1) / 2
            for i, ln in enumerate(lines):
                text(ax, mid, y + 0.20 + (len(lines) - 1 - i) * LINE_H, ln,
                     size=8.3, color=INK if i == 0 else INK2, mask=True)

    # note boxes: sized from the text that is actually rendered, not an estimate
    fig.canvas.draw()
    inv = ax.transData.inverted()
    for arts in notes:
        boxes = [a.get_window_extent(fig.canvas.get_renderer()) for a in arts]
        x0 = min(b.x0 for b in boxes); x1 = max(b.x1 for b in boxes)
        y0 = min(b.y0 for b in boxes); y1 = max(b.y1 for b in boxes)
        (dx0, dy0), (dx1, dy1) = inv.transform([(x0, y0), (x1, y1)])
        pad = 0.13
        ax.add_patch(FancyBboxPatch((dx0 - pad, dy0 - pad),
                                    (dx1 - dx0) + 2 * pad, (dy1 - dy0) + 2 * pad,
                                    boxstyle="round,pad=0.02,rounding_size=0.06",
                                    linewidth=1.0, edgecolor="#d9b23a",
                                    facecolor="#fdf6e3", zorder=4))

    ax.text(xs[0] - 1.58, bottom - 0.30,
            "preds.json 是 infer 与 eval 之间唯一的接口："
            "swebench eval -p logs/inference/<run>/preds.json  会另起一个干净容器，"
            "把这里的 model_patch 打进去再跑 eval.sh",
            fontsize=8.6, color=INK2, family=CJK, ha="left", va="top")

    ax.set_xlim(-0.4, W + 0.3); ax.set_ylim(bottom - 0.95, 0.75)
    ax.axis("off")
    out = Path("agent_sequence.png")
    fig.savefig(out, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.3)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
