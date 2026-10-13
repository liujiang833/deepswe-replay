#!/usr/bin/env python3
"""Explain command return-code mismatches from step checkpoint preparation.

Pass the verdict.json path printed by prepare_step_dockers.py, or a preparation
logs directory. With no argument, inspect ./step_prepare_logs.
"""

import argparse
import json
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent


def excerpt(value, limit):
    value = str(value or "").strip()
    if not value:
        return "（无记录）"
    return value[:limit] + (f"\n…（已截断，原长 {len(value)} 字符）" if len(value) > limit else "")


def trace_details(path):
    """Map flattened command index to the original step and observation."""
    if not path.is_file():
        return {}
    traj = json.loads(path.read_text(encoding="utf-8"))
    details = {}
    command_index = 0
    for step_index, step in enumerate(s for s in traj["steps"] if s.get("tool_calls")):
        results = (step.get("observation") or {}).get("results") or []
        for call_index, _ in enumerate(step["tool_calls"]):
            raw = results[call_index].get("content", "") if call_index < len(results) else ""
            try:
                observation = json.loads(raw) if isinstance(raw, str) else raw
                output = observation.get("output", "") if isinstance(observation, dict) else raw
            except ValueError:
                output = raw
            details[command_index] = {"step": step_index, "output": output}
            command_index += 1
    return details


def command_records(path):
    if not path.is_file():
        return {}
    records = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                record = json.loads(line)
                records[record["i"]] = record
    return records


def hint(mismatch):
    trace_rc = mismatch.get("trace_rc")
    replay_rc = mismatch.get("replay_rc")
    if trace_rc is None:
        return "轨迹里没有解析到 returncode；先看原始 observation 是否缺失或格式不同。"
    if mismatch.get("timed_out") or trace_rc == -1:
        return "两边的超时结果不同；核对机器速度、命令超时和原始 observation。"
    if replay_rc in (126, 127):
        return "重放侧可能无法执行命令或找不到程序；检查镜像和解释器环境。"
    return "检查镜像、依赖及更早命令留下的文件状态；单凭退出码不能确定根因。"


def diagnose_verdict(path, trials_dir, max_chars):
    verdict = json.loads(path.read_text(encoding="utf-8"))
    records = command_records(path.with_name("commands.jsonl"))
    trial = path.parent.name
    trajectory = trials_dir / trial / "trajectory.json"
    if not trajectory.is_file():
        trajectory = HERE / trial / "trajectory.json"
    originals = trace_details(trajectory)
    mismatches = [m for m in verdict.get("rc_mismatches", [])
                  if m.get("semantic_match") is False]
    lines = [f"轨迹: {trial}", f"判定文件: {path}",
             f"重放超时策略: {verdict.get('timeout_policy', '旧结果未记录')}",
             f"语义匹配: {verdict.get('rc_match_semantic', '未知')}；实际重放: {verdict.get('n_replayed', '未知')} 条",
             f"真正不匹配: {len(mismatches)} 条"]
    if not mismatches:
        lines.append("没有语义退出码差异。非零退出码若两边相同，也属于匹配。")
        return "\n".join(lines)
    for mismatch in mismatches:
        index = mismatch.get("i")
        record = records.get(index, {})
        original = originals.get(index, {})
        step = record.get("step", mismatch.get("step", original.get("step", "未知")))
        lines.extend([
            "",
            f"命令 i={index}，step={step}，原始 rc={mismatch.get('trace_rc')}，"
            f"重放 rc={mismatch.get('replay_rc')}，重放耗时={mismatch.get('wall_s', record.get('wall_s', '未知'))}s，"
            f"重放上限={record.get('cmd_timeout_s', '未知')}s",
            "命令:", excerpt(record.get("cmd", mismatch.get("cmd", "")), max_chars),
            "原始输出:", excerpt(original.get("output"), max_chars),
            "重放 stdout:", excerpt(record.get("stdout_head"), max_chars),
            "重放 stderr:", excerpt(record.get("stderr_head"), max_chars),
            "排查线索: " + hint(mismatch),
        ])
    if not records:
        lines.append("\n缺少 commands.jsonl，只有 verdict.json 中截断的命令可用。")
    if not originals:
        lines.append(f"\n未找到原始轨迹: {trajectory}；可用 --trials-dir 指定 full_trials 目录。")
    return "\n".join(lines)


def diagnose_log(path):
    tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
    return f"尚无 verdict.json；重放可能在写出判定前退出。\n日志: {path}\n" + "\n".join(tail)


def find_targets(path):
    if path.is_file():
        if path.name == "verdict.json":
            return [("verdict", path)]
        if path.name == "prepare.log":
            verdicts = list(path.parent.rglob("verdict.json"))
            return [("verdict", p) for p in verdicts] or [("log", path)]
        raise ValueError("请传 verdict.json、prepare.log 或准备日志目录")
    if not path.is_dir():
        raise FileNotFoundError(f"找不到 {path}；可用报错中的 verdict.json 路径或 --logs-dir 指定的目录")
    verdicts = sorted(path.rglob("verdict.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    logs = sorted(path.rglob("prepare.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    missing = [log for log in logs if not any(log.parent in verdict.parents for verdict in verdicts)]
    return [("verdict", p) for p in verdicts] + [("log", p) for p in missing]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, nargs="?", default=Path("step_prepare_logs"),
                    help="verdict.json、prepare.log 或准备日志目录（默认 ./step_prepare_logs）")
    ap.add_argument("--trials-dir", type=Path, default=HERE / "full_trials",
                    help="原始 trajectory.json 所在目录")
    ap.add_argument("--max-chars", type=int, default=800, help="每段命令/输出最多显示的字符数")
    ap.add_argument("-o", "--output", type=Path, help="同时将报告保存为文本文件")
    args = ap.parse_args()
    if args.max_chars < 1:
        ap.error("--max-chars 必须大于 0")
    try:
        targets = find_targets(args.path)
        if not targets:
            raise ValueError(f"{args.path} 下没有 verdict.json 或 prepare.log")
        reports = [diagnose_verdict(path, args.trials_dir, args.max_chars)
                   if kind == "verdict" else diagnose_log(path)
                   for kind, path in targets]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"诊断失败: {exc}", file=sys.stderr)
        return 1
    report = "\n\n".join(reports) + "\n"
    print(report, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"报告已保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
