#!/usr/bin/env python3
"""串行重放 bundle 里的全部 trial，并与随包的基线判定逐条对比。

为什么要串行：`replay.py` 采的是 cgroup 的 CPU/内存/IO，两条同时跑会互相争抢，
性能数字直接失去可比性（`crosslang/INDEX.md`「本轮的口径污染」记的就是上次并发
导致 rust/ts/js 三条指标偏悲观、只有 python/go 两条干净）。保真度结论
（patch_identical）不受并发影响，但既然要跑就一次跑干净。

判定分两层，含义不同，不要混着看：
  - **保真度**：`patch_identical` —— 容器内 `git diff --binary <base> HEAD` 与
    下载到的 `model.patch` 逐字节相等。这是硬校验，5 条应当全为真；有一条为假
    就是环境不对，性能数字全部作废。
  - **一致性**：`rc_match` 与基线的差异 —— 允许有出入。基线里已知有 4 类不可
    消除的来源（双侧超时、上游 flaky 测试、机器快慢导致的单侧超时、dash 方言），
    见 `INDEX.md`「rc 不匹配逐条归因」。所以这里只报差异，不判失败。

用法:
    python3 run_batch.py                          # 跑全部，输出到 ./runs/<时间戳>
    python3 run_batch.py --only go,rust           # 只跑指定语言
    python3 run_batch.py --smoke 5                # 每条只跑前 5 条命令（冒烟，跳过保真度校验）
    python3 run_batch.py --dry-run                # 只做预检和排程，不真跑
"""

import collections
import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]


def sh(args):
    return subprocess.run(args, capture_output=True)


def find_replay():
    """replay.py 可能与本脚本同级（打好的 bundle），也可能在上一层（仓库原布局）。"""
    for p in (HERE / "replay.py", HERE.parent / "replay.py"):
        if p.exists():
            return p
    return None


def load_trials():
    """扫描同目录下的 trial 子目录。判据是四个必需文件齐全，不靠目录名。"""
    need = ("meta.json", "trajectory.json", "model.patch", "task.json")
    out = []
    for d in sorted(HERE.iterdir()):
        if not d.is_dir() or not all((d / f).exists() for f in need):
            continue
        meta = json.loads((d / "meta.json").read_text())
        base = d / "replay" / "verdict.json"
        out.append({
            "dir": d,
            "name": d.name,
            "lang": meta.get("language", "?"),
            "task_id": meta.get("task_id", "?"),
            "image": (meta.get("image") or {}).get("docker_image", ""),
            "n_commands": meta.get("n_commands"),
            "model": (meta.get("agent") or {}).get("model_name", "?"),
            "baseline": json.loads(base.read_text()) if base.exists() else None,
        })
    out.sort(key=lambda t: (LANG_ORDER.index(t["lang"]) if t["lang"] in LANG_ORDER else 99, t["name"]))
    return out


def preflight(trials, need_cgroup, skip_missing=False):
    """跑之前把「一定会失败」的情况先查出来，避免跑到一半才炸。

    skip_missing：全量 118 条对应 113 个镜像，不可能一次全建好。开了它之后
    缺镜像的 trial 被剔除而不是让整批拒绝启动，这样可以边建边跑。
    """
    problems = []
    if sh(["docker", "version"]).returncode != 0:
        problems.append("docker 不可用（未安装 / daemon 没起 / 当前用户无权限）")
    fs = sh(["stat", "-fc", "%T", "/sys/fs/cgroup"])
    fstype = fs.stdout.decode().strip() if fs.returncode == 0 else "未知"
    # 只有采指标时 cgroup v2 才是硬要求；只跑重放 + 保真校验的话完全不碰 cgroup
    if fstype != "cgroup2fs" and need_cgroup:
        problems.append(f"/sys/fs/cgroup 是 {fstype}，不是 cgroup2fs —— 指标口径只适用 cgroup v2"
                        f"（不加 --metrics 就不需要它）")
    missing = [t for t in trials if t["image"] and sh(["docker", "image", "inspect", t["image"]]).returncode != 0]
    if not skip_missing:
        for t in missing:
            problems.append(f"镜像不在本地: [{t['lang']}] {t['image']}")
    return problems, fstype, missing


def cmp_baseline(base, cur):
    """与基线对比。基线缺失时返回 None（冒烟模式或首次跑）。"""
    if not base or not cur:
        return None
    def rc(v):
        s = (v or {}).get("rc_match") or "0/0"
        try:
            return int(s.split("/")[0]), int(s.split("/")[1])
        except (ValueError, IndexError):
            return 0, 0
    b_ok, b_n = rc(base)
    c_ok, c_n = rc(cur)
    return {
        "baseline_patch_identical": base.get("patch_identical"),
        "baseline_rc_match": base.get("rc_match"),
        "baseline_elapsed_s": base.get("elapsed_s"),
        "rc_delta": c_ok - b_ok,
        "rc_n_same": b_n == c_n,
        "elapsed_ratio": (round(cur["elapsed_s"] / base["elapsed_s"], 2)
                          if base.get("elapsed_s") else None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--outdir", default="",
                    help="输出根目录，默认 ./runs/<UTC 时间戳>")
    ap.add_argument("--only", default="",
                    help="只跑这些语言，逗号分隔（python,go,rust,typescript,javascript）")
    ap.add_argument("--smoke", type=int, default=0,
                    help="每条只跑前 N 条命令。注意：这会跳过 patch 保真度校验（patch 不完整），"
                         "只用来确认容器能起、cgroup 能读、命令能执行")
    ap.add_argument("--cmd-timeout", type=int, default=30,
                    help="单条命令超时秒数，默认 30（对齐原 harness，改了就无法与基线对比）")
    ap.add_argument("--metrics", action="store_true",
                    help="额外采 cgroup 性能指标。默认不采——打通阶段用不上，"
                         "而且它会引入「必须 cgroup v2 且宿主侧目录可读」这条硬约束")
    ap.add_argument("--dry-run", action="store_true", help="只做预检和排程，不真跑")
    ap.add_argument("--skip-missing", action="store_true",
                    help="镜像还没建好的 trial 直接跳过而不是拒绝启动（全量集边建边跑用）")
    ap.add_argument("--keep-going", action="store_true",
                    help="某条失败后继续跑剩下的（默认遇错即停）")
    ap.add_argument("--replay", default="", help="replay.py 路径（默认自动定位）")
    args = ap.parse_args()

    replay = pathlib.Path(args.replay) if args.replay else find_replay()
    if not replay or not replay.exists():
        print(f"找不到 replay.py（找过 {HERE}/replay.py 和 {HERE.parent}/replay.py）")
        return 1

    trials = load_trials()
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        trials = [t for t in trials if t["lang"] in want]
    if not trials:
        print("没有可跑的 trial（--only 过滤掉了全部，或目录里没有合规的 trial）")
        return 1

    problems, fstype, missing = preflight(trials, args.metrics, args.skip_missing)
    if args.skip_missing and missing:
        miss_set = {id(t) for t in missing}
        trials = [t for t in trials if id(t) not in miss_set]
        print(f"--skip-missing：{len(missing)} 条因镜像未建好被跳过，实跑 {len(trials)} 条\n")
        if not trials:
            print("没有任何一条的镜像已就绪 —— 先跑 build_arm.sh")
            return 1

    print("=" * 78)
    print(f"bundle    {HERE}")
    print(f"replay.py {replay}")
    print(f"cgroup    {fstype}")
    print(f"内核      {os.uname().release}   CPU {os.cpu_count()}")
    print(f"待跑      {len(trials)} 条（串行）")
    print(f"指标      {'采集 cgroup 性能数据' if args.metrics else '不采（--metrics 可开）'}")
    print("=" * 78)
    # 全量集下逐条列出会刷几百行；只在小批量时详列
    listing = trials if len(trials) <= 12 else []
    if not listing:
        cnt = collections.Counter(t["lang"] for t in trials)
        print("  " + "  ".join(f"{k}×{v}" for k, v in sorted(cnt.items())))
        nb = sum(1 for t in trials if t["baseline"])
        print(f"  其中 {nb} 条有本地基线可对比")
    for t in listing:
        b = t["baseline"]
        bs = (f"基线 patch={'✅' if b.get('patch_identical') else '❌'} "
              f"rc={b.get('rc_match')} {b.get('elapsed_s')}s") if b else "无基线"
        mark = "  ✗镜像缺失" if t in missing else ""
        print(f"  [{t['lang']:<10}] {t['name']:<45} {t['n_commands']:>4} 条  {bs}{mark}")

    if problems:
        print("\n预检未通过：")
        # 全量集下缺镜像会有上百条，全打出来把真正的问题（docker 不可用等）冲没了
        for p in problems[:8]:
            print(f"  ✗ {p}")
        if len(problems) > 8:
            print(f"  …… 另有 {len(problems) - 8} 条同类问题")
        if missing:
            langs = ",".join(sorted({t["lang"] for t in missing}))
            print(f"\n缺 {len(missing)} 个镜像。目标环境通常拉不到 registry，用本地基座重建：")
            print(f"  bash build_arm.sh --ca-cert <内网CA.crt> {langs}")
            print(f"或先跑已建好的部分：  python3 run_batch.py --skip-missing")
        return 1
    print("\n预检通过 ✅")

    if args.dry_run:
        print("--dry-run：到此为止，未执行重放")
        return 0

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = pathlib.Path(args.outdir) if args.outdir else (HERE / "runs" / stamp)
    out.mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(exist_ok=True)
    print(f"输出      {out}\n")

    results, t_all = [], time.monotonic()
    for i, t in enumerate(trials, 1):
        print("─" * 78)
        print(f"[{i}/{len(trials)}] {t['lang']}  {t['name']}")
        print("─" * 78)
        cmd = [sys.executable, str(replay), str(t["dir"]), str(t["dir"] / "task.json"),
               "-o", str(out), "--cmd-timeout", str(args.cmd_timeout)]
        if args.smoke:
            cmd += ["--limit", str(args.smoke)]
        if not args.metrics:
            cmd += ["--no-metrics"]

        log = out / "logs" / f"{t['name']}.log"
        t0 = time.monotonic()
        # 边跑边显示，同时落盘：单条要跑几分钟，没有实时输出很难判断是卡住还是在编译
        with open(log, "w") as lf:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                sys.stdout.write(line)
                lf.write(line)
            rc = p.wait()
        dt = time.monotonic() - t0

        vf = out / t["name"] / "verdict.json"
        verdict = json.loads(vf.read_text()) if vf.exists() else None
        results.append({
            "lang": t["lang"], "trial": t["name"], "task_id": t["task_id"],
            "model": t["model"], "exit_code": rc, "wall_s": round(dt, 1),
            "verdict": verdict, "log": str(log.relative_to(out)),
            "vs_baseline": cmp_baseline(t["baseline"], verdict),
        })

        if rc != 0:
            print(f"\n❌ 退出码 {rc}，详见 {log}")
            if not args.keep_going:
                print("（--keep-going 可跳过失败项继续）")
                break
        print()

    elapsed = time.monotonic() - t_all
    write_summary(out, results, elapsed, args, fstype)
    return 0 if all(r["exit_code"] == 0 for r in results) else 1


def write_summary(out, results, elapsed, args, fstype):
    smoke = bool(args.smoke)
    rows, n_ok = [], 0
    for r in results:
        v, c = r["verdict"] or {}, r["vs_baseline"]
        pi = v.get("patch_identical")
        # 冒烟模式下 patch 天然不完整，不能当失败
        ok = (r["exit_code"] == 0) and (smoke or pi is True)
        n_ok += ok
        rows.append({
            "语言": r["lang"],
            "退出": r["exit_code"],
            "保真": ("—(冒烟)" if smoke else ("✅" if pi else "❌" if pi is False else "?")),
            "rc_match": v.get("rc_match", "—"),
            "rc_语义": v.get("rc_match_semantic", "—"),
            "耗时s": v.get("elapsed_s", r["wall_s"]),
            "vs基线": (f"rc{c['rc_delta']:+d} 时长×{c['elapsed_ratio']}"
                       if c and c.get("elapsed_ratio") else "—"),
        })

    hdr = list(rows[0].keys()) if rows else []
    w = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in hdr} if rows else {}
    lines = ["", "=" * 78,
             f"汇总  {n_ok}/{len(results)} 条通过" + ("（冒烟模式：未校验保真度）" if smoke else ""),
             "=" * 78]
    if rows:
        lines.append("  " + "  ".join(h.ljust(w[h]) for h in hdr))
        lines.append("  " + "  ".join("-" * w[h] for h in hdr))
        for r in rows:
            lines.append("  " + "  ".join(str(r[h]).ljust(w[h]) for h in hdr))
    lines += ["", f"总墙钟 {elapsed:.0f}s", f"输出   {out}"]
    if not smoke and any((r["verdict"] or {}).get("patch_identical") is False for r in results):
        lines += ["", "⚠️  有 patch_identical=false —— 环境与原始运行不一致，性能数字不可用。",
                  "    先看该条 logs/*.log 里的 rc 不匹配和时序背离。"]
    text = "\n".join(lines)
    print(text)

    (out / "summary.json").write_text(json.dumps({
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": {"kernel": os.uname().release, "nproc": os.cpu_count(), "cgroup_fstype": fstype},
        "options": {"smoke": args.smoke, "cmd_timeout": args.cmd_timeout},
        "elapsed_s": round(elapsed, 1),
        "n_pass": n_ok, "n_total": len(results),
        "results": results,
    }, indent=2, ensure_ascii=False))

    md = ["# 重放批次汇总", "",
          f"- 时间（UTC）：{datetime.datetime.now(datetime.timezone.utc).isoformat()}",
          f"- 主机：{os.uname().release} / {os.cpu_count()} CPU / cgroup {fstype}",
          f"- 单命令超时：{args.cmd_timeout}s" + ("（冒烟模式）" if smoke else ""),
          f"- 结果：**{n_ok}/{len(results)} 通过**，总墙钟 {elapsed:.0f}s", ""]
    if rows:
        md += ["| " + " | ".join(hdr) + " |",
               "|" + "|".join("---" for _ in hdr) + "|"]
        md += ["| " + " | ".join(str(r[h]) for r in [row] for h in hdr) + " |" for row in rows]
    md += ["", "## 逐条", ""]
    for r in results:
        v = r["verdict"] or {}
        md += [f"### [{r['lang']}] {r['trial']}", "",
               f"- 模型：`{r['model']}`",
               f"- 退出码：{r['exit_code']}，墙钟 {r['wall_s']}s",
               f"- patch_identical：**{v.get('patch_identical')}**"
               f"（重放 {v.get('replayed_patch_bytes')}B / 原始 {v.get('model_patch_bytes')}B）",
               f"- rc_match：{v.get('rc_match')}（语义 {v.get('rc_match_semantic')}）",
               f"- 时序背离：{len(v.get('timing_divergences') or [])} 条",
               f"- 日志：`{r['log']}`"]
        if r["vs_baseline"]:
            c = r["vs_baseline"]
            md += [f"- 对比基线：基线 rc={c['baseline_rc_match']} / {c['baseline_elapsed_s']}s，"
                   f"本次 rc 差 {c['rc_delta']:+d}，耗时 ×{c['elapsed_ratio']}"]
        md.append("")
    (out / "SUMMARY.md").write_text("\n".join(md))
    print(f"       {out}/SUMMARY.md, summary.json")


if __name__ == "__main__":
    raise SystemExit(main())
