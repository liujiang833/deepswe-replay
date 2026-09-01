#!/usr/bin/env python3
"""Re-run the trajectory's commands in the same image, timing each one.

The trajectory can only time execution per *step*: mini runs every action of a
step and only then formats the observations, so the two commands of a 2-action
step share one timestamp (3ms apart). To get per-command execution time the
commands have to be replayed, which also reproduces the state evolution -- the
edit at #7 lands before the pytest runs, so those are timed against the patched
tree exactly as the agent saw them.

Usage: measure_exec.py <traj.json> [--repeat N] [--json out.json]
"""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

IMAGE = "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
ENV = {                       # exactly what config/benchmarks/swebench.yaml sets
    "PAGER": "cat", "MANPAGER": "cat", "LESS": "-R",
    "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1",
    "BASH_ENV": "/root/.bashrc",
}


def commands(traj: Path) -> list[str]:
    msgs = json.loads(traj.read_text())["messages"]
    out = []
    for m in msgs:
        if m.get("role") == "assistant":
            for a in (m.get("extra", {}) or {}).get("actions", []) or []:
                out.append(a["command"])
    return out


def run_pass(cmds: list[str]) -> list[dict]:
    name = f"exectime-{uuid.uuid4().hex[:8]}"
    cid = subprocess.run(
        ["docker", "run", "-d", "--name", name, "-w", "/testbed", "--rm",
         IMAGE, "sleep", "30m"],
        capture_output=True, text=True, check=True).stdout.strip()
    rows = []
    try:
        for i, cmd in enumerate(cmds, 1):
            argv = ["docker", "exec", "-w", "/testbed"]
            for k, v in ENV.items():
                argv += ["-e", f"{k}={v}"]
            argv += [cid, "bash", "-c", cmd]
            t = time.perf_counter()
            p = subprocess.run(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               errors="replace", timeout=120)
            dt = time.perf_counter() - t
            rows.append({"n": i, "seconds": round(dt, 3),
                         "returncode": p.returncode,
                         "out_chars": len(p.stdout),
                         "command": cmd})
    finally:
        subprocess.run(["docker", "rm", "-f", cid],
                       capture_output=True, text=True)
    return rows


def main():
    args = sys.argv[1:]
    traj = Path(args[0])
    repeat = int(args[args.index("--repeat") + 1]) if "--repeat" in args else 1
    cmds = commands(traj)
    print(f"replaying {len(cmds)} commands x{repeat} in {IMAGE}\n")
    passes = [run_pass(cmds) for _ in range(repeat)]

    merged = []
    for i in range(len(cmds)):
        secs = [p[i]["seconds"] for p in passes]
        r = dict(passes[0][i])
        r["seconds"] = round(min(secs), 3)      # min = least noisy estimate
        r["all_runs"] = secs
        merged.append(r)

    total = sum(r["seconds"] for r in merged)
    print(f"{'#':>3} {'sec':>7} {'%':>6} {'rc':>3} {'out_B':>7}  command")
    for r in merged:
        head = r["command"].split("\n")[0]
        head = (head[:76] + "…") if len(head) > 76 else head
        print(f"{r['n']:>3} {r['seconds']:>7.3f} {r['seconds']/total*100:>5.1f}% "
              f"{r['returncode']:>3} {r['out_chars']:>7}  {head}")
    print(f"\n总执行时间 {total:.2f}s（{len(cmds)} 条命令）")

    if "--json" in args:
        out = Path(args[args.index("--json") + 1])
        out.write_text(json.dumps({"image": IMAGE, "repeat": repeat,
                                   "total_seconds": total, "commands": merged},
                                  indent=2))
        print(f"[wrote {out}]")


if __name__ == "__main__":
    main()
