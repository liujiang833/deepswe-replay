#!/usr/bin/env python3
"""Analyze a mini-SWE-agent (v2, tool-call format) trajectory.

Answers: how many LM calls, how many bash invocations, and -- the point --
what those bash commands actually *did*, broken down by the program invoked
and by intent, with return codes, output sizes and per-step latency.

Usage: analyze_traj.py <instance.traj.json> [--json out.json] [--dump-commands]
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

# ---------------- shell decomposition ----------------

HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# separators that start a NEW command; a pipeline stays one command
SEP = re.compile(r"&&|\|\||;")


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
# ordered: first match wins
# A python heredoc counts as an *edit* only if its body actually writes something;
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
    ("search_content",r"\b(grep|rg|ack|ag)\b"),
    ("search_files",  r"\b(find|fd|locate)\b"),
    ("read_file",     r"\b(cat|head|tail|sed -n|nl|less|more)\b"),
    ("navigate",      r"^(cd|pwd|ls)\b"),
    ("vcs",           r"^git\b"),
    ("pkg_env",       r"\b(pip|conda|apt-get|npm|uv)\b"),
    ("shell_info",    r"^(echo|which|whereis|env|export|printenv|whoami|uname|df|wc|rm|mkdir|cp|mv|chmod|diff)\b"),
]


def classify(cmd: str) -> str:
    for name, pat in INTENT_RULES:
        if re.search(pat, cmd):
            return name
    return "other"


FILE_RE = re.compile(r"[\w./-]*\.(?:py|txt|cfg|toml|ini|yaml|yml|rst|md|json)\b")


# ---------------- main analysis ----------------

def analyze(path: Path) -> dict:
    data = json.loads(path.read_text())
    messages = data["messages"]
    info = data.get("info", {})

    # map tool_call_id -> observation message
    obs_by_id, obs_in_order = {}, []
    for m in messages:
        if m.get("role") == "tool":
            obs_in_order.append(m)
            if m.get("tool_call_id"):
                obs_by_id[m["tool_call_id"]] = m

    calls = []          # one per bash tool call
    lm_turns = 0
    prev_ts = None
    for idx, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        lm_turns += 1
        extra = m.get("extra", {}) or {}
        ts = extra.get("timestamp")
        think = m.get("content") or ""
        if isinstance(think, list):
            think = "".join(b.get("text", "") for b in think if isinstance(b, dict))
        for act in extra.get("actions", []) or []:
            obs = obs_by_id.get(act.get("tool_call_id")) or {}
            oe = obs.get("extra", {}) or {}
            calls.append({
                "turn": lm_turns,
                "msg_index": idx,
                "command": act["command"],
                "returncode": oe.get("returncode"),
                "out_chars": len(oe.get("raw_output") or ""),
                "exception": oe.get("exception_info") or "",
                "think_chars": len(think),
                "latency_s": (round(ts - prev_ts, 1) if (ts and prev_ts) else None),
                "cost": extra.get("cost"),
            })
        if ts:
            prev_ts = ts

    binaries, intents, files = Counter(), Counter(), Counter()
    rcs = Counter()
    ops = 0
    for c in calls:
        cmds = split_commands(c["command"])
        c["n_ops"] = len(cmds)
        c["ops"] = []
        ops += len(cmds)
        for one in cmds:
            k = classify(one)
            intents[k] += 1
            for b in pipeline_binaries(one):
                binaries[b] += 1
            for f in FILE_RE.findall(one):
                if f not in (".py",):
                    files[f] += 1
            c["ops"].append({"cmd": one, "intent": k, "binaries": pipeline_binaries(one)})
        rcs[c["returncode"]] += 1

    return {
        "file": str(path),
        "instance_id": data.get("instance_id"),
        "exit_status": info.get("exit_status"),
        "model": (info.get("config", {}).get("model", {}) or {}).get("model_name"),
        "image": (info.get("config", {}).get("environment", {}) or {}).get("image"),
        "api_calls": (info.get("model_stats") or {}).get("api_calls"),
        "instance_cost": (info.get("model_stats") or {}).get("instance_cost"),
        "n_messages": len(messages),
        "n_lm_turns": lm_turns,
        "n_bash_calls": len(calls),
        "n_shell_ops": ops,
        "returncodes": dict(rcs),
        "intents": intents.most_common(),
        "binaries": binaries.most_common(),
        "files": files.most_common(25),
        "total_observation_chars": sum(c["out_chars"] for c in calls),
        "submission_chars": len(info.get("submission") or ""),
        "calls": calls,
    }


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    res = analyze(Path(args[0]))
    if "--json" in args:
        out = Path(args[args.index("--json") + 1])
        out.write_text(json.dumps(res, indent=2))
        print(f"[wrote {out}]")
    print(f"instance   : {res['instance_id']}")
    print(f"model      : {res['model']}")
    print(f"image      : {res['image']}")
    print(f"exit_status: {res['exit_status']}   cost=${res['instance_cost']:.4f}  api_calls={res['api_calls']}")
    print(f"messages={res['n_messages']}  LM turns={res['n_lm_turns']}  "
          f"bash tool calls={res['n_bash_calls']}  shell ops inside them={res['n_shell_ops']}")
    print(f"observation bytes fed back: {res['total_observation_chars']:,}   "
          f"submitted patch: {res['submission_chars']} chars")
    print(f"return codes: {res['returncodes']}")
    print("\n-- intent mix (per shell operation) --")
    for k, v in res["intents"]:
        print(f"   {k:16s} {v}")
    print("\n-- programs invoked --")
    for k, v in res["binaries"]:
        print(f"   {k:16s} {v}")
    print("\n-- files referenced --")
    for k, v in res["files"]:
        print(f"   {k:64s} {v}")
    print("\n-- per bash call --")
    for i, c in enumerate(res["calls"], 1):
        kinds = ",".join(sorted({o["intent"] for o in c["ops"]}))
        first = c["command"].split("\n")[0][:88]
        lat = f"{c['latency_s']}s" if c["latency_s"] is not None else "-"
        print(f"  #{i:>3} t{c['turn']:<3} rc={str(c['returncode']):<4} out={c['out_chars']:>6} "
              f"lat={lat:>7} [{kinds:22s}] {first}")
    if "--dump-commands" in args:
        print("\n-- full commands --")
        for i, c in enumerate(res["calls"], 1):
            print(f"\n===== #{i} (turn {c['turn']}, rc={c['returncode']}) =====")
            print(c["command"])


if __name__ == "__main__":
    main()
