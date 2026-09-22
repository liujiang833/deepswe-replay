#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 topdown_out 下所有 trial 的 topdown_steps_cleaned.json，四级聚类。

层级：
  L3  per-trial    : 每个 trial 内部独立聚类
  L2  per-language : 同语言的 step 合并后聚类
  L2  per-tool-type: 同 program（git/grep/go test/…）的 step 合并后聚类
  L1  all-trials   : 全部 step 合并后聚类

聚类方法：L∞ 贪心——按 cycles 降序处理，尝试加入已有 cluster
（加入后该 cluster 内任意两点的任意单维差 < 阈值），不行就新建。

输出：
  - stdout 表格
  - topdown_cluster.json（机读）
  - clusters/ 目录下 5 个 Excel 文件：
      per_trial.xlsx     per_language.xlsx   per_tool_type.xlsx
      all_trials.xlsx    steps.xlsx
  每个 cluster 有唯一 ID（如 trial#C1 / go#C1 / go test#C1 / all#C1），
  steps.xlsx 含 per_trial_cid / per_lang_cid / per_tool_cid / all_cid 四列，
  可按 cluster ID 过滤查看该 cluster 包含的 step 和指令。

用法：
  python3 topdown_cluster.py topdown_out/                    # 四级全做
  python3 topdown_cluster.py topdown_out/ --threshold 0.08  # 收紧到 8%
  python3 topdown_cluster.py topdown_out/ --level tool       # 只做 per-tool-type
"""

import argparse
import datetime
import json
import os
import pathlib
import sys

CLUSTER_THRESHOLD_DEFAULT = 0.05
LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]

# ── 导入命令分类器 ──
here = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(here.parent))
import summarize_replay as sr

# ── Excel sheet 定义 ──

CLUSTER_HEADERS = [
    "cluster_id", "scope", "trial", "lang", "program", "cluster#", "n_steps",
    "wall_s", "cycles", "pct_cycles",
    "Retiring%", "BadSpec%", "FrontendBound%", "BackendBound%",
    "max_spread", "n_trials", "rep_cmd", "rep_trial", "rep_step",
]
CLUSTER_KEYS = [
    "cluster_id", "scope", "trial", "lang", "program", "cluster", "n_steps",
    "wall_s", "cycles", "pct_cycles",
    "Retiring", "BadSpec", "FrontendBound", "BackendBound",
    "max_spread", "n_trials", "rep_cmd", "rep_trial", "rep_step",
]

STEP_HEADERS = [
    "trial", "lang", "program", "step", "n_cmds", "wall_s", "sleep_s", "cycles",
    "Retiring%", "BadSpec%", "FrontendBound%", "BackendBound%",
    "commands", "per_trial_cid", "per_lang_cid", "per_tool_cid", "all_cid",
]
STEP_KEYS = [
    "trial", "lang", "program", "step", "n_cmds", "wall_s", "sleep_s", "cycles",
    "Retiring", "BadSpec", "FrontendBound", "BackendBound",
    "commands", "per_trial_cid", "per_lang_cid", "per_tool_cid", "all_cid",
]


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


STEPS_JSON_NAME = "topdown_steps_cleaned.json"


def step_program(commands):
    """从 step 的命令列表推断 program 字段。

    多条命令时取优先级最高的（跑测试 > 写文件 > 搜索 > 版本控制 > 读文件 > 其他），
    用该条命令的 program 字段（如 go test / cargo build / grep / git / python）。
    空命令或无法分类返回 "unknown"。
    """
    if not commands:
        return "unknown"
    best_prog = None
    best_rank = len(sr.PRIORITY)
    for cmd in commands:
        if not cmd or not cmd.strip():
            continue
        try:
            c = sr.classify_command_full(cmd)
        except Exception:
            continue
        prog = c.get("program")
        if not prog:
            continue
        cat = c.get("cat", sr.OTHER)
        try:
            r = sr.PRIORITY.index(cat)
        except ValueError:
            r = len(sr.PRIORITY)
        if r < best_rank:
            best_rank = r
            best_prog = prog
    return best_prog or "unknown"


def regen_step_data(tdir, here):
    """对单个 trial 目录调用 topdown_steps.py 生成 topdown_steps_cleaned.json。

    需要 tdir/topdown/ 下有 perf.json/perf.csv + perf_start_mono.txt，
    以及 tdir/ 下有 commands.jsonl + verdict.json。
    返回 True 成功，False 失败（缺文件或 topdown_steps.py 报错）。
    """
    import subprocess

    td = tdir / "topdown"
    perf = td / "perf.json"
    if not perf.exists():
        perf = td / "perf.csv"
    if not perf.exists():
        print(f"  ⚠️ 跳过 {tdir.name}：无 perf.json/perf.csv")
        return False
    psm = td / "perf_start_mono.txt"
    if not psm.exists():
        print(f"  ⚠️ 跳过 {tdir.name}：无 perf_start_mono.txt（非 per-step 模式采集？）")
        return False
    cmds = tdir / "commands.jsonl"
    if not cmds.exists():
        print(f"  ⚠️ 跳过 {tdir.name}：无 commands.jsonl")
        return False
    verdict = tdir / "verdict.json"
    if not verdict.exists():
        print(f"  ⚠️ 跳过 {tdir.name}：无 verdict.json")
        return False

    out_json = td / STEPS_JSON_NAME
    steps_py = here / "topdown_steps.py"
    conf = here / "topdown.conf"
    cmd = [
        sys.executable, str(steps_py), str(perf),
        "--conf", str(conf),
        "--commands", str(cmds),
        "--verdict", str(verdict),
        "--perf-start-mono", str(psm),
        "--json-out", str(out_json),
        "--title", f"ARM L1 Topdown (per-step) · {tdir.name}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            # topdown_steps.py 的错误信息可能在 stdout 或 stderr
            err = (r.stderr or r.stdout or "").strip()[-500:]
            print(f"  ⚠️ topdown_steps.py 失败 ({tdir.name}, rc={r.returncode}):")
            for line in err.splitlines()[-8:]:
                print(f"     {line}")
            return False
        return out_json.exists()
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ topdown_steps.py 超时 ({tdir.name}, 120s)")
        return False
    except Exception as e:
        print(f"  ⚠️ topdown_steps.py 异常 ({tdir.name}): {e}")
        return False


def load_steps(topdown_out, trials_dir=None, regen=False):
    """扫描 topdown_out/<trial>/topdown/topdown_steps_cleaned.json，收集所有 step。

    如果某 trial 缺 cleaned.json 但有 perf 数据，自动调用 topdown_steps.py 生成。
    regen=True 时强制重新生成所有 cleaned.json。

    返回 list of dict，每个 dict 是一个 step 加上 trial 元信息：
      {trial, lang, step, n_cmds, wall_s, sleep_s, cycles, vec: (r,b,f,be), commands}

    trials_dir 指向 trial 源目录（如 full_trials），从那里的 <trial>/meta.json
    读 language 字段。不指定时从 trial 名猜，大部分非 go-/python- 开头的会变 unknown。
    """
    steps = []
    topdown_out = pathlib.Path(topdown_out)
    if not topdown_out.is_dir():
        print(f"❌ 不是目录: {topdown_out}")
        return steps

    here = pathlib.Path(__file__).resolve().parent
    n_trials = 0          # trial 目录总数
    n_cleaned = 0         # 有 cleaned.json 的（含新生成的）
    n_regen_ok = 0        # 本次新生成成功的
    n_old = 0             # 回退到旧版 topdown_steps.json 的
    n_skip = 0            # 完全没有 step 数据的
    n_skip_no_steps = 0   # 有 JSON 但 steps 为空或全被过滤的
    for tdir in sorted(topdown_out.iterdir()):
        if not tdir.is_dir():
            continue
        n_trials += 1
        jf = tdir / "topdown" / STEPS_JSON_NAME
        was_missing = not jf.exists()
        if not jf.exists() or regen:
            if regen_step_data(tdir, here):
                n_regen_ok += 1
            elif regen:
                pass  # regen 模式下失败也不跳过已有数据
        if not jf.exists():
            # 也检查旧版 topdown_steps.json（兼容未重跑的 trial）
            old = tdir / "topdown" / "topdown_steps.json"
            if not old.exists():
                n_skip += 1
                continue
            jf = old
            n_old += 1
        else:
            n_cleaned += 1
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            n_skip += 1
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
        trial_steps_before = len(steps)
        for s in data.get("steps", []):
            td = s.get("topdown")
            counts = s.get("counts", {})
            cyc = counts.get("cpu_cycles", 0)
            if not td or cyc <= 0:
                continue
            steps.append({
                "trial": trial,
                "lang": lang,
                "program": step_program(s.get("commands", [])),
                "step": s.get("step"),
                "n_cmds": s.get("n_cmds", 0),
                "wall_s": s.get("wall_s", 0),
                "sleep_s": s.get("sleep_s", 0),
                "cycles": cyc,
                "vec": (td["Retiring"], td["BadSpec"],
                        td["FrontendBound"], td["BackendBound"]),
                "commands": s.get("commands", []),
            })
        added = len(steps) - trial_steps_before
        if added == 0:
            n_skip_no_steps += 1

    # ── 诊断输出 ──
    print()
    print("── step 加载诊断 ──────────────────────────────────────────")
    print(f"  trial 目录总数     {n_trials}")
    print(f"  cleaned.json 已有  {n_cleaned}（含本次新生成 {n_regen_ok}）")
    print(f"  回退旧版 .json     {n_old}")
    print(f"  无 step 数据跳过   {n_skip}")
    print(f"  有 JSON 但 step 空 {n_skip_no_steps}")
    print(f"  实际加载 step 数   {len(steps)}")
    total_wall = sum(s["wall_s"] for s in steps)
    total_sleep = sum(s.get("sleep_s", 0) for s in steps)
    print(f"  wall_s 合计        {total_wall:.1f}s（sleep 扣除 {total_sleep:.1f}s）")
    print("───────────────────────────────────────────────────────────")
    return steps


def _kmeans(X, k, n_init=10, max_iter=300, seed=42):
    """标准 k-means（L2 距离），k-means++ 初始化，n_init 次重启取最优。

    返回 labels (int array, len=n)。
    """
    import numpy as np
    rng = np.random.RandomState(seed)
    n, d = X.shape
    if k == 1:
        return np.zeros(n, dtype=int)
    if k >= n:
        return np.arange(n, dtype=int)

    X_sq = np.sum(X ** 2, axis=1)  # (n,) 预计算
    best_labels = None
    best_inertia = float("inf")

    for _ in range(n_init):
        # k-means++ 初始化（k>100 时用随机初始化，避免 O(n*k²) 瓶颈）
        if k > 100:
            idxs = rng.choice(n, size=k, replace=False)
            centers = X[idxs].copy()
        else:
            centers = [X[rng.randint(n)]]
            for _ in range(1, k):
                d2 = np.min([np.sum((X - c) ** 2, axis=1) for c in centers], axis=0)
                total = d2.sum()
                if total == 0:
                    idx = rng.randint(n)
                else:
                    probs = d2 / total
                    idx = rng.choice(n, p=probs)
                centers.append(X[idx])
            centers = np.array(centers)

        # Lloyd 迭代
        labels = np.zeros(n, dtype=int)
        for _ in range(max_iter):
            # 分配：||x-c||² = ||x||² - 2x·c + ||c||²  （矩阵乘法，BLAS 加速）
            C_sq = np.sum(centers ** 2, axis=1)
            cross = X @ centers.T
            dists_sq = X_sq[:, None] - 2 * cross + C_sq[None, :]
            np.maximum(dists_sq, 0, out=dists_sq)  # 消除浮点负值
            new_labels = dists_sq.argmin(axis=1)
            if np.array_equal(new_labels, labels):
                break
            labels = new_labels
            for j in range(k):
                mask = labels == j
                if mask.any():
                    centers[j] = X[mask].mean(axis=0)

        inertia = 0.0
        for j in range(k):
            mask = labels == j
            if mask.any():
                inertia += np.sum((X[mask] - centers[j]) ** 2)
        if inertia < best_inertia:
            best_inertia = inertia
            best_labels = labels.copy()

    return best_labels


def _check_spread(X, labels, k, threshold):
    """检查所有 cluster 内任意两点任意单维差 < threshold（L∞ spread）。"""
    for i in range(k):
        pts = X[labels == i]
        if len(pts) <= 1:
            continue
        for d in range(X.shape[1]):
            if pts[:, d].max() - pts[:, d].min() >= threshold:
                return False
    return True


def cluster(steps, threshold, n_workers=8):
    """K-means 二分搜索：找最小的 k 使得每个 cluster 内 L∞ spread < threshold。

    二分搜索代替暴力遍历：O(log n) 次 k-means 代替 O(n) 次。
    搜索阶段 n_init=3（快），最终确认 n_init=10（准）。
    k>100 时 k-means 用随机初始化代替 k-means++（避免 O(n*k²) 瓶颈）。
    """
    if not steps:
        return []

    import numpy as np
    n = len(steps)
    X = np.array([s["vec"] for s in steps], dtype=float)

    if n == 1:
        cl = {"members": steps, "vecs": [steps[0]["vec"]],
              "cycles": steps[0]["cycles"], "wall_s": steps[0]["wall_s"]}
        return [cl]

    # 二分搜索：spread 关于 k 在实践中单调递减
    lo, hi = 1, n
    best_k = n
    while lo <= hi:
        mid = (lo + hi) // 2
        # 大 k 时减少迭代次数（每个 cluster 很小，收敛快）
        mi = 50 if mid > 100 else 300
        labels = _kmeans(X, mid, n_init=3, max_iter=mi)
        if _check_spread(X, labels, mid, threshold):
            best_k = mid
            hi = mid - 1
        else:
            lo = mid + 1

    # 最终用 n_init=10 重跑确认（k-means 随机性可能导致不同结果）
    mi = 50 if best_k > 100 else 300
    labels = _kmeans(X, best_k, n_init=10, max_iter=mi)
    while best_k < n and not _check_spread(X, labels, best_k, threshold):
        best_k += 1
        mi = 50 if best_k > 100 else 300
        labels = _kmeans(X, best_k, n_init=10, max_iter=mi)

    clusters = []
    for i in range(best_k):
        mask = labels == i
        members = [steps[j] for j in range(n) if labels[j] == i]
        if not members:
            continue
        cl = {"members": members, "vecs": [m["vec"] for m in members]}
        cl["cycles"] = sum(m["cycles"] for m in members)
        cl["wall_s"] = sum(m["wall_s"] for m in members)
        clusters.append(cl)
    clusters.sort(key=lambda c: c["wall_s"], reverse=True)
    return clusters


def summarize_cluster(cl, total_cyc, ci):
    """一个 cluster -> 可打印 + 可 JSON 的 dict。"""
    n = len(cl["members"])
    cyc = cl["cycles"]
    # cycles 加权平均四象限 = cycles 加权 centroid
    ret_w = sum(v[0] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    bad_w = sum(v[1] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    fe_w = sum(v[2] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    be_w = sum(v[3] * m["cycles"] for v, m in zip(cl["vecs"], cl["members"]))
    centroid = (ret_w / cyc, bad_w / cyc, fe_w / cyc, be_w / cyc) if cyc else (0,0,0,0)
    # max spread
    max_spread = 0.0
    for d in range(4):
        vals = [v[d] for v in cl["vecs"]]
        spread = max(vals) - min(vals)
        if spread > max_spread:
            max_spread = spread
    # 代表 step：离 cycles 加权 centroid 最近（L∞）的那条
    rep = min(cl["members"], key=lambda m: max(
        abs(m["vec"][d] - centroid[d]) for d in range(4)))
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
        "rep_trial": rep["trial"],
        "rep_step": rep["step"],
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


# ── Excel 输出 ──

def cluster_to_rows(clusters, total_cyc, scope, trial="", lang="", program=""):
    """生成 cluster 摘要行，每行带唯一 cluster_id。"""
    rows = []
    for ci, cl in enumerate(clusters):
        s = summarize_cluster(cl, total_cyc, ci + 1)
        if scope == "per-trial":
            s["cluster_id"] = f"{trial}#C{ci + 1}"
        elif scope == "per-language":
            s["cluster_id"] = f"{lang}#C{ci + 1}"
        elif scope == "per-tool-type":
            s["cluster_id"] = f"{program}#C{ci + 1}"
        else:
            s["cluster_id"] = f"all#C{ci + 1}"
        s["scope"] = scope
        s["trial"] = trial
        s["lang"] = lang
        s["program"] = program
        s["n_trials"] = len(s.get("trials", []))
        rows.append(s)
    return rows


def build_step_cid_map(clusters, prefix):
    """从 raw clusters 构建 (trial, step) -> cluster_id 映射。"""
    cid_map = {}
    for ci, cl in enumerate(clusters):
        cid = f"{prefix}#C{ci + 1}"
        for m in cl["members"]:
            cid_map[(m["trial"], m["step"])] = cid
    return cid_map


def build_step_rows(steps, trial_cid, lang_cid, tool_cid, all_cid):
    """构建全量 step 行，含四级 cluster ID。"""
    rows = []
    for s in steps:
        key = (s["trial"], s["step"])
        rows.append({
            "trial": s["trial"],
            "lang": s["lang"],
            "program": s.get("program", "unknown"),
            "step": s["step"],
            "n_cmds": s["n_cmds"],
            "wall_s": s["wall_s"],
            "sleep_s": s.get("sleep_s", 0),
            "cycles": s["cycles"],
            "Retiring": round(s["vec"][0] * 100, 2),
            "BadSpec": round(s["vec"][1] * 100, 2),
            "FrontendBound": round(s["vec"][2] * 100, 2),
            "BackendBound": round(s["vec"][3] * 100, 2),
            "commands": "; ".join(s.get("commands", [])),
            "per_trial_cid": trial_cid.get(key, ""),
            "per_lang_cid": lang_cid.get(key, ""),
            "per_tool_cid": tool_cid.get(key, ""),
            "all_cid": all_cid.get(key, ""),
        })
    rows.sort(key=lambda r: r["wall_s"], reverse=True)
    return rows


# ── 累计 wall_s 着色阈值 ──
CUMUL_COLORS = [
    (0.50, "DCE6F1"),  # 浅蓝 — 50%
    (0.60, "B8CCE4"),  # 蓝   — 60%
    (0.70, "95B3D7"),  # 中蓝 — 70%
    (0.80, "C6EFCE"),  # 绿   — 80%
    (0.85, "FFEB9C"),  # 黄   — 85%
    (0.90, "FFD966"),  # 橙   — 90%
    (0.95, "F4B084"),  # 红   — 95%
]


def _write_sheet(ws, headers, keys, rows, mark_cumul=False):
    """填充一个 worksheet：表头 + 数据行 + 冻结首行 + 自动列宽。

    mark_cumul=True 时对 cluster 表按累计 wall_s 标记 80/85/90/95% 的行。
    """
    import openpyxl  # noqa: F401
    from openpyxl.styles import Font, PatternFill, Alignment

    bold = Font(bold=True)
    header_fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2",
                              fill_type="solid")
    for ci, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for ri, r in enumerate(rows, 2):
        for ci, k in enumerate(keys, 1):
            v = r.get(k, "")
            if isinstance(v, list):
                v = "; ".join(str(x) for x in v)
            ws.cell(row=ri, column=ci, value=v)

    # ── 累计 wall_s 着色（仅 cluster 表）──
    if mark_cumul and "wall_s" in keys:
        total_wall = sum(r.get("wall_s", 0) for r in rows)
        if total_wall > 0:
            cumul = 0.0
            crossed = set()
            for ri, r in enumerate(rows, 2):
                cumul += r.get("wall_s", 0)
                pct = cumul / total_wall
                for threshold, color in CUMUL_COLORS:
                    if pct >= threshold and threshold not in crossed:
                        crossed.add(threshold)
                        fill = PatternFill(start_color=color, end_color=color,
                                           fill_type="solid")
                        for ci in range(1, len(headers) + 1):
                            ws.cell(row=ri, column=ci).fill = fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        letter = col[0].column_letter
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[letter].width = min(max_len + 2, 60)


def write_excel(xlsx_dir, trial_rows, lang_rows, tool_rows, all_rows, step_rows):
    """写 5 个独立 Excel 文件到 xlsx_dir：
    per_trial.xlsx / per_language.xlsx / per_tool_type.xlsx / all_trials.xlsx / steps.xlsx
    """
    import openpyxl

def merge_consecutive_program_rows(rows):
    """合并 per-tool-type 表中连续同 program 的行。

    合并后：
    - cluster_id: python#C5 + python#C6 → python#C5_C6
    - n_steps / wall_s / cycles: 求和
    - pct_cycles: 重新计算
    - Retiring/BadSpec/FE/BE: cycles 加权平均
    - max_spread: 取最大
    - trials: 取并集
    - rep_cmd / rep_trial / rep_step: 取 wall_s 最大的那行
    """
    if not rows:
        return rows
    merged = []
    i = 0
    while i < len(rows):
        group = [rows[i]]
        j = i + 1
        while j < len(rows) and rows[j].get("program") == rows[i].get("program"):
            group.append(rows[j])
            j += 1
        if len(group) == 1:
            merged.append(group[0])
        else:
            total_cyc = sum(r["cycles"] for r in group)
            total_wall = sum(r["wall_s"] for r in group)
            total_steps = sum(r["n_steps"] for r in group)
            # cycles 加权四象限
            ret_w = sum(r["Retiring"] * r["cycles"] for r in group)
            bad_w = sum(r["BadSpec"] * r["cycles"] for r in group)
            fe_w = sum(r["FrontendBound"] * r["cycles"] for r in group)
            be_w = sum(r["BackendBound"] * r["cycles"] for r in group)
            # cluster_id 合并
            cids = []
            for r in group:
                cid = r["cluster_id"]
                # 提取 C{n} 部分
                cnum = cid.rsplit("#C", 1)[-1] if "#C" in cid else cid
                cids.append(cnum)
            merged_cid = f"{group[0]['program']}#C{'_C'.join(cids)}"
            # trials 并集
            all_trials = set()
            for r in group:
                all_trials.update(r.get("trials", []))
            # 代表 step：取 wall_s 最大的
            best = max(group, key=lambda r: r["wall_s"])
            merged.append({
                "cluster_id": merged_cid,
                "scope": group[0]["scope"],
                "trial": "",
                "lang": "",
                "program": group[0]["program"],
                "cluster": merged_cid,
                "n_steps": total_steps,
                "wall_s": round(total_wall, 2),
                "cycles": int(total_cyc),
                "pct_cycles": round(100.0 * total_cyc / total_cyc, 1),  # 占自身组的比例
                "Retiring": round(ret_w / total_cyc, 2) if total_cyc else 0,
                "BadSpec": round(bad_w / total_cyc, 2) if total_cyc else 0,
                "FrontendBound": round(fe_w / total_cyc, 2) if total_cyc else 0,
                "BackendBound": round(be_w / total_cyc, 2) if total_cyc else 0,
                "max_spread": max(r["max_spread"] for r in group),
                "trials": sorted(all_trials),
                "n_trials": len(all_trials),
                "rep_cmd": best["rep_cmd"],
                "rep_trial": best["rep_trial"],
                "rep_step": best["rep_step"],
            })
        i = j
    return merged


def write_cluster_excel(xlsx_dir, fname, rows):
    """写单个 cluster Excel 文件（带累计 wall_s 着色）。"""
    import openpyxl
    xlsx_dir = pathlib.Path(xlsx_dir)
    xlsx_dir.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = fname.replace(".xlsx", "")
    _write_sheet(ws, CLUSTER_HEADERS, CLUSTER_KEYS, rows, mark_cumul=True)
    p = xlsx_dir / fname
    wb.save(str(p))
    print(f"  Excel  {p}")


def write_steps_excel(xlsx_dir, rows):
    """写 steps Excel 文件。"""
    import openpyxl
    xlsx_dir = pathlib.Path(xlsx_dir)
    xlsx_dir.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "steps"
    _write_sheet(ws, STEP_HEADERS, STEP_KEYS, rows, mark_cumul=False)
    p = xlsx_dir / "steps.xlsx"
    wb.save(str(p))
    print(f"  Excel  {p}")


def main():
    ap = argparse.ArgumentParser(
        description="扫描 topdown_out 下所有 trial 的 topdown_steps.json，三级聚类")
    ap.add_argument("topdown_out", help="topdown_out 目录（含 <trial>/topdown/topdown_steps.json）")
    ap.add_argument("--threshold", type=float, default=CLUSTER_THRESHOLD_DEFAULT,
                    help=f"L∞ 聚类阈值（默认 {CLUSTER_THRESHOLD_DEFAULT} = 10%%）")
    ap.add_argument("--level", choices=("trial", "language", "tool", "all", "all-full"), default="all-full",
                    help="只做某一级（默认 all-full = 四级全做）")
    ap.add_argument("--json-out", default="", help="机读结果落盘路径")
    ap.add_argument("--xlsx-dir", default="",
                    help="Excel 输出目录（默认 <topdown_out>/clusters/），"
                         "产出 per_trial.xlsx / per_language.xlsx / per_tool_type.xlsx / "
                         "all_trials.xlsx / steps.xlsx")
    ap.add_argument("--only-lang", default="", help="只看某语言（per-language 和 all-trials 都过滤）")
    ap.add_argument("--trials-dir", default="full_trials",
                    help="trial 源目录（默认 full_trials），从 <trial>/meta.json 读语言")
    ap.add_argument("--regen-steps", action="store_true",
                    help="强制重新生成所有 trial 的 topdown_steps_cleaned.json（调用 topdown_steps.py）")
    ap.add_argument("--workers", type=int, default=8,
                    help="K-means 暴力搜索并行进程数（默认 8）")
    args = ap.parse_args()

    here = pathlib.Path(__file__).resolve().parent
    trials_dir = pathlib.Path(args.trials_dir)
    if not trials_dir.is_absolute():
        trials_dir = here / trials_dir
    trials_dir = trials_dir if trials_dir.is_dir() else None
    steps = load_steps(args.topdown_out, trials_dir, regen=args.regen_steps)
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
    # program 分布
    prog_counts = {}
    for s in steps:
        prog_counts[s["program"]] = prog_counts.get(s["program"], 0) + 1

    print("=" * 78)
    print(f" topdown 向量聚类（L∞ 阈值 {args.threshold*100:.0f}%）")
    print(f" 数据来源   {args.topdown_out}")
    print(f" step 总数   {len(steps)}")
    print(f" 语言分布   {lang_counts}")
    print(f" program 分布（{len(prog_counts)} 种）:")
    for prog, cnt in sorted(prog_counts.items(), key=lambda x: -x[1]):
        print(f"   {prog:30s} {cnt:5d}")
    print(f" 阈值       任意两点任意单维差 < {args.threshold*100:.0f}%")
    print("=" * 78)

    result = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "topdown_out": str(args.topdown_out),
        "threshold": args.threshold,
        "n_steps": len(steps),
        "lang_counts": lang_counts,
        "program_counts": prog_counts,
    }

    # 用于构建 steps sheet 的 cluster ID 映射
    trial_cid_map = {}
    lang_cid_map = {}
    tool_cid_map = {}
    all_cid_map = {}

    # ── Excel 输出目录 ──
    xlsx_dir = pathlib.Path(args.xlsx_dir) if args.xlsx_dir \
        else pathlib.Path(args.topdown_out) / "clusters"

    # ── Level 1: per-trial ──
    trial_rows = []
    if args.level in ("trial", "all-full"):
        print()
        print("═══════════════════════════════════════════════════════════")
        print(" Level 3: per-trial（每个 trial 内部独立聚类）")
        print("═══════════════════════════════════════════════════════════")

        trials_map = {}
        for s in steps:
            trials_map.setdefault(s["trial"], []).append(s)

        trial_clusters = {}
        for trial in sorted(trials_map.keys()):
            t_steps = trials_map[trial]
            lang = t_steps[0]["lang"] if t_steps else ""
            clusters = cluster(t_steps, args.threshold, n_workers=args.workers)
            total_cyc = sum(cl["cycles"] for cl in clusters)
            print_clusters(clusters, total_cyc, f"trial={trial}（{len(t_steps)} steps）")
            trial_clusters[trial] = [summarize_cluster(cl, total_cyc, ci + 1)
                                     for ci, cl in enumerate(clusters)]
            trial_rows.extend(cluster_to_rows(
                clusters, total_cyc, "per-trial", trial=trial, lang=lang))
            trial_cid_map.update(build_step_cid_map(clusters, trial))
        result["per_trial"] = trial_clusters
        trial_rows.sort(key=lambda r: r["wall_s"], reverse=True)
        write_cluster_excel(xlsx_dir, "per_trial.xlsx", trial_rows)

    # ── Level 2: per-language ──
    lang_rows = []
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
            clusters = cluster(l_steps, args.threshold, n_workers=args.workers)
            total_cyc = sum(cl["cycles"] for cl in clusters)
            print_clusters(clusters, total_cyc, f"lang={lang}（{len(l_steps)} steps, {len(set(s['trial'] for s in l_steps))} trials）")
            lang_clusters[lang] = [summarize_cluster(cl, total_cyc, ci + 1)
                                   for ci, cl in enumerate(clusters)]
            lang_rows.extend(cluster_to_rows(
                clusters, total_cyc, "per-language", lang=lang))
            lang_cid_map.update(build_step_cid_map(clusters, lang))
        result["per_language"] = lang_clusters
        lang_rows.sort(key=lambda r: r["wall_s"], reverse=True)
        write_cluster_excel(xlsx_dir, "per_language.xlsx", lang_rows)

    # ── Level 2b: per-tool-type ──
    tool_rows = []
    if args.level in ("tool", "all-full"):
        print()
        print("═══════════════════════════════════════════════════════════")
        print(" Level 2: per-tool-type（同 program 的 step 合并后聚类）")
        print("═══════════════════════════════════════════════════════════")

        tool_map = {}
        for s in steps:
            tool_map.setdefault(s["program"], []).append(s)

        tool_clusters = {}
        for prog in sorted(tool_map.keys()):
            p_steps = tool_map[prog]
            clusters = cluster(p_steps, args.threshold, n_workers=args.workers)
            total_cyc = sum(cl["cycles"] for cl in clusters)
            print_clusters(clusters, total_cyc, f"tool={prog}（{len(p_steps)} steps, {len(set(s['trial'] for s in p_steps))} trials）")
            tool_clusters[prog] = [summarize_cluster(cl, total_cyc, ci + 1)
                                    for ci, cl in enumerate(clusters)]
            tool_rows.extend(cluster_to_rows(
                clusters, total_cyc, "per-tool-type", program=prog))
            tool_cid_map.update(build_step_cid_map(clusters, prog))
        result["per_tool_type"] = tool_clusters
        tool_rows.sort(key=lambda r: r["wall_s"], reverse=True)
        write_cluster_excel(xlsx_dir, "per_tool_type.xlsx", tool_rows)

        # 合并连续同 program 的行，写合并版
        merged_tool_rows = merge_consecutive_program_rows(tool_rows)
        write_cluster_excel(xlsx_dir, "per_tool_type_merged.xlsx", merged_tool_rows)

    # ── Level 1: all-trials ──
    all_rows = []
    if args.level in ("all", "all-full"):
        print()
        print("═══════════════════════════════════════════════════════════")
        print(" Level 1: all-trials（全部 step 合并后聚类）")
        print("═══════════════════════════════════════════════════════════")

        clusters = cluster(steps, args.threshold, n_workers=args.workers)
        total_cyc = sum(cl["cycles"] for cl in clusters)
        print_clusters(clusters, total_cyc, f"all-trials（{len(steps)} steps）")
        result["all_trials"] = [summarize_cluster(cl, total_cyc, ci + 1)
                                for ci, cl in enumerate(clusters)]
        all_rows = cluster_to_rows(clusters, total_cyc, "all-trials")
        all_cid_map = build_step_cid_map(clusters, "all")
        all_rows.sort(key=lambda r: r["wall_s"], reverse=True)
        write_cluster_excel(xlsx_dir, "all_trials.xlsx", all_rows)

    # ── steps 表（需要所有级别的 cid map）──
    step_rows = build_step_rows(steps, trial_cid_map, lang_cid_map, tool_cid_map, all_cid_map)
    write_steps_excel(xlsx_dir, step_rows)

    # ── JSON 落盘 ──
    out_path = pathlib.Path(args.json_out) if args.json_out \
        else pathlib.Path(args.topdown_out) / "topdown_cluster.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"  机读结果  {out_path}")

    # ── 保存本次命令行（便于复现）──
    cmd_path = xlsx_dir / "run_cmd.txt"
    cmd_path.write_text(
        f"# {' '.join([__file__] + sys.argv[1:])}\n"
        f"# generated: {result['generated_utc']}\n"
        f"# steps: {len(steps)}, threshold: {args.threshold}\n",
        encoding="utf-8")
    print(f"  复现命令  {cmd_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
