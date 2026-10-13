#!/usr/bin/env python3
"""Build filesystem checkpoints before selected steps from a trial/step CSV.

`step` is the zero-based tool-call step used by replay.py and commands.jsonl.
For target s and buffer B, the checkpoint is taken before step max(0, s-B).
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from replay import load_trace, task_field


HERE = Path(__file__).resolve().parent
REPLAY_LABEL = "org.deepswe.per-step-replay"
SOURCE_LABEL = REPLAY_LABEL + ".source"
FIELDS = ("input_step", "step_base", "checkpoint_step", "warmup_steps", "image_tag", "image_id", "base_image_id",
          "trace_sha256", "task_sha256", "status", "error")


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def image_id(ref):
    p = docker("image", "inspect", "-f", "{{.Id}}", ref)
    if p.returncode:
        raise RuntimeError(f"镜像不可用 {ref}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def checkpoint_tag(base_ref, token, input_step):
    """Retain build_arm's task image repository/tag and end with the CSV step."""
    last_colon = base_ref.rfind(":")
    if last_colon <= base_ref.rfind("/"):
        raise ValueError(f"task.toml 的 docker_image 缺少 tag: {base_ref}")
    suffix = f"-{token[:12]}-replay-step{input_step}"
    if len(base_ref[last_colon + 1:] + suffix) > 128:
        raise ValueError(f"检查点镜像 tag 超过 Docker 的 128 字符上限: {base_ref}")
    return base_ref + suffix


def trial_info(trials_dir, trial):
    if not trial or Path(trial).name != trial or trial in (".", ".."):
        raise ValueError(f"trial 必须是 full_trials 下的目录名: {trial!r}")
    directory = trials_dir / trial
    trajectory = directory / "trajectory.json"
    task_file = directory / "task.json"
    raw = trajectory.read_bytes()
    task_raw = task_file.read_bytes()
    task = json.loads(task_raw)
    files = {f["path"]: f["content"] for f in task["files"]}
    base_ref = task_field(files["task.toml"], "docker_image")
    items = load_trace(json.loads(raw))
    count = max((it["step"] for it in items), default=-1) + 1
    return (directory, hashlib.sha256(raw).hexdigest(),
            hashlib.sha256(task_raw).hexdigest(), base_ref, items, count)


def prepare_checkpoint(trial_dir, checkpoint_step, base_ref, log_dir, tag, token):
    """Replay the prefix, commit its filesystem, and always remove containers."""
    changes = ("--change", f"LABEL {REPLAY_LABEL}=true",
               "--change", f"LABEL {SOURCE_LABEL}={token}")
    if checkpoint_step == 0:
        name = "step_replay_base_" + secrets.token_hex(6)
        created = docker("create", "--name", name, base_ref, "sleep", "infinity")
        if created.returncode:
            raise RuntimeError(f"创建初始检查点容器失败: {created.stderr.strip()[:300]}")
        try:
            committed = docker("commit", *changes, name, tag)
            if committed.returncode:
                raise RuntimeError(f"docker commit 失败: {committed.stderr.strip()[:300]}")
            return image_id(tag)
        finally:
            docker("rm", "-f", name)
    salt = secrets.token_hex(6)
    name_prefix = "replay_" + re.sub(r"[^A-Za-z0-9_.-]", "_", trial_dir.name)[:44]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "prepare.log"
    cmd = [sys.executable, str(HERE / "replay.py"), str(trial_dir),
           str(trial_dir / "task.json"), "-o", str(log_dir),
           "--stop-before-step", str(checkpoint_step), "--no-metrics", "--keep",
           "--container-salt", salt]
    proc = None
    container = None
    try:
        with log_file.open("w") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            container = f"{name_prefix}_{proc.pid}_{salt}"
            rc = proc.wait()
        if rc:
            raise RuntimeError(f"前缀重放退出码 {rc}；见 {log_file}")
        verdict_path = log_dir / trial_dir.name / "verdict.json"
        verdict = json.loads(verdict_path.read_text())
        if verdict.get("container_name") != container:
            raise RuntimeError(f"容器身份不一致；见 {verdict_path}")
        n = verdict.get("n_replayed")
        if n is None or verdict.get("rc_match_semantic") != f"{n}/{n}":
            raise RuntimeError(f"前缀命令退出码与轨迹不符；运行 python3 "
                               f"{HERE / 'diagnose_step_prepare.py'} {verdict_path}")
        mounts = docker("inspect", "-f", "{{json .Mounts}}", container)
        if mounts.returncode:
            raise RuntimeError(f"读取容器挂载失败: {mounts.stderr.strip()[:300]}")
        if json.loads(mounts.stdout or "[]"):
            raise RuntimeError("容器存在挂载卷；docker commit 无法保存卷内状态")
        processes = docker("top", container)
        if processes.returncode:
            raise RuntimeError(f"读取容器进程失败: {processes.stderr.strip()[:300]}")
        if len([line for line in processes.stdout.splitlines() if line.strip()]) > 2:
            raise RuntimeError("检查点处仍有后台进程；docker commit 无法保存进程状态")
        committed = docker("commit", *changes, container, tag)
        if committed.returncode:
            raise RuntimeError(f"docker commit 失败: {committed.stderr.strip()[:300]}")
        return image_id(tag)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if container:
            docker("rm", "-f", container + "-sink", container)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("steps_csv", type=Path, help="含 trial,step 两列；step 为 0 基 tool-call step")
    ap.add_argument("--trials-dir", type=Path, default=HERE / "full_trials")
    ap.add_argument("-o", "--output", type=Path,
                    help="输出 CSV；正式构建默认 steps_docker.csv，预览默认 steps_docker.plan.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="只验证输入、检查原始镜像并生成计划 CSV；不启动容器或创建镜像")
    ap.add_argument("--buffer-steps", type=int, default=5)
    ap.add_argument("--step-base", type=int, choices=(0, 1), default=0,
                    help="输入 CSV 的 step 编号起点；输出统一转换为 0 基（默认 0）")
    ap.add_argument("--logs-dir", type=Path, default=Path("step_prepare_logs"))
    args = ap.parse_args()
    if args.buffer_steps < 0:
        ap.error("--buffer-steps 不能为负")
    if args.output is None:
        args.output = Path("steps_docker.plan.csv" if args.dry_run else "steps_docker.csv")
    if args.output.resolve() == args.steps_csv.resolve():
        ap.error("输出 CSV 不能覆盖输入 steps.csv")
    with args.steps_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"trial", "step"}.issubset(reader.fieldnames):
            ap.error("steps.csv 必须包含 trial 和 step 两列")
        source_fields = list(reader.fieldnames)
        rows = list(reader)
    output_fields = source_fields + [field for field in FIELDS if field not in source_fields]
    cache = {}
    trial_cache = {}
    failed = 0
    for index, row in enumerate(rows, 1):
        row.update({key: "" for key in FIELDS})
        try:
            trial = row["trial"].strip()
            row["input_step"] = row["step"].strip()
            row["step_base"] = str(args.step_base)
            step = int(row["input_step"]) - args.step_base
            row["step"] = str(step)
            if trial not in trial_cache:
                trial_cache[trial] = trial_info(args.trials_dir, trial)
            directory, trace_hash, task_hash, base_ref, items, n_steps = trial_cache[trial]
            if not 0 <= step < n_steps or not any(
                    it["step"] == step and not it["sentinel"] for it in items):
                raise ValueError(f"step {step} 不存在或没有可执行命令；有效范围 0..{n_steps-1}")
            checkpoint = max(0, step - args.buffer_steps)
            base_id = image_id(base_ref)
            row["checkpoint_step"] = str(checkpoint)
            row["warmup_steps"] = str(step - checkpoint)
            row["trace_sha256"] = trace_hash
            row["task_sha256"] = task_hash
            row["base_image_id"] = base_id
            key = (trial, checkpoint, trace_hash, task_hash, base_id)
            token = hashlib.sha256(repr(key).encode()).hexdigest()[:20]
            tag = checkpoint_tag(base_ref, token, row["input_step"])
            row["image_tag"] = tag
            if args.dry_run:
                row["status"] = "planned"
                print(f"[{index}/{len(rows)}] PLAN {trial} step={step} checkpoint={checkpoint} {tag}")
                continue
            if key not in cache:
                log_dir = args.logs_dir / f"{index:04d}-{token}"
                cache[key] = prepare_checkpoint(directory, checkpoint, base_ref, log_dir, tag, token)
            else:
                tagged = docker("tag", cache[key], tag)
                if tagged.returncode:
                    raise RuntimeError(f"给共用检查点添加 step tag 失败: {tagged.stderr.strip()[:300]}")
            row["image_id"] = cache[key]
            row["status"] = "ready"
            print(f"[{index}/{len(rows)}] {trial} step={step} checkpoint={checkpoint} {tag}")
        except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
            row["status"] = "error"
            row["error"] = str(exc).replace("\n", " ")
            failed += 1
            print(f"[{index}/{len(rows)}] ERROR {row.get('trial')} step={row.get('step')}: {exc}", file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + f".{os.getpid()}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(args.output)
    state = "planned" if args.dry_run else "ready"
    print(f"输出 {args.output}；{state}={len(rows)-failed} error={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
