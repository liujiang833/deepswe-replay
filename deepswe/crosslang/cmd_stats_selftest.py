#!/usr/bin/env python3
"""cmd_stats.py 的端到端自检：用仓库里现有的 commands.jsonl 合成假 run 目录，跑一遍，再独立复算核对。

不跑重放、不碰 docker。依赖本机有这些基线 jsonl（被 .gitignore 排除，不在就无法自检）：
    crosslang/<5 条 trial>/replay/commands.jsonl + verdict.json（+ actionlint 的 replay_v1_bashlc）
    deepswe/replay_out/gql-incremental-graphql-delivery__nnFNKRL/commands.jsonl + verdict.json
    crosslang/runs/full-arm-test/returns-validated-error-accumula__8JQj5gw/（--no-metrics 的真实一轮）

合成两个 run：
    fake_run/        5 种语言 7 条该纳入（其中 1 条 --no-metrics，测 cpu_s 的 null 语义），
                     另有 失败 / --limit 截断 / 条数对不上 / 缺 jsonl / 坏 JSON 各 1 条该排除；
                     1 个不在 summary.json 里的游离目录（应被无视）；
                     cmd_stats/per_benchmark/ 下 1 份上次残留的 json（应被清掉）。
    fake_smoke_run/  options.smoke=5，2 条 exit 0（run_batch 口径算通过）但被截断 —— 应全部排除。

检查项：
    1. 纳入 / 排除集合与原因关键词；残留文件被清；游离目录被无视
    2. 独立复算（不 import cmd_stats）：各层级逐 key 的 count / wall_s / median / p90 / max /
       超时 / rc≠0 / cpu_s（null 语义）/ n_benchmarks_with_key，分位数用 statistics 模块从合并后的逐条记录算
    3. CSV 三份的行数与 JSON 分组行数一致、抽查数值一致
    4. bundle 布局（cmd_stats.py 与 summarize_replay.py 平铺同级）下也能跑，且用的是同级那份分类器；
       不带参数时取 runs/ 下 summary.json 最新的一轮
    5. 篡改 per_benchmark 文件 → aggregate 退出码 1；check_conservation 对不守恒的输入报错
    6. run_batch.run_cmd_stats：成功打一行、失败打一行，且都不抛异常

用法：python3 cmd_stats_selftest.py [--workdir DIR]     # 缺省建临时目录，跑完保留并打印路径
"""
import argparse
import csv
import importlib.util
import json
import math
import os
import pathlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
DS = HERE.parent
FAILS = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def lines(p):
    return [l for l in pathlib.Path(p).read_text().splitlines() if l.strip()]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 合成


def put(run, trial, jl, verdict, lang, exit_code=0, write_jsonl=True):
    d = run / trial
    d.mkdir(parents=True, exist_ok=True)
    if write_jsonl:
        (d / "commands.jsonl").write_text("\n".join(jl))
    return {"lang": lang, "trial": trial, "task_id": trial.split("__")[0], "model": "fake",
            "exit_code": exit_code, "wall_s": 1.0, "verdict": verdict,
            "log": f"logs/{trial}.log", "vs_baseline": None}


def base(trial, sub="replay"):
    p = HERE / trial / sub
    return lines(p / "commands.jsonl"), json.loads((p / "verdict.json").read_text())


def write_summary(run, results, smoke=0):
    n_pass = sum(1 for r in results if r["exit_code"] == 0
                 and (smoke or (r["verdict"] or {}).get("patch_identical") is True))
    (run / "summary.json").write_text(json.dumps({
        "generated_utc": "2026-09-14T00:00:00+00:00", "host": {},
        "options": {"smoke": smoke, "cmd_timeout": 30, "jobs": 1, "metrics": False},
        "elapsed_s": 0.0, "n_pass": n_pass, "n_total": len(results), "n_planned": len(results),
        "n_ran": len(results), "n_not_run": 0, "interrupted": False, "not_run_reason": None,
        "results": results}, ensure_ascii=False, indent=1))


GOOD = [("returns-validated-error-accumula__8JQj5gw", "python"),
        ("actionlint-action-pinning-lint__23b2uyq", "go"),
        ("fd-deterministic-multi-key-sorti__fK6jc93", "rust"),
        ("true-myth-iterable-collection-co__BBLS6Fy", "typescript"),
        ("yjs-map-conflict-detection__gSSidka", "javascript")]
GQL = DS / "replay_out/gql-incremental-graphql-delivery__nnFNKRL"
ARM = HERE / "runs/full-arm-test/returns-validated-error-accumula__8JQj5gw"
EXPECT_EXCLUDED = {
    "actionlint-action-pinning-lint__failed": ["退出码 1", "patch_identical=False"],
    "true-myth-iterable-collection-co__truncated": ["截断", "--limit"],
    "yjs-map-conflict-detection__mismatch": ["60 条 ≠ verdict.n_replayed 65"],
    "fd-deterministic-multi-key-sorti__nojsonl": ["commands.jsonl 不存在"],
    "actionlint-action-pinning-lint__badjson": ["不是合法 JSON"],
}


def build_fake_runs(wd):
    run = wd / "fake_run"
    shutil.rmtree(run, ignore_errors=True)
    R = []
    for trial, lang in GOOD:
        j, v = base(trial)
        R.append(put(run, trial, j, v, lang))
    R.append(put(run, GQL.name, lines(GQL / "commands.jsonl"),
                 json.loads((GQL / "verdict.json").read_text()), "python"))
    R.append(put(run, "returns-validated-error-accumula__nometrics", lines(ARM / "commands.jsonl"),
                 json.loads((ARM / "verdict.json").read_text()), "python"))

    j, v = base("actionlint-action-pinning-lint__23b2uyq", "replay_v1_bashlc")
    R.append(put(run, "actionlint-action-pinning-lint__failed", j, dict(v, patch_identical=False),
                 "go", exit_code=1))
    j, v = base("true-myth-iterable-collection-co__BBLS6Fy")   # --limit 5：没有 patch 校验
    v = {k: x for k, x in v.items() if k not in ("patch_identical", "model_patch_bytes")}
    v.update(n_replayed=5, n_skipped_sentinel=0)
    R.append(put(run, "true-myth-iterable-collection-co__truncated", j[:5], v, "typescript"))
    j, v = base("yjs-map-conflict-detection__gSSidka")
    R.append(put(run, "yjs-map-conflict-detection__mismatch", j[:60], v, "javascript"))
    j, v = base("fd-deterministic-multi-key-sorti__fK6jc93")
    R.append(put(run, "fd-deterministic-multi-key-sorti__nojsonl", j, v, "rust", write_jsonl=False))
    j, v = base("actionlint-action-pinning-lint__23b2uyq")
    R.append(put(run, "actionlint-action-pinning-lint__badjson", j[:-1] + ["{not json"], v, "go"))
    j, v = base("returns-validated-error-accumula__8JQj5gw")
    put(run, "stray-dir-not-in-summary__x", j, v, "python")
    write_summary(run, R)
    (run / "cmd_stats/per_benchmark").mkdir(parents=True)
    (run / "cmd_stats/per_benchmark/stale-trial__old.json").write_text("{}")

    sm = wd / "fake_smoke_run"
    shutil.rmtree(sm, ignore_errors=True)
    R2 = []
    for trial, lang in GOOD[1:3]:
        j, v = base(trial)
        v = {k: x for k, x in v.items() if k not in ("patch_identical", "model_patch_bytes")}
        v.update(n_replayed=4, n_skipped_sentinel=1)
        R2.append(put(sm, trial, j[:4], v, lang))
    write_summary(sm, R2, smoke=5)
    return run, sm


# ---------------------------------------------------------------- 独立复算


def q90(xs):
    return xs[0] if len(xs) == 1 else statistics.quantiles(xs, n=10, method="inclusive")[8]


def expected_groups(benches, sr):
    """benches: [(trial, lang, jsonl 路径)] -> {scope: {dim: {key: 指标}}}，scope = trial / lang / __all__"""
    recs = []
    for trial, lang, path in benches:
        for l in lines(path):
            x = json.loads(l)
            c = sr.classify_command_full(x["cmd_stripped"])
            recs.append({"trial": trial, "lang": lang, "wall": x["wall_s"], "rc": x["rc"],
                         "to": bool(x["timed_out"]), "u": x.get("usage_usec"),
                         "cat": c["cat"], "cat_detail": f"{c['cat']} › {c['detail']}",
                         "program": c["program"] if c["program"] is not None else "(空命令)"})
    scopes = {}
    for r in recs:
        for s in (r["trial"], "lang:" + r["lang"], "__all__"):
            scopes.setdefault(s, []).append(r)
    out = {}
    for s, rs in scopes.items():
        out[s] = {}
        for dim in ("cat", "cat_detail", "program"):
            g = {}
            for r in rs:
                g.setdefault(r[dim], []).append(r)
            out[s][dim] = {k: {"count": len(v), "wall_s": sum(x["wall"] for x in v),
                               "median_s": statistics.median([x["wall"] for x in v]),
                               "p90_s": q90(sorted(x["wall"] for x in v)),
                               "max_s": max(x["wall"] for x in v),
                               "n_timed_out": sum(x["to"] for x in v),
                               "n_rc_nonzero": sum(x["rc"] != 0 for x in v),
                               "cpu_s": (None if any(x["u"] is None for x in v)
                                         else sum(x["u"] for x in v) / 1e6),
                               "n_benchmarks_with_key": len({x["trial"] for x in v})}
                           for k, v in g.items()}
    return out


def close(a, b, tol=1e-6):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol * max(1.0, abs(b))


def compare_groups(label, got_groups, want, with_bench):
    bad = []
    for dim, rows in got_groups.items():
        got = {r["key"]: r for r in rows}
        if set(got) != set(want[dim]):
            bad.append(f"{dim} 键集合不同")
            continue
        for k, w in want[dim].items():
            g = got[k]
            for f in ("count", "n_timed_out", "n_rc_nonzero"):
                if g[f] != w[f]:
                    bad.append(f"{dim}/{k}/{f} {g[f]}≠{w[f]}")
            for f in ("wall_s", "median_s", "p90_s", "max_s", "cpu_s"):
                if not close(g[f], w[f]):
                    bad.append(f"{dim}/{k}/{f} {g[f]}≠{w[f]}")
            if with_bench and g.get("n_benchmarks_with_key") != w["n_benchmarks_with_key"]:
                bad.append(f"{dim}/{k}/n_benchmarks_with_key {g.get('n_benchmarks_with_key')}≠"
                           f"{w['n_benchmarks_with_key']}")
    check(label, not bad, "；".join(bad[:5]))


# ---------------------------------------------------------------- main


def run_stats(script, *args):
    return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="")
    a = ap.parse_args()
    wd = pathlib.Path(a.workdir) if a.workdir else pathlib.Path(tempfile.mkdtemp(prefix="cmd_stats_selftest_"))
    wd.mkdir(parents=True, exist_ok=True)
    wd = wd.resolve()
    script = HERE / "cmd_stats.py"
    sr = load("summarize_replay_for_selftest", DS / "summarize_replay.py")
    print(f"workdir {wd}")

    run, sm = build_fake_runs(wd)

    print("[1] fake_run：纳入 / 排除")
    p = run_stats(script, run)
    check("cmd_stats.py <run_dir> 退出码 0", p.returncode == 0, p.stderr[-400:])
    out = run / "cmd_stats"
    man = json.loads((out / "manifest.json").read_text())
    inc = {x["trial"] for x in man["included"]}
    want_inc = {t for t, _ in GOOD} | {GQL.name, "returns-validated-error-accumula__nometrics"}
    check("纳入集合 = 7 条好的", inc == want_inc, f"{sorted(inc ^ want_inc)}")
    exc = {x["trial"]: "；".join(x["reasons"]) for x in man["excluded"]}
    check("排除集合 = 5 条坏的", set(exc) == set(EXPECT_EXCLUDED), f"{sorted(set(exc) ^ set(EXPECT_EXCLUDED))}")
    for t, kws in EXPECT_EXCLUDED.items():
        check(f"排除原因 {t}", all(k in exc.get(t, "") for k in kws), exc.get(t))
    check("n_pass 与 summary.json 一致", man["n_pass_consistent"])
    pb = sorted(x.stem for x in (out / "per_benchmark").glob("*.json"))
    check("残留 stale json 被清、游离目录被无视", pb == sorted(want_inc), f"{pb}")

    print("[2] 独立复算")
    benches = [(t, l, run / t / "commands.jsonl") for t, l in GOOD] + \
              [(GQL.name, "python", run / GQL.name / "commands.jsonl"),
               ("returns-validated-error-accumula__nometrics", "python",
                run / "returns-validated-error-accumula__nometrics/commands.jsonl")]
    want = expected_groups(benches, sr)
    for t, l, _ in benches:
        doc = json.loads((out / "per_benchmark" / f"{t}.json").read_text())
        compare_groups(f"per_benchmark {t}", doc["groups"], want[t], False)
        check(f"per_benchmark {t} 逐条记录条数 = jsonl 行数",
              len(doc["commands"]) == len(lines(run / t / "commands.jsonl")))
    for l in sorted({l for _, l, _ in benches}):
        doc = json.loads((out / "per_language" / f"{l}.json").read_text())
        compare_groups(f"per_language {l}", doc["groups"], want["lang:" + l], True)
        check(f"per_language {l} n_benchmarks", doc["n_benchmarks"] == sum(1 for _, x, _ in benches if x == l))
    alld = json.loads((out / "all.json").read_text())
    compare_groups("all", alld["groups"], want["__all__"], True)
    check("all n_benchmarks = 7", alld["n_benchmarks"] == 7)
    py_cat = {r["key"]: r for r in json.loads((out / "per_language/python.json").read_text())["groups"]["cat"]}
    go_cat = {r["key"]: r for r in json.loads((out / "per_language/go.json").read_text())["groups"]["cat"]}
    check("cpu_s null 语义：含 --no-metrics 的 python 层记 null，go 层有数",
          all(r["cpu_s"] is None for r in py_cat.values()) and all(r["cpu_s"] is not None for r in go_cat.values()))
    # 不是「平均的平均」：python 层 median 与三条 benchmark median 的均值应当不同（至少一个 key）
    diff = False
    for k, r in py_cat.items():
        meds = []
        for t in (GQL.name, "returns-validated-error-accumula__8JQj5gw", "returns-validated-error-accumula__nometrics"):
            d = {x["key"]: x for x in json.loads((out / "per_benchmark" / f"{t}.json").read_text())["groups"]["cat"]}
            if k in d:
                meds.append(d[k]["median_s"])
        if len(meds) > 1 and not close(r["median_s"], sum(meds) / len(meds)):
            diff = True
    check("language 层 median 不是各 benchmark median 的平均", diff)

    print("[3] CSV")
    for name, docs in (("per_benchmark.csv", [json.loads(x.read_text()) for x in (out / "per_benchmark").glob("*.json")]),
                       ("per_language.csv", [json.loads(x.read_text()) for x in (out / "per_language").glob("*.json")]),
                       ("all.csv", [alld])):
        with open(out / name, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        n = sum(len(d["groups"][dim]) for d in docs for dim in ("cat", "cat_detail", "program"))
        check(f"{name} 行数 = JSON 分组行数 ({n})", len(rows) == n, f"{len(rows)}")
        top = alld["groups"]["cat"][0]
        if name == "all.csv":
            r0 = [r for r in rows if r["dim"] == "cat" and r["key"] == top["key"]][0]
            check("all.csv 抽查 count / wall_s", int(r0["count"]) == top["count"]
                  and abs(float(r0["wall_s"]) - top["wall_s"]) < 1e-3)

    print("[4] smoke 批次 / bundle 布局 / 缺省取最新一轮")
    p = run_stats(script, sm, "-o", wd / "fake_smoke_stats")
    m2 = json.loads((wd / "fake_smoke_stats/manifest.json").read_text())
    check("smoke 批次：退出码 0、纳入 0、排除 2 且原因含 --smoke",
          p.returncode == 0 and m2["n_included"] == 0 and m2["n_excluded"] == 2
          and all("--smoke 5" in "；".join(e["reasons"]) for e in m2["excluded"]), p.stderr[-300:])
    bd = wd / "bundle_layout"
    shutil.rmtree(bd, ignore_errors=True)
    (bd / "runs").mkdir(parents=True)
    shutil.copy2(script, bd / "cmd_stats.py")
    shutil.copy2(DS / "summarize_replay.py", bd / "summarize_replay.py")
    shutil.copytree(sm, bd / "runs/older", ignore=shutil.ignore_patterns("cmd_stats"))
    shutil.copytree(run, bd / "runs/newer", ignore=shutil.ignore_patterns("cmd_stats"))
    old_t = time.time() - 3600
    os.utime(bd / "runs/older/summary.json", (old_t, old_t))
    # 名字上 older > newer（字典序），但 mtime 上 newer 更新 —— 必须按 mtime 挑
    (bd / "runs/older").rename(bd / "runs/zzz-older")
    p = subprocess.run([sys.executable, "cmd_stats.py"], cwd=bd, capture_output=True, text=True)
    check("bundle 布局无参运行 退出码 0", p.returncode == 0, p.stderr[-300:])
    mb = json.loads((bd / "runs/newer/cmd_stats/manifest.json").read_text()) \
        if (bd / "runs/newer/cmd_stats/manifest.json").exists() else {}
    check("缺省取 summary.json mtime 最新的一轮（newer，而不是名字更大的 zzz-older）",
          mb.get("n_included") == 7 and not (bd / "runs/zzz-older/cmd_stats").exists())
    check("bundle 布局用的是同级 summarize_replay.py",
          mb.get("classifier", {}).get("path") == str((bd / "summarize_replay.py").resolve()))
    ab = json.loads((bd / "runs/newer/cmd_stats/all.json").read_text())
    check("bundle 布局与仓库布局结果一致（all.json 分组）", ab["groups"] == alld["groups"])

    print("[5] 篡改 / 守恒")
    tam = wd / "tampered"
    shutil.rmtree(tam, ignore_errors=True)
    shutil.copytree(out, tam)
    f = tam / "per_benchmark" / f"{GOOD[1][0]}.json"
    d = json.loads(f.read_text())
    d["groups"]["program"][0]["count"] += 1
    f.write_text(json.dumps(d, ensure_ascii=False))
    p = run_stats(script, "aggregate", tam)
    check("篡改分组表 → aggregate 退出码 1", p.returncode == 1 and "自检不过" in p.stderr, p.stderr[-300:])
    d["groups"]["program"][0]["count"] -= 1
    d["commands"][0]["wall_s"] += 1.0
    f.write_text(json.dumps(d, ensure_ascii=False))
    p = run_stats(script, "aggregate", tam)
    check("篡改逐条记录 → aggregate 退出码 1", p.returncode == 1 and "自检不过" in p.stderr, p.stderr[-300:])
    cs = load("cmd_stats_for_selftest", script)
    lang = json.loads((out / "per_language/python.json").read_text())
    kids = [(t, json.loads((out / "per_benchmark" / f"{t}.json").read_text())) for t in lang["trials"]]
    kids = [(t, k["groups"], k) for t, k in kids]
    try:
        cs.check_conservation(lang["groups"], lang, kids, "selftest")
        check("check_conservation 对守恒输入不报错", True)
    except cs.InvariantError as e:
        check("check_conservation 对守恒输入不报错", False, str(e))
    broken = json.loads(json.dumps(lang))
    broken["groups"]["cat"][0]["wall_s"] += 0.5
    try:
        cs.check_conservation(broken["groups"], broken, kids, "selftest")
        check("check_conservation 对不守恒输入报错", False, "没报错")
    except cs.InvariantError:
        check("check_conservation 对不守恒输入报错", True)
    p = run_stats(script, "collect", wd / "no_such_dir")
    check("collect 不存在的目录 → 退出码 2", p.returncode == 2, p.stderr[-200:])

    print("[6] run_batch.run_cmd_stats")
    rb = load("run_batch_for_selftest", HERE / "run_batch.py")
    rbw = wd / "rb_ok"
    shutil.rmtree(rbw, ignore_errors=True)
    shutil.copytree(run, rbw, ignore=shutil.ignore_patterns("cmd_stats"))
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rb.run_cmd_stats(rbw)
    s = buf.getvalue().strip()
    check("成功时打一行、含 SUMMARY.md 与纳入/排除条数",
          len(s.splitlines()) == 1 and "SUMMARY.md" in s and "纳入 7 条 / 排除 5 条" in s, s)
    buf = io.StringIO()
    bad = wd / "rb_bad"
    bad.mkdir(exist_ok=True)
    with contextlib.redirect_stdout(buf):
        rb.run_cmd_stats(bad)                        # 没有 summary.json
    s = buf.getvalue().strip()
    check("失败时只打一行提示、不抛异常", len(s.splitlines()) == 1 and "命令统计失败" in s, s)

    print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项：' + '、'.join(FAILS)}（workdir {wd}）")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
