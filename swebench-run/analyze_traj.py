#!/usr/bin/env python3
"""Analyze a mini-SWE-agent (v2, tool-call format) trajectory.

Two things this is careful about:

1. Every command the agent issues starts with `cd /testbed && ...` because each
   `docker exec ... bash -c` is a fresh subshell. Counting that `cd` as work
   drowns out the real operations, so it is stripped and reported separately.

2. Cost is reported per step, split into the LM window and the exec window, with
   the harness's own per-step overhead (serializing + writing the trajectory)
   measured directly rather than guessed.

Usage:
    analyze_traj.py <instance.traj.json> [--json out.json] [--dump-commands]
                                         [--no-harness-probe]
"""
import json
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

# ---------------- shell decomposition ----------------

HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SEP = re.compile(r"&&|\|\||;")          # a pipeline stays ONE command
BARE_CD = re.compile(r"^\s*cd\s+(\S+)\s*$")
CD_PREFIX = re.compile(r"^\s*cd\s+(\S+)\s*&&\s*")


def split_commands(block: str) -> list[str]:
    """One shell block -> the individual commands inside it (heredocs kept whole)."""
    out, lines, i = [], block.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        m = HEREDOC.search(line)
        if m:
            delim, body, i = m.group(2), [line], i + 1
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i]); i += 1
            if i < len(lines):
                body.append(lines[i])
            out.append("\n".join(body)); i += 1
            continue
        for part in SEP.split(line):
            part = part.strip()
            if part:
                out.append(part)
        i += 1
    return out


def strip_cd(op: str) -> tuple[list[str], str]:
    """Peel leading `cd <dir> &&` chains. Returns (dirs_cd_into, remaining_command).

    A heredoc op keeps its whole line, so `cd /testbed && python - <<'PY' ...`
    arrives here as one string and would otherwise be attributed to `cd`.
    """
    dirs = []
    while (m := CD_PREFIX.match(op)):
        dirs.append(m.group(1))
        op = op[m.end():]
    return dirs, op


ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
WRAPPERS = {"sudo", "command", "exec", "time", "nohup", "env", "then", "do", "else", "fi", "done"}


def head_binary(cmd: str) -> str:
    """The program actually invoked (skip env assignments / wrappers)."""
    cmd = cmd.strip().lstrip("(")
    for tok in cmd.split():
        if ENV_ASSIGN.match(tok) or tok in WRAPPERS:
            continue
        return tok.split("/")[-1].strip("`$(")
    return "?"


def pipeline_binaries(cmd: str) -> list[str]:
    """Every program in a pipeline, so `grep ... | head` counts both."""
    return [head_binary(seg) for seg in cmd.split("|") if seg.strip()]


# ---------------- intent classification ----------------
# A python heredoc is an *edit* only if its body actually writes something;
# otherwise it only prints, i.e. it is a reproduction / verification script.
HEREDOC_WRITES = r"write_text|\.write\(|open\([^)]*['\"][wa]|os\.replace|shutil\."

INTENT_RULES = [
    ("submit",        r"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ("make_patch",    r"git diff\b.*>|git diff --\s"),
    ("run_tests",     r"\b(pytest|py\.test|tox|nosetests)\b|python -m (pytest|unittest)"),
    ("edit_file",     r"\b(sed -i|patch -|apply_patch)\b|cat\s*>\s*\S|>\s*\S+\.py\b|tee\b"
                      r"|python[0-9.]* - <<[\s\S]*(?:" + HEREDOC_WRITES + r")"),
    ("repro_script",  r"python[0-9.]* - <<"
                      r"|python[0-9.]*\s+\S*(repro|test_|check|verify|debug)\S*\.py"),
    ("run_python",    r"^python[0-9.]*\b|^python -c"),
    ("search_content", r"\b(grep|rg|ack|ag)\b"),
    ("search_files",  r"\b(find|fd|locate)\b"),
    ("read_file",     r"\b(cat|head|tail|sed -n|nl|less|more)\b"),
    ("list_dir",      r"^(ls|pwd)\b"),
    ("vcs",           r"^git\b"),
    ("pkg_env",       r"\b(pip|conda|apt-get|npm|uv)\b"),
    ("shell_misc",    r"^(echo|which|whereis|env|export|printenv|whoami|uname|df|wc|rm|mkdir|cp|mv|chmod|diff)\b"),
]


def classify(cmd: str) -> str:
    for name, pat in INTENT_RULES:
        if re.search(pat, cmd):
            return name
    return "other"


FILE_RE = re.compile(r"[\w./-]*\.(?:py|txt|cfg|toml|ini|yaml|yml|rst|md|json)\b")


# ---------------- run log (gives the loop its t0) ----------------

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})")


def read_run_log(traj_path: Path) -> dict:
    """Timestamps the trajectory itself cannot carry: dataset load, container start."""
    log = traj_path.parent.parent / "minisweagent.log"
    if not log.is_file():
        return {}
    import datetime
    marks = {}
    for line in log.read_text().splitlines():
        m = LOG_TS.match(line)
        if not m:
            continue
        ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").timestamp()
        if "Loading dataset" in line:
            marks.setdefault("dataset_load_start", ts)
        elif "Running on" in line:
            marks.setdefault("dataset_ready", ts)
        elif "Starting container" in line:
            marks.setdefault("container_start", ts)
        elif "Started container" in line:
            marks.setdefault("container_ready", ts)
    return marks


# ---------------- harness overhead probe ----------------

def probe_harness_overhead(messages: list, info: dict) -> list[float]:
    """Measure what the agent framework itself costs per step.

    `DefaultAgent.run` calls `self.save(...)` in a `finally` after every step, and
    `serialize()` json-dumps the whole message list. That cost grows with the
    conversation, so it is measured against the real messages rather than assumed
    negligible. Returns seconds per step.
    """
    out = []
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "t.json"
        n_steps = sum(1 for m in messages if m.get("role") == "assistant")
        # replay the message list as it stood at the end of each step
        idx, seen = [], 0
        for i, m in enumerate(messages):
            if m.get("role") == "assistant":
                seen += 1
                if seen <= n_steps:
                    idx.append(i)
        for i in idx:
            # everything up to and including this step's observations
            j = i + 1
            while j < len(messages) and messages[j].get("role") in ("tool", "user"):
                j += 1
            snapshot = {"info": info, "messages": messages[:j],
                        "trajectory_format": "mini-swe-agent-1.1"}
            t = time.perf_counter()
            target.write_text(json.dumps(snapshot, indent=2))
            out.append(time.perf_counter() - t)
    return out


# ---------------- main analysis ----------------

def analyze(path: Path, harness_probe: bool = True) -> dict:
    data = json.loads(path.read_text())
    messages = data["messages"]
    info = data.get("info", {})
    marks = read_run_log(path)

    # ---- group messages into steps -------------------------------------
    steps = []
    cur = None
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            if cur:
                steps.append(cur)
            e = m.get("extra", {}) or {}
            r = e.get("response", {}) or {}
            u = r.get("usage", {}) or {}
            cd = u.get("completion_tokens_details") or {}
            pd = u.get("prompt_tokens_details") or {}
            think = m.get("content") or ""
            if isinstance(think, list):
                think = "".join(b.get("text", "") for b in think if isinstance(b, dict))
            cur = {
                "assistant_ts": e.get("timestamp"),
                "actions": [a["command"] for a in (e.get("actions") or [])],
                "cost": e.get("cost", 0.0),
                "prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "reasoning_tokens": cd.get("reasoning_tokens", 0) or 0,
                "cached_tokens": pd.get("cached_tokens", 0) or 0,
                "think_chars": len(think),
                "obs": [],
                "last_tool_ts": None,
            }
        elif role == "tool" and cur is not None:
            oe = m.get("extra", {}) or {}
            cur["obs"].append({
                "returncode": oe.get("returncode"),
                "out_chars": len(oe.get("raw_output") or ""),
                "exception": oe.get("exception_info") or "",
            })
            if oe.get("timestamp"):
                cur["last_tool_ts"] = oe["timestamp"]
    if cur:
        steps.append(cur)

    harness = probe_harness_overhead(messages, info) if harness_probe else [None] * len(steps)

    # ---- timing ---------------------------------------------------------
    t0 = marks.get("container_ready")
    prev_end = t0
    for n, s in enumerate(steps):
        s["step"] = n + 1
        s["lm_window"] = (s["assistant_ts"] - prev_end) if (prev_end and s["assistant_ts"]) else None
        s["exec_window"] = ((s["last_tool_ts"] - s["assistant_ts"])
                            if (s["last_tool_ts"] and s["assistant_ts"]) else None)
        s["harness_s"] = harness[n] if n < len(harness) else None
        s["total_s"] = sum(x for x in (s["lm_window"], s["exec_window"]) if x is not None) or None
        s["obs_chars"] = sum(o["out_chars"] for o in s["obs"])
        prev_end = s["last_tool_ts"] or s["assistant_ts"]
    for n, s in enumerate(steps):
        s["delta_prompt_tokens"] = (s["prompt_tokens"] - steps[n - 1]["prompt_tokens"]) if n else s["prompt_tokens"]

    # ---- shell operations, cd stripped ---------------------------------
    binaries, intents, files = Counter(), Counter(), Counter()
    cd_prefixes = Counter()
    n_cd = 0
    real_ops = 0
    calls = []
    for s in steps:
        for k, cmd in enumerate(s["actions"]):
            obs = s["obs"][k] if k < len(s["obs"]) else {}
            entry = {"step": s["step"], "command": cmd, "ops": [],
                     "returncode": obs.get("returncode"),
                     "out_chars": obs.get("out_chars", 0)}
            for one in split_commands(cmd):
                if (m := BARE_CD.match(one)):
                    n_cd += 1
                    cd_prefixes[m.group(1)] += 1
                    continue
                dirs, real = strip_cd(one)
                if dirs:
                    n_cd += len(dirs)
                    for d in dirs:
                        cd_prefixes[d] += 1
                if not real.strip():
                    continue
                real_ops += 1
                intent = classify(real)
                bins = pipeline_binaries(real)
                intents[intent] += 1
                for b in bins:
                    binaries[b] += 1
                for f in FILE_RE.findall(real):
                    if f != ".py":
                        files[f] += 1
                entry["ops"].append({"cmd": real, "intent": intent, "binaries": bins})
            calls.append(entry)

    lm = [s["lm_window"] for s in steps if s["lm_window"] is not None]
    ex = [s["exec_window"] for s in steps if s["exec_window"] is not None]
    hs = [s["harness_s"] for s in steps if s["harness_s"] is not None]
    loop_end = max((s["last_tool_ts"] or s["assistant_ts"]) for s in steps)

    return {
        "file": str(path),
        "instance_id": data.get("instance_id"),
        "exit_status": info.get("exit_status"),
        "model": (info.get("config", {}).get("model", {}) or {}).get("model_name"),
        "image": (info.get("config", {}).get("environment", {}) or {}).get("image"),
        "api_calls": (info.get("model_stats") or {}).get("api_calls"),
        "instance_cost": (info.get("model_stats") or {}).get("instance_cost"),
        "n_messages": len(messages),
        "n_steps": len(steps),
        "n_bash_calls": sum(len(s["actions"]) for s in steps),
        "n_shell_ops_total": real_ops + n_cd,
        "n_real_ops": real_ops,
        "n_cd_prefix": n_cd,
        "cd_targets": cd_prefixes.most_common(),
        "intents": intents.most_common(),
        "binaries": binaries.most_common(),
        "files": files.most_common(25),
        "marks": marks,
        "timing": {
            "t0_container_ready": t0,
            "loop_wall_s": (loop_end - t0) if t0 else None,
            "lm_window_total_s": sum(lm),
            "exec_window_total_s": sum(ex),
            "harness_save_total_s": sum(hs) if hs else None,
            "startup_dataset_s": (marks.get("dataset_ready", 0) - marks.get("dataset_load_start", 0)) or None,
            "startup_container_s": (marks.get("container_ready", 0) - marks.get("container_start", 0)) or None,
        },
        "tokens": {
            "prompt_total": sum(s["prompt_tokens"] for s in steps),
            "completion_total": sum(s["completion_tokens"] for s in steps),
            "reasoning_total": sum(s["reasoning_tokens"] for s in steps),
            "cached_total": sum(s["cached_tokens"] for s in steps),
            "final_context": steps[-1]["prompt_tokens"] if steps else 0,
        },
        "total_observation_chars": sum(s["obs_chars"] for s in steps),
        "submission_chars": len(info.get("submission") or ""),
        "steps": [{k: v for k, v in s.items() if k != "obs"} for s in steps],
        "calls": calls,
    }


def _f(x, w=7, p=2):
    return f"{x:{w}.{p}f}" if isinstance(x, (int, float)) else f"{'n/a':>{w}}"


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    res = analyze(Path(args[0]), harness_probe="--no-harness-probe" not in args)
    if "--json" in args:
        out = Path(args[args.index("--json") + 1])
        out.write_text(json.dumps(res, indent=2))
        print(f"[wrote {out}]")

    t, tk = res["timing"], res["tokens"]
    print(f"instance   : {res['instance_id']}")
    print(f"model      : {res['model']}")
    print(f"exit_status: {res['exit_status']}   cost=${res['instance_cost']:.4f}  steps={res['n_steps']}")
    print(f"bash calls : {res['n_bash_calls']}   shell ops={res['n_shell_ops_total']} "
          f"(cd prefix {res['n_cd_prefix']} + real work {res['n_real_ops']})")

    print("\n===== agent loop 开销 =====")
    print(f"  启动（不在 loop 内）: 载数据集 {_f(t['startup_dataset_s'],5,1)}s | 起容器 {_f(t['startup_container_s'],5,2)}s")
    print(f"  loop 墙钟总时长      : {_f(t['loop_wall_s'],6,1)}s")
    print(f"    ├ LM 窗口合计      : {_f(t['lm_window_total_s'],6,1)}s "
          f"({t['lm_window_total_s']/t['loop_wall_s']*100:.0f}%)")
    print(f"    └ exec 窗口合计    : {_f(t['exec_window_total_s'],6,1)}s "
          f"({t['exec_window_total_s']/t['loop_wall_s']*100:.0f}%)")
    if t["harness_save_total_s"]:
        print(f"  其中框架自身开销     : {_f(t['harness_save_total_s'],6,2)}s "
              f"（每步 save() 序列化+写盘轨迹，实测；含在 LM 窗口里）")
    print(f"  上下文放大           : 累计输入 {tk['prompt_total']:,} tok / 末轮上下文 {tk['final_context']:,} tok "
          f"= {tk['prompt_total']/max(tk['final_context'],1):.1f}x")
    print(f"  输出 token           : {tk['completion_total']:,}（其中 reasoning {tk['reasoning_total']:,}）"
          f" | 缓存命中 {tk['cached_total']:,}")

    print("\n===== 每个 step 的开销 =====")
    print(f"{'st':>3} {'act':>3} {'LM_s':>7} {'exec_s':>7} {'step_s':>7} {'in_tok':>7} {'Δin':>6} "
          f"{'cache%':>7} {'out':>5} {'reas':>5} {'cost$':>8} {'obs_B':>7}")
    for s in res["steps"]:
        hit = s['cached_tokens'] / s['prompt_tokens'] * 100 if s['prompt_tokens'] else 0
        print(f"{s['step']:>3} {len(s['actions']):>3} {_f(s['lm_window'])} {_f(s['exec_window'])} "
              f"{_f(s['total_s'])} {s['prompt_tokens']:>7} {s['delta_prompt_tokens']:>6} "
              f"{hit:>6.0f}% {s['completion_tokens']:>5} {s['reasoning_tokens']:>5} "
              f"{s['cost']:>8.5f} {s['obs_chars']:>7}")

    print(f"\n===== 操作意图分布（已剥离 cd 前缀，共 {res['n_real_ops']} 个真实操作）=====")
    for k, v in res["intents"]:
        print(f"   {k:16s} {v:>3}  {v/res['n_real_ops']*100:5.1f}%")
    print(f"\n   [另计] cd 前缀 {res['n_cd_prefix']} 次 -> {dict(res['cd_targets'])}"
          f"（每条 docker exec 都是新子 shell，必须重新 cd）")

    print("\n===== 实际调用的程序（cd 已剥离）=====")
    for k, v in res["binaries"]:
        print(f"   {k:16s} {v}")

    print("\n===== 引用到的文件 =====")
    for k, v in res["files"]:
        print(f"   {k:64s} {v}")

    print("\n===== 每次 bash 调用（显示剥掉 cd 之后的真实命令）=====")
    for i, c in enumerate(res["calls"], 1):
        kinds = ",".join(sorted({o["intent"] for o in c["ops"]})) or "-"
        first = (c["ops"][0]["cmd"] if c["ops"] else c["command"]).split("\n")[0][:78]
        print(f"  #{i:>3} st{c['step']:<3} rc={str(c['returncode']):<4} out={c['out_chars']:>6} "
              f"[{kinds:24s}] {first}")

    if "--dump-commands" in args:
        print("\n===== 完整命令 =====")
        for i, c in enumerate(res["calls"], 1):
            print(f"\n----- #{i} (step {c['step']}, rc={c['returncode']}) -----")
            print(c["command"])


if __name__ == "__main__":
    main()
