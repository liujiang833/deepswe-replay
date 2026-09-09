#!/usr/bin/env python3
"""重放 bundle 里的全部 trial，并与随包的基线判定逐条对比。

默认串行，这是刻意的：`replay.py` 采的是 cgroup 的 CPU/内存/IO，两条同时跑会互相
争抢，性能数字直接失去可比性（`crosslang/INDEX.md`「本轮的口径污染」记的就是上次
并发导致 rust/ts/js 三条指标偏悲观、只有 python/go 两条干净）。

`--jobs N` 可以并发，代价是放弃性能数字。所以它与 `--metrics` **互斥且直接报错退出**，
不是警告后继续 —— 静默采到一批看着像真的假数据，比跑不起来危险得多。保真度结论
（patch_identical）和 rc 比对不受并发影响，「先跑通」阶段用 `--jobs` 是划算的。

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
    python3 run_batch.py -j 8                     # 8 条并发（日志只落盘，不实时刷屏）
    python3 run_batch.py -j auto                  # 按本机 CPU / 可用内存自动定并发度
    python3 run_batch.py --smoke 5                # 每条只跑前 5 条命令（冒烟，跳过保真度校验）
    python3 run_batch.py --dry-run                # 只做预检和排程，不真跑
"""

import collections
import argparse
import concurrent.futures as cf
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]

# replay.py 给每条 trial 附带一个 403 sinkhole sidecar 容器，那边写死 `--memory=256m`。
# 算资源账时不能漏掉它：并发 32 就是额外 8GB。
SINK_MB = 256

# `--jobs auto` 的内存宽松系数。`--memory` 是**上限不是预留**，容器不会一启动就占满
# 8GB；重放跑的是编译和测试，实际峰值通常远低于限额。取 4 = 假定实际峰值不超过限额的
# 1/4。这是个假设、不是实测值：调大更激进（更容易撞 OOM），调小更保守。
MEM_OVERCOMMIT = 4.0

# 并发模式下的心跳间隔。并发时没有实时输出，单条又动辄跑几分钟到十几分钟，屏幕全静
# 时分不清「在正常编译」还是「整批卡死」——这批任务被 pnpm install / 网络卡住是有前科的。
# 60s：比最短的单条命令超时（--cmd-timeout 30）长，不会把正常节奏刷成噪音；
# 又短到几小时的批次里任何一次卡死都能在一分钟内看出来。
HEARTBEAT_S = 60


def sh(args):
    return subprocess.run(args, capture_output=True)


def find_replay():
    """replay.py 可能与本脚本同级（打好的 bundle），也可能在上一层（仓库原布局）。"""
    for p in (HERE / "replay.py", HERE.parent / "replay.py"):
        if p.exists():
            return p
    return None


def toml_field(text, key, default):
    """与 replay.py 的 `task_field` 同一个正则、同一个默认值。

    刻意重复而不是 import：两边算的必须是同一个数，资源账才和真正下给 docker 的
    `--cpus/--memory` 对得上；口径一旦分叉，警告就成了误导。
    """
    m = re.search(rf'^{key}\s*=\s*"?([^"\n]+)"?', text, re.M)
    return m.group(1).strip() if m else default


def trial_limits(d):
    """读这条 trial 自己的容器限额。

    限额不是 run_batch 定的，而是随包 task.toml（内嵌在 task.json 的 files 数组里）
    带过来的原 harness 口径，这里只是照抄——所以必须逐条读，不能拿一个常数乘 jobs。
    读不到就按全批实际值兜底：资源账只是给人看的参考，不值得为它让整批跑不起来。
    """
    try:
        task = json.loads((d / "task.json").read_text())
        toml = {f["path"]: f["content"] for f in task["files"]}["task.toml"]
        return float(toml_field(toml, "cpus", "2")), int(float(toml_field(toml, "memory_mb", "8192")))
    except Exception:
        return 2.0, 8192


def meminfo_mb(key):
    """从 /proc/meminfo 读一项，返回 MB。读不到返回 None（资源账少打一行，不报错）。"""
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(key + ":"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
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
        cpus, mem_mb = trial_limits(d)
        out.append({
            "dir": d,
            "name": d.name,
            "lang": meta.get("language", "?"),
            "task_id": meta.get("task_id", "?"),
            "image": (meta.get("image") or {}).get("docker_image", ""),
            "n_commands": meta.get("n_commands"),
            "model": (meta.get("agent") or {}).get("model_name", "?"),
            "cpus": cpus,
            "memory_mb": mem_mb,
            "baseline": json.loads(base.read_text()) if base.exists() else None,
        })
    out.sort(key=lambda t: (LANG_ORDER.index(t["lang"]) if t["lang"] in LANG_ORDER else 99, t["name"]))
    return out


def preflight(trials, need_cgroup, skip_missing=False):
    """跑之前把「一定会失败」的情况先查出来，避免跑到一半才炸。

    skip_missing：全量 113 条各对应一个镜像（若带上 5 条对照组则 118 条、仍 113 个
    镜像），不可能一次全建好。开了它之后
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


def worst_case_limits(trials):
    """并发时按全批最大值估限额。

    哪 N 条会撞在一起是调度决定的，事前不知道；按最大值估才不会低报。
    （实际全批都是 cpus=2 / memory_mb=8192，最大值就是它本身。）
    """
    cpus = max((t["cpus"] for t in trials), default=2.0)
    mem = max((t["memory_mb"] for t in trials), default=8192)
    return cpus, mem


def auto_jobs(trials):
    """`--jobs auto`：取 CPU 与内存两个约束里更紧的那个，返回 (jobs, 推导说明)。

    CPU 侧：os.cpu_count() // 每条 cpus —— 一条一份配额，不超卖。
    内存侧：用 MemAvailable 而不是 MemTotal。page cache 可回收，但已被别的进程真正
    吃掉的内存回不来，MemAvailable 才是真能拿去跑容器的量。再乘 MEM_OVERCOMMIT
    （见文件头常量：`--memory` 是上限不是预留，实际峰值远低于限额）。

    推导说明要打出来让人能复核：auto 是个估算，估错了得看得见是哪一侧卡住的。
    """
    cpus, mem_mb = worst_case_limits(trials)
    per_mb = mem_mb + SINK_MB

    ncpu = os.cpu_count() or 1
    by_cpu = max(1, int(ncpu // cpus))

    avail = meminfo_mb("MemAvailable")
    if avail is None:
        # 读不到 MemAvailable 就只认 CPU 侧，并说清楚——别让人以为内存也算过了
        return by_cpu, f"CPU {ncpu}核/{cpus:g} = {by_cpu}；内存侧未知（读不到 MemAvailable），只按 CPU 定"
    by_mem = max(1, int(avail * MEM_OVERCOMMIT // per_mb))

    jobs = max(1, min(by_cpu, by_mem))
    why = (f"CPU {ncpu}核/{cpus:g} = {by_cpu}；"
           f"内存 {avail}MB可用×{MEM_OVERCOMMIT:g}/{per_mb}MB每条 = {by_mem}；"
           f"取小 → {jobs}")
    return jobs, why


def print_budget(trials, jobs):
    """开跑前把资源账摆出来，超配只警告不拦。

    不拦的理由：现阶段目标是「先跑通」，并发度由跑的人决定；而且限额是上限不是预留，
    超配不等于一定出事。但两侧风险完全不同，必须分开说——见下面的警告文案。
    """
    cpus, mem_mb = worst_case_limits(trials)
    per_mb = mem_mb + SINK_MB
    need_cpu, need_mb = jobs * cpus, jobs * per_mb
    ncpu = os.cpu_count() or 1
    total, avail = meminfo_mb("MemTotal"), meminfo_mb("MemAvailable")

    print(f"资源账    {jobs} × (cpus={cpus:g}, mem={mem_mb}MB + sinkhole {SINK_MB}MB)")
    print(f"          CPU   需 {need_cpu:g} 核配额  /  本机 {ncpu} 核")
    host_mem = f"本机 {total}MB 总" + (f"、{avail}MB 可用" if avail is not None else "")
    print(f"          内存  需 {need_mb}MB  /  {host_mem}" if total is not None
          else f"          内存  需 {need_mb}MB  /  本机内存未知（读不到 /proc/meminfo）")

    if need_cpu > ncpu:
        print(f"\n⚠️  CPU 超配（需 {need_cpu:g} 核 / 本机 {ncpu} 核）：只是变慢，不会坏。")
        print(f"    --cpus 是 CFS 配额不是绑核，超了内核按比例分时，每条各自变慢而已。")
    if avail is not None and need_mb > avail:
        print(f"\n⚠️  内存超配（需 {need_mb}MB / 可用 {avail}MB）：这一侧才会真出事。")
        print(f"    --memory 是上限不是预留，实际占用通常远低于限额，所以超配不等于一定 OOM；")
        print(f"    但只要若干条同时冲高就会触发 OOM kill。被 OOM 掉的容器在重放里表现成")
        print(f"    莫名其妙的命令失败（rc=137，或编译器/测试进程被杀后的一堆次生错误），")
        print(f"    很难和真实的 task 失败区分开。")
        print(f"    → 并发跑时遇到解释不了的失败，先 `dmesg | grep -i oom`，再怀疑 task 本身。")

    if jobs > 1:
        # 并发下 N 个 docker run + N 个 sidecar 同时压 daemon，这一侧没实测过：
        # 是 daemon 扛不住还是 task 本身失败，从重放日志里很难分辨，所以别一上来就顶格。
        print(f"\n提示：首次真跑建议从 -j 4 起步观察，确认 docker daemon 扛得住再往上调"
              f"（并发下 N 个 docker run + N 个 sidecar 同时压 daemon，这一侧尚未实测）。")


def fmt_dur(s):
    """短的用秒（好和 --cmd-timeout 对照），长的用分钟（几小时的批次看秒数没意义）。"""
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f}m"


def heartbeat_loop(hb_stop, lock, running, done, n_total, t_all):
    """并发模式的「还活着」信号：每 HEARTBEAT_S 打一行在跑什么、各跑了多久。

    为什么必须有：并发下没有实时流式输出，唯一的动静是每条跑完那一行。一批 78 条
    跑几小时，开头十几分钟屏幕全静是正常的——但整批卡死时屏幕也是全静的，两者
    在屏幕上长得一模一样。心跳把「在跑几条 + 各跑了多久」摆出来，卡住的那条会
    因为耗时明显长于同伴而自己浮出来。

    用 stop.wait(间隔) 而不是 sleep：主循环一结束就能立刻醒来退出，不用等满一轮。
    线程本身是 daemon，任何情况下都不会拦着进程退出。
    """
    while not hb_stop.wait(HEARTBEAT_S):
        with lock:
            now = time.monotonic()
            # 按已跑时长倒序：卡住的那条排在最前面，一眼能看见
            live = sorted(((now - t0, name) for name, t0 in running.values()), reverse=True)
            n_done = len(done)
            n_fail = sum(1 for r in done.values() if r["exit_code"] != 0)
            head = "  ".join(f"{name} {fmt_dur(dt)}" for dt, name in live[:3])
            more = f"  …等 {len(live) - 3} 条" if len(live) > 3 else ""
            print(f"··· [心跳 +{fmt_dur(now - t_all)}] 在跑 {len(live)} / 完成 {n_done}"
                  f"（失败 {n_fail}）/ 共 {n_total} ｜ {head or '（无）'}{more}", flush=True)


def run_one(idx, t, n_total, replay, out, args, stream):
    """跑一条 trial，返回结果 dict。

    stream=True 只在 --jobs 1 下用：单条要跑几分钟，没有实时输出很难判断是卡住还是在
    编译。并发时 N 条的输出会交错成乱码，所以只落盘——logs/<trial>.log 是排查的唯一依据，
    两种模式下都必须写。
    """
    if stream:
        print("─" * 78)
        print(f"[{idx + 1}/{n_total}] {t['lang']}  {t['name']}")
        print("─" * 78)

    cmd = [sys.executable, str(replay), str(t["dir"]), str(t["dir"] / "task.json"),
           "-o", str(out), "--cmd-timeout", str(args.cmd_timeout)]
    if args.smoke:
        cmd += ["--limit", str(args.smoke)]
    if not args.metrics:
        cmd += ["--no-metrics"]

    # 日志（并发下唯一的排查通道，开场白让人 tail -f 它）链路上有两层缓冲，少拆一层
    # 都不实时：
    #   1. 子进程侧：replay.py 的 stdout 被管道接走时是块缓冲的，攒够 8KB 才吐一次。
    #      实测容器已 Up 半分钟、跑过二十几条命令，日志文件仍是 0 字节。
    #   2. 本进程侧：见下面的 buffering=1。
    # 两种模式都注入 PYTHONUNBUFFERED，串行也不例外：串行号称「实时刷屏」，其实同样
    # 被这 8KB 卡着，等于也在骗人。这个变量只改 replay.py 自己 stdout 的落盘时机，
    # 不碰性能数字的口径（cgroup 采的是容器，--cmd-timeout 管的是容器内的命令），
    # 所以 -j 1 的输出内容仍与从前逐字一致，只是出现得更早。
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    log = out / "logs" / f"{t['name']}.log"
    t0 = time.monotonic()
    # buffering=1 = 行缓冲。默认的块缓冲只在 close() 时落盘，进程被 ^C/kill 掉时
    # 尾巴还会整段丢掉——恰恰是最需要看日志的那一刻。
    with open(log, "w", buffering=1) as lf:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env)
        for line in p.stdout:
            if stream:
                sys.stdout.write(line)
            lf.write(line)
        rc = p.wait()
    dt = time.monotonic() - t0

    vf = out / t["name"] / "verdict.json"
    verdict = json.loads(vf.read_text()) if vf.exists() else None
    return {
        "lang": t["lang"], "trial": t["name"], "task_id": t["task_id"],
        "model": t["model"], "exit_code": rc, "wall_s": round(dt, 1),
        "verdict": verdict, "log": str(log.relative_to(out)),
        "vs_baseline": cmp_baseline(t["baseline"], verdict),
    }


def progress_line(n_done, n_total, r, smoke):
    """并发模式下每条跑完打的那一行：没有实时输出，这是唯一的进度感知。"""
    v = r["verdict"] or {}
    pi = v.get("patch_identical")
    ok = r["exit_code"] == 0 and (smoke or pi is True)
    w = len(str(n_total))
    tail = f"  退出码 {r['exit_code']}" if r["exit_code"] != 0 else ""
    return (f"[{n_done:>{w}}/{n_total}] {'✅' if ok else '❌'} "
            f"{r['lang']:<10} {r['trial']:<45} {r['wall_s']:>7.1f}s  "
            f"patch={'✅' if pi else '❌' if pi is False else '—'}  "
            f"rc={v.get('rc_match', '—')}{tail}")


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
    ap.add_argument("-j", "--jobs", default="1", metavar="N",
                    help="并发跑几条，默认 1（串行，与加本选项前完全一致，含实时输出）。"
                         "auto = 按本机 CPU 与可用内存自动定。不设上限：并发高只是变慢或撞 OOM，"
                         "由跑的人决定，脚本只在开跑前把资源账摆出来。与 --metrics 互斥")
    ap.add_argument("--metrics", action="store_true",
                    help="额外采 cgroup 性能指标。默认不采——打通阶段用不上，"
                         "而且它会引入「必须 cgroup v2 且宿主侧目录可读」这条硬约束")
    ap.add_argument("--dry-run", action="store_true", help="只做预检和排程，不真跑")
    ap.add_argument("--skip-missing", action="store_true",
                    help="镜像还没建好的 trial 直接跳过而不是拒绝启动（全量集边建边跑用）")
    ap.add_argument("--keep-going", action="store_true",
                    help="某条失败后继续跑剩下的（默认遇错即停）。"
                         "并发下「停」= 不再调度新的，已在跑的让它跑完")
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

    if args.jobs == "auto":
        jobs, jobs_why = auto_jobs(trials)
        jobs_src = f"auto → {jobs}（{jobs_why}）"
    else:
        try:
            jobs = int(args.jobs)
        except ValueError:
            print(f"--jobs 只接受正整数或 auto，收到 {args.jobs!r}")
            return 1
        if jobs < 1:
            print(f"--jobs 至少是 1，收到 {jobs}")
            return 1
        jobs_src = "显式指定"

    # 收敛之前先把「用户到底要了多大并发」记下来。下面的互斥报错必须引用这个值：
    # 拿收敛后的数字去说「你指定的 --jobs 2」，而用户敲的明明是 4，人会以为是脚本认错了参数；
    # -j auto 更糟——同一行里既说 2 又说 8，自相矛盾。
    jobs_req = jobs
    jobs_asked = (f"--jobs auto（算出 {jobs_req}）" if args.jobs == "auto"
                  else f"--jobs {args.jobs}（显式指定）")

    # 并发度本身不设上限，但超过待跑条数就只是空转的线程，还会让下面的资源账报出一个
    # 根本不会发生的数字（-j 64 跑 2 条会吓人地写「需 540GB」）。这不是限流，是如实计数。
    # 收敛这件事必须显式打出来，否则看到「并发度 2」会以为自己的 -j 没生效。
    jobs_note = ""
    if jobs > len(trials):
        asked = f"auto 算出 {jobs}，" if args.jobs == "auto" else f"-j {args.jobs} "
        jobs_note = f"（{asked}收敛到待跑条数 {len(trials)}）"
        jobs = len(trials)

    # 并发采指标 = 采一批看着像真的假数据。这里报错退出而不是警告后继续：
    # 这个项目的原则是「静默采错比报错危险」——警告会被日志淹掉，数字却进了 verdict.json。
    # 判的是收敛后的 jobs（真正会同时跑几条才决定争不争抢），说的是用户敲的 jobs_asked。
    if args.metrics and jobs > 1:
        print(f"--metrics 与 {jobs_asked}互斥，拒绝启动。\n"
              f"  并发下 cgroup 采到的是几条 trial 互相争抢之后的数字：CPU 时间被 CFS 按配额\n"
              f"  切碎、内存受另几条挤压、IO 在同一块盘上排队。既不是这条 trial 的真实开销，\n"
              f"  事后也没法校正——采了也不能用，存进 verdict.json 反而会被当成真数据引用。\n"
              f"  （crosslang/INDEX.md「本轮的口径污染」记的就是上次并发的后果。）\n"
              f"  要指标：--jobs 1（或不带 --jobs）；要快：去掉 --metrics。")
        return 1
    if args.metrics and jobs_req > 1:
        # 收敛到 1 之后不会有争抢，指标口径是干净的，所以放行而不是报错。
        # 但用户敲的是一组自相矛盾的参数，静默放行会让人以为 --jobs N 生效了、
        # 事后拿这批数字当「并发采的」去解释——必须说破。
        print(f"提示：{jobs_asked}与 --metrics 本互斥，但待跑只有 {len(trials)} 条，"
              f"并发度已收敛到 1。\n"
              f"  实际是串行跑，不会有争抢，指标口径干净 —— 照常采集，不拦。\n")

    print("=" * 78)
    print(f"bundle    {HERE}")
    print(f"replay.py {replay}")
    print(f"cgroup    {fstype}")
    print(f"内核      {os.uname().release}   CPU {os.cpu_count()}")
    print(f"待跑      {len(trials)} 条（{'串行' if jobs == 1 else f'并发 {jobs}'}）")
    print(f"并发度    {jobs}{jobs_note}   来源 {jobs_src}")
    print(f"指标      {'采集 cgroup 性能数据' if args.metrics else '不采（--metrics 可开）'}")
    print_budget(trials, jobs)
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
            # 空格分隔而非逗号：build_arm.sh 的并列目标是位置参数（`build_arm.sh go python`），
            # 逗号会被当成一个目标名，直接报「认不出目标」。
            # （反过来 run_batch.py 自己的 --only 才吃逗号，两者口径不同，别混。）
            langs = " ".join(sorted({t["lang"] for t in missing}))
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

    if jobs > 1:
        print(f"并发 {jobs} 条：N 条的实时输出会交错成乱码，所以不再流到屏幕，")
        print(f"各条只落盘到 logs/<trial>.log。看某一条的实时进度：")
        print(f"  tail -f {out}/logs/<trial>.log")
        print(f"每条跑完会在这里打一行结果，另外每 {HEARTBEAT_S}s 打一行心跳。\n")

    stream = (jobs == 1)          # 串行下保持原样：实时输出 + 原来的分隔线表头
    stop = threading.Event()      # 置位后不再开新的（已在跑的不打断）
    hb_stop = threading.Event()   # 心跳单独一个：stop 会被首个失败提前置位，心跳不能跟着停
    lock = threading.Lock()
    done = {}                     # idx -> result；按 idx 收，不按完成先后
    running = {}                  # idx -> (trial 名, 起跑时刻)，只给心跳用
    t_all = time.monotonic()

    def worker(idx, t):
        # 检查点放在真正开跑之前：--keep-going 未开时「停」的语义是不再调度新的，
        # 而不是杀掉在跑的——半路杀掉会留下没 teardown 的容器。
        if stop.is_set():
            return
        with lock:
            running[idx] = (t["name"], time.monotonic())
        try:
            r = run_one(idx, t, len(trials), replay, out, args, stream)
        except Exception as e:
            # 单条炸在 run_one 里（比如日志文件写不了）不该把整批带走，记成失败继续
            r = {"lang": t["lang"], "trial": t["name"], "task_id": t["task_id"],
                 "model": t["model"], "exit_code": -1, "wall_s": 0.0,
                 "verdict": None, "log": f"logs/{t['name']}.log",
                 "vs_baseline": None, "error": repr(e)}
        with lock:
            running.pop(idx, None)
            done[idx] = r
            # 收结果、心跳、进度行共用一把锁：并发下两条同时跑完会把行拼在一起
            if stream:
                if r["exit_code"] != 0:
                    print(f"\n❌ 退出码 {r['exit_code']}，详见 {out / r['log']}")
                    if not args.keep_going:
                        print("（--keep-going 可跳过失败项继续）")
                if r["exit_code"] == 0 or args.keep_going:
                    print()
            else:
                print(progress_line(len(done), len(trials), r, bool(args.smoke)), flush=True)
        if r["exit_code"] != 0 and not args.keep_going:
            stop.set()

    # 心跳只在并发下开：-j 1 有实时流式输出，本来就看得见在跑什么，插心跳反而是噪音。
    # daemon=True：无论主循环怎么退出（含 ^C、异常），它都拦不住进程退出。
    hb = None
    if jobs > 1:
        hb = threading.Thread(target=heartbeat_loop, daemon=True,
                              args=(hb_stop, lock, running, done, len(trials), t_all))
        hb.start()

    # 真正的活在子进程里，线程只是在读管道，ThreadPoolExecutor 足够
    pool = cf.ThreadPoolExecutor(max_workers=jobs)
    futs = [pool.submit(worker, i, t) for i, t in enumerate(trials)]
    interrupted = False
    try:
        for f in cf.as_completed(futs):
            f.result()
    except KeyboardInterrupt:
        # Ctrl-C 的 SIGINT 由终端发给整个进程组，replay.py 各自收到后走自己的
        # finally→teardown() 删容器。所以这里只停止调度、等在跑的收尾，
        # 绝不 kill 子进程——SIGKILL 会跳过 teardown，留下孤儿容器。
        interrupted = True
        stop.set()
        for f in futs:
            f.cancel()
        print("\n^C：不再调度新的 trial，等在跑的收尾（容器由各自的 replay.py 清理）……")
    finally:
        pool.shutdown(wait=True)
        # 心跳要活到最后一条收尾为止（^C 后等在跑的那几分钟也要有动静），
        # 所以放在 shutdown 之后停；join 给个上限，绝不因为它卡住汇总。
        hb_stop.set()
        if hb is not None:
            hb.join(timeout=2)

    # 按原 trial 顺序还原：并发下完成先后是乱的，汇总要能和串行跑的报告逐行 diff，
    # 顺序就不能跟着完成时间抖。
    results = [done[i] for i in sorted(done)]
    elapsed = time.monotonic() - t_all
    n_skipped = len(trials) - len(results)
    # 串行下不打这行：上面「（--keep-going 可跳过失败项继续）」已经说过了，而且 -j 1 的
    # 输出要和加并发前逐字一致。^C 是例外——那种情况下必须说清楚少跑了几条。
    if n_skipped and (jobs > 1 or interrupted):
        print(f"\n未跑 {n_skipped} 条：前面有失败且未加 --keep-going（或收到 ^C）。")

    # n_planned / interrupted 必须一路传到产物里：少跑了几条这件事以前只打在 stdout 上，
    # 事后翻 summary.json 只看得到 n_total=2（实际待跑 6），分不清是被 ^C 打断还是环境真坏了。
    write_summary(out, results, elapsed, args, fstype, jobs,
                  n_planned=len(trials), interrupted=interrupted)
    return 0 if results and all(r["exit_code"] == 0 for r in results) else 1


def write_summary(out, results, elapsed, args, fstype, jobs=1, n_planned=None, interrupted=False):
    smoke = bool(args.smoke)
    # 缺省 = 全部跑完了。老调用方（只传 6 个位置参数）因此仍得到 n_not_run=0，不会误报。
    if n_planned is None:
        n_planned = len(results)
    n_not_run = n_planned - len(results)
    not_run_why = ("收到 ^C 后不再调度新的" if interrupted
                   else "前面有失败且未加 --keep-going")
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
    # 并发下总墙钟 ≠ 各条耗时之和，标一句免得被当成串行时长去比
    lines += ["", f"总墙钟 {elapsed:.0f}s" + (f"（并发 {jobs}，非各条之和）" if jobs > 1 else ""),
              f"输出   {out}"]
    if not smoke and any((r["verdict"] or {}).get("patch_identical") is False for r in results):
        lines += ["", "⚠️  有 patch_identical=false —— 环境与原始运行不一致，性能数字不可用。",
                  "    先看该条 logs/*.log 里的 rc 不匹配和时序背离。"]
    text = "\n".join(lines)
    print(text)

    (out / "summary.json").write_text(json.dumps({
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": {"kernel": os.uname().release, "nproc": os.cpu_count(), "cgroup_fstype": fstype},
        "options": {"smoke": args.smoke, "cmd_timeout": args.cmd_timeout,
                    "jobs": jobs, "metrics": bool(args.metrics)},
        "elapsed_s": round(elapsed, 1),
        "n_pass": n_ok, "n_total": len(results),
        # n_total 是「实际跑了几条」（保持原含义不动，下游还在读）；下面四个键回答的是
        # 「这批是不是跑完了」——没有它们就只能靠 n_total 和记忆里的待跑数去猜。
        "n_planned": n_planned, "n_ran": len(results), "n_not_run": n_not_run,
        "interrupted": interrupted,
        "not_run_reason": (not_run_why if n_not_run else None),
        "results": results,
    }, indent=2, ensure_ascii=False))

    md = ["# 重放批次汇总", "",
          f"- 时间（UTC）：{datetime.datetime.now(datetime.timezone.utc).isoformat()}",
          f"- 主机：{os.uname().release} / {os.cpu_count()} CPU / cgroup {fstype}",
          f"- 单命令超时：{args.cmd_timeout}s" + ("（冒烟模式）" if smoke else ""),
          f"- 并发：{jobs}" + ("（串行，性能数字可比）" if jobs == 1
                              else "（并发，未采指标；墙钟不可与串行批次直接比）"),
          f"- 结果：**{n_ok}/{len(results)} 通过**，总墙钟 {elapsed:.0f}s"]
    # 只在真没跑满时插这一行：跑满的批次报告要能和历史报告逐行 diff，不能凭空多一行。
    if n_not_run:
        md.append(f"- ⚠️ **未跑完**：原计划 {n_planned} 条 / 实际跑了 {len(results)} 条 / "
                  f"未启动 {n_not_run} 条（{not_run_why}）。"
                  f"上面的 {n_ok}/{len(results)} 是**已跑那部分**的通过率，不是全批。")
    elif interrupted:
        # ^C 来得晚，所有 trial 其实都已经跑完了。仍要留痕：否则这批报告看起来
        # 和一次干净的完整运行一模一样，没人会知道当时按过 ^C。
        md.append(f"- 收到 ^C，但 {n_planned} 条都已跑完，没有少跑。")
    md.append("")
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
