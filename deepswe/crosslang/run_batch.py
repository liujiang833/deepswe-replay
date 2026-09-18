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

`--topdown` 顺带采 ARM topdown 四象限。它**强制串行**（与 `--jobs > 1` 互斥且报错退出），
理由比 `--metrics` 那条更硬，见 main() 里那段报错文案：多个 `perf stat -a` 会话争抢的是
**同一批物理计数器**，超过通用计数器个数就触发复用，整批数据一起作废。

`--topdown` 打开时每条 trial 改调 `topdown_trial.sh` 而不是直接调 `replay.py` ——
perf 那套逻辑（`-G` 的参数顺序、cgroup v1/v2 的路径口径、等容器、排掉 sidecar）
只有那一个地方有，这里绝不再写第二份。

一轮跑完（写好 summary.json 之后）会自动调 `cmd_stats.py <输出目录>`，把「命令类型 × 次数/耗时」
统计落到 `<输出目录>/cmd_stats/`（`--dry-run` 不调）。它失败只打一行提示，不影响本脚本的退出码。

用法:
    python3 run_batch.py                          # 跑全部，输出到 ./runs/<时间戳>
    python3 run_batch.py --only go,rust           # 只跑指定语言
    python3 run_batch.py -j 8                     # 8 条并发（日志只落盘，不实时刷屏）
    python3 run_batch.py -j auto                  # 按本机 CPU / 可用内存自动定并发度
    python3 run_batch.py --smoke 5                # 每条只跑前 5 条命令（冒烟，跳过保真度校验）
    python3 run_batch.py --dry-run                # 只做预检和排程，不真跑
    python3 run_batch.py --topdown --skip-missing --no-metrics   # ARM 上批量采 topdown
    python3 run_batch.py --topdown --per-lang 2 --no-metrics     # 每种语言抽 2 条先看横向
"""

import collections
import argparse
import concurrent.futures as cf
import datetime
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
LANG_ORDER = ["python", "go", "rust", "typescript", "javascript"]

# topdown_parse.py 的自检键 → 报告里那一列的短标签。
# 「是哪一条红了」必须出现在汇总表里，不能只打一个 ❌：C1 红了是「SLOTS 偏小 / 事件号错」，
# C5 红了是「计数器复用」，两者的排查方向毫无交集。只写 ❌ 等于把诊断信息扔掉，
# 事后还得回去逐条翻 topdown.json。
# 顺序 = 报告里列出失败项的顺序，和 topdown_parse.py 的打印顺序保持一致。
TOPDOWN_CHECK_LABELS = [
    ("counters_present",                 "事件齐全"),   # 事件全都采到了吗
    ("nonzero",                          "计数非零"),   # CPU_CYCLES 不为 0（cgroup 没滤空）
    ("sum_in_tolerance",                 "求和"),   # 只有直接法才是校验
    ("C1_residual_nonneg",               "C1"),
    ("C2_op_spec_ge_op_retired",         "C2"),
    ("C3_op_retired_per_cycle_le_slots", "C3"),
    ("C4_quadrants_in_range",            "C4"),
    ("C5_no_multiplexing",               "C5"),
    ("X_backend_cross_check",            "X"),
]

# 四象限在报告里的列名。用短名是为了让这四列能塞进已经很宽的那张总表 ——
# 总表是横向对比不同 benchmark 用的，一行折了就白做了。
TOPDOWN_QUADRANTS = [
    ("Retiring",      "Retiring"),
    ("BadSpec",       "BadSpec"),
    ("FrontendBound", "FE"),
    ("BackendBound",  "BE"),
]

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


def find_topdown():
    """topdown_trial.sh 与本脚本同级（两个包里都是这个布局）。"""
    p = HERE / "topdown_trial.sh"
    return p if p.exists() else None


def read_json(p):
    """读一份 JSON，读不到 / 坏了都返回 None。

    这些产物是采集脚本写的旁证，不是判定依据 —— 少一份、坏一份都不该让汇总跑不出来。
    """
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def topdown_verdict(raw):
    """从 topdown.json 算出「哪几条自检红了」和那一列要显示的字符串。

    判据取 `checks_run`（topdown_parse.py 真正跑过的那几条）而不是整个 `checks` 字典 ——
    后者里混着 `sum_tolerance` / `C1_slots_min` / `min_pcnt_running` 这类**数值**，
    还混着「这轮没跑所以记 None」的项（残差法下的 sum_in_tolerance、没开 X 时的
    X_backend_cross_check）。拿 `checks` 整个去判会把「没跑」误报成「没过」。

    counters_present / nonzero 两条是例外：它们在算四象限之前就落了，不进 checks_run，
    但恰恰是「一条都没采到」时唯一红的东西，必须单独补上。

    值是 None 且**跑过了**算失败（C5 的 None = perf 输出里没有调度占比字段，
    topdown_parse.py 那边也是按失败处理的）。
    """
    checks = (raw or {}).get("checks") or {}
    run = list((raw or {}).get("checks_run") or [])
    order = [k for k, _ in TOPDOWN_CHECK_LABELS
             if k in ("counters_present", "nonzero") and k in checks]
    order += [k for k, _ in TOPDOWN_CHECK_LABELS if k in run and k not in order]
    label = dict(TOPDOWN_CHECK_LABELS)
    failed = [label.get(k, k) for k in order if checks.get(k) is not True]
    return order, failed


def collect_topdown(out, name):
    """把 topdown_trial.sh 落下的 run_status.json + topdown.json 读成一个 dict。

    两份文件各回答一个问题，缺一不可：
      run_status.json  三个退出码分开记（重放 / perf / 解析）—— 脚本自己只能吐一个
                       退出码，而「trial 失败」和「PMU 数不可信」是正交的两件事。
      topdown.json     四象限 + 逐条自检 + 事件计数。

    采废了就返回 available=False 并写清原因 —— **绝不让它影响这条 trial 的重放结论**。
    """
    td_dir = out / name / "topdown"
    status = read_json(td_dir / "run_status.json")
    raw = read_json(td_dir / "topdown.json")
    rel = lambda f: str((td_dir / f).relative_to(out))

    info = {
        "available": False,
        "reason": None,
        "quadrants": None,
        "backend_method": (raw or {}).get("backend_method"),
        "slots": (raw or {}).get("slots"),
        "sum_check_meaningful": (raw or {}).get("sum_check_meaningful"),
        "cross_check_enabled": (raw or {}).get("cross_check_enabled"),
        "n_events": (raw or {}).get("n_events"),
        # 事件计数与事件号都留下来：事后只剩报告时，「数不对」最常见的根因就是事件号
        # 指错了，而光看计数值无从判断当时用的是 0x003a 还是别的。
        "events": (raw or {}).get("events"),
        "event_codes": (raw or {}).get("event_codes"),
        "checks": (raw or {}).get("checks"),
        "checks_run": (raw or {}).get("checks_run"),
        "checks_failed": None,
        "verify": "—",
        "replay_rc": (status or {}).get("replay_rc"),
        "perf_rc": (status or {}).get("perf_rc"),
        "parse_rc": (status or {}).get("parse_rc"),
        "artifacts": {"run_status_json": rel("run_status.json"),
                      "topdown_json": rel("topdown.json")},
    }

    if raw is None:
        info["reason"] = ("没有 topdown.json，采集没跑到解析这一步"
                          "（perf 没起来 / 等不到容器 / 重放自己就挂了）")
        return info
    _, failed = topdown_verdict(raw)
    info["checks_failed"] = failed
    quad = raw.get("topdown")
    if not quad:
        info["reason"] = ("解析在算四象限之前就停了"
                          + (f"，未通过：{'、'.join(failed)}" if failed else ""))
        return info

    vals = {k: quad.get(k) for k, _ in TOPDOWN_QUADRANTS}
    # 四个象限必须都是数才算「可用」。少一个就整条判不可用，而不是留个半拉子的
    # available=True —— 后者会让分组小结的账对不上：这条既进不了均值（值不是数），
    # 又不算「自检没过被剔掉」，于是「可用 N 条 = 进均值 X 条 + 剔除 Y 条」这个
    # 等式凭空少一条，而报告上看不出少在哪。
    # （topdown_parse.py 一向是四个一起写，所以现实中走不到这里；但账要自洽。）
    bad = [k for k, v in vals.items() if not isinstance(v, (int, float))]
    if bad:
        info["reason"] = f"四象限里有值不是数字：{'、'.join(bad)}"
        return info

    info["available"] = True
    info["quadrants"] = vals
    info["sum"] = quad.get("sum")
    info["op_retired_per_cycle"] = quad.get("op_retired_per_cycle")
    info["verify"] = "✅" if not failed else "❌" + ",".join(failed)
    return info


def pick_per_lang(trials, missing_ids, n, strategy="median"):
    """每种语言只取 N 条，返回 (选中的 trial, 逐语言的选取明细)。

    两条铁律，与策略无关：
      1. **只在镜像已建好的里面挑**。目标机上镜像是边建边跑的，挑到没建的那条等于
         白排一个必然跳过的槽。
      2. **确定性，不用随机**。同样的输入必须选出同一批，否则两次跑的数没法比。
         所以排序键一律是 (n_commands, 目录名)，命令数并列时也不会抖。

    三种策略 —— 差别是**耗时与数据干净度的权衡**，不是好坏之分：

      median（默认）  按 n_commands 升序取**正中间的 N 条**。快。
      heaviest        降序取前 N。命令最多的那几条。
      lightest        升序取前 N。最快，只想验证链路通不通时用。

    ⚠️ 为什么这个选择会影响数据质量：topdown 采的是**整条 trial 的聚合值**，里面固定
       含容器启动和收尾 `git diff` 的开销。trial 越轻，这笔固定开销在聚合值里占比越大，
       四象限就越掺进「容器启动 + 解释器 import 长什么样」的成分，而不是 workload 本身。
       heaviest 的信噪比明显更好，代价是慢几倍（全量 113 条里五种语言各取 2 条，
       median 合计约 18 分钟、heaviest 约 68 分钟，差 3.8 倍）。
       真正干净的对比要等 per-command 归因那一版，这一版只能在这两头之间选。

    某语言一条可用的都没有 → 贡献 0 条，不报错也不中断：那正是「这台机器上这门语言
    的镜像还没建」，是预期内的情况，不该拖垮整批。
    """
    by_lang = {}
    for t in trials:
        by_lang.setdefault(t["lang"], []).append(t)
    picked, report = [], []
    for lang, ts in by_lang.items():
        avail = [t for t in ts if id(t) not in missing_ids]
        # n_commands 可能是 None（meta.json 没这个字段），当 0 排到最前而不是炸掉
        avail.sort(key=lambda t: ((t["n_commands"] or 0), t["name"]))
        if strategy == "heaviest":
            # ⚠️ 这里**不能**写成 `list(reversed(avail))[:n]`。avail 是按
            # (n_commands, 目录名) 升序排的，整体 reverse 之后命令数确实降序了，
            # 但**并列项的目录名也跟着变成降序** —— 三条都是 50 条命令时取到的是
            # 名字最大的那几条，与「同数按目录名升序兜底」正好相反。
            # 仍然是确定的（不会抖），所以跑起来一切正常，只是选错了人，
            # 而且跟 median / lightest 的兜底方向不一致 —— 两批数据之间就此不可比。
            # 只有把命令数取负单独作为主键，才能让目录名保持升序。
            take = sorted(avail, key=lambda t: (-(t["n_commands"] or 0), t["name"]))[:n]
            take.sort(key=lambda t: ((t["n_commands"] or 0), t["name"]))
        elif strategy == "lightest":
            take = avail[:n]
        else:
            # 正中间的 N 条：start = (L - N) // 2。L 是偶数、N 是偶数时正好居中；
            # L 为奇数时整除向下取，偏轻的那一侧 —— 偏向更快，且是确定的。
            start = max(0, (len(avail) - n) // 2)
            take = avail[start:start + n]
        picked.extend(take)
        report.append({
            "语言": lang,
            "n_candidates": len(ts),
            "n_available": len(avail),
            "n_picked": len(take),
            "short": len(avail) < n,
            "trials": [{"trial": t["name"], "n_commands": t["n_commands"]} for t in take],
        })
    return picked, report


# 三种策略在报告里的人话说明。选取结果里必须带上用的是哪种：不同策略选出的批次
# 之间不可直接比较（median 的那批天生更轻、固定开销占比更大），报告里看不出策略
# 就等于把两批不可比的数摆在一起。
PICK_DESC = {
    "median":   "按命令数取中位，同数按名字",
    "heaviest": "按命令数降序取，同数按名字",
    "lightest": "按命令数升序取，同数按名字",
}


def print_pick(report, n, strategy):
    """开跑前把选取结果摊开。

    必须打：抽样跑和全量跑的报告长得一模一样，不把「这轮只挑了几条、每种语言各几条、
    哪几条、用的哪种策略」当场说清楚，事后翻 SUMMARY.md 会直接把它当成全量结论去引用。
    每条的命令数也要带上 —— 那是判断这批 topdown 数据里掺了多少启动开销的唯一线索。
    """
    print(f"选取      --per-lang {n} --pick {strategy}"
          f"（{PICK_DESC.get(strategy, strategy)}）")
    w = max((len(r["语言"]) for r in report), default=6)
    wf = max((len(f"{r['n_available']}/{r['n_candidates']}") for r in report), default=3)
    for r in report:
        frac = f"{r['n_available']}/{r['n_candidates']}"
        if r["n_picked"] == 0:
            print(f"  {r['语言']:<{w}}  {frac:<{wf}} 已建镜像 → 跳过")
            continue
        short = f"（可用的不够 {n} 条）" if r["short"] else ""
        print(f"  {r['语言']:<{w}}  {frac:<{wf}} 已建镜像 → 取 {r['n_picked']} 条{short}")
        for x in r["trials"]:
            nc = "?" if x["n_commands"] is None else f"{x['n_commands']} 条命令"
            print(f"  {'':<{w}}    {x['trial']:<45} ({nc})")
    print(f"  合计 {sum(r['n_picked'] for r in report)} 条")


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


def load_trials(root=None):
    """扫描 trial 子目录。判据是四个必需文件齐全，不靠目录名。

    root 默认是本脚本所在目录（打好的 bundle 就是这个平铺布局）。仓库布局里
    全量 113 条在 `full_trials/` 子目录下，而 crosslang/ 根下只有已验证的那几条 ——
    所以 `--trials-dir full_trials` 是仓库里跑全量的入口，不必把脚本拷来拷去。
    """
    root = root or HERE
    need = ("meta.json", "trajectory.json", "model.patch", "task.json")
    out = []
    for d in sorted(root.iterdir()):
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


def run_one(idx, t, n_total, replay, out, args, stream, topdown=None):
    """跑一条 trial，返回结果 dict。

    stream=True 只在 --jobs 1 下用：单条要跑几分钟，没有实时输出很难判断是卡住还是在
    编译。并发时 N 条的输出会交错成乱码，所以只落盘——logs/<trial>.log 是排查的唯一依据，
    两种模式下都必须写。

    topdown 非 None 时**改调 topdown_trial.sh**，而不是直接调 replay.py。
    那个脚本内部自己会起 replay.py、等容器、推 cgroup 路径、挂 perf、调解析器 ——
    perf 那一套（`-G` 必须排在 `-e` 之后、cgroup v1 走 perf_event 独立层级、
    兜底找容器时必须排掉 `-sink`）只有它那一份。这里**绝不重写第二份**：
    这轮已经在 `-G` 顺序和 cgroup v1 上各栽过一次，两份实现只会跟着一起错。
    输出目录约定是对齐的（两边都是 `-o <out>`，产物都落在 `<out>/<trial名>/`），
    所以下面收 verdict.json 的路径两种模式共用。
    """
    if stream:
        print("─" * 78)
        print(f"[{idx + 1}/{n_total}] {t['lang']}  {t['name']}")
        print("─" * 78)

    if topdown is not None:
        cmd = ["bash", str(topdown), str(t["dir"]),
               "-o", str(out), "--cmd-timeout", str(args.cmd_timeout)]
        if args.smoke:
            cmd += ["--limit", str(args.smoke)]
        if args.per_step:
            cmd += ["--per-step"]
        # --metrics 默认是关的，所以这里默认就会透传 --no-metrics 下去。
        # 这不是顺手：目标机是 cgroup v1，replay.py 那套指标只认 v2 的
        # cpu.stat / memory.current，不关掉它整条采集在启动时就报
        # 「sinkhole cgroup 初始化失败」，一条都跑不起来。
        if not args.metrics:
            cmd += ["--no-metrics"]
    else:
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
    res = {
        "lang": t["lang"], "trial": t["name"], "task_id": t["task_id"],
        "model": t["model"], "exit_code": rc, "wall_s": round(dt, 1),
        "verdict": verdict, "log": str(log.relative_to(out)),
        "vs_baseline": cmp_baseline(t["baseline"], verdict),
    }
    if topdown is not None:
        td = collect_topdown(out, t["name"])
        res["topdown"] = td
        # ⚠️ **topdown 采废了不能把 trial 判成失败。**
        # patch_identical 是保真度硬标准（容器内 git diff 与 model.patch 逐字节相等），
        # topdown 的 C1~C5 是数据可信度 —— 两者正交，一条红了跟另一条毫无关系。
        # topdown_trial.sh 只能吐一个退出码，它把「重放挂了」和「自检没过」压成了同一个数
        # （解析器自检没过是 2）。所以这里改用 run_status.json 里单独记的 replay_rc：
        # 有它就以它为准，这条 trial 的重放结论照常记录；topdown 那几列另外标不可用。
        res["script_exit_code"] = rc           # topdown_trial.sh 自己的退出码，留痕备查
        if td.get("replay_rc") is not None:
            res["exit_code"] = td["replay_rc"]
        # 没有 run_status.json = 采集连重放都没起起来（事件号不合法、等不到容器…），
        # 那种情况这条 trial 确实没跑成，保留进程退出码当失败 —— 这是有意的。
    return res


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
    ap.add_argument("--no-metrics", action="store_true",
                    help="显式关掉 cgroup 指标（默认行为，写出来只为让命令行自我说明）。"
                         "cgroup v1 的机器必须是关的：replay.py 那套指标只认 v2 的 "
                         "cpu.stat / memory.current，不关整条起不来")
    ap.add_argument("--topdown", action="store_true",
                    help="每条 trial 顺带采 ARM topdown 四象限（改调 topdown_trial.sh，"
                         "perf 逻辑不在这里重写）。**强制串行，与 --jobs > 1 互斥且报错退出**："
                         "多个 perf stat -a 会话抢的是同一批物理计数器。"
                         "topdown 采废不影响这条 trial 的重放结论，两者正交")
    ap.add_argument("--per-step", action="store_true",
                    help="配合 --topdown：每条 trial 产出 per-step topdown（perf stat -I 10），"
                         "事后用 topdown_cluster.py 做多级聚类（per-trial / per-language / all）")
    ap.add_argument("--per-lang", type=int, default=0, metavar="N",
                    help="每种语言只抽 N 条跑（只在镜像已建好的里面挑）。"
                         "某语言一条都没建好就贡献 0 条，不报错。可与 --only 叠加")
    ap.add_argument("--pick", choices=("median", "heaviest", "lightest"), default="median",
                    help="--per-lang 的选取策略，默认 median（按命令数取中位）。"
                         "heaviest = 取命令最多的，数据更干净但慢几倍"
                         "（全量各取 2 条：median 约 18 分钟 / heaviest 约 68 分钟）；"
                         "lightest = 最快，只验链路通不通。"
                         "⚠️ trial 越轻，容器启动 + 收尾 git diff 这笔固定开销在聚合值里"
                         "占比越大，topdown 就越掺进「容器启动 + 解释器 import」的成分")
    ap.add_argument("--topdown-script", default="",
                    help="topdown_trial.sh 路径（默认取本脚本同目录）")
    ap.add_argument("--dry-run", action="store_true", help="只做预检和排程，不真跑")
    ap.add_argument("--trials-dir", default="",
                    help="去哪个目录找 trial（默认本脚本所在目录）。仓库布局下全量 113 条"
                         "在 full_trials/，用 --trials-dir full_trials")
    ap.add_argument("--skip-missing", action="store_true",
                    help="镜像还没建好的 trial 直接跳过而不是拒绝启动（全量集边建边跑用）")
    ap.add_argument("--keep-going", action="store_true",
                    help="某条失败后继续跑剩下的（默认遇错即停）。"
                         "并发下「停」= 不再调度新的，已在跑的让它跑完")
    ap.add_argument("--replay", default="", help="replay.py 路径（默认自动定位）")
    args = ap.parse_args()

    # --metrics 与 --no-metrics 同时给 = 自相矛盾。不静默挑一个：挑错了的后果是
    # 整批要么白跑（v1 上起不来），要么采了一批没人要的 cgroup 数，都得重来。
    if args.metrics and args.no_metrics:
        print("--metrics 与 --no-metrics 同时给了，自相矛盾，拒绝启动。\n"
              "  默认就是不采（等价于 --no-metrics），要采才加 --metrics。")
        return 1
    if args.no_metrics:
        args.metrics = False
    if args.per_lang < 0:
        print(f"--per-lang 至少是 0（0 = 不抽样，跑全部），收到 {args.per_lang}")
        return 1

    replay = pathlib.Path(args.replay) if args.replay else find_replay()
    if not replay or not replay.exists():
        print(f"找不到 replay.py（找过 {HERE}/replay.py 和 {HERE.parent}/replay.py）")
        return 1

    # topdown_trial.sh 自己也会去找 replay.py，但**这里必须先确认它在**：
    # 少了它的话，下面每条 trial 都会以「bash: 找不到文件」失败一次，跑满 113 条才发现。
    topdown = None
    if args.topdown:
        topdown = (pathlib.Path(args.topdown_script) if args.topdown_script
                   else find_topdown())
        if not topdown or not topdown.exists():
            print(f"--topdown 需要 topdown_trial.sh，没找到（找过 {HERE}/topdown_trial.sh）。\n"
                  f"  它和 topdown.conf / topdown_parse.py 一起在 topdown 包里，"
                  f"主重放包不带 —— 确认解开的是带 topdown 的那个包。")
            return 1
        conf = topdown.parent / "topdown.conf"
        if not conf.exists():
            print(f"--topdown 需要 {conf} —— 事件号和 SLOTS 全在那里面。\n"
                  f"  先跑 bash probe_pmu.sh 把这台机器的事件号验出来再填。")
            return 1

    # trial 根目录：相对路径按「本脚本所在目录」解释，这样在 crosslang/ 下
    # `--trials-dir full_trials` 和在别处用绝对路径都成立。
    if args.trials_dir:
        trials_root = pathlib.Path(args.trials_dir)
        if not trials_root.is_absolute():
            trials_root = HERE / trials_root
        if not trials_root.is_dir():
            print(f"❌ --trials-dir 不是目录: {trials_root}")
            return 1
    else:
        trials_root = HERE
    trials = load_trials(trials_root)
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        trials = [t for t in trials if t["lang"] in want]
    if not trials:
        print("没有可跑的 trial（--only 过滤掉了全部，或目录里没有合规的 trial）")
        return 1

    # --per-lang 天然只在「镜像已建好」的里面挑，所以它和 --skip-missing 是同一个前提：
    # 缺镜像不该让预检整批拒绝启动。两个给哪个都行，同时给也不冲突。
    tolerate_missing = args.skip_missing or bool(args.per_lang)
    problems, fstype, missing = preflight(trials, args.metrics, tolerate_missing)
    miss_set = {id(t) for t in missing}
    pick_report = None
    if args.per_lang:
        n_candidates = len(trials)
        picked, pick_report = pick_per_lang(trials, miss_set, args.per_lang, args.pick)
        # 选完再按全局口径排一次序（语言顺序 + 目录名）：抽样批次的汇总表要能和全量
        # 批次逐行对齐着看，行序就不能跟着「各语言内部按命令数排」走。
        picked.sort(key=lambda t: (LANG_ORDER.index(t["lang"])
                                   if t["lang"] in LANG_ORDER else 99, t["name"]))
        print_pick(pick_report, args.per_lang, args.pick)
        print()
        trials = picked
        if not trials:
            print(f"--per-lang {args.per_lang}：{n_candidates} 条候选里一条镜像都没建好，"
                  f"无事可做 —— 先跑 build_arm.sh")
            return 1
    elif args.skip_missing and missing:
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
    # ── --topdown 与并发：比 --metrics 那条更硬，机制也不同 ────────────────────
    # --metrics 抢的是**机器资源**（CPU 配额、内存、磁盘队列），采到的是被挤压之后的
    # 数字，至少每条 trial 还各有各的一份数据。
    # --topdown 抢的是**同一批物理计数器**：PMU 上通用计数器就那么几个（Neoverse 一般 6 个，
    # NMI watchdog 开着只剩 5 个），而我们一轮要开 4~6 个事件、还要求它们作为一个 {} 组
    # 同上同下。两个 `perf stat -a` 会话一起要，内核只能**复用**（multiplexing）——
    # 每个事件只在一部分时间窗口里真正计数，其余靠外推。
    # 后果不是「数字偏一点」，是**整批作废**：
    #   · 每条 trial 的 C5（最低调度占比 > 99.9%）自检全部失败；
    #   · 四象限的分子分母来自不同的时间窗口，比值不再有物理意义；
    #   · 而屏幕上每条看起来都「跑完了」，数也都在 0~1 之间。
    # 所以这里报错退出，不是警告后继续。判的是收敛后的 jobs（真正会同时跑几条），
    # 说的是用户敲的 jobs_asked。
    if args.topdown and jobs > 1:
        print(f"--topdown 与 {jobs_asked}互斥，拒绝启动。\n"
              f"  这不是「不兼容」，是**计数器竞争**：\n"
              f"  topdown 靠宿主侧 `perf stat -a -G <cgroup>` 采 PMU 事件，一轮要开 4~6 个\n"
              f"  事件，还要求它们作为一个 {{}} 组被内核同时上、同时下。而 PMU 上的通用\n"
              f"  计数器是**物理的、全机共享的**：Neoverse 一般 6 个，NMI watchdog 开着\n"
              f"  只剩 5 个。并发 {jobs} 条就是 {jobs} 个 perf 会话同时要这批计数器，\n"
              f"  总需求 ≈ {jobs} × 4~6 个，一定超。超了内核不会报错，它会**复用**\n"
              f"  （multiplexing）：每个事件只在一部分时间窗口里真正计数，剩下靠外推。\n"
              f"  后果是整批作废，不是「偏一点」——\n"
              f"    · 每条 trial 的 C5（最低调度占比 > 99.9%）自检**全部失败**；\n"
              f"    · 四象限的分子和分母来自不同的时间窗口，比值不再有物理意义；\n"
              f"    · 而屏幕上每条都「跑完了」，四个数也都规规矩矩落在 0~1 之间。\n"
              f"  要 topdown：--jobs 1（或不带 --jobs）。113 条串行要数小时，\n"
              f"  先用 --per-lang 2 每种语言抽几条，或 --skip-missing 只跑已建好的镜像。\n"
              f"  要快：去掉 --topdown。")
        return 1
    if args.topdown and jobs_req > 1:
        # 收敛到 1 之后只有一个 perf 会话，不存在计数器竞争，所以放行而不是报错。
        # 但用户敲的是一组自相矛盾的参数，静默放行会让人以为 --jobs N 生效了。
        print(f"提示：{jobs_asked}与 --topdown 本互斥，但待跑只有 {len(trials)} 条，"
              f"并发度已收敛到 1。\n"
              f"  实际是串行跑，只有一个 perf 会话，不会有计数器竞争 —— 照常采集，不拦。\n")
    if args.metrics and jobs_req > 1:
        # 收敛到 1 之后不会有争抢，指标口径是干净的，所以放行而不是报错。
        # 但用户敲的是一组自相矛盾的参数，静默放行会让人以为 --jobs N 生效了、
        # 事后拿这批数字当「并发采的」去解释——必须说破。
        print(f"提示：{jobs_asked}与 --metrics 本互斥，但待跑只有 {len(trials)} 条，"
              f"并发度已收敛到 1。\n"
              f"  实际是串行跑，不会有争抢，指标口径干净 —— 照常采集，不拦。\n")

    # ── round-robin 跨语言重排：避免没跑完时缺某些语言 ──────────────────
    # 原 load_trials 按 (LANG_ORDER, name) 排，python 全跑完才轮到 go。
    # 如果只跑了 60%，typescript 和 javascript 可能一条没碰到。
    # 重排成 round-robin：每轮从各语言 pool 各取一条，保证跑一半时每种语言都覆盖到。
    if len(trials) > 1:
        pools = {}
        for t in trials:
            pools.setdefault(t["lang"], []).append(t)
        # 各语言内部保持原顺序（按命令数/名字），跨语言交错
        rr = []
        max_pool = max(len(p) for p in pools.values()) if pools else 0
        for i in range(max_pool):
            for lang in sorted(pools.keys(), key=lambda l: LANG_ORDER.index(l) if l in LANG_ORDER else 99):
                if i < len(pools[lang]):
                    rr.append(pools[lang][i])
        trials = rr

    print("=" * 78)
    print(f"bundle    {HERE}")
    print(f"replay.py {replay}")
    print(f"cgroup    {fstype}")
    print(f"内核      {os.uname().release}   CPU {os.cpu_count()}")
    print(f"待跑      {len(trials)} 条（{'串行' if jobs == 1 else f'并发 {jobs}'}）")
    print(f"并发度    {jobs}{jobs_note}   来源 {jobs_src}")
    print(f"指标      {'采集 cgroup 性能数据' if args.metrics else '不采（--metrics 可开）'}")
    if args.topdown:
        print(f"topdown   采（每条改调 {topdown.name}；强制串行，见上文计数器竞争）")
        print(f"          配置 {topdown.parent / 'topdown.conf'}")
        print(f"          ⚠️ 采到的是**整条 trial 的聚合值**：含容器启动与收尾 git diff，")
        print(f"             各 trial 的命令数差异很大，跨 trial 横向比要带上这个背景")
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
            # 用户给了 --trials-dir 就原样带上：两个脚本同目录，相对路径都按脚本所在目录解释，
            # 不带的话 build_arm.sh 只看得见根下那几条，全量集的语言目标会解析错。
            td = f"--trials-dir {args.trials_dir} " if args.trials_dir else ""
            print(f"\n缺 {len(missing)} 个镜像。目标环境通常拉不到 registry，用本地基座重建：")
            print(f"  bash build_arm.sh {td}--ca-cert <内网CA.crt> {langs}")
            print(f"或先跑已建好的部分：  python3 run_batch.py {td}--skip-missing")
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
            r = run_one(idx, t, len(trials), replay, out, args, stream, topdown)
        except Exception as e:
            # 单条炸在 run_one 里（比如日志文件写不了）不该把整批带走，记成失败继续
            r = {"lang": t["lang"], "trial": t["name"], "task_id": t["task_id"],
                 "model": t["model"], "exit_code": -1, "wall_s": 0.0,
                 "verdict": None, "log": f"logs/{t['name']}.log",
                 "vs_baseline": None, "error": repr(e)}
            if args.topdown:
                # 这一条压根没跑起来，topdown 自然也没有。给个占位而不是缺键：
                # 汇总渲染那边按「每条都有 topdown 字段」写的，缺键会在汇总时再炸一次。
                r["topdown"] = {"available": False, "reason": f"这条 trial 没跑起来：{e!r}",
                                "verify": "—", "quadrants": None}
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
                  n_planned=len(trials), interrupted=interrupted,
                  pick_report=pick_report)
    # 收尾顺手出命令类型统计。放在 write_summary 之后：cmd_stats 读的就是刚写好的 summary.json。
    # --dry-run 在上面早就 return 了，走不到这里。
    run_cmd_stats(out)
    return 0 if results and all(r["exit_code"] == 0 for r in results) else 1


def run_cmd_stats(out):
    """对本轮输出目录跑 cmd_stats.py（collect + aggregate），结果落 <out>/cmd_stats/。

    **它失败不能影响本批的退出码**：统计是重放结论之外的附加产物，重放本身跑成什么样
    由 summary.json 说了算。所以这里起子进程（统计脚本自己炸了也带不走本进程）、
    吞掉一切异常，失败只打一行提示，告诉人怎么手动重跑。
    成功时也只打一行：-j 1 的屏幕输出要尽量保持原样。
    """
    script = HERE / "cmd_stats.py"
    retry = f"python3 {script} {out}"
    try:
        if not script.exists():
            print(f"命令统计  跳过：没找到 {script}（旧版包没带它）")
            return
        p = subprocess.run([sys.executable, str(script), str(out)],
                           capture_output=True, text=True, timeout=600)
        if p.returncode == 0:
            m = read_json(out / "cmd_stats" / "manifest.json") or {}
            print(f"命令统计  {out / 'cmd_stats' / 'SUMMARY.md'}"
                  f"（纳入 {m.get('n_included', '?')} 条 / 排除 {m.get('n_excluded', '?')} 条）")
        else:
            why = ((p.stderr or p.stdout).strip().splitlines() or ["无输出"])[-1]
            print(f"⚠️  命令统计失败（退出码 {p.returncode}，不影响本批结果）：{why}；手动重跑 {retry}")
    except Exception as e:                     # 超时 / 起不来 / 其他一切
        print(f"⚠️  命令统计失败（不影响本批结果）：{e!r}；手动重跑 {retry}")


def topdown_group_stats(results):
    """按语言给四象限的均值 / 中位数，外加一行「全部」。

    只统计**自检也全过**的那些条。两道门，缺一不可：
      1. available —— 数采到了、四象限算出来了；
      2. 自检全过 —— C1~C5（+ 可选的 X）一条不红。

    为什么第 2 道门必须有：C5 红了表示发生了计数器复用，那组四象限的分子和分母来自
    不同的时间窗口，**比值本身没有物理意义**；C1 红了表示 SLOTS 或事件号错了，那组数
    整体按同一比例偏移。把这种数混进平均值里，一条坏数据就能把一整门语言的画像带偏，
    而平均值这个形式恰恰把「哪一条坏了」抹掉了 —— 报告上完全看不出来。
    逐条的 ❌C1 / ❌C5 仍然照常出现在上面那张总表里，信息并没有丢，只是不进均值。

    每组都带 n（真正参与统计的条数）和被剔掉的条数，让人自己判断这几个数值不值得信 ——
    n=1 的「中位数」只是那一条本身。

    ⚠️ 这里聚合的是**整条 trial 的聚合值**：每条里面都固定含容器启动和收尾的
       `git diff`，而不同 trial 的命令数从几十到几百不等，固定开销的占比因此差很多。
       所以跨语言的差异里既有 workload 本身的差异，也有「这门语言这几条 trial 恰好
       更轻/更重」的成分。**这不是纯 workload 对比。**
    """
    by_lang, allv = {}, []
    dropped, dropped_all = {}, 0
    for r in results:
        td = r.get("topdown") or {}
        if not td.get("available"):
            continue
        # available=True 已经保证四个象限都是数（见 collect_topdown），所以这里
        # 只剩「自检过没过」一道门。于是账是闭合的：
        #   可用条数 = 进均值的 n + 被剔掉的 n_excluded_check_failed
        vals = {k: (td.get("quadrants") or {}).get(k) for k, _ in TOPDOWN_QUADRANTS}
        if td.get("checks_failed"):
            dropped[r["lang"]] = dropped.get(r["lang"], 0) + 1
            dropped_all += 1
            continue
        by_lang.setdefault(r["lang"], []).append(vals)
        allv.append(vals)

    def block(rows, n_drop):
        out = {"n": len(rows), "n_excluded_check_failed": n_drop}
        for key, _ in TOPDOWN_QUADRANTS:
            xs = [x[key] for x in rows]
            # 一条都没剩就只记 n 和剔除数，均值/中位写 None —— 空列表没有平均数，
            # 硬算会炸；而这一行必须**留着**（见下面 langs 的取法）。
            out[key] = ({"mean": statistics.fmean(xs), "median": statistics.median(xs)}
                        if xs else {"mean": None, "median": None})
        return out

    # 语言名单要把「采到了数但全被剔掉」的那些也算进来，哪怕它们一条都不剩。
    # 否则这门语言会从小结里**整个消失**，读的人只会以为它没跑 —— 而真相是
    # 跑了、也采到了，只是数不可信。这两件事在报告里必须长得不一样。
    seen = set(by_lang) | set(dropped)
    langs = sorted(seen, key=lambda l: LANG_ORDER.index(l) if l in LANG_ORDER else 99)
    stats = {l: block(by_lang.get(l, []), dropped.get(l, 0)) for l in langs}
    if allv:
        stats["__all__"] = block(allv, dropped_all)
    return stats, dropped_all


def topdown_group_lines(stats):
    """把分组小结渲染成表格行（stdout 与 Markdown 共用同一批单元格，免得两边算出两套数）。"""
    hdr = ["语言", "n", "剔除"] + [f"{short} 均值/中位" for _, short in TOPDOWN_QUADRANTS]
    rows = []
    for lang, b in stats.items():
        name = "全部" if lang == "__all__" else lang
        # 「剔除」= 数采到了但自检没过、因此不进均值的条数。必须单独成列而不是脚注：
        # n=2 剔除 3 和 n=2 剔除 0 是完全不同的可信度，混在一个 n 里看不出来。
        row = [name, str(b["n"]), str(b.get("n_excluded_check_failed", 0))]
        for key, _ in TOPDOWN_QUADRANTS:
            m, md = b[key]["mean"], b[key]["median"]
            row.append("—" if m is None else f"{m * 100:.1f}% / {md * 100:.1f}%")
        rows.append(row)
    return hdr, rows


def write_summary(out, results, elapsed, args, fstype, jobs=1, n_planned=None,
                  interrupted=False, pick_report=None):
    smoke = bool(args.smoke)
    # 没开 --topdown 时下面所有 topdown 相关的分支都不进，报告与加本功能前**逐字节一致**
    # —— 历史批次的 SUMMARY.md 要能直接 diff，多一行空行都算回归。
    td_on = bool(getattr(args, "topdown", False))
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
        row = {
            "语言": r["lang"],
            "退出": r["exit_code"],
            "保真": ("—(冒烟)" if smoke else ("✅" if pi else "❌" if pi is False else "?")),
            "rc_match": v.get("rc_match", "—"),
            "rc_语义": v.get("rc_match_semantic", "—"),
            "耗时s": v.get("elapsed_s", r["wall_s"]),
            "vs基线": (f"rc{c['rc_delta']:+d} 时长×{c['elapsed_ratio']}"
                       if c and c.get("elapsed_ratio") else "—"),
        }
        if td_on:
            # **追加在原有那张总表后面**，不另起一张：这张表就是用来横向对比不同
            # benchmark 的，把四象限拆到另一张表就得来回对着行名找，一眼比不了。
            # 采废的那条这几列写 —，重放结论（退出/保真/rc）照常记 —— 两者正交。
            td = r.get("topdown") or {}
            q = td.get("quadrants") or {}
            for key, short in TOPDOWN_QUADRANTS:
                val = q.get(key)
                row[short] = (f"{val * 100:.1f}%" if isinstance(val, (int, float)) else "—")
            row["校验"] = td.get("verify") or "—"
        rows.append(row)

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

    # ── 按语言分组的横向小结（只在 --topdown 下出现）──────────────────────────
    td_stats, td_dropped = topdown_group_stats(results) if td_on else ({}, 0)
    n_td_ok = sum(1 for r in results if (r.get("topdown") or {}).get("available"))
    if td_on:
        lines += ["", f"topdown  {n_td_ok}/{len(results)} 条采到可用数据"
                      + (f"，其中 {td_dropped} 条自检没过、不进下面的均值" if td_dropped else "")]
        if td_stats:
            g_hdr, g_rows = topdown_group_lines(td_stats)
            gw = {i: max(len(g_hdr[i]), *(len(r[i]) for r in g_rows))
                  for i in range(len(g_hdr))}
            lines.append("  " + "  ".join(g_hdr[i].ljust(gw[i]) for i in range(len(g_hdr))))
            lines.append("  " + "  ".join("-" * gw[i] for i in range(len(g_hdr))))
            for r in g_rows:
                lines.append("  " + "  ".join(r[i].ljust(gw[i]) for i in range(len(g_hdr))))
            lines += ["  ⚠️ 这是**整条 trial 的聚合值**：每条都含容器启动与收尾 git diff，",
                      "     而各 trial 的命令数从几十到几百不等，固定开销占比因此差很多。",
                      "     跨 trial / 跨语言的差异里混着这部分，不是纯 workload 对比。",
                      "     n = 真正进均值的条数（自检没过的不算，见上表「校验」列）。"]
        else:
            lines.append("  （没有一条既采到数据又自检全过，分组小结略）")
    # 并发下总墙钟 ≠ 各条耗时之和，标一句免得被当成串行时长去比
    lines += ["", f"总墙钟 {elapsed:.0f}s" + (f"（并发 {jobs}，非各条之和）" if jobs > 1 else ""),
              f"输出   {out}"]
    if not smoke and any((r["verdict"] or {}).get("patch_identical") is False for r in results):
        lines += ["", "⚠️  有 patch_identical=false —— 环境与原始运行不一致，性能数字不可用。",
                  "    先看该条 logs/*.log 里的 rc 不匹配和时序背离。"]
    text = "\n".join(lines)
    print(text)

    doc = {
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
    }
    # 抽样跑必须留痕：抽样批次的 summary.json 和全量批次长得一模一样，没有这一块
    # 事后会被当成全量结论去引用。策略也要记 —— median 那批天生更轻、固定开销占比更大，
    # 和 heaviest 那批**不可直接比较**。
    if pick_report is not None:
        doc["options"]["per_lang"] = args.per_lang
        doc["options"]["pick"] = args.pick
        doc["sampling"] = {
            "per_lang": args.per_lang,
            "pick": args.pick,
            "pick_desc": PICK_DESC.get(args.pick, args.pick),
            "n_selected": sum(r["n_picked"] for r in pick_report),
            "n_available_total": sum(r["n_available"] for r in pick_report),
            "n_candidates_total": sum(r["n_candidates"] for r in pick_report),
            "by_lang": pick_report,
            "note": "这批是抽样跑，不是全量结果；不同 --pick 策略选出的批次之间不可直接比较",
        }
    if td_on:
        doc["options"]["topdown"] = True
        doc["topdown"] = {
            "enabled": True,
            "n_available": n_td_ok,
            "n_unavailable": len(results) - n_td_ok,
            "n_excluded_check_failed": td_dropped,
            "by_lang": td_stats,
            "note": ("四象限是整条 trial 的聚合值，含容器启动与收尾 git diff；"
                     "各 trial 命令数差异很大，跨 trial 比较需带上这个背景"),
        }
    (out / "summary.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False))

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
    # 抽样跑同样只在真抽样时插行 —— 全量批次的报告要能和历史报告逐行 diff。
    if pick_report is not None:
        picked_n = sum(r["n_picked"] for r in pick_report)
        avail_n = sum(r["n_available"] for r in pick_report)
        cand_n = sum(r["n_candidates"] for r in pick_report)
        md.append(f"- ⚠️ **抽样跑**：`--per-lang {args.per_lang} --pick {args.pick}`"
                  f"（{PICK_DESC.get(args.pick, args.pick)}）—— "
                  f"候选 {cand_n} 条 / 镜像已建好 {avail_n} 条 / 实际选中 **{picked_n} 条**。"
                  f"下面的结果**不是全量结论**；不同 `--pick` 策略选出的批次之间也不可直接比较。")
        for r in pick_report:
            if r["n_picked"] == 0:
                md.append(f"  - `{r['语言']}`：{r['n_available']}/{r['n_candidates']} "
                          f"已建镜像 → 跳过")
                continue
            short = f"（可用的不够 {args.per_lang} 条）" if r["short"] else ""
            names = "、".join(f"`{x['trial']}`({x['n_commands']} 条命令)"
                              for x in r["trials"])
            md.append(f"  - `{r['语言']}`：{r['n_available']}/{r['n_candidates']} "
                      f"已建镜像 → 取 {r['n_picked']} 条{short} —— {names}")
    if td_on:
        md.append(f"- topdown：**{n_td_ok}/{len(results)} 条**采到可用数据"
                  + (f"，其中 **{td_dropped} 条自检没过**、不进下面的均值"
                     if td_dropped else "")
                  + "。强制串行（`--jobs > 1` 会被拒绝："
                    "多个 `perf stat -a` 抢同一批物理计数器）")
    md.append("")
    if rows:
        md += ["| " + " | ".join(hdr) + " |",
               "|" + "|".join("---" for _ in hdr) + "|"]
        md += ["| " + " | ".join(str(r[h]) for r in [row] for h in hdr) + " |" for row in rows]
    if td_on:
        md += ["", "## topdown 横向小结（按语言）", ""]
        if td_stats:
            g_hdr, g_rows = topdown_group_lines(td_stats)
            md += ["| " + " | ".join(g_hdr) + " |",
                   "|" + "|".join("---" for _ in g_hdr) + "|"]
            md += ["| " + " | ".join(r) + " |" for r in g_rows]
            md += ["",
                   "> `n` = 真正进均值的条数；`剔除` = 数采到了但自检没过（见上表「校验」列）、",
                   "> 因此**不进均值**的条数 —— C5 红了说明发生了计数器复用，那组四象限的分子",
                   "> 和分母来自不同的时间窗口，比值没有物理意义，混进平均数只会污染整组。",
                   "",
                   "> ⚠️ **这是整条 trial 的聚合值，不是纯 workload 对比。** 采集窗口从主容器",
                   "> 出现到 `replay.py` 退出，里面固定含**容器启动**和收尾的 `git diff` 保真校验；",
                   "> 而各 trial 的命令数从几十到几百不等，这笔固定开销占聚合值的比重因此差很多 ——",
                   "> 轻的 trial 里它能占大头。所以跨 trial / 跨语言的差异中混着「这几条恰好更轻",
                   "> 或更重」的成分，读的时候要一并带上上表里各条的命令数。",
                   "> 要真正干净的对比，得等 per-command 归因那一版。"]
        else:
            md += ["（没有一条采到可用数据。逐条看下面「topdown」那几行的原因。）"]
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
        if td_on:
            td = r.get("topdown") or {}
            if td.get("available"):
                q = td.get("quadrants") or {}
                quad = "，".join(f"{short} {q[key] * 100:.1f}%"
                                 for key, short in TOPDOWN_QUADRANTS
                                 if isinstance(q.get(key), (int, float)))
                be = {"direct": "直接法", "residual": "残差法"}.get(
                    td.get("backend_method"), td.get("backend_method"))
                md += [f"- topdown：{quad}",
                       f"  - 口径：{be}，SLOTS={td.get('slots')}，"
                       f"事件 {td.get('n_events')} 个"
                       f"{'，开了 X 交叉校验' if td.get('cross_check_enabled') else '，未开 X 交叉校验'}",
                       f"  - 自检：{td.get('verify')}"
                       + (f"（跑过 {len(td.get('checks_run') or [])} 条）"
                          if td.get("checks_run") else ""),
                       f"  - 机读：`{(td.get('artifacts') or {}).get('topdown_json')}`"]
            else:
                # 采废了要**在这条 trial 名下**写清楚原因，而不是只在总表里打一个 —。
                # 上面的退出码 / patch_identical 仍然是有效结论 —— 两者正交，
                # 不写清楚的话读报告的人会以为这条 trial 整个失败了。
                md += [f"- topdown：**不可用** —— {td.get('reason') or '未知原因'}",
                       f"  （这不影响上面的 patch_identical 与 rc_match：保真度与 PMU "
                       f"数据可信度是正交的两件事）"]
                if td.get("replay_rc") is not None:
                    md.append(f"  - 退出码分解：重放 {td.get('replay_rc')} / "
                              f"perf {td.get('perf_rc')} / 解析 {td.get('parse_rc')}")
        md.append("")
    (out / "SUMMARY.md").write_text("\n".join(md))
    print(f"       {out}/SUMMARY.md, summary.json")


if __name__ == "__main__":
    raise SystemExit(main())
