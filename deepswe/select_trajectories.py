#!/usr/bin/env python3
"""为每个 task 确定性地选出一条 trial 轨迹，写出 TRAJECTORY_SELECTION.json。

规则（严格、可复现、无随机性）：
  硬条件：outcome == "pass" 且 has_trajectory is True
  模型优先级（逐级回退，只有本级完全没有合格 trial 才降到下一级）：
      tier 1: claude-fable-5
      tier 2: gpt-5-6-sol
      tier 3: claude-sonnet-5
  同级内 tie-break：n_agent_steps 升序 -> cost_usd 升序 -> trial_name 字典序，取第一条。
  不允许降级到这三个模型之外。三级都没有合格 trial 的 task 记入 unresolved（不跳过、不替换）。

输入（均为本地文件）：
  data/tasks_extracted/   —— 目录名即 task_name（任务清单的权威来源）
  data/trials.json        —— {"rows": [ {...}, ... ]}

用法：
    python select_trajectories.py                 # 写 TRAJECTORY_SELECTION.json
    python select_trajectories.py --check         # 只打印摘要，不写文件
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
TASKS_DIR = HERE / "data" / "tasks_extracted"
TRIALS_JSON = HERE / "data" / "trials.json"
OUT_JSON = HERE / "TRAJECTORY_SELECTION.json"

# 模型优先级：索引 0 -> tier 1
MODEL_TIERS = ["claude-fable-5", "gpt-5-6-sol", "claude-sonnet-5"]

RULE = {
    "hard_filters": {"outcome": "pass", "has_trajectory": True},
    "model_priority": [
        {"tier": i + 1, "model": m} for i, m in enumerate(MODEL_TIERS)
    ],
    "fallback": (
        "strictly tiered: only descend to the next tier when the current tier has "
        "zero qualifying trials; never fall back outside model_priority"
    ),
    "tie_break": ["n_agent_steps asc", "cost_usd asc", "trial_name lexicographic asc"],
    "unresolved_policy": (
        "a task with no qualifying trial in any tier is recorded in `unresolved`; "
        "it is never skipped silently nor substituted with another model"
    ),
    "selections_order": "task_name lexicographic asc",
}

# tie-break 用的排序键；缺失值一律排到最后，保证确定性而不抛异常
_MISSING_NUM = float("inf")


def sort_key(row: dict) -> tuple:
    steps = row.get("n_agent_steps")
    cost = row.get("cost_usd")
    return (
        _MISSING_NUM if steps is None else steps,
        _MISSING_NUM if cost is None else cost,
        row["trial_name"],
    )


def load_tasks() -> list[str]:
    if not TASKS_DIR.is_dir():
        raise SystemExit(f"missing task dir: {TASKS_DIR}")
    return sorted(p.name for p in TASKS_DIR.iterdir() if p.is_dir())


def load_rows() -> list[dict]:
    if not TRIALS_JSON.is_file():
        raise SystemExit(f"missing index: {TRIALS_JSON}")
    data = json.loads(TRIALS_JSON.read_text())
    return data["rows"] if isinstance(data, dict) else data


def select(tasks: list[str], rows: list[dict]) -> dict:
    by_task: dict[str, list[dict]] = {t: [] for t in tasks}
    for r in rows:
        bucket = by_task.get(r.get("task_name"))
        if bucket is not None:
            bucket.append(r)

    selections: list[dict] = []
    unresolved: list[str] = []
    tier_counts = {str(i + 1): 0 for i in range(len(MODEL_TIERS))}

    for task in tasks:
        qualified = [
            r
            for r in by_task[task]
            if r.get("outcome") == "pass" and r.get("has_trajectory") is True
        ]
        chosen = None
        for tier_idx, model in enumerate(MODEL_TIERS, start=1):
            same_model = [r for r in qualified if r.get("model") == model]
            if not same_model:
                continue
            same_model.sort(key=sort_key)
            chosen = (tier_idx, same_model[0], len(same_model))
            break

        if chosen is None:
            unresolved.append(task)
            continue

        tier, row, n_cand = chosen
        tier_counts[str(tier)] += 1
        selections.append(
            {
                "task_name": task,
                "trial_name": row["trial_name"],
                "model": row["model"],
                "tier": tier,
                "config": row.get("config"),
                "reasoning_effort": row.get("reasoning_effort"),
                "n_agent_steps": row.get("n_agent_steps"),
                "cost_usd": row.get("cost_usd"),
                "reward": row.get("reward"),
                "n_pass_candidates": n_cand,
                "verifier_files": list(row.get("verifier_files") or []),
            }
        )

    selections.sort(key=lambda s: s["task_name"])
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "rule": RULE,
        "n_tasks": len(tasks),
        "tier_counts": tier_counts,
        "unresolved": unresolved,
        "selections": selections,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只打印摘要，不写文件")
    ap.add_argument("-o", "--out", default=str(OUT_JSON))
    args = ap.parse_args()

    tasks = load_tasks()
    rows = load_rows()
    manifest = select(tasks, rows)

    print(f"tasks           : {manifest['n_tasks']}")
    print(f"selections      : {len(manifest['selections'])}")
    for tier, model in enumerate(MODEL_TIERS, start=1):
        print(f"  tier {tier} {model:<16}: {manifest['tier_counts'][str(tier)]}")
    print(f"unresolved      : {len(manifest['unresolved'])}")
    for t in manifest["unresolved"]:
        print(f"  - {t}")
    n_files = sum(2 + len(s["verifier_files"]) for s in manifest["selections"])
    print(f"files to fetch  : {n_files}")

    if args.check:
        return 0

    out = pathlib.Path(args.out)
    out.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
