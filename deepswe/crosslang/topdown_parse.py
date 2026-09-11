#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 perf stat 的输出（JSON Lines 或 CSV）算成 ARM L1 topdown 四象限。

只用标准库。输入两样东西：
  1. perf 的输出文件（topdown_trial.sh / probe_pmu.sh 写出来的）
  2. topdown.conf（拿 SLOTS 和事件名单；事件号本身在这里只用于回显和判口径）

输出：原始计数 + 四象限比值 + 一组自检，同时落一份 topdown.json 给机读。

—— 两种后端口径，必须分清 ——

**直接法**（topdown.conf 里 EV_STALL_SLOT_BE 填了值）：
    BackendBound = STALL_SLOT_BACKEND / (CPU_CYCLES × SLOTS)
四个象限各自独立测量，于是「四象限求和 ≈ 1」是一条**真的**自检 ——
分母（SLOTS）错了、某个事件号指错了、发生了复用，求和都会离开 1。

**残差法**（EV_STALL_SLOT_BE 留空，目标核没实现这个计数器时的唯一选择）：
    BackendBound = 1 − (Retiring + BadSpec + FrontendBound)
此时**求和恒等于 1**，那条自检退化成恒等式，判别力为零。
继续给它打一个绿色 ✅ 比不检查更糟 —— 那是**假的安全感**。所以本脚本在残差法下
**不给求和打 ✅**，改跑下面这组仍然有判别力的不等式：

    C1 残差非负     Retiring + BadSpec + FrontendBound ≤ 1
    C2 投机 ≥ 退休   OP_SPEC ≥ OP_RETIRED
    C3 退休率封顶   OP_RETIRED / CPU_CYCLES ≤ SLOTS
    C4 取值域       四个象限都落在 [0, 1]
    C5 复用检查     最低调度占比 > 99.9%

C2~C5 在两种口径下都成立，所以直接法下**求和自检 + C2~C5 一起跑**，只是不跑 C1
（直接法下 R+BS+FE 本来就可以 > 1，那正是求和自检要抓的现象，不该由 C1 重复报）。

另外，topdown.conf 里 EV_STALL_SLOT（PMUv3 架构值 0x003f，总停顿 slot 数）填了的话，
会多跑一条 **X 交叉校验**：
    BE_indep = (STALL_SLOT − STALL_SLOT_FRONTEND) / (CPU_CYCLES × SLOTS)
拿它和上面算出来的 BackendBound 比。这条不依赖「1 减出来」的恒等式，是残差法下
**唯一**能恢复的真·交叉校验 —— 它能抓到求和自检本来该抓的那些错，
包括 C1~C4 一概抓不到的「SLOTS 偏大」。

—— 为什么解析要这么小心 ——

**CSV 的列序不可靠。** 不加 -G 时 perf 的 CSV 是
    counter-value,unit,event,run-time,pcnt-running,...
加了 -G 之后会**多出一列 cgroup**，而且各 perf 版本把它插在事件名前还是后并不一致
（6.x 是插在事件名之后，更早的版本另有做法）。按列号取值在某一台机器上能跑通，
换一台就静默读错列 —— 读到的还是个数字，不会报错。所以这里一律**按事件的
`name=` 匹配**：topdown_trial.sh 在 -e 里给每个事件都起了唯一名字，先定位事件名
那一列，再从它周围按「形状」找计数值和调度占比，列号一次都不用。

**JSON 是逐行 JSON 对象，不是一个数组。** `perf stat -j` 每个事件吐一行独立的
`{...}`，整文件 json.load() 会直接报错。必须逐行解析（本脚本仍保留了整文件是
数组时的兜底，以防哪个版本改了主意）。
"""

import argparse
import datetime
import json
import os
import pathlib
import re
import sys

# L1 四象限**永远必需**的四个事件：配置里的 KEY → 传给 perf 的 name= → 打印时的人话
L1_REQUIRED = [
    ("EV_CPU_CYCLES",    "cpu_cycles",          "CPU_CYCLES"),
    ("EV_OP_RETIRED",    "op_retired",          "OP_RETIRED"),
    ("EV_OP_SPEC",       "op_spec",             "OP_SPEC"),
    ("EV_STALL_SLOT_FE", "stall_slot_frontend", "STALL_SLOT_FRONTEND"),
]
# 可选：填了就走直接法，留空就走残差法
EV_BE = ("EV_STALL_SLOT_BE", "stall_slot_backend", "STALL_SLOT_BACKEND")
# 可选：总停顿 slot 数，只用来做交叉校验，不参与四象限
# （perf 的 name= 刻意取 stall_slot_total 而不是 stall_slot：后者是
#  stall_slot_frontend / stall_slot_backend 的前缀，子串匹配会出歧义。）
EV_TOT = ("EV_STALL_SLOT", "stall_slot_total", "STALL_SLOT")

SUM_TOL = 0.03      # 直接法下四象限求和容差：1 ± 0.03
CROSS_TOL = 0.03    # X 交叉校验容差：两种口径算出的 BackendBound 差值上限
PCNT_MIN = 99.9     # 调度占比下限，低于它说明发生了计数器复用
# 不等式类自检（C1 / C3 / C4）的越界容差。
# 硬件定义上这些不等式是**严格成立**的，给 0.005 纯粹是为了容忍计数器起停那一点
# 极小偏斜（组内事件同上同下，偏斜极小），不是为了放水 —— 真出错时偏离量是
# 几个百分点起步，0.005 拦不住的错也就不存在了。
EPS = 0.005

# perf 采不到时会把这些字符串原样放进 counter-value
NOT_COUNTED = ("<not supported>", "<not counted>", "<unsupported>", "<未支持>")

# 调度占比的「形状」：必须带小数点，且落在 0~100。
# 要求带小数点是有意的 —— run-time 是纳秒整数（几十亿），不带点就不会被误认成占比。
_PCNT_RE = re.compile(r"^\d+\.\d{1,6}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


# ── topdown.conf ───────────────────────────────────────────────────────────
def parse_conf(path):
    """逐行读 KEY=VALUE。刻意不调 shell —— 配置文件是要被 source 的，
    但 python 这边跟着 source 一次就等于给了配置文件任意执行权，不值当。"""
    conf = {}
    if not os.path.exists(path):
        return conf
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            # 带引号的值：先吃掉配对的引号，引号之后的内容一律当注释
            # （EV_EXTRA="a=0x1 b=0x2"  # 说明 —— 这种写法必须支持）
            if v[:1] in ("'", '"'):
                q = v[0]
                end = v.find(q, 1)
                v = v[1:end] if end > 0 else v[1:]
            else:
                v = v.split("#", 1)[0].strip()
            conf[k] = v
    return conf


def extra_events(conf):
    """EV_EXTRA="名字=0x00xx 名字2=0x00yy" → [(名字, 事件号), ...]"""
    out = []
    for tok in (conf.get("EV_EXTRA") or "").split():
        if "=" not in tok:
            continue
        name, code = tok.split("=", 1)
        name, code = name.strip(), code.strip()
        if name and _NAME_RE.match(name):
            out.append((name, code))
    return out


# ── perf 输出 ──────────────────────────────────────────────────────────────
def _num(s):
    """counter-value 可能是字符串也可能是数字，还可能是 <not supported>。"""
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    for m in NOT_COUNTED:
        if m in s:
            return None
    s = s.replace(",", "")          # 没加 -x 时 perf 会给大数加千位分隔符
    try:
        return float(s)
    except ValueError:
        return None


def _rec(name, value, pcnt, cgroup, raw):
    return {"name": name, "value": value, "pcnt_running": pcnt,
            "cgroup": cgroup, "raw": raw}


def parse_json_lines(text, names):
    """perf stat -j：一行一个 JSON 对象。"""
    objs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict):
            objs.append(o)
    if not objs:
        # 兜底：万一哪个版本把整个输出包成了数组
        try:
            o = json.loads(text)
            if isinstance(o, list):
                objs = [x for x in o if isinstance(x, dict)]
        except ValueError:
            pass

    rows = []
    for o in objs:
        if "counter-value" not in o:
            continue            # metric 行（只有 metric-value / metric-unit），跳过
        ev = str(o.get("event", ""))
        hit = _match_name(ev, names)
        if hit is None:
            continue
        pcnt = o.get("pcnt-running")
        try:
            pcnt = float(pcnt) if pcnt is not None else None
        except (TypeError, ValueError):
            pcnt = None
        rows.append(_rec(hit, _num(o["counter-value"]), pcnt,
                         o.get("cgroup"), str(o["counter-value"])))
    return rows


def _match_name(field, names):
    """先精确匹配，再退回子串匹配（多个命中时取**最长**的那个）。

    退回子串是为了兼容「perf 没把 name= 当成显示名，而是原样回显整段事件描述」
    的版本 —— 那时 field 长这样：armv8_pmuv3_0/event=0x11,name=cpu_cycles/。

    「取最长」不是可有可无的细节：名单里一旦出现互为前缀的名字
    （典型的就是 stall_slot / stall_slot_frontend，EV_EXTRA 里也很容易撞上），
    一个 field 会同时命中两个，「唯一命中才算」的老规则会把这个事件直接**丢掉**，
    表现是「perf 输出里缺事件」，排查方向完全被带偏。最长命中是唯一正确的那个：
    field 里真正出现的是长名字，短名字只是它的一段。只有最长的那个不唯一
    （两个一样长的名字同时出现在一个 field 里）才真的有歧义，那时宁可不认。
    """
    if field in names:
        return field
    hits = [n for n in names if n in field]
    if not hits:
        return None
    longest = max(len(n) for n in hits)
    top = [n for n in hits if len(n) == longest]
    return top[0] if len(top) == 1 else None


def parse_csv(text, names):
    """perf stat -x,：按事件名定位，绝不按列号。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = [x.strip() for x in line.split(",")]
        idx = None
        for i, v in enumerate(f):
            hit = _match_name(v, names)
            if hit is not None:
                idx, name = i, hit
                break
        if idx is None:
            continue

        # counter-value：perf 的 CSV 一直把它放在第 0 列（cgroup 是插在事件名附近，
        # 动不到第 0 列）。但仍然做一次形状校验 —— 万一哪个版本变了，宁可从全行里
        # 重新找，也不要读到一个「看着像数字」的别的列。
        value = _num(f[0]) if f else None
        if value is None and f and not any(m in f[0] for m in NOT_COUNTED):
            for v in f[:idx]:
                value = _num(v)
                if value is not None:
                    break

        # 调度占比：事件名之后第一个「带小数点且 ≤ 100」的字段。
        # cgroup 那一列是路径（system.slice/docker-xxx.scope），带点但不是纯数字，
        # 不会被误认；run-time 是纳秒整数，不带点也不会被误认。
        pcnt = None
        for v in f[idx + 1:]:
            if _PCNT_RE.match(v):
                x = float(v)
                if 0.0 <= x <= 100.0:
                    pcnt = x
                    break
        cg = None
        for v in f[idx + 1:]:
            if "/" in v or ".scope" in v or ".slice" in v:
                cg = v
                break
        rows.append(_rec(name, value, pcnt, cg, f[0] if f else line))
    return rows


def sniff_format(text, declared):
    """declared 为 auto 时看内容第一非空行：以 { 开头就是 JSON。
    以内容为准而不是以 topdown.conf 的 PERF_OUTPUT 为准 —— 配置写的是「想要什么」，
    实际拿到什么取决于那台机器上的 perf 支不支持 -j。"""
    if declared in ("json", "csv"):
        return declared
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return "json" if line.startswith("{") or line.startswith("[") else "csv"
    return "csv"


# ── 主流程 ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="把 perf stat 的输出算成 ARM L1 topdown 四象限")
    ap.add_argument("perf_out", help="perf 的输出文件（-j 的 JSON Lines 或 -x, 的 CSV）")
    ap.add_argument("--conf", default="", help="topdown.conf 路径（默认取脚本同目录）")
    ap.add_argument("--format", choices=("auto", "json", "csv"), default="auto",
                    help="强制输出格式；默认 auto 按文件内容判断")
    ap.add_argument("--json-out", default="",
                    help="机读结果落盘路径（默认 <perf_out 所在目录>/topdown.json）")
    ap.add_argument("--slots", default="",
                    help="覆盖 SLOTS（一般不用；调用方已经从 caps/slots 读过了）")
    ap.add_argument("--title", default="ARM L1 Topdown",
                    help="打印时的标题，probe 与正式采集用它区分")
    args = ap.parse_args()

    here = pathlib.Path(__file__).resolve().parent
    conf_path = args.conf or str(here / "topdown.conf")
    conf = parse_conf(conf_path)

    perf_path = pathlib.Path(args.perf_out)
    if not perf_path.exists():
        print(f"❌ perf 输出文件不存在: {perf_path}")
        return 1
    text = perf_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print(f"❌ perf 输出文件是空的: {perf_path}")
        print("   多半是 perf 根本没起来（没装 / 没权限 / sudo 失败）——")
        print("   去看同目录下的 perf.stderr 或 topdown_trial.sh 的屏幕输出。")
        return 1

    # ── 口径判定：EV_STALL_SLOT_BE 有值 = 直接法，留空 = 残差法 ──
    # 这个判定必须和 topdown_trial.sh / probe_pmu.sh 拼事件组的口径完全一致
    # （它们同样是「空就不拼进 -e」），否则这里等一个 perf 根本没采的事件。
    be_code = (conf.get(EV_BE[0]) or "").strip()
    tot_code = (conf.get(EV_TOT[0]) or "").strip()
    mode = "direct" if be_code else "residual"

    wanted = list(L1_REQUIRED)
    if be_code:
        wanted.append(EV_BE)
    if tot_code:
        wanted.append(EV_TOT)

    extras = extra_events(conf)
    names = [n for _, n, _ in wanted] + [n for n, _ in extras]

    fmt = sniff_format(text, args.format)
    rows = parse_json_lines(text, names) if fmt == "json" else parse_csv(text, names)

    by_name = {}
    for r in rows:
        by_name.setdefault(r["name"], r)   # 同名多行（多 cgroup）只取第一行

    # SLOTS：命令行 > 配置文件。两个都没有就没法算四象限 —— 直接报错，
    # 绝不「猜一个常数」：猜错会让四个比值一起按同一比例静默偏移。
    slots_raw = (args.slots or conf.get("SLOTS") or "").strip()
    slots = None
    if slots_raw:
        try:
            slots = int(slots_raw, 0)
        except ValueError:
            print(f"❌ SLOTS 值无法解析: {slots_raw!r}")
            return 1

    if mode == "direct":
        mode_line = ("直接法（EV_STALL_SLOT_BE=%s）"
                     "  BackendBound = STALL_SLOT_BACKEND / (CPU_CYCLES × SLOTS)" % be_code)
    else:
        mode_line = ("残差法（EV_STALL_SLOT_BE 留空）"
                     "  BackendBound = 1 − (Retiring + BadSpec + FrontendBound)")

    print("=" * 62)
    print(f" {args.title}")
    print(f" perf 输出  {perf_path}  （按 {fmt.upper()} 解析）")
    print(f" 配置       {conf_path}")
    print(f" 后端口径   {mode_line}")
    if mode == "residual":
        print("            ⚠️ 残差法下「四象限求和 ≈ 1」是恒等式，**不是校验**；"
              "改跑 C1~C5")
    print(f" 交叉校验   {'EV_STALL_SLOT=' + tot_code + ' → 跑 X 交叉校验' if tot_code else 'EV_STALL_SLOT 留空 → 不跑 X 交叉校验'}")
    print("=" * 62)

    # ── 原始计数 ──
    print()
    print("── 原始计数 ────────────────────────────────────────────────")
    missing, unsupported = [], []
    # 打印顺序固定成「四个必需 + BE + STALL_SLOT」，留空的也占一行（值写 (留空)）——
    # 这样这轮到底采了什么、哪个是空的，位置不变，扫一眼就知道。
    # 「(留空)」里 2 个汉字各占 2 个显示列，按字符数补齐会和 {code:8s} 那一列错开，手动补。
    display = list(L1_REQUIRED)
    display.append(EV_BE if be_code else (EV_BE[0], None, EV_BE[2]))
    display.append(EV_TOT if tot_code else (EV_TOT[0], None, EV_TOT[2]))
    for key, name, human in display:
        if name is None:
            why = ("—— 不采集，BackendBound 走残差法" if key == EV_BE[0]
                   else "—— 不采集，X 交叉校验不跑")
            print(f"  {human:22s} (留空)   {why}")
            continue
        r = by_name.get(name)
        code = conf.get(key, "?")
        if r is None:
            missing.append(name)
            print(f"  {human:22s} {code:8s} ❌ perf 输出里找不到这个事件")
            continue
        if r["value"] is None:
            unsupported.append(name)
            print(f"  {human:22s} {code:8s} ❌ {r['raw'][:60]}")
            continue
        pc = "?" if r["pcnt_running"] is None else f"{r['pcnt_running']:.2f}%"
        print(f"  {human:22s} {code:8s} {int(r['value']):>18,d}   调度占比 {pc}")

    if extras:
        print()
        print("  额外事件（只打印，不参与四象限）：")
        for name, code in extras:
            r = by_name.get(name)
            if r is None or r["value"] is None:
                print(f"  {name:22s} {code:8s} ❌ 没采到"
                      f"{'' if r is None else '：' + r['raw'][:50]}")
                continue
            pc = "?" if r["pcnt_running"] is None else f"{r['pcnt_running']:.2f}%"
            print(f"  {name:22s} {code:8s} {int(r['value']):>18,d}   调度占比 {pc}")

    n_ev = len(wanted) + len(extras)
    event_codes = {name: (conf.get(key) or "").strip() for key, name, _ in wanted}
    event_codes.update({name: code for name, code in extras})
    result = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "perf_out": str(perf_path),
        "perf_format": fmt,
        "conf": conf_path,
        "pmu": conf.get("PMU") or None,
        "slots": slots,
        # 机读侧一眼能看出这轮是哪种口径、跑了哪几条校验
        "backend_method": mode,
        "sum_check_meaningful": mode == "direct",
        "cross_check_enabled": bool(tot_code),
        "n_events": n_ev,
        "events": {r["name"]: {"value": r["value"], "pcnt_running": r["pcnt_running"],
                               "cgroup": r["cgroup"]} for r in by_name.values()},
        # 事件名 → 这轮实际用的事件号。之所以要落盘：批量跑完只剩一堆 topdown.json，
        # 而「数不对」最常见的根因就是事件号指错了 —— 事后光看计数值根本无从判断
        # 当时用的是 0x003a 还是别的。配置文件是会被改的，结果文件必须自证口径。
        "event_codes": event_codes,
        "topdown": None,
        "checks_run": [],
        "checks": {},
    }

    def dump(rc):
        out = pathlib.Path(args.json_out) if args.json_out else perf_path.parent / "topdown.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        result["exit_code"] = rc
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        print()
        print(f"  机读结果  {out}")
        return rc

    if missing or unsupported:
        print()
        if unsupported:
            print("❌ 有事件是 <not supported> —— 这个事件号在当前 PMU 上无效。")
            print("   两种可能：事件号写错了，或者这颗核压根不实现这个事件。")
            print("   ARM PMUv3 只把一小部分事件号定为架构必需，其余各家核自己编号，")
            print("   所以必须逐个对照**目标核 TRM** 的 PMU 事件表核 topdown.conf。")
            print("   （组里只要有一个不对，整组 {} 都开不起来。）")
            if "stall_slot_backend" in unsupported:
                print("   ⚠️ 不支持的正好是 STALL_SLOT_BACKEND —— 这颗核多半没实现它。")
                print("      把 topdown.conf 的 EV_STALL_SLOT_BE 清空改走**残差法**即可，")
                print("      代价是求和自检失效（换成 C1~C5），详见 topdown.conf 的注释。")
            if "stall_slot_total" in unsupported:
                print("   ⚠️ 不支持的正好是 STALL_SLOT —— 这颗核没实现总停顿计数器。")
                print("      把 topdown.conf 的 EV_STALL_SLOT 清空即可，只是少一条交叉校验。")
            print("   如果是所有事件都 <not supported>，先跑 probe_pmu.sh 看 PMU 有没有被识别。")
        if missing:
            print(f"❌ perf 输出里缺事件: {', '.join(missing)}")
            print("   要么 -e 里的 name= 和本脚本的名单对不上（两边必须一致），")
            print("   要么 perf 压根没跑起来 —— 看一眼 perf 输出文件的原始内容。")
        result["checks"]["counters_present"] = False
        return dump(1)
    result["checks"]["counters_present"] = True

    vals = {name: by_name[name]["value"] for _, name, _ in wanted}

    if slots is None:
        print()
        print("❌ SLOTS 未知：topdown.conf 里是空的，命令行也没给 --slots。")
        print("   正常路径是 topdown_trial.sh 运行时从")
        print("   /sys/bus/event_source/devices/<PMU>/caps/slots 读出来再传进来。")
        print("   （不给默认值是刻意的：猜错 SLOTS 会让四个比值一起静默偏移。）")
        return dump(1)

    cyc = vals["cpu_cycles"]
    denom = cyc * slots
    if denom <= 0:
        print()
        print(f"❌ CPU_CYCLES = {int(cyc)}，分母为 0，算不了。")
        print("   计数全 0 通常意味着 -G 过滤的 cgroup 路径不对（滤空了），")
        print("   或者容器在采集窗口里根本没跑 —— 先核对 cgroup 路径。")
        result["checks"]["nonzero"] = False
        return dump(1)
    result["checks"]["nonzero"] = True

    retiring = vals["op_retired"] / denom
    badspec = (vals["op_spec"] - vals["op_retired"]) / denom
    frontend = vals["stall_slot_frontend"] / denom
    r3 = retiring + badspec + frontend      # = (OP_SPEC + STALL_SLOT_FE) / 分母
    if mode == "direct":
        backend = vals["stall_slot_backend"] / denom
    else:
        backend = 1.0 - r3

    quad = {
        "Retiring": retiring,
        "BadSpec": badspec,
        "FrontendBound": frontend,
        "BackendBound": backend,
    }
    total = sum(quad.values())
    ipc = vals["op_retired"] / cyc

    print()
    print("── L1 四象限 ───────────────────────────────────────────────")
    print(f"  SLOTS = {slots}   分母 = CPU_CYCLES × SLOTS = {int(denom):,d}")
    print()
    for k in ("Retiring", "BadSpec", "FrontendBound", "BackendBound"):
        v = quad[k]
        bar = "#" * max(0, min(40, int(round(v * 40))))
        tag = "  ← 残差" if (k == "BackendBound" and mode == "residual") else ""
        print(f"  {k:14s} {v * 100:6.2f}%  {bar}{tag}")
    # 「求和」2 个汉字占 4 个显示列，按字符数补齐会和上面的 ASCII 标签错开，手动补
    if mode == "direct":
        print(f"  求和{' ' * 10} {total * 100:6.2f}%")
    else:
        print(f"  求和{' ' * 10} {total * 100:6.2f}%   ← **恒等于 100%**，"
              "因为 BackendBound 就是 1 减出来的")
        print("                          这一行**不构成校验**，别拿它当安全信号；"
              "判据看下面 C1~C5")
    print()
    print(f"  参考：OP_RETIRED / CPU_CYCLES = {ipc:.3f}（每周期退休微操作数）")

    result["topdown"] = {k: quad[k] for k in quad}
    result["topdown"]["sum"] = total
    result["topdown"]["op_retired_per_cycle"] = ipc
    result["topdown"]["backend_method"] = mode
    result["topdown"]["r3_retiring_badspec_frontend"] = r3

    # ── 自检 ──
    print()
    print("── 自检 ────────────────────────────────────────────────────")
    state = {"ok": True}

    def good(msg, *lines):
        print(f"  ✅ {msg}")
        for ln in lines:
            print(f"     {ln}")

    def bad(msg, *lines):
        state["ok"] = False
        print(f"  ❌ {msg}")
        for ln in lines:
            print(f"     {ln}")

    def record(key, passed):
        result["checks"][key] = passed
        result["checks_run"].append(key)

    # ── 求和：直接法才是校验 ──
    if mode == "direct":
        sum_ok = abs(total - 1.0) <= SUM_TOL
        result["checks"]["sum_tolerance"] = SUM_TOL
        record("sum_in_tolerance", sum_ok)
        if sum_ok:
            good(f"求和  四象限求和 {total:.4f}，在 1 ± {SUM_TOL} 内")
        else:
            bad(f"求和  四象限求和 {total:.4f}，超出 1 ± {SUM_TOL}",
                "四象限本该按定义正好铺满全部 slot，求和不等于 1 只有三种原因：",
                f"  1. SLOTS 不对（当前用的是 {slots}）—— 最常见。配置里写死过常数？换核了？",
                "  2. 事件号不对 —— 五个里有一个指到了别的事件，按 TRM 再核一遍",
                "  3. 发生了计数器复用 —— 看下面 C5")
            if total > 0:
                print(f"     反推：若事件号无误，SLOTS 应约为 {slots * total:.2f}")
    else:
        result["checks"]["sum_in_tolerance"] = None
        print(f"  ·  求和 = {total:.4f} —— 残差法下这是**恒等式，不是校验**，"
              "故不打 ✅/❌")
        print("     （BackendBound = 1 − 其余三项，求和必然是 1，哪怕 SLOTS 和事件号"
              "全错了也一样）")
        print(f"     它原本是防「SLOTS 取错 / 事件号写错」的主要安全网，"
              f"这里由 C1~C4{' + X' if tot_code else ''} 顶上。")

    # ── C1 残差非负（只在残差法下跑）──
    # 直接法下 R+BS+FE > 1 同样是错，但那由「求和自检」报，不必重复一条。
    if mode == "residual":
        c1_ok = r3 <= 1.0 + EPS
        record("C1_residual_nonneg", c1_ok)
        if c1_ok:
            good(f"C1  残差非负：Retiring+BadSpec+FrontendBound = {r3:.4f} ≤ 1"
                 f"（容差 {EPS}）")
        else:
            slots_min = (vals["op_spec"] + vals["stall_slot_frontend"]) / cyc
            result["checks"]["C1_slots_min"] = slots_min
            bad(f"C1  残差非负失败：Retiring+BadSpec+FrontendBound = {r3:.4f} > 1",
                f"    → BackendBound = {backend:.4f}，是个**负数**，物理上不可能。",
                "三种原因：SLOTS 偏小 / 某个事件号指错了 / 发生了计数器复用（看 C5）。",
                f"反推最小自洽 SLOTS = (OP_SPEC + STALL_SLOT_FE) / CPU_CYCLES = "
                f"{slots_min:.4f}",
                f"  （当前用的 SLOTS = {slots}，至少要 {slots_min:.4f} 才不出负数）",
                "  ⚠️ 若这个值**接近某个整数**（常见的是 8 = Neoverse V1、5 = N2），",
                "     那多半是 **SLOTS 取错**而不是事件号错 —— 先去核对",
                "     /sys/bus/event_source/devices/<PMU>/caps/slots 和目标核 TRM。",
                "     离整数很远则更可能是事件号指错了或发生了复用。")

    # ── C2 OP_SPEC ≥ OP_RETIRED ──
    c2_ok = vals["op_spec"] >= vals["op_retired"]
    record("C2_op_spec_ge_op_retired", c2_ok)
    if c2_ok:
        good(f"C2  OP_SPEC {int(vals['op_spec']):,d} ≥ OP_RETIRED "
             f"{int(vals['op_retired']):,d}（BadSpec 非负）")
    else:
        bad(f"C2  OP_SPEC {int(vals['op_spec']):,d} < OP_RETIRED "
            f"{int(vals['op_retired']):,d}",
            f"    → BadSpec = {badspec:.4f}，是个**负数**。",
            "投机执行的 op 必然 ≥ 退休的 op（退休的每一条都先被投机执行过），",
            "所以这几乎必然是 **EV_OP_SPEC / EV_OP_RETIRED 里有一个事件号写错了**，",
            "或者两个写反了。按目标核 TRM 核对 0x003b / 0x003a。",
            "（这条和 SLOTS 无关 —— 分母约掉了，所以它能把「事件号错」从"
            "「SLOTS 错」里单独摘出来。）")

    # ── C3 OP_RETIRED / CPU_CYCLES ≤ SLOTS ──
    c3_ok = ipc <= slots + EPS
    record("C3_op_retired_per_cycle_le_slots", c3_ok)
    if c3_ok:
        good(f"C3  OP_RETIRED/CPU_CYCLES = {ipc:.4f} ≤ SLOTS = {slots}"
             f"（容差 {EPS}）")
    else:
        bad(f"C3  OP_RETIRED/CPU_CYCLES = {ipc:.4f} > SLOTS = {slots}",
            "一个周期最多只能退休 SLOTS 个 op，超了物理上不可能。",
            f"→ SLOTS 至少得是 {ipc:.4f}（即至少 {int(ipc) + 1}），"
            f"当前的 {slots} 偏小；",
            "  或者 EV_CPU_CYCLES / EV_OP_RETIRED 里有一个事件号指错了。",
            "（这条只用到 OP_RETIRED、CPU_CYCLES、SLOTS 三个量，"
            "和前端/后端事件完全无关，",
            "  所以它能独立把「SLOTS 偏小」或「CPU_CYCLES 事件错」摘出来。）")

    # ── C4 四象限取值域 ──
    out_of_range = [(k, v) for k, v in quad.items() if v < -EPS or v > 1.0 + EPS]
    c4_ok = not out_of_range
    record("C4_quadrants_in_range", c4_ok)
    if c4_ok:
        good(f"C4  四个象限都落在 [0, 1] 内（容差 {EPS}）")
    else:
        bad("C4  有象限落在 [0, 1] 之外：",
            *[f"    {k} = {v:.4f}" for k, v in out_of_range])
        print("     比值是「占全部 slot 的份额」，出界就是口径坏了。")
        print("     负数 → 看 C1/C2 哪条也红了，那条的提示更具体；")
        print("     大于 1 → 分母太小（SLOTS 偏小）或该事件的事件号指错了。")

    # ── C5 计数器复用 ──
    pcnts = [by_name[n]["pcnt_running"] for _, n, _ in wanted]
    known = [p for p in pcnts if p is not None]
    if not known:
        state["ok"] = False
        result["checks"]["min_pcnt_running"] = None
        record("C5_no_multiplexing", None)
        print("  ❌ C5  perf 输出里没有调度占比字段，无法判断是否发生计数器复用")
        print("     （换个 perf 版本，或改用 PERF_OUTPUT=json 再采一次）")
    else:
        lo = min(known)
        mux_ok = lo > PCNT_MIN
        result["checks"]["min_pcnt_running"] = lo
        record("C5_no_multiplexing", mux_ok)
        if mux_ok:
            good(f"C5  最低调度占比 {lo:.2f}% > {PCNT_MIN}%，没有发生计数器复用")
        else:
            bad(f"C5  最低调度占比 {lo:.2f}% ≤ {PCNT_MIN}% —— 发生了计数器复用",
                "复用之下每个事件只在一部分时间真在计数，其余靠外推，四象限不再可信。",
                f"本轮一共开了 {n_ev} 个事件"
                f"（{'残差法 4' if mode == 'residual' else '直接法 5'} 个必需"
                f"{' + EV_STALL_SLOT 1 个' if tot_code else ''}"
                f" + EV_EXTRA {len(extras)} 个）。")
            if n_ev > 6:
                print("     多半就是事件开多了：Neoverse 一般只有 6 个通用计数器，")
                print("     watchdog 开着时只剩 5 个。去掉 topdown.conf 里的 EV_EXTRA"
                      "分两轮采，")
                print("     或者先把 EV_STALL_SLOT 清掉（少一条交叉校验，换一个计数器）。")
            else:
                print("     事件数不算多，那问题在别处：本机通用计数器可能少于 6 个，")
                print("     或者同一时刻还有另一个 perf 会话在抢计数器（ps aux | grep perf）。")
            print("     本机计数器个数看 probe_pmu.sh 的「计数器个数」一行。")

    # ── X 交叉校验（EV_STALL_SLOT 填了才有）──
    if tot_code:
        be_indep = (vals["stall_slot_total"] - vals["stall_slot_frontend"]) / denom
        delta = abs(be_indep - backend)
        x_ok = delta <= CROSS_TOL
        result["checks"]["X_backend_indep"] = be_indep
        result["checks"]["X_backend_delta"] = delta
        result["checks"]["X_tolerance"] = CROSS_TOL
        record("X_backend_cross_check", x_ok)
        src = "残差法" if mode == "residual" else "直接法"
        print()
        if x_ok:
            why = ("这是残差法下**唯一**一条不依赖「1 减出来」的恒等式的校验 —— "
                   "它过了，SLOTS 和事件号才算真的对上了。"
                   if mode == "residual" else
                   "它和求和自检各用一组不同的分子，两条都过，"
                   "SLOTS 和事件号基本可以放心了。")
            good(f"X   交叉校验通过：独立算的 BackendBound = {be_indep:.4f}，"
                 f"{src}算的 = {backend:.4f}，差 {delta:.4f} ≤ {CROSS_TOL}",
                 "（独立式 = (STALL_SLOT − STALL_SLOT_FRONTEND) / (CPU_CYCLES × SLOTS)）",
                 why)
        else:
            bad(f"X   交叉校验失败：独立算的 BackendBound = {be_indep:.4f}，"
                f"{src}算的 = {backend:.4f}，差 {delta:.4f} > {CROSS_TOL}",
                "独立式 = (STALL_SLOT − STALL_SLOT_FRONTEND) / (CPU_CYCLES × SLOTS)，",
                "它和四象限用的是同一个分母但不同的分子，两者对不上说明：",
                f"  1. SLOTS 不对（当前 {slots}）—— 这条**能抓到 SLOTS 偏大**，"
                "而 C1~C4 一概抓不到；",
                "  2. STALL_SLOT / STALL_SLOT_FRONTEND 里有一个事件号指错了；",
                "  3. 发生了复用（看 C5）。",
                "在残差法下这是判别力最强的一条，红了就别用这组数。")
    else:
        result["checks"]["X_backend_cross_check"] = None
        if mode == "residual":
            print()
            print("  ·  X 交叉校验**没跑**（topdown.conf 的 EV_STALL_SLOT 留空）。")
            print("     这意味着本轮**没有任何一条校验能抓到「SLOTS 偏大」**：")
            print("     分母放大只会让残差里的 BackendBound 跟着变大，C1~C4 反而更宽松。")
            print("     目标核若实现了 STALL_SLOT（架构值 0x003f），强烈建议填上它。")

    n_run = len(result["checks_run"])
    print()
    print("  " + (f"✅ {n_run} 条自检都过，这组四象限可用。"
                  if state["ok"] else "❌ 自检未全过，上面这组四象限不要直接用。"))
    if state["ok"] and mode == "residual" and not tot_code:
        print("     ⚠️ 但请记住：残差法 + 没有 X 交叉校验 = 「SLOTS 偏大」这类错"
              "查不出来。")
    return dump(0 if state["ok"] else 2)


if __name__ == "__main__":
    sys.exit(main())
