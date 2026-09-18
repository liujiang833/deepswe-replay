#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 topdown_out 下所有 trial 的 topdown_steps.json，三级聚类。

三级：
  per-trial   : 每个 trial 内部独立聚类
  per-language: 同语言的 trial 的 step 合并后聚类
  all-trials  : 全部 step 合并后聚类

聚类方法：L∞ 贪心——按 cycles 降序处理，尝试加入已有 cluster
（加入后该 cluster 内任意两点的任意单维差 < 阈值），不行就新建。
自然找到最少的 cluster 数。

用法：
  python3 topdown_cluster.py topdown_out/                    # 三级全做
  python3 topdown_cluster.py topdown_out/ --threshold 0.08   # 收紧到 8%
  python3 topdown_cluster.py topdown_out/ --json-out cluster.json
  python3 topdown_cluster.py topdown_out/ --level all        # 只做 all-trials
"""

import argparse
import datetime
import json
import os
import pathlib
import sys

CLUSTER_THRESHOLD_DEFAULT = 0.10
LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]

# 语言推断：优先从 trial 目录里的 meta.json 读 language 字段；
# 没有就退回从 trial 名猜（go-foo__abc → go）。
def trial_lang(name, tdir=None):
    if tdir is not None:
        meta = tdir / "meta.json"
        if not meta.exists():
            meta = tdir / ".." / "meta.json"
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                lang = m.get("language", "")
                if lang:
                    return lang
            except Exception:
                pass
    parts = name.split("-")
    for p in parts:
        if p in LANG_ORDER:
            return p
    first = name.split("__", 1)[0]
    for lang in LANG_ORDER:
        if first.startswith(lang):
            return lang
    return "unknown"


def load_steps(topdown_out, trials_dir=None):
    """扫描 topdown_out/<trial>/topdown/topdown_steps.json，收集所有 step。

    返回 list of dict，每个 dict 是一个 step 加上 trial 元信息：
      {trial, lang, step, n_cmds, wall_s, cycles, vec: (r,b,f,be), commands}

    trials_dir 指向 trial 源目录（如 full_trials），从那里的 <trial>/meta.json
    读 language 字段。不指定时从 trial 名猜，大部分非 go-/python- 开头的会变 unknown。
    """
    steps = []
    topdown_out = pathlib.Path(topdown_out)
    if not topdown_out.is_dir():
        print(f"❌ 不是目录: {topdown_out}")
        return steps

    for tdir in sorted(topdown_out.iterdir()):
        if not tdir.is_dir():
            continue
        jf = tdir / "topdown" / "topdown_steps.json"
        if not jf.exists():
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        trial = tdir.name
        # 优先从 trials_dir 的 meta.json 读语言
        lang = "unknown"
        if trials_dir is not None:
            meta_path = trials_dir / trial / "meta.json"
            if meta_path.exists():
                try:
                    m = json.loads(meta_path.read_text(encoding="utf-8"))
                    lang = m.get("language", "unknown")
                except Exception:
                    pass
        if lang == "unknown":
            lang = trial_lang(trial, tdir)
        for s in data.get("steps", []):
            td = s.get("topdown")
            counts = s.get("counts", {})
            cyc = counts.get("cpu_cycles", 0)
            if not td or cyc <= 0:
                continue
            steps.append({
                "trial": trial,
                "lang": lang,
                "step": s.get("step"),
                "n_cmds": s.get("n_cmds", 0),
                "wall_s": s.get("wall_s", 0),
                "cycles": cyc,
                "vec": (td["Retiring"], td["BadSpec"],
                        td["FrontendBound"], td["BackendBound"]),
                "commands": s.get("commands", []),
            })
    return steps


def cluster(steps, threshold):
    """L∞ 贪心聚类。

    每个 step 的 vec 是 4 维 (Ret, Bad, FE, BE)。
    按 cycles 降序处理，尝试加入已有 cluster（加入后任意两点任意单维差 < threshold）。
    返回 list of cluster dict。
    """
    if not steps:
        return []

    ordered = sorted(steps, key=lambda s: s["cycles"], reverse=True)
    clusters = []
    for s in ordered:
        vec = s["vec"]
        placed = False
        for cl in clusters:
            # 检查加入后该 cluster 内任意两点的任意单维差 < threshold
            all_vecs = cl["vecs"] + [vec]
            ok = True
            for d in range(4):
                vals = [v[d] for v in all_vecs]
                if max(vals) - min(vals) >= threshold:
                    ok = False
                    break
            if ok:
                cl["members"].append(s)
                cl["vecs"].append(vec)
                placed = True
                break
        if not placed:
            clusters.append({"members": [s], "vecs": [vec]})

    # 按 cluster 总 cycles 降序
    for cl in clusters:
        cl["cycles"] = sum(m["cycles"] for m in cl["members"])
        cl["wall_s"] = sum(m["wall_s"] for m in cl["members"])
    clusters.sort(key=lambda c: c["cycles"], reverse=True)
    return clusters


def summarize_cluster(cl, total_cyc, ci):
    """一个 cluster -> 可打印 + 可 JSON 的 dict。"""
    n = len(cl["members"])
    cyc = cl["cycles"]
    # cycles 加权平均四象限
    ret_w = sum(v[0] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    bad_w = sum(v[1] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    fe_w = sum(v[2] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    be_w = sum(v[3] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    # max spread
    max_spread = 0.0
    for d in range(4):
        vals = [v[d] for v in cl["vecs"]]
        spread = max(vals) - min(vals)
        if spread > max_spread:
            max_spread = spread
    # 代表命令
    rep = max(cl["members"], key=lambda m: m["cycles"])
    head_cmd = ""
    for cmd_str in rep.get("commands", []):
        if cmd_str:
            head_cmd = cmd_str[:50]
            break
    return {
        "cluster": ci,
        "n_steps": n,
        "wall_s": round(cl["wall_s"], 2),
        "cycles": int(cyc),
        "pct_cycles": round(100.0 * cyc / total_cyc, 1) if total_cyc else 0,
        "Retiring": round(100 * ret_w / cyc, 2) if cyc else 0,
        "BadSpec": round(100 * bad_w / cyc, 2) if cyc else 0,
        "FrontendBound": round(100 * fe_w / cyc, 2) if cyc else 0,
        "BackendBound": round(100 * be_w / cyc, 2) if cyc else 0,
        "max_spread": round(max_spread, 4),
        "trials": sorted(set(m["trial"] for m in cl["members"])),
        "langs": sorted(set(m["lang"] for m in cl["members"])),
        "rep_cmd": head_cmd,
    }


def print_clusters(clusters, total_cyc, title):
    if not clusters:
        print("  (无数据)")
        return
    print()
    print(f"── {title}（{len(clusters)} 个 cluster）──")
    print(f"  {'C':>3s}  {'steps':>5s}  {'wall_s':>7s}  {'cycles':>12s}  "
          f"{'占比':>6s}  {'Ret%':>6s} {'Bad%':>6s} {'FE%':>6s} {'BE%':>6s}  "
          f"{'spread':>6s}  {'trials':>5s}  代表命令")
    print(f"  {'---':>3s}  {'-----':>5s}  {'-------':>7s}  {'------------':>12s}  "
          f"{'------':>6s}  {'------':>6s} {'------':>6s} {'------':>6s} {'------':>6s}  "
          f"{'------':>6s}  {'------':>5s}  --------")
    for ci, cl in enumerate(clusters):
        s = summarize_cluster(cl, total_cyc, ci + 1)
        n_trials = len(s["trials"])
        print(f"  C{s['cluster']:<2d}  {s['n_steps']:>5d}  {s['wall_s']:>7.1f}s  "
              f"{s['cycles']:>12,d}  {s['pct_cycles']:>5.1f}%  "
              f"{s['Retiring']:>5.1f}% {s['BadSpec']:>5.1f}% "
              f"{s['FrontendBound']:>5.1f}% {s['BackendBound']:>5.1f}%  "
              f"{s['max_spread']*100:>5.1f}%  {n_trials:>5d}  {s['rep_cmd']}")
    print(f"  {'':>3s}  {sum(len(cl['members']) for cl in clusters):>5d}  "
          f"{sum(cl['wall_s'] for cl in clusters):>7.1f}s  "
          f"{int(total_cyc):>12,d}  100.0%")


def main():
    ap = argparse.ArgumentParser(
        description="扫描 topdown_out 下所有 trial 的 topdown_steps.json，三级聚类")
    ap.add_argument("topdown_out", help="topdown_out 目录（含 <trial>/topdown/topdown_steps.json）")
    ap.add_argument("--threshold", type=float, default=CLUSTER_THRESHOLD_DEFAULT,
                    help=f"L∞ 聚类阈值（默认 {CLUSTER_THRESHOLD_DEFAULT} = 10%%）")
    ap.add_argument("--level", choices=("trial", "language", "all", "all-full"), default="all-full",
                    help="只做某一级（默认 all-full = 三级全做）")
    ap.add_argument("--json-out", default="", help="机读结果落盘路径")
    ap.add_argument("--only-lang", default="", help="只看某语言（per-language 和 all-trials 都过滤）")
    ap.add_argument("--trials-dir", default="full_trials",
                    help="trial 源目录（默认 full_trials），从 <trial>/meta.json 读语言")
    args = ap.parse_args()

    here = pathlib.Path(__file__).resolve().parent
    trials_dir = pathlib.Path(args.trials_dir)
    if not trials_dir.is_absolute():
        trials_dir = here / trials_dir
    trials_dir = trials_dir if trials_dir.is_dir() else None
    steps = load_steps(args.topdown_out, trials_dir)
    if not steps:
        print(f"❌ 在 {args.topdown_out} 下没找到任何 topdown_steps.json")
        return 1

    if args.only_lang:
        steps = [s for s in steps if s["lang"] == args.only_lang]
        if not steps:
            print(f"❌ 没有语言为 {args.only_lang} 的 step")
            return 1

    # 语言分布
    lang_counts = {}
    for s in steps:
        lang_counts[s["lang"]] = lang_counts.get(s["lang"], 0) + 1

    print("=" * 78)
    print(f" topdown 向量聚类（L∞ 阈值 {args.threshold*100:.0f}%）")
    print(f" 数据来源   {args.topdown_out}")
    print(f" step 总数   {len(steps)}")
    print(f" 语言分布   {lang_counts}")
    print(f" 阈值       任意两点任意单维差 < {args.threshold*100:.0f}%")
    print("=" * 78)

    result = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "topdown_out": str(args.topdown_out),
        "threshold": args.threshold,
        "n_steps": len(steps),
        "lang_counts": lang_counts,
    }

    # ── Level 1: per-trial ──
    if args.level in ("trial", "all-full"):
        print()
        print("═══════════════════════════════════════════════════════════")
        print(" Level 1: per-trial（每个 trial 内部独立聚类）")
        print("═══════════════════════════════════════════════════════════")

        trials_map = {}
        for s in steps:
            trials_map.setdefault(s["trial"], []).append(s)

        trial_clusters = {}
        for trial in sorted(trials_map.keys()):
            t_steps = trials_map[trial]
            clusters = cluster(t_steps, args.threshold)
            total_cyc = sum(cl["cycles"] for cl in clusters)
            print_clusters(clusters, total_cyc, f"trial={trial}（{len(t_steps)} steps）")
            trial_clusters[trial] = [summarize_cluster(cl, total_cyc, ci + 1)
                                     for ci, cl in enumerate(clusters)]
        result["per_trial"] = trial_clusters

    # ── Level 2: per-language ──
    if args.level in ("language", "all-full"):
        print()
        print("═══════════════════════════════════════════════════════════")
        print(" Level 2: per-language（同语言的 step 合并后聚类）")
        print("═══════════════════════════════════════════════════════════")

        lang_map = {}
        for s in steps:
            lang_map.setdefault(s["lang"], []).append(s)

        lang_clusters = {}
        for lang in sorted(lang_map.keys()):
            l_steps = lang_map[lang]
            clusters = cluster(l_steps, args.threshold)
            total_cyc = sum(cl["cycles"] for cl in clusters)
            print_clusters(clusters, total_cyc, f"lang={lang}（{len(l_steps)} steps, {len(set(s['trial'] for s in l_steps))} trials）")
            lang_clusters[lang] = [summarize_cluster(cl, total_cyc, ci + 1)
                                   for ci, cl in enumerate(clusters)]
        result["per_language"] = lang_clusters

    # ── Level 3: all-trials ──
    if args.level in ("all", "all-full"):
        print()
        print("═══════════════════════════════════════════════════════════")
        print(" Level 3: all-trials（全部 step 合并后聚类）")
        print("═══════════════════════════════════════════════════════════")

        clusters = cluster(steps, args.threshold)
        total_cyc = sum(cl["cycles"] for cl in clusters)
        print_clusters(clusters, total_cyc, f"all-trials（{len(steps)} steps）")
        result["all_trials"] = [summarize_cluster(cl, total_cyc, ci + 1)
                                for ci, cl in enumerate(clusters)]

    # ── 落盘 ──
    out_path = pathlib.Path(args.json_out) if args.json_out \
        else pathlib.Path(args.topdown_out) / "topdown_cluster.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print()
    print(f"  机读结果  {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
