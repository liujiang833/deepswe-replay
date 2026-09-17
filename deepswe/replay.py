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

可移植性（v4，为「拷到另一台机器上跑」而改，不改任何指标口径）：
  - **cgroup 目录改为三级探测**，不再硬编码。原先写死 systemd driver 的
    `/sys/fs/cgroup/system.slice/docker-<id>.scope`，换台机器就 raise 退出——
    本机实测 driver 是 `cgroupfs`，实际落点是 `/sys/fs/cgroup/docker/<id>`，
    旧路径 `is_dir()` 直接为 False。现在依次试：`/proc/<pid>/cgroup`（内核自报，
    最准，但 dockerd 在别的 pid namespace 时不可用）→ 四条已知 driver 候选
    （cgroupfs / systemd / rootless 两种）→ cgroup 树搜索。全失败时把
    `/sys/fs/cgroup` 实际类型与试过的路径一并抛出，便于在服务器上定位。
  - `io.stat` 缺失（io 控制器没启用）从「退出」降级为「rbytes/wbytes 记 0」——
    它不影响 patch_identical 与 rc_match 这两个结论。
  - **容器名带 PID**。原先只按 trial 名推导且启动前无条件 `docker rm -f`，
    两个进程重放同一条 trial 会静默互删容器（crosslang/INDEX.md「已知风险」）。
    现在同前缀存量容器会被检测出来并拒绝启动，`--force` 可越过。
  - **镜像不在本地时默认拒绝启动**（`--allow-pull` 越过）：实测出口吞吐 0.27 MB/s，
    误触一个 ~800MB 的镜像就是几十分钟。
  - verdict 里增记 `host`（内核 / cgroup 目录与探测方式 / CPU 数 / 镜像），
    跨机比性能数字时这些是前提。

用法:
    python3 deepswe/replay.py <trial_dir> <task_json> -o <outdir>

    批量跑 crosslang 那 5 条并与基线对比，用 crosslang/run_batch.py；
    服务器上的完整流程见 crosslang/RUNBOOK.md。
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import threading
import time
from datetime import datetime

MEM_POLL_S = 0.02      # memory.current 轮询间隔
OUTER_SLACK_S = 60     # 宿主侧兜底超时 = --cmd-timeout + 该值（正常绝不触发）
SINK_PORT = 3128       # sinkhole 代理监听端口（容器 netns 内的 127.0.0.1）
CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")

# 单条 trial 内部的进度节奏。并发跑时 stdout 不再刷屏，logs/<trial>.log 是唯一的
# 观察通道，这两个值决定了 `tail -f` 到底能看见多少。
PROGRESS_EVERY_N = 5
# 时间下限比按条数打更关键：卡住的时候 i 恰恰不增长，只按 `i % N` 打的话，「卡死了」
# 和「跑完了」在日志里长得一模一样——都是没有新行。所以另起一个线程盯「离上次打印
# 过了多久」，而不是「跑到第几条」。
# 30s 与批次级心跳的 60s（run_batch.py 的 HEARTBEAT_S）刻意错开：心跳只回答「这条
# 还活着」，trial 内部要回答「卡在哪条命令上」，粒度必须更细，否则先等一轮心跳、
# 再等一轮 trial 进度，定位一次要两分钟。
PROGRESS_MAX_SILENCE_S = 30

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


def cgroup_fstype():
    """`/sys/fs/cgroup` 的文件系统类型。v2 统一层级是 `cgroup2fs`。"""
    r = sh(["stat", "-fc", "%T", str(CGROUP_ROOT)])
    return r.stdout.decode().strip() if r.returncode == 0 else ""


def _cg_from_proc(pid, cid):
    """从 `/proc/<pid>/cgroup` 读 v2 相对路径 —— 内核自己报告的，对任何 driver 都准。

    返回 None 表示这条路走不通，调用方要继续试候选路径。会走不通的情况：
      - dockerd 在别的 pid namespace（WSL2 / Docker Desktop），
      - 容器已退出（pid=0），
      - 挂的是 cgroup v1（行首是 `N:subsys:` 而非 `0::`）。

    **必须用容器 ID 复核读到的路径**：WSL 实测过一次假阳性——`.State.Pid` 是
    dockerd 所在 namespace 里的编号，拿到宿主 /proc 下恰好对上了另一个真实进程，
    于是"读成功"了，但读出来的是 `/sys/fs/cgroup/init.scope`，即**别人的 cgroup**。
    那种情况下指标会被静默采错，比读不到危险得多。所以路径里认不出容器 ID 就当没读到。
    """
    if not pid or pid == "0":
        return None
    try:
        txt = pathlib.Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None
    for line in txt.splitlines():
        if line.startswith("0::"):          # v2 统一层级固定这一行
            rel = line[3:].strip()
            if rel.startswith("/") and cid[:12] in rel:
                return CGROUP_ROOT / rel.lstrip("/")
    return None


def _cg_candidates(cid):
    """各 docker cgroup driver 的已知落点，按常见度排序。

    driver 与 rootless 与否会让路径完全不同，这是换机器时最先炸的一环：
    本机 driver=cgroupfs，实际在 `/sys/fs/cgroup/docker/<id>`，而旧版本硬编码的是
    systemd driver 的 `system.slice/docker-<id>.scope` —— 直接 raise 退出。
    """
    uid = os.getuid()
    user_svc = CGROUP_ROOT / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    return [
        CGROUP_ROOT / "docker" / cid,                             # cgroupfs driver
        CGROUP_ROOT / "system.slice" / f"docker-{cid}.scope",     # systemd driver
        user_svc / "user.slice" / f"docker-{cid}.scope",          # rootless + systemd
        user_svc / "docker.service" / cid,                        # rootless + cgroupfs
    ]


def _cg_search(cid):
    """兜底：在 cgroup 树里按容器 ID 搜一次。driver 是自定义 cgroup-parent 时只剩这条路。"""
    r = sh(["find", str(CGROUP_ROOT), "-maxdepth", "6", "-type", "d",
            "-name", f"*{cid}*", "-print", "-quit"])
    out = r.stdout.decode().strip().splitlines()
    return pathlib.Path(out[0]) if out else None


def discover_cgroup_dir(container):
    """定位容器的 cgroup v2 目录，返回 (路径, 探测方式, 试过的路径列表)。

    三级探测，每级都可能因环境而失效，所以都要试；全失败时把试过的路径原样抛出去，
    让服务器上能直接看出是 driver 问题还是权限问题。
    """
    tried = []
    cid = sh(["docker", "inspect", "-f", "{{.Id}}", container])
    if cid.returncode != 0:
        raise RuntimeError(f"docker inspect 取容器 ID 失败: {cid.stderr.decode()[:300]}")
    cid = cid.stdout.decode().strip()

    pid = sh(["docker", "inspect", "-f", "{{.State.Pid}}", container])
    pid = pid.stdout.decode().strip() if pid.returncode == 0 else ""

    p = _cg_from_proc(pid, cid)
    if p:
        tried.append(str(p))
        if (p / "cpu.stat").exists():
            return p, f"/proc/{pid}/cgroup", tried

    for c in _cg_candidates(cid):
        tried.append(str(c))
        if (c / "cpu.stat").exists():
            return c, "已知 driver 候选路径", tried

    p = _cg_search(cid)
    if p:
        tried.append(str(p) + "  (find)")
        if (p / "cpu.stat").exists():
            return p, "cgroup 树搜索", tried

    fs = cgroup_fstype()
    hint = ("挂的不是 cgroup v2（本脚本的 cpu.stat/io.stat/memory.current 口径只适用 v2）"
            if fs != "cgroup2fs" else
            "cgroup v2 正常，但容器目录没找到——可能是 rootless、自定义 cgroup-parent，"
            "或 /sys/fs/cgroup 未以可读方式挂进当前 namespace")
    raise RuntimeError(
        f"找不到容器 {container} 的 cgroup 目录。\n"
        f"  /sys/fs/cgroup 类型: {fs or '未知'}\n"
        f"  容器 ID: {cid}\n"
        f"  State.Pid: {pid or '不可用'}\n"
        f"  {hint}\n"
        f"  已试过:\n    " + "\n    ".join(tried or ["（无）"]))


class NullCgroup:
    """`--no-metrics` 模式下的占位：不碰 cgroup，指标字段一律记 null。

    这条路把「必须 cgroup v2 + 宿主侧 cgroup 目录可读」从硬约束里去掉了 ——
    rootless docker、受限容器、cgroup v1 的机器都能跑。
    代价只有性能指标；**保真度校验（patch_identical）与 rc 序列比对完全不依赖 cgroup**，
    所以只要目的是「打通」，用这个就够。
    """

    path = None
    how = "禁用（--no-metrics）"
    has_io = False

    def sample(self):
        return {}


class NullMemSampler:
    """与 MemSampler 同接口的空实现，让主循环不必分支。"""

    def start(self):
        pass

    def reset(self):
        pass

    def take(self):
        return None, 0

    def stop(self):
        pass


class Cgroup:
    """从宿主侧读容器 cgroup v2 累计值，前后做差归因到单条命令（容器内串行，差值即该命令）。"""

    def __init__(self, container):
        self.path, self.how, self.tried = discover_cgroup_dir(container)
        self.cid = sh(["docker", "inspect", "-f", "{{.Id}}", container]).stdout.decode().strip()
        # 不静默降级：缺文件直接报错退出，否则会像 v1 一样悄悄写出一堆 0
        for f in ("cpu.stat", "memory.current"):
            if not (self.path / f).exists():
                raise RuntimeError(f"缺少 cgroup 文件: {self.path / f}")
        # io.stat 在部分内核/驱动下可能缺失（如 io 控制器没启用）。它只影响 rbytes/wbytes
        # 两个字段，不影响 patch_identical 与 rc_match 这两个结论，所以降级而不是退出。
        self.has_io = (self.path / "io.stat").exists()

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
        for line in (self._read("io.stat").splitlines() if self.has_io else []):
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


class ProgressTicker(threading.Thread):
    """静默超过 PROGRESS_MAX_SILENCE_S 就强制打一行「现在卡在哪条」。

    只按 `i % N` 打进度有个洞：卡住的时候 i 不增长，于是一行都不打。这个线程盯的是
    「离上次打印过了多久」，所以命令卡在 docker exec 里、或者收尾的 git diff 很慢时，
    照样有输出。

    进度行一律走 say()，与本线程共用一把锁：run_batch.py 是按行读管道的，两个线程
    同时 print 会把两行拼成一行。
    """

    def __init__(self, n_all):
        super().__init__(daemon=True)
        self.n_all = n_all
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.last = time.monotonic()
        self.label, self.t0 = "启动中", time.monotonic()

    def say(self, text):
        """主线程打进度行：顺手把静默计时清零，免得刚打完 ticker 又补一行。"""
        with self.lock:
            self.last = time.monotonic()
            print(text, flush=True)

    def mark(self, label):
        """记下当前在做什么（只记不打），供静默时那一行引用。"""
        with self.lock:
            self.label, self.t0 = label, time.monotonic()

    def run(self):
        # 1s 一轮：远细于 30s 阈值，触发时刻的误差可忽略；用 wait 而不是 sleep 是
        # 为了让 stop() 立刻生效，不用等满一轮。
        while not self.stop_evt.wait(1.0):
            with self.lock:
                now = time.monotonic()
                if now - self.last < PROGRESS_MAX_SILENCE_S:
                    continue
                self.last = now
                print(f"  {self.label} …仍在跑 {now - self.t0:>6.0f}s"
                      f"（已 {PROGRESS_MAX_SILENCE_S}s 无新输出）", flush=True)

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
                "step": si,
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
    ap.add_argument("--allow-pull", action="store_true",
                    help="镜像不在本地时允许 docker run 触发拉取。默认禁止：实测出口吞吐只有"
                         "0.27 MB/s，误触一个 ~800MB 的镜像就是几十分钟")
    ap.add_argument("--force", action="store_true",
                    help="发现同 trial 的存量重放容器时照样启动（默认拒绝，防止两个进程互删容器）")
    ap.add_argument("--no-metrics", action="store_true",
                    help="不采 cgroup 性能指标，只跑命令 + 保真校验。"
                         "去掉「必须 cgroup v2 且宿主侧目录可读」这条硬约束，"
                         "rootless / 受限环境也能跑。patch_identical 与 rc 比对不受影响")
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
    # 容器名带上 PID：旧版本只按 trial 名推导，两个进程重放同一条 trial 会静默互删容器
    # （crosslang/INDEX.md「已知风险」记的就是这个）。加 PID 后各进程互不干扰。
    prefix = "replay_" + re.sub(r"[^A-Za-z0-9_.-]", "_", tdir.name)[:44]
    name = f"{prefix}_{os.getpid()}"
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
    print(f"container {name}")

    # 镜像必须已在本地：默认不允许 docker run 顺手去 pull
    if sh(["docker", "image", "inspect", image]).returncode != 0:
        if not args.allow_pull:
            print(f"\n镜像不在本地: {image}\n"
                  f"  三条路，按环境选：\n"
                  f"    1. 能连 registry：docker pull {image}\n"
                  f"    2. 连不上但能搬文件：在有网机器上 docker save | zstd，"
                  f"目标机 docker load\n"
                  f"    3. 连不上 registry 但能连各包源：crosslang/build_arm.sh "
                  f"从本地 mars-base 重建\n"
                  f"  （--allow-pull 可让 docker run 自己去拉；默认拒绝是因为实测出口"
                  f"吞吐约 0.27 MB/s，一个 ~800MB 镜像要几十分钟）")
            return 1
        print("镜像不在本地，--allow-pull 已开，docker run 将触发拉取（可能很慢）")

    # 同一条 trial 的存量重放容器 → 说明可能有另一个进程在跑，撞上就是互相删容器
    ex = sh(["docker", "ps", "-aq", "--filter", f"name=^{prefix}"])
    stale = [c for c in ex.stdout.decode().split() if c]
    if stale:
        names = sh(["docker", "ps", "-a", "--filter", f"name=^{prefix}",
                    "--format", "{{.Names}} ({{.Status}})"]).stdout.decode().strip()
        if not args.force:
            print(f"\n发现同 trial 的存量重放容器 {len(stale)} 个：\n  "
                  + names.replace("\n", "\n  ")
                  + "\n  可能有另一个重放进程正在跑。确认无人在用后清理：\n"
                    f"    docker rm -f $(docker ps -aq --filter name=^{prefix})\n"
                    f"  或加 --force 忽略本检查。")
            return 1
        print(f"--force：忽略 {len(stale)} 个存量容器 {names.splitlines()}")

    run = ["docker", "run", "-d", "--name", name,
           f"--cpus={cpus}", f"--memory={mem_mb}m", f"--memory-swap={mem_mb}m"]
    if not allow_net:
        run += ["--network=none"]
    if use_sink:
        # 镜像 config 的 Env 里本来没有代理变量——原 harness 也是运行时注入的
        for v in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            run += ["-e", f"{v}={proxy_url}"]
        # NO_PROXY 必须显式钉死，两个理由：
        #   1. `~/.docker/config.json` 里配了 proxies 的话，docker run 会自动把宿主的
        #      代理设置注入容器——包括它的 NO_PROXY。那会让部分域名绕过 sinkhole，
        #      报错文本从「403 Forbidden」变成连接失败，与原始 trace 不符。
        #   2. trace 里有打 localhost 的 curl（prometheus 那条 18 次），回环不能走代理。
        for v in ("NO_PROXY", "no_proxy"):
            run += ["-e", f"{v}=localhost,127.0.0.1,::1"]
    run += [image, "sleep", "infinity"]

    def teardown():
        # 幂等，而且必须幂等：主容器刚 docker run 出来、sidecar 还没起（甚至永远起不来）
        # 这种半成品状态下也会被调用。`docker rm -f` 删一个不存在的容器只是返回非零，
        # sh() 把输出吃掉，所以两个名字无脑都删一遍即可。
        sh(["docker", "rm", "-f", name])
        sh(["docker", "rm", "-f", sink])

    # 从这里到最后都必须在 try/finally 里。以前的 try 要到主循环才开始，中间「起主容器 →
    # 起 sidecar → 最长 10s 的探活轮询 → cgroup 初始化」整段裸奔在保护之外，在那个窗口里
    # 按 ^C 会稳定留下「主容器 + -sink」两个孤儿容器；并发度越高、daemon 越忙，窗口越宽。
    #
    # 注意 try 要罩住 sh(run) 本身，不能只罩它返回之后：`docker run -d` 是先让 daemon 把
    # 容器建出来再返回，容器在 docker ps 里显示 Created 的那一刻 sh(run) 还没返回。实测
    # 在这一刻 ^C，容器已经存在而代码还没进 try —— 照样漏。容器名是提前算好的定值，
    # 所以哪怕 docker run 被打断在半路，teardown 里的 rm -f <name> 也删得掉。
    #
    # mem / ticker 先占位：finally 里要停它们，而失败或 ^C 可能发生在它们建起来之前，
    # 直接引用会变成 NameError —— 那会把真正的 KeyboardInterrupt 盖掉，容器照样漏。
    mem = ticker = None
    try:
        r = sh(run)
        if r.returncode != 0:
            # 走 finally 顺手 teardown：docker run 也可能是「建出来了但没起来」才失败的，
            # 那种情况下容器以 Created 状态留在那儿。
            print("容器启动失败:", r.stderr.decode()[:400])
            return 1

        sink_cg = None
        if use_sink:
            # sidecar 与主容器共享 netns（--network=container:），但 cgroup 各自独立，
            # 所以代理自身的 CPU/内存**不会**记进被测容器的指标。
            r = sh(["docker", "run", "-d", "--name", sink,
                    f"--network=container:{name}", "--memory=256m",
                    image, "python3", "-c", SINKHOLE_SRC, str(SINK_PORT)])
            if r.returncode != 0:
                print("sinkhole sidecar 启动失败:", r.stderr.decode()[:400])
                # 这一段里的几处显式 teardown() 保留而不是交给 finally：环境没搭起来的
                # 半成品容器留着也调不出东西，所以这里连 --keep 都不认；finally 那次
                # 只是幂等地再删一遍（--keep 时不删），两者不冲突。
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
            if not args.no_metrics:
                try:
                    sink_cg = Cgroup(sink)
                except RuntimeError as e:
                    print(f"sinkhole cgroup 初始化失败：{e}")
                    teardown()
                    return 1
            print(f"sinkhole  {proxy_url}（sidecar 容器 {sink}"
                  + ("，独立 cgroup）" if sink_cg else "）"))

        if args.no_metrics:
            cg, mem = NullCgroup(), NullMemSampler()
            print("cgroup    不采集（--no-metrics）—— 只跑命令 + 保真校验")
        else:
            try:
                cg = Cgroup(name)
            except RuntimeError as e:
                print(f"cgroup 初始化失败：{e}\n"
                      f"\n只想打通、不要性能数据的话，加 --no-metrics 可绕过整个 cgroup 依赖。")
                teardown()
                return 1
            print(f"cgroup    {cg.path}")
            print(f"          （探测方式：{cg.how}"
                  + ("" if cg.has_io else "；io.stat 缺失 → rbytes/wbytes 记 0，不影响保真度结论")
                  + "）")
            mem = MemSampler(cg)
        mem.start()
        if not args.no_metrics:
            print(f"mem       后台轮询 memory.current @ {int(MEM_POLL_S * 1000)}ms")
        print()

        recs = []
        sink_cpu0 = sink_cg.sample()["usage_usec"] if sink_cg else 0
        t_start = time.monotonic()
        ticker = ProgressTicker(n_all)
        ticker.start()
        for i, it in todo:
            cmd = it["cmd"]
            # 先算好摘要：进度行和 ticker 的静默行引用的是同一个字符串，
            # 两边长得一样，看日志时才对得上是同一条命令。
            head = strip_cd(cmd)[:88].splitlines()
            head = head[0] if head else ""
            # 开跑前就登记：卡住时 ticker 要说得出卡在哪条上
            ticker.mark(f"[{i:>4}/{n_all}] {head}")
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
                # --no-metrics 下两边都是空 dict，记 None 而不是 0，
                # 免得下游把"没采"读成"真的是 0"
                if not before and not after:
                    return None
                return after.get(k, 0) - before.get(k, 0)

            # timeout 命令：TERM 超时退 124；-k 之后被 KILL 退 137
            timed_out = rc in (124, 137)

            rec = {"i": i, "step": it.get("step"), "rc": rc, "timed_out": timed_out, "trace_rc": it["trace_rc"],
                   "wall_s": round(dt, 4), "abs_start_s": round(t0 - t_start, 4), "outer_timeout": outer,
                   "stdout_bytes": len(so), "stderr_bytes": len(se),
                   "user_usec": d("user_usec"), "system_usec": d("system_usec"),
                   "usage_usec": d("usage_usec"),
                   "rbytes": d("rbytes"), "wbytes": d("wbytes"),
                   "mem_peak": mem_peak, "mem_samples": mem_n,
                   "cmd": cmd, "cmd_stripped": strip_cd(cmd),
                   "stdout_head": so[:4000].decode(errors="replace")}
            recs.append(rec)
            # 慢命令（dt > 10）照旧必打——它本身就是最该被看见的那种。
            if i % PROGRESS_EVERY_N == 0 or dt > 10:
                ticker.say(f"  [{i:>4}/{n_all}] rc={rc:<4}{'TO' if timed_out else '  '} {dt:>7.2f}s  "
                           f"{head}")

        elapsed = time.monotonic() - t_start
        ticker.say(f"\n重放完成 {elapsed:.0f}s")

        # ---- 保真度硬校验：git diff base..HEAD vs model.patch ----
        # 这一步没有超时兜底，仓库大时可以跑很久，是最该被 ticker 罩住的收尾环节
        ticker.mark("收尾 git diff base..HEAD")
        gd = sh(["docker", "exec", "-w", "/app", name, "git", "diff", "--binary", base_sha, "HEAD"])
        # 往下全是密集的汇总打印，ticker 再插话只会把行拼乱；git diff 一回来就停。
        ticker.stop()
        ticker.join(timeout=2)
        replayed = gd.stdout
        (out / "replayed.patch").write_bytes(replayed)
        orig_p = tdir / "model.patch"
        verdict = {"elapsed_s": round(elapsed, 1),
                   "cmd_timeout_s": args.cmd_timeout,
                   "interpreter": interp,
                   "net_mode": "internet" if allow_net else ("sinkhole403" if use_sink else "none"),
                   "n_cmds_trace": n_all, "n_skipped_sentinel": n_skipped,
                   "n_replayed": len(recs),
                   "replayed_patch_bytes": len(replayed),
                   "metrics_collected": not args.no_metrics,
                   "t_start_mono": round(t_start, 6),
                   # 跨机对比时这些是解释性能差异的前提，必须随判定一起落盘
                   "host": {"kernel": os.uname().release,
                            "cgroup_dir": str(cg.path) if cg.path else None,
                            "cgroup_discovery": cg.how,
                            "cgroup_has_io": cg.has_io,
                            "nproc": os.cpu_count(),
                            "image": image}}
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
        # mem / ticker 可能还是 None：失败或 ^C 发生在容器起好、它们还没建的那段窗口里。
        # ticker 正常路径上已经停过一次，stop() 幂等，这里只是兜住异常路径。
        if mem is not None:
            mem.stop()
        if ticker is not None:
            ticker.stop()
        if not args.keep:
            teardown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
