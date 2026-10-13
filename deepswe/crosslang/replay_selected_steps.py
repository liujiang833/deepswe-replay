#!/usr/bin/env python3
"""Replay selected steps from prepared images and collect per-step ARM Top-Down."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys


HERE = Path(__file__).resolve().parent
RESULT_FIELDS = ("repeat", "run_dir", "status", "error", "replay_rc", "perf_rc",
                 "parse_rc", "target_cycles", "Retiring", "BadSpec",
                 "FrontendBound", "BackendBound")


def interval_span(path):
    """Return the first and last interval end timestamps in perf's time domain."""
    times = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                if path.suffix == ".json":
                    value = json.loads(line).get("interval")
                else:
                    value = line.split(",", 1)[0]
                if value is not None:
                    times.append(float(value))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
    if not times:
        raise ValueError(f"perf 没有 interval 时间戳: {path}")
    return min(times), max(times)


def run_one(row, index, repetition, trials_dir, root, cpuset):
    result = dict(row)
    result.update({key: "" for key in RESULT_FIELDS})
    result["repeat"] = str(repetition)
    try:
        if row.get("status") != "ready":
            raise ValueError("检查点行不是 ready")
        trial = row["trial"].strip()
        if not trial or Path(trial).name != trial or trial in (".", ".."):
            raise ValueError(f"非法 trial 目录名: {trial!r}")
        step = int(row["step"])
        checkpoint = int(row["checkpoint_step"])
        if not 0 <= checkpoint <= step or step - checkpoint != int(row["warmup_steps"]):
            raise ValueError("checkpoint_step、step、warmup_steps 不一致")
        directory = trials_dir / trial
        actual_hash = hashlib.sha256((directory / "trajectory.json").read_bytes()).hexdigest()
        if actual_hash != row["trace_sha256"]:
            raise ValueError("trajectory.json 哈希与构建检查点时不同")
        task_hash = hashlib.sha256((directory / "task.json").read_bytes()).hexdigest()
        if task_hash != row["task_sha256"]:
            raise ValueError("task.json 哈希与构建检查点时不同")
        image = row["image_id"].strip()
        inspect = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", image],
                                 capture_output=True, text=True)
        if inspect.returncode or inspect.stdout.strip() != image:
            raise ValueError(f"本地检查点镜像不可用: {image}")
        tag = row["image_tag"].strip()
        tagged = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", tag],
                                capture_output=True, text=True)
        if tagged.returncode or tagged.stdout.strip() != image:
            raise ValueError(f"检查点 tag 未指向 CSV 中的镜像 ID: {tag}")
        slug = hashlib.sha256(f"{index}:{trial}:{step}".encode()).hexdigest()[:8]
        out_root = root / f"{index:04d}-{slug}-step{step}-r{repetition}"
        out_root.mkdir(parents=True, exist_ok=False)
        result["run_dir"] = str(out_root)
        cmd = ["bash", str(HERE / "topdown_trial.sh"), str(directory),
               "--per-step", "--no-metrics", "--from-image", image,
               "--start-step", str(checkpoint), "--stop-before-step", str(step + 1),
               "-o", str(out_root)]
        if cpuset:
            cmd.extend(["--cpuset-cpus", cpuset])
        with (out_root / "runner.log").open("w") as log:
            process = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        trial_out = out_root / trial
        topdown_dir = trial_out / "topdown"
        status_path = topdown_dir / "run_status.json"
        if not status_path.exists():
            raise RuntimeError(f"采集入口退出码 {process.returncode}，未生成 run_status.json；见 runner.log")
        status = json.loads(status_path.read_text())
        for key in ("replay_rc", "perf_rc", "parse_rc"):
            result[key] = str(status.get(key))
        if any(status.get(key) != 0 for key in ("replay_rc", "perf_rc", "parse_rc")):
            raise RuntimeError(f"重放/perf/解析失败：{result['replay_rc']}/{result['perf_rc']}/{result['parse_rc']}")
        verdict = json.loads((trial_out / "verdict.json").read_text())
        if (verdict.get("start_step"), verdict.get("stop_before_step")) != (checkpoint, step + 1):
            raise RuntimeError("重放的 step 范围与 CSV 不一致")
        n = verdict.get("n_replayed")
        if n is None or verdict.get("rc_match_semantic") != f"{n}/{n}":
            raise RuntimeError(f"预热或目标命令退出码与轨迹不符：{verdict.get('rc_match_semantic')}")
        commands = [json.loads(line) for line in (trial_out / "commands.jsonl").read_text().splitlines() if line]
        target_commands = [cmd for cmd in commands if cmd.get("step") == step]
        if not target_commands:
            raise RuntimeError("目标 step 没有执行任何命令")
        cleaned = json.loads((topdown_dir / "topdown_steps_cleaned.json").read_text())
        match = [entry for entry in cleaned.get("steps", []) if entry.get("step") == step]
        if len(match) != 1:
            raise RuntimeError("Top-Down 结果缺少唯一的目标 step；可能预热缓冲不足")
        entry = match[0]
        if entry.get("n_cmds") != len(target_commands):
            raise RuntimeError("目标 step 命令数与 Top-Down 归并结果不一致")
        perf_file = topdown_dir / "perf.json"
        if not perf_file.exists():
            perf_file = topdown_dir / "perf.csv"
        first, last = interval_span(perf_file)
        lo, hi = entry["t_start_perf"], entry["t_end_perf"]
        interval_s = cleaned.get("interval_ms", 10) / 1000.0
        # perf timestamps mark interval ends; allow one interval at each edge.
        if first - interval_s > lo or last + interval_s < hi:
            raise RuntimeError(f"perf interval 未覆盖目标 step：[{first:.3f},{last:.3f}] vs [{lo:.3f},{hi:.3f}]；增大预热步数")
        counts = entry.get("counts") or {}
        topdown = entry.get("topdown") or {}
        if counts.get("cpu_cycles", 0) <= 0 or not topdown:
            raise RuntimeError("目标 step 的 PMU 计数不足")
        result["target_cycles"] = str(counts["cpu_cycles"])
        for key in ("Retiring", "BadSpec", "FrontendBound", "BackendBound"):
            result[key] = str(topdown[key])
        result["status"] = "ok"
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        result["status"] = "error"
        result["error"] = str(exc).replace("\n", " ")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("steps_docker_csv", type=Path)
    ap.add_argument("--trials-dir", type=Path, default=HERE / "full_trials")
    ap.add_argument("--output-dir", type=Path, default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--cpuset-cpus", default="")
    args = ap.parse_args()
    if args.repeat < 1:
        ap.error("--repeat 必须大于 0")
    with args.steps_docker_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"trial", "step", "checkpoint_step", "warmup_steps", "image_tag", "image_id",
                    "trace_sha256", "task_sha256", "status"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            ap.error(f"steps_docker.csv 缺列: {sorted(required - set(reader.fieldnames or []))}")
        source_fields = list(reader.fieldnames)
        rows = list(reader)
    root = args.output_dir or (Path("step_replay_runs") /
           (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)))
    root.mkdir(parents=True, exist_ok=False)
    results = []
    for index, row in enumerate(rows, 1):
        for repetition in range(1, args.repeat + 1):
            result = run_one(row, index, repetition, args.trials_dir, root, args.cpuset_cpus)
            results.append(result)
            print(f"[{index}/{len(rows)} repeat={repetition}] {row.get('trial')} step={row.get('step')} "
                  f"{result['status']} {result['error']}")
    columns = source_fields + [field for field in RESULT_FIELDS if field not in source_fields]
    with (root / "results.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(results)
    print(f"输出 {root / 'results.csv'}")
    return 1 if any(result["status"] != "ok" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
