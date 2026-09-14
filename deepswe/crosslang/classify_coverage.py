#!/usr/bin/env python3
"""分类器覆盖率检查：拿 full_trials/ 下全部 trace 的命令去跑 summarize_replay 的分类器，
按语言统计主类别落进「其他」的比例、其中的 top 细类 / program，便于对比分类器改动前后。

命令来源与重放完全一致：replay.py 的 load_trace 摊平 trace、跳过哨兵，再用 replay.py 的
strip_cd 剥掉 `cd /app && ` 前缀（= commands.jsonl 里的 cmd_stripped）。不需要真跑重放。

用法：
    # 用当前分类器
    python3 classify_coverage.py run --label after -o classify_coverage/after.json
    # 用任意一个版本的分类器文件（比如 git show <rev>:deepswe/summarize_replay.py 导出来的）
    python3 classify_coverage.py run --classifier /tmp/old.py --label before -o classify_coverage/before.json
    # 两份结果对比成 Markdown
    python3 classify_coverage.py compare classify_coverage/before.json classify_coverage/after.json \\
        -o classify_coverage/COVERAGE.md

「其他」里的 top 细类：细类 `未识别:X` 就是程序 X 没进任何表；`环境查询` / `接口探查` / `子 shell`
这类是认识但归「其他」的。program 列只有带 classify_command_full 的新版分类器才有。
"""

import argparse
import collections
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]
TOP_N = 15


def load_module(name, candidates):
    for p in candidates:
        p = pathlib.Path(p)
        if p.exists():
            spec = importlib.util.spec_from_file_location(name, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, p
    raise SystemExit(f"找不到 {name}（找过 {', '.join(map(str, candidates))}）")


def lang_key(l):
    return (LANG_ORDER.index(l) if l in LANG_ORDER else 99, l)


def run(args):
    sr, sr_path = load_module("summarize_replay", [args.classifier] if args.classifier else
                              [HERE / "summarize_replay.py", HERE.parent / "summarize_replay.py"])
    rp, rp_path = load_module("replay", [HERE / "replay.py", HERE.parent / "replay.py"])
    root = pathlib.Path(args.trials_dir) if args.trials_dir else HERE / "full_trials"
    has_full = hasattr(sr, "classify_command_full")

    per_cmd = []
    trials = collections.Counter()
    for d in sorted(root.iterdir()):
        if not (d / "meta.json").is_file() or not (d / "trajectory.json").is_file():
            continue
        lang = json.loads((d / "meta.json").read_text()).get("language", "?")
        trials[lang] += 1
        items = rp.load_trace(json.loads((d / "trajectory.json").read_text()))
        for i, it in enumerate(items):
            if it["sentinel"]:
                continue
            cmd = rp.strip_cd(it["cmd"])
            if has_full:
                c = sr.classify_command_full(cmd)
                cat, detail, prog, pairs = c["cat"], c["detail"], c["program"], c["pairs"]
            else:
                cat, _, pairs, detail = sr.classify_command(cmd)
                prog = None
            unrec_stmt = any(str(d_).startswith("未识别:") for _, d_ in pairs)
            per_cmd.append([d.name, lang, i, cat, detail, prog, unrec_stmt])

    other = sr.OTHER
    langs = sorted(trials, key=lang_key)
    stats = {}
    for scope in langs + ["__all__"]:
        rows = [r for r in per_cmd if scope == "__all__" or r[1] == scope]
        n = len(rows)
        oth = [r for r in rows if r[3] == other]
        stats[scope] = {
            "n_trials": sum(trials.values()) if scope == "__all__" else trials[scope],
            "n_commands": n,
            "n_other": len(oth),
            "other_pct": 100.0 * len(oth) / n if n else None,
            "n_other_unrecognized": sum(1 for r in oth if str(r[4]).startswith("未识别:")),
            "n_cmds_with_unrecognized_stmt": sum(1 for r in rows if r[6]),
            "by_cat": dict(collections.Counter(r[3] for r in rows).most_common()),
            "other_top_detail": collections.Counter(r[4] for r in oth).most_common(TOP_N),
            "other_top_program": (collections.Counter(r[5] for r in oth).most_common(TOP_N)
                                  if has_full else None),
        }
    # classifier_source：给人看的来源说明。--classifier 指向临时导出的旧版本时，路径跑完就没了，
    # 报告里要写的是「它从哪来、怎么再生成」，不是一个以后不存在的 /tmp 路径。
    doc = {"label": args.label, "classifier": str(sr_path),
           "classifier_source": args.classifier_source or str(sr_path), "replay": str(rp_path),
           "trials_dir": str(root), "has_program": has_full, "stats": stats,
           "per_command_cols": ["trial", "lang", "i", "cat", "detail", "program", "has_unrecognized_stmt"],
           "per_command": per_cmd}
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=0))
    for scope in langs + ["__all__"]:
        s = stats[scope]
        print(f"{scope:<11} 命令 {s['n_commands']:>5}  其他 {s['n_other']:>4} ({s['other_pct']:.1f}%)  "
              f"其中未识别 {s['n_other_unrecognized']:>4}  含未识别语句的命令 {s['n_cmds_with_unrecognized_stmt']}")
    print(f"-> {out}")


def tick(x):
    """Markdown 里的反引号片段：旧分类器会把续行符后的换行吞进程序名（`未识别:\\nsed`），换成 ⏎。"""
    return "`" + str(x).replace("\n", "⏎") + "`"


def compare(args):
    a = json.loads(pathlib.Path(args.before).read_text())
    b = json.loads(pathlib.Path(args.after).read_text())
    langs = [l for l in sorted(a["stats"], key=lang_key) if l != "__all__"] + ["__all__"]
    md = ["# 分类器覆盖率：改动前 vs 改动后", "",
          f"- 命令来源：`{b['trials_dir']}` 下全部 trace（replay.py load_trace，跳过哨兵，strip_cd）",
          f"- before：`{a['label']}` = {a.get('classifier_source') or a['classifier']}",
          f"- after：`{b['label']}` = {b.get('classifier_source') or b['classifier']}",
          "- 「其他%」= 主类别落进「其他」的命令占比；「未识别」= 其中细类是 `未识别:X`（程序不在任何表里）的条数；",
          "  「含未识别语句」= 整条命令里至少有一条语句是 `未识别:X`（主类别可能已被别的语句决定，",
          "  比如 `sed -i … && go test` 在改动前被判成「写文件」）。", "",
          "## 总览", ""]
    md += ["| 语言 | trial | 命令 | 其他% before | 其他% after | 未识别 before | 未识别 after | 含未识别语句 before | 含未识别语句 after |",
           "|---|---|---|---|---|---|---|---|---|"]
    for l in langs:
        x, y = a["stats"][l], b["stats"][l]
        name = "全部" if l == "__all__" else l
        md.append(f"| {name} | {x['n_trials']} | {x['n_commands']} | {x['other_pct']:.1f}% ({x['n_other']}) | "
                  f"{y['other_pct']:.1f}% ({y['n_other']}) | {x['n_other_unrecognized']} | "
                  f"{y['n_other_unrecognized']} | {x['n_cmds_with_unrecognized_stmt']} | "
                  f"{y['n_cmds_with_unrecognized_stmt']} |")

    # 主类别迁移：逐条对齐（trial, i）
    key = lambda r: (r[0], r[2])
    bmap = {key(r): r for r in b["per_command"]}
    md += ["", "## 主类别迁移（before → after，只列变了的）", ""]
    for l in langs:
        mv = collections.Counter()
        for r in a["per_command"]:
            if l != "__all__" and r[1] != l:
                continue
            r2 = bmap.get(key(r))
            if r2 and r2[3] != r[3]:
                mv[(r[3], r2[3])] += 1
        name = "全部" if l == "__all__" else l
        md.append(f"- **{name}**：" + ("；".join(f"{p} → {q} {n} 条" for (p, q), n in mv.most_common())
                                       if mv else "无变化"))

    md += ["", "## 主类别分布（after）", ""]
    cats = list(b["stats"]["__all__"]["by_cat"])
    md += ["| 语言 | " + " | ".join(cats) + " |", "|---|" + "---|" * len(cats)]
    for l in langs:
        s = b["stats"][l]
        name = "全部" if l == "__all__" else l
        md.append(f"| {name} | " + " | ".join(
            f"{s['by_cat'].get(c, 0)} ({100.0 * s['by_cat'].get(c, 0) / s['n_commands']:.1f}%)" for c in cats) + " |")

    md += ["", "## 落进「其他」的 top 细类 / program", ""]
    for l in langs:
        x, y = a["stats"][l], b["stats"][l]
        name = "全部" if l == "__all__" else l
        md += [f"### {name}", "",
               "- before 细类：" + "、".join(f"{tick(d)} {n}" for d, n in x["other_top_detail"]),
               "- after 细类：" + ("、".join(f"{tick(d)} {n}" for d, n in y["other_top_detail"]) or "（无）")]
        if y.get("other_top_program") is not None:
            md.append("- after program：" + ("、".join(f"{tick(d)} {n}" for d, n in y["other_top_program"]) or "（无）"))
        md.append("")
    out = pathlib.Path(args.out)
    out.write_text("\n".join(md))
    print(f"-> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--classifier", default="", help="summarize_replay.py 路径（缺省自动定位当前版本）")
    r.add_argument("--classifier-source", default="",
                   help="报告里写的分类器来源说明（缺省就是路径）；--classifier 指向临时文件时用")
    r.add_argument("--trials-dir", default="", help="trace 目录，缺省 full_trials/")
    r.add_argument("--label", default="current")
    r.add_argument("-o", "--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    c.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    run(a) if a.cmd == "run" else compare(a)


if __name__ == "__main__":
    main()
