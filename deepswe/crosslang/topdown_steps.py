#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 perf stat -I 10 的 interval 输出按 step 归并，算 per-step ARM L1 topdown。

和 topdown_parse.py 的区别：topdown_parse.py 处理整条 trial 的单个聚合值；
本脚本处理 perf stat -I 的多 interval 输出，把 interval 按时间窗口归到
replay.py 的每个 step 上，算出 per-step 的四象限。

数据流：

  perf stat -I 10 -j -o perf.json   →  每 10ms 一组计数，带 interval 时间戳
  replay.py commands.jsonl           →  每条命令的 step / abs_start_s / wall_s
  replay.py verdict.json             →  t_start_mono（命令循环起点的 monotonic）
  perf_start_mono.txt                →  perf 启动时的 monotonic

  对齐：interval 时间戳是相对 perf 启动的偏移（从 0 开始的小数）
        step 的 abs_start_s 是相对 t_start 的偏移（从 0 开始的小数）
        → step 在 perf 域的窗口 = [abs_start_s + offset, abs_start_s + offset + wall_s]
        offset = t_start_mono - perf_start_mono（两者都是绝对 CLOCK_MONOTONIC，
        差值就是 perf 启动到命令循环开始的时差）

  归并：对每个 step，把窗口内的 interval 各事件计数求和 → 算 topdown
        同时把全部 interval 求和 → 整条 trial 的聚合 topdown（交叉校验）

时序示意：

  perf 域:  |---interval---|---interval---|---interval---|---interval---|
            0            0.01           0.02           0.03           0.04

  replay:                    t_start_mono                              t_end
                              |===step0===||===step1===|
                              abs_start=0  wall=0.015   abs_start=0.015 wall=0.02

  offset = t_start_mono - perf_start_mono > 0（perf 先启动，replay 后跑命令）
  step0 perf 域窗口 = [0+offset, 0.015+offset]
  step1 perf 域窗口 = [0.015+offset, 0.035+offset]
"""

import argparse
import bisect
import datetime
import json
import os
import pathlib
import sys

# 复用 topdown_parse.py 的事件定义、配置解析、计数器值解析
from topdown_parse import (
    L1_REQUIRED, EV_BE, EV_TOT, SUM_TOL, CROSS_TOL, PCNT_MIN, EPS,
    NOT_COUNTED, parse_conf, extra_events, _num, _match_name,
    sniff_format, _PCNT_RE,
)

INTERVAL_DEFAULT_MS = 10


# ── interval 解析 ──────────────────────────────────────────────────────────
def parse_intervals_json(text, names):
    """解析 perf stat -I -j 的 JSON Lines 输出。

    返回：(intervals, aggregate)
      intervals = [(t_float, {event_name: count, ...}), ...]  按 t 排序
      aggregate  = {event_name: count, ...} | None   perf 末尾的聚合值（如果有）

    每个(interval, event) 组合输出一行 JSON。同一 interval 的事件连续出现。
    末尾可能有一组不带 interval 字段的聚合行——拿来做交叉校验。
    """
    intervals = []
    aggregate = {}

    cur_t = None
    cur_events = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict) or "counter-value" not in o:
            continue

        ev = str(o.get("event", ""))
        hit = _match_name(ev, names)
        if hit is None:
            continue

        val = _num(o["counter-value"])
        interval = o.get("interval")

        if interval is None:
            # 末尾聚合行（无 interval 字段）
            if val is not None:
                aggregate[hit] = aggregate.get(hit, 0) + val
            continue

        try:
            t = float(interval)
        except (TypeError, ValueError):
            continue

        if cur_t is None or t != cur_t:
            if cur_events:
                intervals.append((cur_t, cur_events))
            cur_t = t
            cur_events = {}

        if val is not None:
            cur_events[hit] = cur_events.get(hit, 0) + val

    if cur_events:
        intervals.append((cur_t, cur_events))

    agg = aggregate if aggregate else None
    return intervals, agg


def parse_intervals_csv(text, names):
    """解析 perf stat -I -x, 的 CSV 输出。

    CSV 带 -I 时第一列是 interval 时间戳。和 JSON 版一样返回
    (intervals, aggregate)。
    """
    intervals = []
    aggregate = {}
    cur_t = None
    cur_events = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = [x.strip() for x in line.split(",")]
        if not f:
            continue

        # 带 -I 的 CSV：第一列是 interval（浮点秒），后面和普通 CSV 一样
        # 不带 -I 的末尾聚合行：第一列是计数（整数/浮点），不是 interval
        try:
            interval_val = float(f[0])
            is_interval = 0.0 < interval_val < 1e8  # interval 是秒，合理范围
        except (ValueError, IndexError):
            is_interval = False

        if is_interval:
            # 在剩余列里找事件名
            rest = f[1:]
        else:
            # 末尾聚合行：在所有列里找
            rest = f

        idx = None
        for i, v in enumerate(rest):
            hit = _match_name(v, names)
            if hit is not None:
                idx, name = i, hit
                break
        if idx is None:
            continue

        # counter-value 在 rest 的第 0 列（或附近）
        value = _num(rest[0]) if rest else None
        if value is None and rest:
            for v in rest[:idx]:
                value = _num(v)
                if value is not None:
                    break

        if not is_interval:
            if value is not None:
                aggregate[name] = aggregate.get(name, 0) + value
            continue

        t = interval_val
        if cur_t is None or t != cur_t:
            if cur_events:
                intervals.append((cur_t, cur_events))
            cur_t = t
            cur_events = {}

        if value is not None:
            cur_events[name] = cur_events.get(name, 0) + value

    if cur_events:
        intervals.append((cur_t, cur_events))

    agg = aggregate if aggregate else None
    return intervals, agg


# ── step 归并 ──────────────────────────────────────────────────────────────
def aggregate_window(intervals, times_sorted, lo, hi):
    """对 [lo, hi] 时间窗口内的 interval 求和。

    times_sorted 是 interval 时间戳的排序数组（和 intervals 的第一维一致），
    用 bisect 做 O(log n) 查找。

    归并规则：包含 interval 时间戳 T 满足 lo < T <= hi 的所有 interval。
    边界误差 ≤ 1 个 interval（10ms），对典型 step（几百 ms ~ 几秒）可忽略。
    """
    lo_i = bisect.bisect_right(times_sorted, lo)
    hi_i = bisect.bisect_right(times_sorted, hi)

    total = {}
    for i in range(lo_i, hi_i):
        for ev, val in intervals[i][1].items():
            total[ev] = total.get(ev, 0) + val
    return total


def aggregate_all(intervals):
    """把全部 interval 求和，得到整条 trial 的聚合计数。"""
    total = {}
    for _, events in intervals:
        for ev, val in events.items():
            total[ev] = total.get(ev, 0) + val
    return total


# ── topdown 计算 ───────────────────────────────────────────────────────────
def compute_topdown(vals, slots, mode, be_event=None, tot_event=None):
    """从事件计数算 L1 四象限。和 topdown_parse.py 的公式完全一致。

    返回 (quad, ipc, r3) 或 (None, None, None)（计数不足时）。
    """
    needed = ["cpu_cycles", "op_retired", "op_spec", "stall_slot_frontend"]
    if mode == "direct":
        needed.append("stall_slot_backend")

    for n in needed:
        if n not in vals or vals[n] is None:
            return None, None, None

    cyc = vals["cpu_cycles"]
    if cyc is None or cyc <= 0:
        return None, None, None

    denom = cyc * slots
    r8 = vals["op_retired"]
    spec = vals["op_spec"]
    fe = vals["stall_slot_frontend"]

    retiring = r8 / denom
    badspec = (spec - r8) / denom
    frontend = fe / denom
    r3 = retiring + badspec + frontend

    if mode == "direct":
        backend = vals["stall_slot_backend"] / denom
    else:
        backend = 1.0 - r3

    quad = {
        "Retiring": retiring,
        "BadSpec": badspec,
        "FrontendBound": frontend,
        "BackendBound": backend,
    }
    ipc = r8 / cyc
    return quad, ipc, r3


def run_checks(vals, quad, ipc, slots, mode, pcnt_min=None):
    """跑 C2~C5 自检（C1/SUM 见 topdown_parse.py，这里按需跑）。

    返回 {check_name: bool|None}。
    """
    checks = {}
    ok = True

    # C2: OP_SPEC >= OP_RETIRED
    if "op_spec" in vals and "op_retired" in vals and vals["op_spec"] is not None and vals["op_retired"] is not None:
        c2 = vals["op_spec"] >= vals["op_retired"]
        checks["C2_op_spec_ge_op_retired"] = c2
        if not c2:
            ok = False

    # C3: OP_RETIRED / CPU_CYCLES <= SLOTS
    if ipc is not None and ipc > slots + EPS:
        checks["C3_op_retired_per_cycle_le_slots"] = False
        ok = False
    else:
        checks["C3_op_retired_per_cycle_le_slots"] = True

    # C4: 四象限取值域 [0, 1]
    if quad is not None:
        out_of_range = [(k, v) for k, v in quad.items() if v < -EPS or v > 1.0 + EPS]
        checks["C4_quadrants_in_range"] = not out_of_range
        if out_of_range:
            ok = False

    # 直接法下跑求和自检
    if mode == "direct" and quad is not None:
        total = sum(quad.values())
        checks["sum_in_tolerance"] = abs(total - 1.0) <= SUM_TOL
        if abs(total - 1.0) > SUM_TOL:
            ok = False

    # 残差法下跑 C1
    if mode == "residual":
        # r3 需要从外部传入或重算
        pass  # 在调用侧处理

    checks["_all_ok"] = ok
    return checks


# ── 主流程 ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="把 perf stat -I 的 interval 输出按 step 归并，算 per-step topdown")
    ap.add_argument("perf_out", help="perf stat -I 的输出文件（-j JSON Lines 或 -x, CSV）")
    ap.add_argument("--conf", default="", help="topdown.conf 路径（默认取脚本同目录）")
    ap.add_argument("--format", choices=("auto", "json", "csv"), default="auto")
    ap.add_argument("--slots", default="", help="SLOTS 值（一般由 topdown_trial.sh 传入）")
    ap.add_argument("--commands", default="", help="replay.py 的 commands.jsonl 路径")
    ap.add_argument("--verdict", default="", help="replay.py 的 verdict.json 路径")
    ap.add_argument("--perf-start-mono", default="", help="perf_start_mono.txt 路径")
    ap.add_argument("--interval-ms", type=int, default=INTERVAL_DEFAULT_MS,
                    help="perf -I 的间隔（ms），默认 10")
    ap.add_argument("--json-out", default="", help="机读结果落盘路径")
    ap.add_argument("--title", default="ARM L1 Topdown (per-step)", help="打印标题")
    args = ap.parse_args()

    here = pathlib.Path(__file__).resolve().parent
    conf_path = args.conf or str(here / "topdown.conf")
    conf = parse_conf(conf_path)

    # ── 口径判定 ──
    be_code = (conf.get(EV_BE[0]) or "").strip()
    tot_code = (conf.get(EV_TOT[0]) or "").strip()
    mode = "direct" if be_code else "residual"

    wanted = list(L1_REQUIRED)
    if be_code:
        wanted.append(EV_BE)
    if tot_code:
        wanted.append(EV_TOT)
    extras = extra_events(conf)
    names = [n for _, n, _ in wanted] + [n for n, _ in extras]

    # SLOTS
    slots_raw = (args.slots or conf.get("SLOTS") or "").strip()
    slots = None
    if slots_raw:
        try:
            slots = int(slots_raw, 0)
        except ValueError:
            print(f"❌ SLOTS 值无法解析: {slots_raw!r}")
            return 1
    if slots is None or slots <= 0:
        print("❌ SLOTS 未知：topdown.conf 里是空的，命令行也没给 --slots")
        return 1

    # ── 读 perf interval 输出 ──
    perf_path = pathlib.Path(args.perf_out)
    if not perf_path.exists():
        print(f"❌ perf 输出文件不存在: {perf_path}")
        return 1
    text = perf_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print(f"❌ perf 输出文件是空的: {perf_path}")
        return 1

    fmt = sniff_format(text, args.format)
    if fmt == "json":
        intervals, perf_aggregate = parse_intervals_json(text, names)
    else:
        intervals, perf_aggregate = parse_intervals_csv(text, names)

    if not intervals:
        print("❌ 没有解析到任何 interval 数据")
        print("   确认 perf stat 命令带了 -I 10 参数")
        return 1

    times_sorted = [t for t, _ in intervals]
    interval_dt = args.interval_ms / 1000.0

    print("=" * 70)
    print(f" {args.title}")
    print(f" perf 输出   {perf_path}  （{len(intervals)} 个 interval，{fmt.upper()} 格式）")
    print(f" 配置       {conf_path}")
    print(f" 后端口径   {'直接法' if mode == 'direct' else '残差法'}")
    print(f" SLOTS      {slots}")
    print(f" interval   {args.interval_ms}ms（边界误差 ≤ {args.interval_ms}ms）")
    print("=" * 70)

    # ── 读 replay 的时间锚点 ──
    t_start_mono = None
    if args.verdict:
        try:
            v = json.loads(pathlib.Path(args.verdict).read_text())
            t_start_mono = v.get("t_start_mono")
        except (OSError, ValueError):
            pass

    perf_start_mono = None
    if args.perf_start_mono:
        try:
            perf_start_mono = float(pathlib.Path(args.perf_start_mono).read_text().strip())
        except (OSError, ValueError):
            pass

    if t_start_mono is not None and perf_start_mono is not None:
        offset = t_start_mono - perf_start_mono
        print(f" 时钟对齐   t_start_mono = {t_start_mono:.6f}")
        print(f"           perf_start_mono = {perf_start_mono:.6f}")
        print(f"           offset（t_start - perf_start）= {offset:+.6f}s")
        if perf_start_mono > t_start_mono:
            print(f" ⚠️  perf 在 t_start 之后启动——前几条命令可能没有 interval 覆盖")
    else:
        offset = 0.0
        t_start_mono = None
        print(" ⚠️  缺少时间锚点（verdict.json 的 t_start_mono 或 perf_start_mono.txt），")
        print("    offset 按 0 处理——per-step 归并可能偏移。检查 replay.py 版本和文件路径。")

    # ── 读 commands.jsonl ──
    commands = []
    if args.commands:
        try:
            for line in pathlib.Path(args.commands).read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    commands.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass

    if not commands:
        print("⚠️  没有读到 commands.jsonl——只输出整条 trial 的聚合 topdown")
    else:
        print(f" 命令       {len(commands)} 条（来自 {args.commands}）")

    # ── 整条 trial 聚合（全部 interval 求和）──
    agg_all = aggregate_all(intervals)
    agg_quad, agg_ipc, agg_r3 = compute_topdown(agg_all, slots, mode)

    print()
    print("── 整条 trial 聚合（全部 interval 求和）──")
    if agg_quad is None:
        print("  ❌ 计数不足，算不出聚合 topdown")
        print(f"  事件: {dict((k, v) for k, v in agg_all.items())}")
    else:
        for k in ("Retiring", "BadSpec", "FrontendBound", "BackendBound"):
            v = agg_quad[k]
            bar = "#" * max(0, min(40, int(round(v * 40))))
            tag = "  ← 残差" if (k == "BackendBound" and mode == "residual") else ""
            print(f"  {k:14s} {v * 100:6.2f}%  {bar}{tag}")
        print(f"  IPC = {agg_ipc:.3f}")

        # 和 perf 自己的聚合值交叉校验
        if perf_aggregate:
            print()
            print("  交叉校验（interval 求和 vs perf 末尾聚合）:")
            for n in names:
                mine = agg_all.get(n, 0)
                theirs = perf_aggregate.get(n, 0)
                if theirs > 0:
                    ratio = mine / theirs if theirs else 0
                    tag = "✅" if 0.99 <= ratio <= 1.01 else "⚠️"
                    print(f"    {n:22s} mine={int(mine):>15,d}  perf={int(theirs):>15,d}  ratio={ratio:.4f} {tag}")

    # ── per-step 归并 ──
    step_results = []
    if commands:
        # 诊断：打印 interval 和 step 的时间范围，帮助排查对齐问题
        if intervals:
            print(f"  [diag] interval 时间范围: [{times_sorted[0]:.3f}, {times_sorted[-1]:.3f}]s")
        cmd_starts = [c.get("abs_start_s", 0) for c in commands if c.get("abs_start_s") is not None]
        cmd_ends = [c.get("abs_start_s", 0) + c.get("wall_s", 0) for c in commands
                     if c.get("abs_start_s") is not None and c.get("wall_s") is not None]
        if cmd_starts and cmd_ends:
            print(f"  [diag] step perf 域范围: [{min(cmd_starts) + offset:.3f}, {max(cmd_ends) + offset:.3f}]s"
                  f"（abs_start [{min(cmd_starts):.3f}, {max(cmd_ends):.3f}] + offset {offset:+.3f}）")

        print()
        print("── per-step topdown ────────────────────────────────────────")
        print(f"  {'step':>4s}  {'cmds':>4s}  {'wall_s':>7s}  {'cycles':>12s}  "
              f"{'Ret%':>6s} {'Bad%':>6s} {'FE%':>6s} {'BE%':>6s}  {'IPC':>5s}  command")
        print(f"  {'----':>4s}  {'----':>4s}  {'-------':>7s}  {'------------':>12s}  "
              f"{'------':>6s} {'------':>6s} {'------':>6s} {'------':>6s}  {'-----':>5s}  -------")

        # 按 step 分组
        steps_map = {}
        for cmd in commands:
            si = cmd.get("step")
            if si is None:
                continue
            steps_map.setdefault(si, []).append(cmd)

        for si in sorted(steps_map.keys()):
            cmds = steps_map[si]
            # step 的时间范围：最早命令的 abs_start_s ~ 最晚命令的 abs_start_s + wall_s
            starts = [c.get("abs_start_s", 0) for c in cmds if c.get("abs_start_s") is not None]
            ends = [c.get("abs_start_s", 0) + c.get("wall_s", 0) for c in cmds
                     if c.get("abs_start_s") is not None and c.get("wall_s") is not None]
            if not starts or not ends:
                continue

            step_lo = min(starts) + offset
            step_hi = max(ends) + offset
            step_wall = max(ends) - min(starts)

            agg = aggregate_window(intervals, times_sorted, step_lo, step_hi)
            quad, ipc, r3 = compute_topdown(agg, slots, mode)

            cyc = agg.get("cpu_cycles", 0)
            # 取第一条非哨兵命令做展示
            head_cmd = ""
            for c in cmds:
                cmd_str = c.get("cmd_stripped") or c.get("cmd", "")
                if cmd_str:
                    head_cmd = cmd_str[:60]
                    break

            if quad is None:
                print(f"  {si:>4d}  {len(cmds):>4d}  {step_wall:>7.2f}s  "
                      f"{'(no data)':>12s}  "
                      f"{'--':>6s} {'--':>6s} {'--':>6s} {'--':>6s}  {'--':>5s}  {head_cmd}")
            else:
                ret = quad["Retiring"] * 100
                bad = quad["BadSpec"] * 100
                fe = quad["FrontendBound"] * 100
                be = quad["BackendBound"] * 100
                ipc_s = f"{ipc:.2f}" if ipc else "--"
                print(f"  {si:>4d}  {len(cmds):>4d}  {step_wall:>7.2f}s  "
                      f"{int(cyc):>12,d}  "
                      f"{ret:>5.1f}% {bad:>5.1f}% {fe:>5.1f}% {be:>5.1f}%  {ipc_s:>5s}  {head_cmd}")

            step_results.append({
                "step": si,
                "n_cmds": len(cmds),
                "wall_s": round(step_wall, 4),
                "t_start_perf": round(step_lo, 4),
                "t_end_perf": round(step_hi, 4),
                "counts": {k: int(v) if v == int(v) else v for k, v in agg.items()},
                "topdown": ({k: round(v, 6) for k, v in quad.items()} if quad else None),
                "ipc": round(ipc, 4) if ipc else None,
                "commands": [c.get("cmd_stripped", "")[:200] for c in cmds],
            })

    # ── 自检 ──
    print()
    print("── 自检（聚合级）──────────────────────────────────────────")
    if agg_quad is not None:
        ok = True

        # C2
        if "op_spec" in agg_all and "op_retired" in agg_all:
            c2 = agg_all["op_spec"] >= agg_all["op_retired"]
            print(f"  {'✅' if c2 else '❌'} C2  OP_SPEC ≥ OP_RETIRED "
                  f"({int(agg_all['op_spec']):,d} ≥ {int(agg_all['op_retired']):,d})")
            if not c2:
                ok = False

        # C3
        if agg_ipc is not None:
            c3 = agg_ipc <= slots + EPS
            print(f"  {'✅' if c3 else '❌'} C3  OP_RETIRED/CPU_CYCLES={agg_ipc:.4f} ≤ SLOTS={slots}")
            if not c3:
                ok = False

        # C4
        out_of_range = [(k, v) for k, v in agg_quad.items() if v < -EPS or v > 1.0 + EPS]
        c4 = not out_of_range
        print(f"  {'✅' if c4 else '❌'} C4  四象限取值域 [0,1]")
        if not c4:
            for k, v in out_of_range:
                print(f"       {k} = {v:.4f}")
            ok = False

        # 求和（直接法）或 C1（残差法）
        if mode == "direct":
            total = sum(agg_quad.values())
            sum_ok = abs(total - 1.0) <= SUM_TOL
            print(f"  {'✅' if sum_ok else '❌'} SUM 四象限求和={total:.4f}（容差 {SUM_TOL}）")
            if not sum_ok:
                ok = False
        else:
            c1 = agg_r3 <= 1.0 + EPS
            print(f"  {'✅' if c1 else '❌'} C1  残差非负 R+BS+FE={agg_r3:.4f} ≤ 1")
            if not c1:
                ok = False

        # X 交叉校验
        if tot_code and "stall_slot_total" in agg_all:
            be_indep = (agg_all["stall_slot_total"] - agg_all["stall_slot_frontend"]) / (agg_all["cpu_cycles"] * slots)
            delta = abs(be_indep - agg_quad["BackendBound"])
            x_ok = delta <= CROSS_TOL
            print(f"  {'✅' if x_ok else '❌'} X   交叉校验 delta={delta:.4f}（容差 {CROSS_TOL}）")
            if not x_ok:
                ok = False

        print(f"  {'✅' if ok else '❌'} {'全部通过' if ok else '自检未全过'}")

    # ── 落盘 ──
    result = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "perf_out": str(perf_path),
        "conf": conf_path,
        "slots": slots,
        "interval_ms": args.interval_ms,
        "n_intervals": len(intervals),
        "offset_s": round(offset, 6) if t_start_mono and perf_start_mono else None,
        "t_start_mono": t_start_mono,
        "perf_start_mono": perf_start_mono,
        "backend_method": mode,
        "aggregate": {
            "counts": {k: int(v) if v == int(v) else v for k, v in agg_all.items()},
            "topdown": ({k: round(v, 6) for k, v in agg_quad.items()} if agg_quad else None),
            "ipc": round(agg_ipc, 4) if agg_ipc else None,
        },
        "steps": step_results,
    }

    out_path = pathlib.Path(args.json_out) if args.json_out else perf_path.parent / "topdown_steps.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print()
    print(f"  机读结果  {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
