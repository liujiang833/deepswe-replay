#!/usr/bin/env python3
"""Compare ARM topdown counts for one process, with and without perf -G."""

import argparse
import errno
import hashlib
import json
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone


HERE = pathlib.Path(__file__).resolve().parent
CROSSLANG = HERE.parent
sys.path.insert(0, str(CROSSLANG))
from topdown_parse import EV_BE, EV_TOT, L1_REQUIRED, extra_events, parse_conf  # noqa: E402


PERF_GATE = """
import pathlib, sys
root = pathlib.Path(sys.argv[1])
(root / 'perf_ready').write_text('ready\\n')
with (root / 'perf_stop.fifo').open('rb', buffering=0) as fifo:
    if not fifo.read(1):
        raise SystemExit('perf stop FIFO closed without a signal')
"""


def command(argv, **kwargs):
    result = subprocess.run(argv, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, argv))}\n{result.stdout}{result.stderr}")
    return result.stdout.strip()


def cpu_ids(spec):
    result = set()
    for field in spec.strip().split(","):
        if not field:
            continue
        if "-" in field:
            lo, hi = map(int, field.split("-", 1))
            result.update(range(lo, hi + 1))
        else:
            result.add(int(field))
    return result


def chosen_cpu(requested, pmu):
    allowed = os.sched_getaffinity(0)
    pmu_cpus = None
    if pmu:
        cpus_file = pathlib.Path("/sys/bus/event_source/devices") / pmu / "cpus"
        if cpus_file.exists():
            pmu_cpus = cpu_ids(cpus_file.read_text())
    candidates = allowed & pmu_cpus if pmu_cpus else allowed
    if not candidates:
        raise RuntimeError(f"当前进程可用 CPU {sorted(allowed)} 与 PMU {pmu} 的 CPU 不相交")
    if requested is not None:
        if requested not in candidates:
            raise RuntimeError(f"CPU {requested} 不在可用集合 {sorted(candidates)} 中")
        return requested
    return min(candidates)


def topdown_events(conf_path):
    conf = parse_conf(str(conf_path))
    pmu = (conf.get("PMU") or "").strip()
    if not pmu:
        choices = sorted(p.name for p in pathlib.Path("/sys/bus/event_source/devices").glob("armv8*"))
        if not choices:
            raise RuntimeError("没有找到 armv8* PMU；完整 topdown 对照须在 ARM 服务器上运行")
        pmu = choices[0]
    pmu_dir = pathlib.Path("/sys/bus/event_source/devices") / pmu
    if not pmu_dir.is_dir():
        raise RuntimeError(f"PMU 不存在：{pmu_dir}")
    slots_text = (conf.get("SLOTS") or "").strip()
    if not slots_text:
        slots_text = (pmu_dir / "caps/slots").read_text().strip()
    slots = int(slots_text, 16 if slots_text.lower().startswith("0x") else 10)
    if slots <= 0:
        raise RuntimeError(f"无效 SLOTS：{slots_text!r}")

    pairs = [(name, (conf.get(key) or "").strip()) for key, name, _ in L1_REQUIRED]
    for key, name, _ in (EV_BE, EV_TOT):
        if (conf.get(key) or "").strip():
            pairs.append((name, conf[key].strip()))
    pairs.extend(extra_events(conf))
    for name, code in pairs:
        if not re.fullmatch(r"0[xX][0-9a-fA-F]+", code):
            raise RuntimeError(f"topdown.conf 中 {name} 的事件号无效：{code!r}")
    event_spec = "{" + ",".join(
        f"{pmu}/event={code},name={name}/" for name, code in pairs) + "}"
    return pmu, slots, event_spec


def cgroup_name(pid):
    lines = pathlib.Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    fs_type = command(["stat", "-fc", "%T", "/sys/fs/cgroup"])
    if fs_type == "cgroup2fs":
        paths = [s.split(":", 2)[2] for s in lines if s.startswith("0::")]
        base = pathlib.Path("/sys/fs/cgroup")
    else:
        paths = [parts[2] for s in lines if len(parts := s.split(":", 2)) == 3
                 and "perf_event" in parts[1].split(",")]
        base = pathlib.Path("/sys/fs/cgroup/perf_event")
    if not paths:
        raise RuntimeError(f"无法从 /proc/{pid}/cgroup 找到 perf cgroup：{lines}")
    relative = paths[0].lstrip("/")
    if not relative or not (base / relative).is_dir():
        raise RuntimeError(f"perf cgroup 不存在：{base / relative}")
    return relative


def wait_file(path, timeout_s, processes=()):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        for proc in processes:
            if proc and proc.poll() is not None:
                raise RuntimeError(f"进程提前退出（rc={proc.returncode}），等待文件 {path}")
        time.sleep(0.02)
    raise TimeoutError(f"等待 {path} 超过 {timeout_s}s")


def signal_fifo(path, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
            time.sleep(0.02)
            continue
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)
        return
    raise TimeoutError(f"等待 FIFO 读取端超过 {timeout_s}s：{path}")


def compile_binary(output):
    source = HERE / "matmul.c"
    flags = ["gcc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-static"]
    attempt = subprocess.run(flags + ["-o", str(output), str(source)],
                             capture_output=True, text=True)
    if attempt.returncode:
        print("静态链接不可用，改用动态链接并检查镜像兼容性", flush=True)
        command(flags[:-1] + ["-o", str(output), str(source)])
    return "static" if attempt.returncode == 0 else "dynamic"


def docker_image_check(image, binary):
    command(["docker", "image", "inspect", image])
    # 直接运行一个很小的矩阵，确认宿主编译的二进制在镜像内可执行。
    command(["docker", "run", "--rm", "--network=none", "--mount",
             f"type=bind,src={binary},dst=/matmul,readonly",
             "--entrypoint", "/matmul", image, "16"])


def perf_prefix():
    if os.geteuid() != 0:
        raise RuntimeError("完整 perf 采集请以 root 运行 demo（--smoke 无需 root）")
    version = subprocess.run(["perf", "--version"], text=True, capture_output=True)
    if version.returncode or not re.search(r"perf version \d", version.stdout):
        raise RuntimeError("perf 不可用：" + (version.stdout + version.stderr).strip())
    return ["perf"]


def run_case(mode, repeat, root, binary, image, size, cpu, timeout_s,
             perf_cmd, event_spec, slots, conf_path, smoke):
    label = f"{repeat:02d}-{mode}"
    case_dir = root / label
    case_dir.mkdir()
    os.mkfifo(case_dir / "go.fifo", 0o666)
    os.mkfifo(case_dir / "finish.fifo", 0o666)
    os.mkfifo(case_dir / "perf_stop.fifo", 0o666)
    for fifo in ("go.fifo", "finish.fifo", "perf_stop.fifo"):
        (case_dir / fifo).chmod(0o666)
    shutil.copy2(binary, case_dir / "matmul")

    native = collector = None
    container = None
    log = (case_dir / "workload.log").open("w")
    try:
        if mode == "native_pid":
            env = os.environ.copy()
            env["DEMO_CONTROL_DIR"] = str(case_dir)
            native = subprocess.Popen([str(case_dir / "matmul"), str(size)],
                                      env=env, stdout=log, stderr=subprocess.STDOUT)
            os.sched_setaffinity(native.pid, {cpu})
            pid = native.pid
        else:
            container = f"perf_cgroup_demo_{os.getpid()}_{repeat}_{mode}"
            argv = ["docker", "run", "-d", "--name", container, "--network=none",
                    "--cpuset-cpus", str(cpu), "--user", f"{os.getuid()}:{os.getgid()}",
                    "--mount", f"type=bind,src={case_dir},dst=/bench",
                    "-e", "DEMO_CONTROL_DIR=/bench", "--entrypoint", "/bench/matmul",
                    image, str(size)]
            command(argv)
            pid = int(command(["docker", "inspect", "-f", "{{.State.Pid}}", container]))
            if pid <= 0:
                raise RuntimeError(f"容器 {container} 未正常启动")

        wait_file(case_dir / "ready", timeout_s, (native,))
        actual_affinity = os.sched_getaffinity(pid)
        if actual_affinity != {cpu}:
            raise RuntimeError(f"{mode} 的实际 CPU 亲和性是 {sorted(actual_affinity)}，期望 [{cpu}]")
        cg = cgroup_name(pid) if mode == "docker_cgroup" and not smoke else None

        if not smoke:
            raw = case_dir / "perf.csv"
            opts = ["-x,", "-o", str(raw), "-e", event_spec]
            opts += ["-a", "-G", cg] if cg else ["-p", str(pid)]
            cmd = perf_cmd + ["stat"] + opts + ["--", sys.executable, "-c", PERF_GATE, str(case_dir)]
            (case_dir / "perf_argv.json").write_text(json.dumps(cmd, indent=2) + "\n")
            with (case_dir / "perf.stderr").open("w") as err:
                collector = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
                wait_file(case_dir / "perf_ready", timeout_s, (collector, native))

        signal_fifo(case_dir / "go.fifo", timeout_s)
        wait_file(case_dir / "result.json", timeout_s, (collector, native))
        result = json.loads((case_dir / "result.json").read_text())

        if collector:
            signal_fifo(case_dir / "perf_stop.fifo", timeout_s)
            collector.wait(timeout=timeout_s)
            if collector.returncode:
                raise RuntimeError(f"perf 失败（rc={collector.returncode}）："
                                   + (case_dir / "perf.stderr").read_text()[:1000])
        signal_fifo(case_dir / "finish.fifo", timeout_s)
        if native:
            native.wait(timeout=timeout_s)
            if native.returncode:
                raise RuntimeError(f"原生 workload 退出码 {native.returncode}")
        else:
            rc = int(command(["docker", "wait", container], timeout=timeout_s))
            if rc:
                raise RuntimeError(f"容器 workload 退出码 {rc}：{command(['docker', 'logs', container])}")
            (case_dir / "workload.log").write_text(command(["docker", "logs", container]) + "\n")

        record = {"mode": mode, "repeat": repeat, "pid": pid, "cgroup": cg,
                  "cpu_affinity": sorted(actual_affinity), "size": size,
                  "checksum": result["checksum"],
                  "elapsed_s": result["elapsed_s"], "artifacts": str(case_dir)}
        if not smoke:
            output = case_dir / "topdown.json"
            parsed = subprocess.run([sys.executable, str(CROSSLANG / "topdown_parse.py"),
                                     str(case_dir / "perf.csv"), "--conf", str(conf_path),
                                     "--slots", str(slots), "--json-out", str(output),
                                     "--title", f"perf cgroup demo: {mode}"],
                                    text=True, capture_output=True)
            (case_dir / "topdown.log").write_text(parsed.stdout + parsed.stderr)
            if not output.exists():
                raise RuntimeError(f"topdown 解析失败：{parsed.stdout}{parsed.stderr}")
            data = json.loads(output.read_text())
            record.update(parse_rc=parsed.returncode,
                          events={k: v.get("value") for k, v in data.get("events", {}).items()},
                          pcnt_running={k: v.get("pcnt_running") for k, v in data.get("events", {}).items()},
                          topdown=data.get("topdown"), checks=data.get("checks"))
        return record
    finally:
        if collector and collector.poll() is None:
            collector.terminate()
            try:
                collector.wait(timeout=5)
            except subprocess.TimeoutExpired:
                collector.kill()
                collector.wait()
        if native and native.poll() is None:
            native.terminate()
            try:
                native.wait(timeout=5)
            except subprocess.TimeoutExpired:
                native.kill()
                native.wait()
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        log.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", default="ubuntu:24.04", help="已在本机的同架构 Docker 镜像")
    ap.add_argument("--size", type=int, default=960, help="方阵边长，默认 960（当前开发机约 0.9s）")
    ap.add_argument("--repeats", type=int, default=3, help="每种采法重复次数，默认 3")
    ap.add_argument("--cpu", type=int, help="固定逻辑 CPU；默认取目标 PMU 的首个可用 CPU")
    ap.add_argument("--conf", type=pathlib.Path, default=CROSSLANG / "topdown.conf")
    ap.add_argument("--outdir", type=pathlib.Path,
                    default=HERE / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    ap.add_argument("--timeout", type=int, default=120, help="每次运行的最大等待秒数")
    ap.add_argument("--smoke", action="store_true", help="只验编译和原生/Docker 工作流；不调用 perf")
    args = ap.parse_args()
    if not 16 <= args.size <= 1024 or args.repeats < 1 or args.timeout < 1:
        ap.error("--size 须为 16~1024，--repeats 和 --timeout 须为正数")
    args.outdir.mkdir(parents=True, exist_ok=False)
    root = args.outdir.resolve()
    binary = root / "matmul"
    linkage = compile_binary(binary)
    docker_image_check(args.image, binary)

    pmu = slots = event_spec = None
    perf_cmd = []
    if not args.smoke:
        pmu, slots, event_spec = topdown_events(args.conf)
        perf_cmd = perf_prefix()
    cpu = chosen_cpu(args.cpu, pmu)
    print(f"输出 {root}\n镜像 {args.image}；二进制 {linkage}；CPU {cpu}；矩阵 {args.size}x{args.size}", flush=True)
    if event_spec:
        print(f"PMU {pmu}；SLOTS {slots}；事件组 {event_spec}", flush=True)

    modes = ["native_pid", "docker_pid", "docker_cgroup"] if not args.smoke else ["native_pid", "docker_pid"]
    records = []
    summary = {"host_arch": os.uname().machine, "image": args.image,
               "size": args.size, "repeats": args.repeats, "cpu": cpu,
               "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "linkage": linkage, "pmu": pmu, "slots": slots, "event_spec": event_spec,
               "smoke": args.smoke, "runs": records}
    try:
        for rep in range(1, args.repeats + 1):
            order = modes if rep % 2 else list(reversed(modes))
            for mode in order:
                print(f"[{rep}/{args.repeats}] {mode} …", end=" ", flush=True)
                rec = run_case(mode, rep, root, binary, args.image, args.size, cpu,
                               args.timeout, perf_cmd, event_spec, slots, args.conf, args.smoke)
                records.append(rec)
                (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                print(f"{rec['elapsed_s']:.3f}s checksum={rec['checksum']}"
                      + (f" parse_rc={rec['parse_rc']}" if not args.smoke else ""), flush=True)
        if len({r["checksum"] for r in records}) != 1:
            raise RuntimeError("原生与容器的 checksum 不一致，计数不可比较")
        print("\n三层循环的 checksum 全部一致。")
        if not args.smoke:
            names = sorted({name for r in records for name in r["events"]})
            comparison = {"events": {}, "topdown": {}}
            print("事件计数中位数（同一二进制、同一 CPU）：")
            for name in names:
                values = {mode: [r["events"].get(name) for r in records if r["mode"] == mode]
                          for mode in modes}
                if any(not v or any(x is None for x in v) for v in values.values()):
                    print(f"  {name:22s} 缺少有效计数；详见各次 perf.csv / topdown.log")
                    continue
                by_mode = {mode: statistics.median(values[mode]) for mode in modes}
                base = by_mode["docker_pid"]
                delta = (by_mode["docker_cgroup"] / base - 1) * 100 if base else None
                spread = {mode: (max(values[mode]) - min(values[mode])) / by_mode[mode] * 100
                          if by_mode[mode] else None for mode in modes}
                comparison["events"][name] = {"medians": by_mode,
                                               "range_pct": spread,
                                               "cgroup_vs_docker_pid_pct": delta}
                delta_text = f"{delta:+.2f}%" if delta is not None else "n/a"
                print(f"  {name:22s} native={by_mode['native_pid']:>14,.0f}"
                      f"  docker-pid={base:>14,.0f}  docker-cgroup={by_mode['docker_cgroup']:>14,.0f}"
                      f"  cgroup/pid={delta_text}")
            print("Topdown 四象限中位数：")
            for name in ("Retiring", "BadSpec", "FrontendBound", "BackendBound"):
                values = {mode: [(r.get("topdown") or {}).get(name) for r in records if r["mode"] == mode]
                          for mode in modes}
                if any(not v or any(x is None for x in v) for v in values.values()):
                    print(f"  {name:22s} 缺少有效比例；详见各次 topdown.log")
                    continue
                by_mode = {mode: statistics.median(values[mode]) for mode in modes}
                delta_pp = (by_mode["docker_cgroup"] - by_mode["docker_pid"]) * 100
                spread_pp = {mode: (max(values[mode]) - min(values[mode])) * 100
                             for mode in modes}
                comparison["topdown"][name] = {"medians": by_mode,
                                                "range_pp": spread_pp,
                                                "cgroup_vs_docker_pid_pp": delta_pp}
                print(f"  {name:22s} native={by_mode['native_pid']*100:>6.2f}%"
                      f"  docker-pid={by_mode['docker_pid']*100:>6.2f}%"
                      f"  docker-cgroup={by_mode['docker_cgroup']*100:>6.2f}%"
                      f"  差={delta_pp:+.2f}pp")
            summary["comparison"] = comparison
            bad = [r for r in records if r.get("parse_rc") != 0]
            if bad:
                print(f"⚠️  {len(bad)} 次 topdown 自检未通过；查看各次 topdown.log 和 pcnt_running。")
            (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            if bad or len(comparison["events"]) < len(names):
                return 2
        print(f"详细结果：{root / 'summary.json'}")
        return 0
    except Exception:
        (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, TimeoutError, subprocess.TimeoutExpired) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
