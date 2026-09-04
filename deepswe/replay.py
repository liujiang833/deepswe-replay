#!/usr/bin/env python3
"""按 trace 在长驻容器里串行重放 agent 的命令序列，采集 per-command 负载指标。

设计见 deepswe/REPLAY_DESIGN.md：
  - 长驻容器 + docker exec 串行（因为存在跨 step 的 /tmp 状态依赖）
  - 不 set -e，失败原样保留（失败是负载的一部分）
  - 资源对齐 task.toml：cpus / memory_mb / allow_internet
  - 结束后 git diff base..HEAD 与 model.patch 逐字节比对（保真度硬校验）

指标采集口径（v2）：
  - cgroup 指标从**宿主侧**直接读文件，不走 docker exec：
      /sys/fs/cgroup/system.slice/docker-<64位容器ID>.scope/
    收益是**消除退出码耦合**（「某个 cat 不存在 → 整个 sh -c 非零 → 已取到的
    cpu/io 被一起丢弃」，v1 全 0 的根因），以及避免采集用的 exec 自身 CPU 被
    计入被测容器。**不是提速**：实测剔掉超时命令后，剩余 422 条 v1 合计 487.5s、
    v2 合计 526.2s，v2 反而慢 7.9%——20ms 内存轮询线程带来的开销（约每条命令
    +20ms）盖过了省下的每条 2 次 exec 往返。
  - 本机 5.15 内核没有 memory.peak（6.8 才有），改用后台线程轮询 memory.current，
    取单条命令执行窗口内的最大值作为 mem_peak。
  - 单命令超时交给**容器内**的 timeout 执行（timeout -k 5 <T>），宿主侧只留一个
    远高于 T 的兜底值防 docker exec 本身挂死。默认 T=30s，对齐原 harness
    （trace 里的 observation 明写 "timed out after 30 seconds"）。

保真度口径（v3，两处对齐原 harness）：
  - **执行器 `/bin/sh -c`（dash），不是 `bash -lc`**。证据是 trace 侧的 observation：
    rust `fd-…__fK6jc93` 的 i=53 / i=56（都含 bash 专有的 `time (...)`）返回
    `{"returncode": 2, "output": "/bin/sh: 1: Syntax error: word unexpected ..."}`
    ——dash 的报错文案，且前缀是全路径 `/bin/sh:`（嵌套调用才是 `sh:`）。
    `bash -lc` 会把这两条**跑成功**，等于重放做了原始运行没做过的功。
    `--interpreter` 可改回（空格分隔解析成 argv）。
  - **网络是「立即回 403 的 sinkhole 代理」，不是纯 `--network=none`**。原环境
    `allow_internet=false` 用的是**主动拒绝的 HTTP 代理**：
      go  `actionlint-…__23b2uyq` i=18  → `<urlopen error Tunnel connection failed: 403 Forbidden>`
      ts  `true-myth-…__BBLS6Fy`  i=34  → `npm error code E403 … 403 Forbidden - GET …/tsx`，rc=0
    `--network=none` 只是「连不上、挂着等」，会让 npm/npx 的重试退避把 30s 预算耗光
    （ts i=34 因此从秒级变成 rc=124），是纯粹多做的功。
    实现：容器仍然 `--network=none`（**绝不可能真的联网**，loopback 仍可用），
    另起一个 **sidecar 容器**用 `--network=container:<主容器>` 加入同一 netns，
    在 127.0.0.1:3128 上跑一个纯 stdlib 的 403 代理（CONNECT 与明文请求都回
    `HTTP/1.1 403 Forbidden`）；主容器通过 `docker run -e` 注入 HTTP(S)_PROXY。
    **用 sidecar 而不是把代理跑在被测容器里**，是因为原 harness 的代理本来就在
    被测容器之外——放进去会把代理自身的 CPU/内存记进被测 cgroup，污染指标。
    sidecar 有独立 cgroup，其开销单独统计进 verdict 的 `sinkhole_*` 字段。
    `--net-mode=none` 可退回旧行为。

用法:
    python3 deepswe/replay.py <trial_dir> <task_json> -o <outdir>
"""

import argparse
import json
import pathlib
import re
import subprocess
import threading
import time
from datetime import datetime

MEM_POLL_S = 0.02      # memory.current 轮询间隔
OUTER_SLACK_S = 60     # 宿主侧兜底超时 = --cmd-timeout + 该值（正常绝不触发）
SINK_PORT = 3128       # sinkhole 代理监听端口（容器 netns 内的 127.0.0.1）

# 纯 stdlib 的 403 sinkhole 代理，作为 `python3 -c` 的实参丢进 sidecar 容器。
# 对一切进来的连接：读完请求头就回 403，然后关闭。两类客户端都能覆到——
#   CONNECT host:443  → http.client 抛 "Tunnel connection failed: 403 Forbidden"
#                       （Node 的 https-proxy-agent 则把这段 403 响应原样重放给
#                        HTTP 解析器，于是 npm 报 E403 / "403 Forbidden - GET <url>"）
#   GET http://…      → 客户端直接拿到一个 403 响应
SINKHOLE_SRC = r"""
import socket, sys, threading
RESP = (b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n\r\n")
def handle(c):
    try:
        c.settimeout(15)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            d = c.recv(4096)
            if not d:
                break
            buf += d
        c.sendall(RESP)
    except OSError:
        pass
    finally:
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        c.close()
port = int(sys.argv[1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(256)
sys.stderr.write("sinkhole up on 127.0.0.1:%d\n" % port)
sys.stderr.flush()
while True:
    try:
        c, _ = s.accept()
    except OSError:
        continue
    threading.Thread(target=handle, args=(c,), daemon=True).start()
"""

# 从被测容器内部探活：既确认代理起来了，也确认两个容器确实共享 netns。
SINK_PROBE = ("import socket\n"
              "s = socket.create_connection(('127.0.0.1', %d), 2)\n"
              "s.close()\n") % SINK_PORT


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, **kw)


def task_field(toml_text, key, default=None):
    m = re.search(rf'^{key}\s*=\s*"?([^"\n]+)"?', toml_text, re.M)
    return m.group(1).strip() if m else default


def strip_cd(c):
    """剥掉 `cd /x && ` 前缀，仅用于统计；重放执行的是原始命令。"""
    return re.sub(r"^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*cd\s+\S+\s*&&\s*", "", c.strip())


class Cgroup:
    """从宿主侧读容器 cgroup v2 累计值，前后做差归因到单条命令（容器内串行，差值即该命令）。"""

    def __init__(self, container):
        cid = sh(["docker", "inspect", "-f", "{{.Id}}", container])
        if cid.returncode != 0:
            raise RuntimeError(f"docker inspect 取容器 ID 失败: {cid.stderr.decode()[:300]}")
        self.cid = cid.stdout.decode().strip()
        self.path = pathlib.Path(f"/sys/fs/cgroup/system.slice/docker-{self.cid}.scope")
        # 不静默降级：路径缺失直接报错退出，否则会像 v1 一样悄悄写出一堆 0
        if not self.path.is_dir():
            raise RuntimeError(f"宿主侧 cgroup 目录不存在: {self.path}\n"
                               f"（cgroup driver 可能不是 systemd，或不是 cgroup v2）")
        for f in ("cpu.stat", "io.stat", "memory.current"):
            if not (self.path / f).exists():
                raise RuntimeError(f"缺少 cgroup 文件: {self.path / f}")

    def _read(self, name):
        try:
            return (self.path / name).read_text()
        except OSError:
            return ""

    def sample(self):
        d = {}
        for line in self._read("cpu.stat").splitlines():
            p = line.split()
            if len(p) == 2 and p[1].isdigit():
                d[p[0]] = int(p[1])
        rb = wb = 0
        # io.stat 每行一个块设备，容器没产生块层 IO 时该文件为空（合法，不是错误）
        for line in self._read("io.stat").splitlines():
            for k, v in re.findall(r"(rbytes|wbytes)=(\d+)", line):
                if k == "rbytes":
                    rb += int(v)
                else:
                    wb += int(v)
        d["rbytes"], d["wbytes"] = rb, wb
        m = self._read("memory.current").strip()
        d["memory_current"] = int(m) if m.isdigit() else 0
        return d


class MemSampler(threading.Thread):
    """后台线程按固定间隔轮询宿主侧 memory.current，取每条命令执行窗口内的最大值。

    代替本机缺失的 memory.peak（6.8 内核才有）。窗口靠 reset()/take() 划定。
    """

    def __init__(self, cgroup, interval=MEM_POLL_S):
        super().__init__(daemon=True)
        self.f = cgroup.path / "memory.current"
        self.interval = interval
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.peak = 0
        self.n = 0

    def _read(self):
        try:
            s = self.f.read_text().strip()
            return int(s) if s.isdigit() else -1
        except OSError:
            return -1

    def _record(self, v):
        if v < 0:
            return
        with self.lock:
            if v > self.peak:
                self.peak = v
            self.n += 1

    def reset(self):
        """开窗：清零并立刻同步采一次，保证极短命令也至少有 1 个样本。"""
        with self.lock:
            self.peak, self.n = 0, 0
        self._record(self._read())

    def take(self):
        """闭窗：返回 (窗口内最大 memory.current, 采样次数)。"""
        with self.lock:
            return self.peak, self.n

    def run(self):
        while not self.stop_evt.is_set():
            self._record(self._read())
            self.stop_evt.wait(self.interval)

    def stop(self):
        self.stop_evt.set()


def load_trace(traj):
    """把 trace 摊平成 [{命令, trace 侧 returncode, 是否哨兵, 是否 trace 侧超时, step 时间差上界}]。

    末条 `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 的 observation 是
    {"returncode": -1, "exception_info": "action was not executed"}——原本就没执行过，
    重放时跳过。判据用 observation 文本而非命令文本，避免硬编码。
    """
    steps = [s for s in traj["steps"] if s.get("tool_calls")]
    items = []
    for si, s in enumerate(steps):
        results = (s.get("observation") or {}).get("results") or []
        for ti, tc in enumerate(s["tool_calls"]):
            content = results[ti]["content"] if ti < len(results) else ""
            m = re.search(r'"returncode":\s*(-?\d+)', content)
            # trace 里没有 per-command 执行耗时，只有 step 时间戳。实测（对齐 14 条
            # 长命令的重放耗时）step 的 timestamp 记的是**该 step 执行完之后**的时刻，
            # 所以归属于命令 i 的时间差是 ts[i] - ts[i-1]，其中还含 step i 的 LLM 推理
            # 时间，因此只能当上界用（字段名已标注 upper）。
            gap = None
            if si > 0:
                try:
                    t0 = datetime.fromisoformat(steps[si - 1]["timestamp"]).timestamp()
                    t1 = datetime.fromisoformat(steps[si]["timestamp"]).timestamp()
                    gap = round(t1 - t0, 1)
                except (ValueError, KeyError):
                    gap = None
            items.append({
                "cmd": tc["arguments"].get("command", ""),
                "trace_rc": int(m.group(1)) if m else None,
                "sentinel": "action was not executed" in content,
                "trace_timed_out": "timed out after" in content,
                "trace_step_gap_s_upper": gap,
            })
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trial_dir", help="含 trajectory.json / model.patch 的目录")
    ap.add_argument("task_json", help="tasks/<task_id>.json")
    ap.add_argument("-o", "--outdir", default="deepswe/replay_out")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条命令（冒烟用）")
    ap.add_argument("--keep", action="store_true", help="结束后保留容器")
    ap.add_argument("--cmd-timeout", type=int, default=30,
                    help="单条命令在容器内的超时秒数，默认 30（对齐原 harness）")
    ap.add_argument("--interpreter", default="/bin/sh -c",
                    help="执行器 argv（空格分隔），默认 '/bin/sh -c'（dash，对齐原 harness）；"
                         "旧口径是 'bash -lc'")
    ap.add_argument("--net-mode", choices=("sinkhole", "none"), default="sinkhole",
                    help="sinkhole=容器仍 --network=none，另起 sidecar 在 127.0.0.1 提供"
                         "「一切请求立即 403」的代理并注入 HTTP(S)_PROXY（默认，对齐原 harness）；"
                         "none=旧口径，纯无网卡（连接会挂着等到超时）")
    args = ap.parse_args()
    interp = args.interpreter.split()
    if not interp:
        ap.error("--interpreter 不能为空")

    tdir = pathlib.Path(args.trial_dir)
    traj = json.loads((tdir / "trajectory.json").read_text())
    task = json.loads(pathlib.Path(args.task_json).read_text())
    files = {f["path"]: f["content"] for f in task["files"]}
    toml = files["task.toml"]

    image = task_field(toml, "docker_image")
    base_sha = task_field(toml, "base_commit_hash")
    cpus = task_field(toml, "cpus", "2")
    mem_mb = task_field(toml, "memory_mb", "8192")
    allow_net = task_field(toml, "allow_internet", "false") == "true"

    items = load_trace(traj)
    n_all = len(items)
    if args.limit:
        items = items[: args.limit]
    todo = [(i, it) for i, it in enumerate(items) if not it["sentinel"]]
    n_skipped = len(items) - len(todo)

    out = pathlib.Path(args.outdir) / tdir.name
    out.mkdir(parents=True, exist_ok=True)
    name = f"replay_{tdir.name}"[:60].replace(".", "_")
    sink = name + "-sink"

    # allow_internet=true 的 task 本来就该真联网，sinkhole 只在断网口径下生效
    use_sink = (not allow_net) and args.net_mode == "sinkhole"
    proxy_url = f"http://127.0.0.1:{SINK_PORT}"

    print(f"image     {image}")
    print(f"base_sha  {base_sha}")
    print(f"limits    cpus={cpus} mem={mem_mb}MB net="
          f"{'on' if allow_net else ('none+403-sinkhole' if use_sink else 'none')}")
    print(f"exec      {' '.join(interp)}")
    print(f"commands  {n_all} 条，跳过哨兵 {n_skipped} 条 → 实际重放 {len(todo)} 条")
    print(f"timeout   容器内 timeout -k 5 {args.cmd_timeout}（宿主兜底 {args.cmd_timeout + OUTER_SLACK_S}s）")

    sh(["docker", "rm", "-f", name])
    sh(["docker", "rm", "-f", sink])
    run = ["docker", "run", "-d", "--name", name,
           f"--cpus={cpus}", f"--memory={mem_mb}m", f"--memory-swap={mem_mb}m"]
    if not allow_net:
        run += ["--network=none"]
    if use_sink:
        # 镜像 config 的 Env 里本来没有代理变量——原 harness 也是运行时注入的
        for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            run += ["-e", f"{v}={proxy_url}"]
    run += [image, "sleep", "infinity"]
    r = sh(run)
    if r.returncode != 0:
        print("容器启动失败:", r.stderr.decode()[:400])
        return 1

    def teardown():
        sh(["docker", "rm", "-f", name])
        sh(["docker", "rm", "-f", sink])

    sink_cg = None
    if use_sink:
        # sidecar 与主容器共享 netns（--network=container:），但 cgroup 各自独立，
        # 所以代理自身的 CPU/内存**不会**记进被测容器的指标。
        r = sh(["docker", "run", "-d", "--name", sink,
                f"--network=container:{name}", "--memory=256m",
                image, "python3", "-c", SINKHOLE_SRC, str(SINK_PORT)])
        if r.returncode != 0:
            print("sinkhole sidecar 启动失败:", r.stderr.decode()[:400])
            teardown()
            return 1
        # 从被测容器内部探活，失败就直接退出——不静默降级成「其实没代理」
        for _ in range(40):
            if sh(["docker", "exec", name, "python3", "-c", SINK_PROBE]).returncode == 0:
                break
            time.sleep(0.25)
        else:
            print(f"sinkhole 探活失败：被测容器连不上 127.0.0.1:{SINK_PORT}")
            print(sh(["docker", "logs", sink]).stderr.decode()[:400])
            teardown()
            return 1
        try:
            sink_cg = Cgroup(sink)
        except RuntimeError as e:
            print(f"sinkhole cgroup 初始化失败：{e}")
            teardown()
            return 1
        print(f"sinkhole  {proxy_url}（sidecar 容器 {sink}，独立 cgroup）")

    try:
        cg = Cgroup(name)
    except RuntimeError as e:
        print(f"cgroup 初始化失败：{e}")
        teardown()
        return 1
    print(f"cgroup    {cg.path}")

    mem = MemSampler(cg)
    mem.start()
    print(f"mem       后台轮询 memory.current @ {int(MEM_POLL_S * 1000)}ms\n")

    recs = []
    sink_cpu0 = sink_cg.sample()["usage_usec"] if sink_cg else 0
    t_start = time.monotonic()
    try:
        for i, it in todo:
            cmd = it["cmd"]
            before = cg.sample()
            mem.reset()
            t0 = time.monotonic()
            try:
                p = subprocess.run(
                    ["docker", "exec", "-w", "/app", name,
                     "timeout", "-k", "5", str(args.cmd_timeout)] + interp + [cmd],
                    capture_output=True, timeout=args.cmd_timeout + OUTER_SLACK_S)
                rc, so, se, outer = p.returncode, p.stdout, p.stderr, False
            except subprocess.TimeoutExpired as e:
                rc, so, se, outer = -9, (e.stdout or b""), (e.stderr or b""), True
            dt = time.monotonic() - t0
            mem_peak, mem_n = mem.take()
            after = cg.sample()

            def d(k):
                return after.get(k, 0) - before.get(k, 0)

            # timeout 命令：TERM 超时退 124；-k 之后被 KILL 退 137
            timed_out = rc in (124, 137)

            rec = {"i": i, "rc": rc, "timed_out": timed_out, "trace_rc": it["trace_rc"],
                   "wall_s": round(dt, 4), "outer_timeout": outer,
                   "stdout_bytes": len(so), "stderr_bytes": len(se),
                   "user_usec": d("user_usec"), "system_usec": d("system_usec"),
                   "usage_usec": d("usage_usec"),
                   "rbytes": d("rbytes"), "wbytes": d("wbytes"),
                   "mem_peak": mem_peak, "mem_samples": mem_n,
                   "cmd": cmd, "cmd_stripped": strip_cd(cmd),
                   "stdout_head": so[:4000].decode(errors="replace")}
            recs.append(rec)
            if i % 25 == 0 or dt > 10:
                head = strip_cd(cmd)[:88].splitlines()
                print(f"  [{i:>4}/{n_all}] rc={rc:<4}{'TO' if timed_out else '  '} {dt:>7.2f}s  "
                      f"{head[0] if head else ''}")

        elapsed = time.monotonic() - t_start
        print(f"\n重放完成 {elapsed:.0f}s")

        # ---- 保真度硬校验：git diff base..HEAD vs model.patch ----
        gd = sh(["docker", "exec", "-w", "/app", name, "git", "diff", "--binary", base_sha, "HEAD"])
        replayed = gd.stdout
        (out / "replayed.patch").write_bytes(replayed)
        orig_p = tdir / "model.patch"
        verdict = {"elapsed_s": round(elapsed, 1),
                   "cmd_timeout_s": args.cmd_timeout,
                   "interpreter": interp,
                   "net_mode": "internet" if allow_net else ("sinkhole403" if use_sink else "none"),
                   "n_cmds_trace": n_all, "n_skipped_sentinel": n_skipped,
                   "n_replayed": len(recs),
                   "replayed_patch_bytes": len(replayed)}
        if sink_cg:
            # 代理自身开销：独立 cgroup，未计入被测容器
            sk = sink_cg.sample()
            verdict["sinkhole_cpu_usec_during_replay"] = sk["usage_usec"] - sink_cpu0
            verdict["sinkhole_cpu_usec_total"] = sk["usage_usec"]
            verdict["sinkhole_mem_current"] = sk["memory_current"]
            print(f"  sinkhole 开销  CPU {(sk['usage_usec'] - sink_cpu0) / 1e6:.3f}s"
                  f"（全生命周期 {sk['usage_usec'] / 1e6:.3f}s）"
                  f" / RSS {sk['memory_current'] / 1e6:.1f}MB —— 独立 cgroup，未计入被测指标")
        if orig_p.exists() and not args.limit:
            orig = orig_p.read_bytes()
            verdict["model_patch_bytes"] = len(orig)
            verdict["patch_identical"] = (replayed == orig)
            print(f"\n=== 保真度硬校验 ===")
            print(f"  重放 patch  {len(replayed):>9,}B")
            print(f"  原始 patch  {len(orig):>9,}B")
            print(f"  逐字节一致  {'✅ 是' if replayed == orig else '❌ 否'}")
        else:
            print("\n（--limit 模式或缺 model.patch，跳过硬校验）")

        # ---- 软校验：rc 序列与 trace 记录逐条比对（哨兵已排除在 recs 之外）----
        # 两个口径都保留：
        #   rc_match          严格逐值比对
        #   rc_match_semantic trace 侧 -1（被原 harness 打死）与重放侧 timed_out
        #                     （容器内 timeout 打死，rc 124/137）行为一致，视为匹配
        same, same_sem, mism = 0, 0, []
        for r in recs:
            strict = (r["trace_rc"] == r["rc"])
            semantic = strict or (r["trace_rc"] == -1 and r["timed_out"])
            if strict:
                same += 1
            else:
                mism.append({"i": r["i"], "trace_rc": r["trace_rc"], "replay_rc": r["rc"],
                             "timed_out": r["timed_out"], "wall_s": r["wall_s"],
                             "semantic_match": semantic,
                             "cmd": r["cmd"][:100]})
            if semantic:
                same_sem += 1
        verdict["rc_match"] = f"{same}/{len(recs)}"
        verdict["rc_match_semantic"] = f"{same_sem}/{len(recs)}"
        verdict["rc_mismatches"] = mism
        print(f"  rc 序列一致 {same}/{len(recs)}（严格）"
              f" / {same_sem}/{len(recs)}（语义：trace -1 == 重放 timed_out）")

        # ---- 时序背离：重放机比原机慢导致的超时口径差（预期存在，不是 bug）----
        div = []
        for r in recs:
            it = items[r["i"]]
            a = r["timed_out"] and it["trace_rc"] != -1          # 重放超时、trace 正常
            b = it["trace_rc"] == -1 and not r["timed_out"]      # trace 超时、重放跑完
            if a or b:
                div.append({"i": r["i"],
                            "kind": "replay_timeout_only" if a else "trace_timeout_only",
                            "cmd": r["cmd"][:100],
                            "replay_wall_s": r["wall_s"], "replay_rc": r["rc"],
                            "trace_rc": it["trace_rc"],
                            "trace_step_gap_s_upper": it["trace_step_gap_s_upper"]})
        verdict["timing_divergences"] = div
        print(f"  时序背离    {len(div)} 条")

        (out / "commands.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs))
        (out / "verdict.json").write_text(json.dumps(verdict, indent=2, ensure_ascii=False))
        print(f"\n-> {out}/commands.jsonl, verdict.json, replayed.patch")
    finally:
        mem.stop()
        if not args.keep:
            teardown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
