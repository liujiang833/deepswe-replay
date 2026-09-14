#!/usr/bin/env python3
"""一轮 replay 的「命令类型 × 次数 / 耗时」统计（只用标准库）。

输入是 run_batch.py 产出的一轮批量 `runs/<batch>/`：`summary.json` + `<trial>/commands.jsonl`。
输出落在一个目录（缺省 `<run_dir>/cmd_stats/`）：

    manifest.json              collect 的清单：纳入了谁、排除了谁、各自原因
    per_benchmark/<trial>.json 每条 benchmark 一份：meta + 逐条命令记录 + 分组统计表
    per_language/<lang>.json   按语言汇聚
    all.json                   全体汇聚
    per_benchmark.csv / per_language.csv / all.csv   三个层级各一份长表
    SUMMARY.md                 给人读的汇总

两个子命令，职责刻意拆开：

    collect <run_dir>    读 summary.json 和 commands.jsonl，分类，写 per_benchmark/*.json 与 manifest.json。
                         这是唯一碰原始产物、唯一调分类器的一步。
    aggregate <out_dir>  **只读 per_benchmark/*.json**（manifest.json 只用来在 SUMMARY.md 里列出排除项），
                         汇聚出 per_language / all、三份 CSV、SUMMARY.md。分位数用逐条命令记录重新算，
                         不拿各 benchmark 的统计值再做平均。

纳入判据（与 run_batch.py 算 n_pass 的判据一致，见 write_summary 里的 `ok = ...`）：
    exit_code == 0 且 (smoke 模式 或 verdict.patch_identical is True)
另外还要同时满足：
    - 没被截断：summary.json 的 options.smoke == 0，且 verdict 里
      n_replayed + n_skipped_sentinel == n_cmds_trace（replay.py --limit 会让左边变小）；
    - <trial>/commands.jsonl 存在、每行都能解析且带齐字段、条数 == verdict.n_replayed。
不满足的都进 manifest.json 的 excluded，逐条写明原因（可能有多条）。

「命令类型」两个维度，分类器是 deepswe/summarize_replay.py 的 classify_command_full：
    cat / cat_detail  主类别、主类别 › 细类
    program           决定主类别的那条语句的程序名；git/go/cargo/npm/pnpm/yarn/bun/deno/pip/uv/poetry
                      带子命令（`go test`），npx 带被启动的程序（`npx vitest`），run/exec/dlx 这类再带
                      脚本名（`npm run build`），`python -m X` 带模块名。完整规则见 program_key 的 docstring。

自检（不过就报错退出，退出码 1）：
    - 每个 benchmark：各维度分组的 count / wall_s 之和 == 该 benchmark 的总数；
      aggregate 读回来时，用逐条记录重算的分组表 == 文件里存的分组表；
    - 守恒：language 层每个 key 的 count / wall_s == 该语言各 benchmark 之和；
      all 层 == 各语言之和（总数与逐 key 都核）。

用法：
    python3 cmd_stats.py                       # 取 runs/ 下最新一轮（summary.json 的 mtime 最新）
    python3 cmd_stats.py runs/<batch>          # collect + aggregate
    python3 cmd_stats.py runs/<batch> -o /tmp/stats
    python3 cmd_stats.py collect runs/<batch> [-o OUT]
    python3 cmd_stats.py aggregate runs/<batch>/cmd_stats
"""

import argparse
import csv
import datetime
import hashlib
import importlib.util
import json
import math
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SCHEMA_BENCH = "cmd_stats.per_benchmark/1"
SCHEMA_LANG = "cmd_stats.per_language/1"
SCHEMA_ALL = "cmd_stats.all/1"
SCHEMA_MANIFEST = "cmd_stats.manifest/1"

# 分组维度：(键名, 给人看的名字)
DIMS = [("cat", "主类别"), ("cat_detail", "主类别 › 细类"), ("program", "program")]
DETAIL_SEP = " › "           # 细类里本身会有 `/`（「语法/编译校验」），所以不用 / 当分隔符
EMPTY_PROGRAM = "(空命令)"
# 浮点和的守恒容差：wall_s 在 jsonl 里是 4 位小数，逐条 fsum 再相加，误差远小于这个数
WALL_TOL = 1e-6
CRITERION = ("exit_code == 0 且 (smoke 模式 或 verdict.patch_identical is True)"
             "（同 run_batch.py 的 n_pass）；且未被 --smoke/--limit 截断；"
             "且 commands.jsonl 存在、可解析、条数 == verdict.n_replayed")
REQUIRED_FIELDS = ("i", "rc", "timed_out", "wall_s", "cmd_stripped")


class InvariantError(Exception):
    """自检不过。"""


class InputError(Exception):
    """输入不对（找不到 summary.json、目录不存在……）。"""


# ---------------------------------------------------------------- 小工具


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def quantile(sorted_vals, p):
    """线性插值分位数。与 summarize_replay.q 同一个公式；这里另写一份，
    是为了 aggregate 不依赖分类器文件（它只读 per_benchmark/*.json）。"""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def pct(x, tot):
    return None if not tot else 100.0 * x / tot


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s or "_")


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def load_classifier():
    """仓库布局：summarize_replay.py 在 crosslang/ 的上一级；bundle 布局：与本脚本同级。
    查找顺序与 run_batch.py 的 find_replay / topdown_trial.sh 定位 replay.py 一致（先同级再上一级）。"""
    tried = [HERE / "summarize_replay.py", HERE.parent / "summarize_replay.py"]
    for p in tried:
        if p.exists():
            spec = importlib.util.spec_from_file_location("summarize_replay", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "classify_command_full"):
                raise InputError(f"{p} 太旧，没有 classify_command_full（需要 2026-09-14 之后的版本）")
            return mod, p
    raise InputError("找不到 summarize_replay.py（找过 " + "、".join(map(str, tried)) + "）")


def latest_run(runs_root):
    """runs/ 下最新的一轮 = 含 summary.json、且 summary.json 的 mtime 最新的那个目录。
    不按目录名排：`-o` 可以起任意名字（full-arm-test），名字不一定是时间戳。"""
    cands = [d for d in runs_root.iterdir() if (d / "summary.json").is_file()] \
        if runs_root.is_dir() else []
    if not cands:
        raise InputError(f"{runs_root} 下没有含 summary.json 的批次目录")
    return max(cands, key=lambda d: ((d / "summary.json").stat().st_mtime, d.name))


# ---------------------------------------------------------------- 分组统计


def key_of(rec, dim):
    if dim == "cat":
        return rec["cat"]
    if dim == "cat_detail":
        return f"{rec['cat']}{DETAIL_SEP}{rec['detail']}"
    return rec["program"] if rec["program"] is not None else EMPTY_PROGRAM


def group_rows(recs, dim, bench_of=None):
    """recs -> 分组表（按 wall_s 降序）。bench_of 给了就另算每个 key 出现在几个 benchmark 里。"""
    tot_n = len(recs)
    tot_w = math.fsum(r["wall_s"] for r in recs)
    buckets = {}
    for r in recs:
        buckets.setdefault(key_of(r, dim), []).append(r)
    rows = []
    for key, g in buckets.items():
        walls = sorted(r["wall_s"] for r in g)
        w = math.fsum(walls)
        n_cpu_null = sum(1 for r in g if r["usage_usec"] is None)
        row = {
            "key": key,
            "count": len(g),
            "count_pct": pct(len(g), tot_n),
            "wall_s": w,
            "wall_pct": pct(w, tot_w),
            "mean_s": w / len(g),
            "median_s": quantile(walls, 0.5),
            "p90_s": quantile(walls, 0.9),
            "max_s": walls[-1],
            "n_timed_out": sum(1 for r in g if r["timed_out"]),
            "n_rc_nonzero": sum(1 for r in g if r["rc"] != 0),
            # 有一条没采就整组记 null：部分求和看着像真数，其实少算了
            "cpu_s": None if n_cpu_null else math.fsum(r["usage_usec"] for r in g) / 1e6,
            "n_cpu_null": n_cpu_null,
        }
        if dim == "cat_detail":
            row["cat"], row["detail"] = g[0]["cat"], g[0]["detail"]
        if bench_of is not None:
            row["n_benchmarks_with_key"] = len({bench_of(r) for r in g})
        rows.append(row)
    rows.sort(key=lambda r: (-r["wall_s"], -r["count"], r["key"]))
    return rows


def all_groups(recs, bench_of=None):
    return {dim: group_rows(recs, dim, bench_of) for dim, _ in DIMS}


def totals(recs):
    n_null = sum(1 for r in recs if r["usage_usec"] is None)
    return {
        "n_commands": len(recs),
        "total_wall_s": math.fsum(r["wall_s"] for r in recs),
        "total_cpu_s": None if (n_null or not recs) else math.fsum(r["usage_usec"] for r in recs) / 1e6,
        "n_cpu_null": n_null,
    }


def check_partition(groups, n, wall, where):
    """每个维度的分组必须把全部命令不重不漏地分完。"""
    for dim, rows in groups.items():
        c = sum(r["count"] for r in rows)
        w = math.fsum(r["wall_s"] for r in rows)
        if c != n or abs(w - wall) > WALL_TOL:
            raise InvariantError(f"{where} 维度 {dim}：分组 count 和 {c} / wall 和 {w!r} "
                                 f"≠ 总数 {n} / {wall!r}")


# ---------------------------------------------------------------- collect


def parse_jsonl(path):
    """-> (records, 错误原因列表)。"""
    recs, errs = [], []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as e:
            errs.append(f"commands.jsonl 第 {ln} 行不是合法 JSON（{e}）")
            continue
        miss = [k for k in REQUIRED_FIELDS if k not in r]
        if miss:
            errs.append(f"commands.jsonl 第 {ln} 行缺字段 {','.join(miss)}")
            continue
        if not isinstance(r["wall_s"], (int, float)) or isinstance(r["wall_s"], bool):
            errs.append(f"commands.jsonl 第 {ln} 行 wall_s 不是数字（{r['wall_s']!r}）")
            continue
        recs.append(r)
    return recs, errs


def collect(run_dir, out_dir):
    run_dir = pathlib.Path(run_dir).resolve()
    out_dir = pathlib.Path(out_dir).resolve()
    sf = run_dir / "summary.json"
    if not sf.is_file():
        raise InputError(f"{sf} 不存在 —— 不是一轮 run_batch 的输出目录，或那一轮没跑到写汇总")
    try:
        summary = json.loads(sf.read_text(encoding="utf-8"))
    except ValueError as e:
        raise InputError(f"{sf} 不是合法 JSON：{e}")
    opts = summary.get("options") or {}
    smoke = int(opts.get("smoke") or 0)
    results = summary.get("results") or []

    sr, sr_path = load_classifier()
    classifier = {"path": str(sr_path), "sha256": sha256_of(sr_path)}

    pb_dir = out_dir / "per_benchmark"
    pb_dir.mkdir(parents=True, exist_ok=True)
    for old in pb_dir.glob("*.json"):          # 上一次 collect 的残留（那次纳入、这次排除的）必须清掉
        old.unlink()

    included, excluded, seen = [], [], set()
    n_pass_recomputed = 0
    for r in results:
        trial = r.get("trial")
        lang = r.get("lang") or "?"
        v = r.get("verdict")
        ec = r.get("exit_code")
        pi = (v or {}).get("patch_identical")
        passed = (ec == 0) and (bool(smoke) or pi is True)      # 与 run_batch.py write_summary 同式
        n_pass_recomputed += passed
        reasons = []

        if not isinstance(trial, str) or not trial or "/" in trial or trial in (".", ".."):
            excluded.append({"trial": trial, "lang": lang, "exit_code": ec,
                             "reasons": [f"trial 名不合法：{trial!r}"]})
            continue
        if trial in seen:
            excluded.append({"trial": trial, "lang": lang, "exit_code": ec,
                             "reasons": ["summary.json 里同名 trial 重复出现，只认第一次"]})
            continue
        seen.add(trial)

        if ec != 0:
            reasons.append(f"退出码 {ec}（run_batch 判失败）")
        if not smoke and pi is not True:
            reasons.append(f"patch_identical={pi}（run_batch 判失败）")
        if smoke:
            reasons.append(f"截断：本轮是 --smoke {smoke}（summary.json options.smoke）")
        n_rep = None
        if not isinstance(v, dict):
            reasons.append("没有 verdict（replay.py 没跑到收尾）")
        else:
            n_rep, n_trace, n_sent = (v.get("n_replayed"), v.get("n_cmds_trace"),
                                      v.get("n_skipped_sentinel"))
            if not isinstance(n_rep, int):
                reasons.append("verdict 缺 n_replayed，无法核对条数")
                n_rep = None
            if all(isinstance(x, int) for x in (n_rep, n_trace, n_sent)) and n_rep + n_sent < n_trace:
                reasons.append(f"截断：n_replayed {n_rep} + 哨兵 {n_sent} < trace 条数 {n_trace}"
                               f"（replay.py --limit）")

        jf = run_dir / trial / "commands.jsonl"
        recs = []
        if not jf.is_file():
            reasons.append("commands.jsonl 不存在")
        else:
            recs, errs = parse_jsonl(jf)
            reasons += errs
            if not errs and n_rep is not None and len(recs) != n_rep:
                reasons.append(f"commands.jsonl {len(recs)} 条 ≠ verdict.n_replayed {n_rep}")

        if reasons:
            excluded.append({"trial": trial, "lang": lang, "exit_code": ec,
                             "run_batch_pass": passed, "reasons": reasons})
            continue

        cmds = []
        for x in recs:
            c = sr.classify_command_full(x["cmd_stripped"])
            u = x.get("usage_usec")
            cmds.append({
                "i": x["i"], "cat": c["cat"], "detail": c["detail"], "program": c["program"],
                "wall_s": x["wall_s"], "rc": x["rc"], "timed_out": bool(x["timed_out"]),
                "usage_usec": u if isinstance(u, (int, float)) and not isinstance(u, bool) else None,
            })
        tot = totals(cmds)
        groups = all_groups(cmds)
        check_partition(groups, tot["n_commands"], tot["total_wall_s"], f"[collect] {trial}")
        doc = {
            "schema": SCHEMA_BENCH,
            "trial": trial, "lang": lang,
            "task_id": r.get("task_id"), "model": r.get("model"),
            "run_dir": str(run_dir),
            "run_generated_utc": summary.get("generated_utc"),
            "generated_utc": now_utc(),
            "source": {"commands_jsonl": f"{trial}/commands.jsonl", "sha256": sha256_of(jf)},
            "classifier": classifier,
            "exit_code": ec,
            "verdict": {k: v.get(k) for k in ("n_cmds_trace", "n_skipped_sentinel", "n_replayed",
                                              "patch_identical", "rc_match", "rc_match_semantic",
                                              "elapsed_s", "cmd_timeout_s", "metrics_collected")},
            "cpu_collected": tot["n_cpu_null"] == 0 and tot["n_commands"] > 0,
            **tot,
            "commands": cmds,
            "groups": groups,
        }
        write_json(pb_dir / f"{trial}.json", doc)
        included.append({"trial": trial, "lang": lang, "n_commands": tot["n_commands"],
                         "total_wall_s": tot["total_wall_s"], "cpu_collected": doc["cpu_collected"]})

    manifest = {
        "schema": SCHEMA_MANIFEST,
        "generated_utc": now_utc(),
        "run_dir": str(run_dir),
        "summary": {k: summary.get(k) for k in ("generated_utc", "n_total", "n_pass", "n_planned",
                                                "n_not_run", "interrupted", "not_run_reason")},
        "options": opts,
        "criterion": CRITERION,
        "n_pass_recomputed": n_pass_recomputed,
        # 对不上一般是 summary.json 由旧版 run_batch 写的；只提示，不拦
        "n_pass_consistent": summary.get("n_pass") in (None, n_pass_recomputed),
        "classifier": classifier,
        "n_included": len(included), "n_excluded": len(excluded),
        "included": included, "excluded": excluded,
    }
    write_json(out_dir / "manifest.json", manifest)
    return manifest


# ---------------------------------------------------------------- aggregate


def rows_by_key(rows):
    return {r["key"]: r for r in rows}


def verify_benchmark(doc, path):
    """per_benchmark 文件读回来先自证：逐条记录重算的分组表必须和文件里存的一致。"""
    if doc.get("schema") != SCHEMA_BENCH:
        raise InvariantError(f"{path.name}：schema 是 {doc.get('schema')!r}，不是 {SCHEMA_BENCH}")
    cmds = doc["commands"]
    tot = totals(cmds)
    if tot["n_commands"] != doc["n_commands"] or abs(tot["total_wall_s"] - doc["total_wall_s"]) > WALL_TOL:
        raise InvariantError(f"{path.name}：逐条记录 {tot['n_commands']} 条 / {tot['total_wall_s']!r}s "
                             f"≠ 文件头 {doc['n_commands']} 条 / {doc['total_wall_s']!r}s")
    fresh = all_groups(cmds)
    check_partition(fresh, tot["n_commands"], tot["total_wall_s"], path.name)
    for dim, _ in DIMS:
        a, b = rows_by_key(fresh[dim]), rows_by_key(doc["groups"].get(dim, []))
        if set(a) != set(b):
            raise InvariantError(f"{path.name} 维度 {dim}：存的分组键与逐条重算的不一致 "
                                 f"（只在存档里：{sorted(set(b) - set(a))[:5]}，"
                                 f"只在重算里：{sorted(set(a) - set(b))[:5]}）")
        for k in a:
            if a[k]["count"] != b[k]["count"] or abs(a[k]["wall_s"] - b[k]["wall_s"]) > WALL_TOL:
                raise InvariantError(f"{path.name} 维度 {dim} 键 {k!r}：存的 count/wall "
                                     f"{b[k]['count']}/{b[k]['wall_s']!r} ≠ 重算 "
                                     f"{a[k]['count']}/{a[k]['wall_s']!r}")


def check_conservation(parent_groups, parent_tot, children, where):
    """parent 的总数与逐 key 的 count / wall_s == 各 child 之和。
    children: [(名字, groups, totals)]"""
    n = sum(t["n_commands"] for _, _, t in children)
    w = math.fsum(t["total_wall_s"] for _, _, t in children)
    if n != parent_tot["n_commands"] or abs(w - parent_tot["total_wall_s"]) > WALL_TOL:
        raise InvariantError(f"{where}：总 count/wall {parent_tot['n_commands']}/"
                             f"{parent_tot['total_wall_s']!r} ≠ 下层之和 {n}/{w!r}")
    for dim, _ in DIMS:
        want_c, want_w = {}, {}
        for _, g, _ in children:
            for row in g[dim]:
                want_c[row["key"]] = want_c.get(row["key"], 0) + row["count"]
                want_w.setdefault(row["key"], []).append(row["wall_s"])
        got = rows_by_key(parent_groups[dim])
        if set(got) != set(want_c):
            raise InvariantError(f"{where} 维度 {dim}：键集合与下层之并不一致")
        for k, row in got.items():
            if row["count"] != want_c[k] or abs(row["wall_s"] - math.fsum(want_w[k])) > WALL_TOL:
                raise InvariantError(f"{where} 维度 {dim} 键 {k!r}：count/wall "
                                     f"{row['count']}/{row['wall_s']!r} ≠ 下层之和 "
                                     f"{want_c[k]}/{math.fsum(want_w[k])!r}")


LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]


def lang_sort_key(lang):
    return (LANG_ORDER.index(lang) if lang in LANG_ORDER else 99, lang)


def aggregate(out_dir):
    out_dir = pathlib.Path(out_dir).resolve()
    pb_dir = out_dir / "per_benchmark"
    if not pb_dir.is_dir():
        raise InputError(f"{pb_dir} 不存在 —— 先跑 collect")
    benches = []
    for p in sorted(pb_dir.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise InputError(f"{p} 不是合法 JSON：{e}")
        verify_benchmark(doc, p)
        benches.append(doc)
    names = [b["trial"] for b in benches]
    if len(set(names)) != len(names):
        raise InvariantError("per_benchmark 里有重复的 trial")

    by_lang = {}
    for b in benches:
        by_lang.setdefault(b["lang"], []).append(b)

    bench_of = lambda r: r["_trial"]
    lang_docs = {}
    for lang in sorted(by_lang, key=lang_sort_key):
        bs = by_lang[lang]
        recs = [dict(c, _trial=b["trial"]) for b in bs for c in b["commands"]]
        tot = totals(recs)
        groups = all_groups(recs, bench_of)
        check_partition(groups, tot["n_commands"], tot["total_wall_s"], f"[language] {lang}")
        check_conservation(groups, tot, [(b["trial"], b["groups"], b) for b in bs],
                           f"[守恒 language={lang}]")
        lang_docs[lang] = {
            "schema": SCHEMA_LANG, "generated_utc": now_utc(), "lang": lang,
            "n_benchmarks": len(bs), "trials": [b["trial"] for b in bs],
            **tot, "groups": groups,
        }

    all_recs = [dict(c, _trial=b["trial"]) for b in benches for c in b["commands"]]
    all_tot = totals(all_recs)
    all_grp = all_groups(all_recs, bench_of)
    check_partition(all_grp, all_tot["n_commands"], all_tot["total_wall_s"], "[all]")
    check_conservation(all_grp, all_tot, [(l, d["groups"], d) for l, d in lang_docs.items()],
                       "[守恒 all=Σlanguage]")
    all_doc = {
        "schema": SCHEMA_ALL, "generated_utc": now_utc(),
        "n_benchmarks": len(benches), "n_languages": len(lang_docs),
        "languages": {l: {"n_benchmarks": d["n_benchmarks"], "n_commands": d["n_commands"],
                          "total_wall_s": d["total_wall_s"], "total_cpu_s": d["total_cpu_s"]}
                      for l, d in lang_docs.items()},
        **all_tot, "groups": all_grp,
        "checks": {"per_benchmark_regrouped": "ok", "language_eq_sum_benchmarks": "ok",
                   "all_eq_sum_languages": "ok"},
    }

    lang_dir = out_dir / "per_language"
    lang_dir.mkdir(parents=True, exist_ok=True)
    for old in lang_dir.glob("*.json"):
        old.unlink()
    for lang, d in lang_docs.items():
        write_json(lang_dir / f"{safe_name(lang)}.json", d)
    write_json(out_dir / "all.json", all_doc)

    write_csv(out_dir / "per_benchmark.csv",
              [("per_benchmark", b["lang"], b["trial"], b["groups"], None) for b in benches])
    write_csv(out_dir / "per_language.csv",
              [("per_language", l, "", d["groups"], d["n_benchmarks"]) for l, d in lang_docs.items()])
    write_csv(out_dir / "all.csv", [("all", "", "", all_grp, all_doc["n_benchmarks"])])

    manifest = None
    mf = out_dir / "manifest.json"
    if mf.is_file():
        try:
            manifest = json.loads(mf.read_text(encoding="utf-8"))
        except ValueError:
            manifest = None
    (out_dir / "SUMMARY.md").write_text(render_md(benches, lang_docs, all_doc, manifest),
                                        encoding="utf-8")
    return all_doc, manifest


# ---------------------------------------------------------------- CSV / Markdown

CSV_COLS = ["level", "lang", "trial", "dim", "key", "cat", "detail", "count", "count_pct",
            "wall_s", "wall_pct", "mean_s", "median_s", "p90_s", "max_s", "n_timed_out",
            "n_rc_nonzero", "cpu_s", "n_cpu_null", "n_benchmarks", "n_benchmarks_with_key"]


def fmt_num(x, nd):
    return "" if x is None else f"{x:.{nd}f}"


def write_csv(path, scopes):
    """长表：一行 = 一个层级范围里、一个维度的一个 key。utf-8-sig 让 Excel 直接认中文。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for level, lang, trial, groups, n_bench in scopes:
            for dim, _ in DIMS:
                for r in groups[dim]:
                    w.writerow([
                        level, lang, trial, dim, r["key"], r.get("cat", r["key"] if dim == "cat" else ""),
                        r.get("detail", ""), r["count"], fmt_num(r["count_pct"], 3),
                        fmt_num(r["wall_s"], 4), fmt_num(r["wall_pct"], 3), fmt_num(r["mean_s"], 4),
                        fmt_num(r["median_s"], 4), fmt_num(r["p90_s"], 4), fmt_num(r["max_s"], 4),
                        r["n_timed_out"], r["n_rc_nonzero"], fmt_num(r["cpu_s"], 6), r["n_cpu_null"],
                        "" if n_bench is None else n_bench,
                        r.get("n_benchmarks_with_key", ""),
                    ])


def md_cell(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


def md_table(hdr, rows):
    out = ["| " + " | ".join(hdr) + " |", "|" + "|".join("---" for _ in hdr) + "|"]
    out += ["| " + " | ".join(md_cell(c) for c in r) + " |" for r in rows]
    return out


def f1(x):
    return "—" if x is None else f"{x:.1f}"


def f2(x):
    return "—" if x is None else f"{x:.2f}"


def group_table(rows, with_bench=False, limit=None):
    hdr = ["key", "条数", "条数%", "wall_s", "耗时%", "mean", "median", "p90", "max",
           "超时", "rc≠0", "cpu_s"]
    if with_bench:
        hdr.append("出现于 benchmark 数")
    body = []
    for r in rows[:limit]:
        row = [f"`{r['key']}`", r["count"], f1(r["count_pct"]), f1(r["wall_s"]), f1(r["wall_pct"]),
               f2(r["mean_s"]), f2(r["median_s"]), f2(r["p90_s"]), f2(r["max_s"]),
               r["n_timed_out"], r["n_rc_nonzero"], f1(r["cpu_s"])]
        if with_bench:
            row.append(r["n_benchmarks_with_key"])
        body.append(row)
    lines = md_table(hdr, body)
    if limit is not None and len(rows) > limit:
        lines.append(f"\n（只列耗时前 {limit} 个，共 {len(rows)} 个；完整表见 CSV）")
    return lines


def render_md(benches, lang_docs, all_doc, manifest):
    md = ["# replay 命令类型统计", ""]
    if manifest:
        s = manifest.get("summary") or {}
        md += [f"- 批次目录：`{manifest.get('run_dir')}`（summary.json 生成于 {s.get('generated_utc')}）",
               f"- 批次选项：`{json.dumps(manifest.get('options'), ensure_ascii=False)}`",
               f"- 分类器：`{manifest['classifier']['path']}`（sha256 {manifest['classifier']['sha256'][:12]}）"]
    md += [f"- 统计生成：{all_doc['generated_utc']}",
           f"- 纳入判据：{CRITERION}",
           f"- 纳入 **{all_doc['n_benchmarks']}** 条 benchmark / {all_doc['n_languages']} 种语言 / "
           f"{all_doc['n_commands']} 条命令 / wall 合计 {all_doc['total_wall_s']:.1f}s"
           + (f"；排除 **{manifest['n_excluded']}** 条" if manifest else ""),
           "- 自检：per_benchmark 逐条重算一致；language = Σbenchmark、all = Σlanguage（count 与 wall_s，"
           "总数与逐 key）全部守恒 ✅",
           ""]
    if manifest and not manifest.get("n_pass_consistent", True):
        md += [f"> ⚠️ summary.json 的 n_pass={manifest['summary'].get('n_pass')} 与按同一判据重算的 "
               f"{manifest.get('n_pass_recomputed')} 对不上（多半是旧版 run_batch 写的 summary.json）。", ""]

    md += ["## 口径", "",
           "- **一条命令一个标签**：主类别 / 细类 / program 都取「决定主类别的那条语句」"
           "（分类器 `summarize_replay.classify_command_full`，优先级 跑测试 > 写文件 > 语法校验 > 搜索 > 版本控制 > 读文件 > 其他）。",
           "- **program**：basename；python* → python、pip* → pip；git/go/cargo/npm/pnpm/yarn/bun/deno/pip/uv/poetry/rustup "
           "带子命令（`go test`）；npx/bunx/uvx 带被启动的程序（`npx vitest`）；run/exec/dlx/`go tool` 再带脚本名或程序名"
           "（`npm run build`，像路径的不带）；`python -m X` 带模块名。",
           "- **占比**的分母是同一层级范围（这条 benchmark / 这门语言 / 全体）的总条数、总 wall_s。",
           "- **mean / median / p90 / max** 是单条命令 wall_s，分位数（线性插值）在每个层级都用逐条记录重新算，不是下层统计值的平均。",
           "- **cpu_s** = Σusage_usec/1e6；组内有任何一条没采（`--no-metrics`）就记 —（null），不记 0。",
           "- **超时** = replay 侧 timed_out（rc 124/137）；**rc≠0** 含超时。", ""]

    # ---- 纳入 / 排除
    md += ["## 纳入的 benchmark", ""]
    rows = []
    for b in sorted(benches, key=lambda b: (lang_sort_key(b["lang"]), b["trial"])):
        top = b["groups"]["program"][0]["key"] if b["groups"]["program"] else "—"
        rows.append([b["lang"], f"`{b['trial']}`", b["n_commands"], f1(b["total_wall_s"]),
                     f1(b["total_cpu_s"]), f"`{top}`"])
    md += md_table(["语言", "trial", "命令数", "wall_s", "cpu_s", "耗时最多的 program"], rows) if rows \
        else ["（没有一条满足纳入判据）"]
    md += ["", "## 排除的 trial", ""]
    if manifest is None:
        md += ["（没有 manifest.json —— 只跑了 aggregate，排除清单不可知）"]
    elif not manifest["excluded"]:
        md += ["（无）"]
    else:
        md += md_table(["语言", "trial", "退出码", "原因"],
                       [[e.get("lang"), f"`{e.get('trial')}`", e.get("exit_code"), "；".join(e["reasons"])]
                        for e in manifest["excluded"]])
    md.append("")

    if not benches:
        return "\n".join(md)

    # ---- 全体
    md += ["## 全体", "", "### 按主类别", ""] + group_table(all_doc["groups"]["cat"], True)
    md += ["", "### 按主类别 › 细类（耗时前 25）", ""] + group_table(all_doc["groups"]["cat_detail"], True, 25)
    md += ["", "### 按 program（耗时前 25）", ""] + group_table(all_doc["groups"]["program"], True, 25)

    # ---- 语言 × 主类别 矩阵
    cats = [r["key"] for r in all_doc["groups"]["cat"]]
    md += ["", "## 按语言", "", "### 语言 × 主类别：耗时占比%（条数占比%）", ""]
    mrows = []
    for lang, d in lang_docs.items():
        g = rows_by_key(d["groups"]["cat"])
        mrows.append([lang, d["n_benchmarks"], d["n_commands"], f1(d["total_wall_s"])] +
                     [(f"{f1(g[c]['wall_pct'])} ({f1(g[c]['count_pct'])})" if c in g else "—") for c in cats])
    md += md_table(["语言", "benchmark 数", "命令数", "wall_s"] + cats, mrows)
    for lang, d in lang_docs.items():
        md += ["", f"### {lang}（{d['n_benchmarks']} 条 benchmark，{d['n_commands']} 条命令）", "",
               "按主类别：", ""] + group_table(d["groups"]["cat"], True)
        md += ["", "按 program（耗时前 10）：", ""] + group_table(d["groups"]["program"], True, 10)

    # ---- per benchmark 概览
    md += ["", "## 按 benchmark：各主类别耗时占比%", ""]
    brows = []
    for b in sorted(benches, key=lambda b: (lang_sort_key(b["lang"]), b["trial"])):
        g = rows_by_key(b["groups"]["cat"])
        brows.append([b["lang"], f"`{b['trial']}`", b["n_commands"], f1(b["total_wall_s"])] +
                     [f1(g[c]["wall_pct"]) if c in g else "—" for c in cats])
    md += md_table(["语言", "trial", "命令数", "wall_s"] + cats, brows)
    md += ["", "逐 benchmark 的完整分组表见 `per_benchmark/<trial>.json` 与 `per_benchmark.csv`。", ""]
    return "\n".join(md)


# ---------------------------------------------------------------- CLI


def default_out(run_dir):
    return pathlib.Path(run_dir) / "cmd_stats"


def run_collect(run_dir, out):
    m = collect(run_dir, out)
    print(f"collect   纳入 {m['n_included']} / 排除 {m['n_excluded']} 条 → {pathlib.Path(out) / 'per_benchmark'}")
    if not m["n_pass_consistent"]:
        print(f"          ⚠️ summary.json n_pass={m['summary'].get('n_pass')} ≠ 按同一判据重算的 "
              f"{m['n_pass_recomputed']}")
    for e in m["excluded"]:
        print(f"          排除 {e.get('trial')}：{'；'.join(e['reasons'])}")
    return m


def run_aggregate(out):
    a, _ = aggregate(out)
    print(f"aggregate {a['n_benchmarks']} 条 benchmark / {a['n_languages']} 种语言 / "
          f"{a['n_commands']} 条命令，守恒自检通过 → {pathlib.Path(out) / 'SUMMARY.md'}")
    return a


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "collect":
            ap = argparse.ArgumentParser(prog="cmd_stats.py collect",
                                         description="读一轮 run 目录，写 per_benchmark/*.json 与 manifest.json")
            ap.add_argument("run_dir")
            ap.add_argument("-o", "--out", default="", help="输出目录，缺省 <run_dir>/cmd_stats")
            a = ap.parse_args(argv[1:])
            run_collect(a.run_dir, a.out or default_out(a.run_dir))
        elif argv and argv[0] == "aggregate":
            ap = argparse.ArgumentParser(prog="cmd_stats.py aggregate",
                                         description="只读 <out_dir>/per_benchmark/*.json，汇聚出 per_language / all / CSV / SUMMARY.md")
            ap.add_argument("out_dir")
            a = ap.parse_args(argv[1:])
            run_aggregate(a.out_dir)
        else:
            ap = argparse.ArgumentParser(prog="cmd_stats.py", description=__doc__,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
            ap.add_argument("run_dir", nargs="?", default="",
                            help="一轮 run 目录；缺省取 runs/ 下 summary.json 最新的那轮")
            ap.add_argument("-o", "--out", default="", help="输出目录，缺省 <run_dir>/cmd_stats")
            a = ap.parse_args(argv)
            run_dir = pathlib.Path(a.run_dir) if a.run_dir else latest_run(HERE / "runs")
            if not a.run_dir:
                print(f"run_dir   {run_dir}（runs/ 下最新一轮）")
            out = pathlib.Path(a.out) if a.out else default_out(run_dir)
            run_collect(run_dir, out)
            run_aggregate(out)
    except InvariantError as e:
        print(f"❌ 自检不过：{e}", file=sys.stderr)
        return 1
    except InputError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
