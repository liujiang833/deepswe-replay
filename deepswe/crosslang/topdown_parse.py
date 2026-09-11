#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 perf stat 的输出（JSON Lines 或 CSV）算成 ARM L1 topdown 四象限。

只用标准库。输入两样东西：
  1. perf 的输出文件（topdown_trial.sh / probe_pmu.sh 写出来的）
  2. topdown.conf（拿 SLOTS 和事件名单；事件号本身在这里只用于回显）

输出：五个事件的原始计数 + 四象限比值 + 两个自检，同时落一份 topdown.json 给机读。

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

# 五个必需事件：配置里的 KEY → 传给 perf 的 name= → 打印时的人话
L1_EVENTS = [
    ("EV_CPU_CYCLES",    "cpu_cycles",          "CPU_CYCLES"),
    ("EV_OP_RETIRED",    "op_retired",          "OP_RETIRED"),
    ("EV_OP_SPEC",       "op_spec",             "OP_SPEC"),
    ("EV_STALL_SLOT_FE", "stall_slot_frontend", "STALL_SLOT_FRONTEND"),
    ("EV_STALL_SLOT_BE", "stall_slot_backend",  "STALL_SLOT_BACKEND"),
]

SUM_TOL = 0.03      # 四象限求和容差：1 ± 0.03
PCNT_MIN = 99.9     # 调度占比下限，低于它说明发生了计数器复用

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
    """先精确匹配，再退回子串匹配。

    退回子串是为了兼容「perf 没把 name= 当成显示名，而是原样回显整段事件描述」
    的版本 —— 那时 field 长这样：armv8_pmuv3_0/event=0x11,name=cpu_cycles/。
    子串匹配要求唯一命中，否则宁可不认（名字互为前缀会出歧义）。
    """
    if field in names:
        return field
    hits = [n for n in names if n in field]
    return hits[0] if len(hits) == 1 else None


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

    extras = extra_events(conf)
    names = [n for _, n, _ in L1_EVENTS] + [n for n, _ in extras]

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

    print("=" * 62)
    print(f" {args.title}")
    print(f" perf 输出  {perf_path}  （按 {fmt.upper()} 解析）")
    print(f" 配置       {conf_path}")
    print("=" * 62)

    # ── 原始计数 ──
    print()
    print("── 原始计数 ────────────────────────────────────────────────")
    missing, unsupported = [], []
    for key, name, human in L1_EVENTS:
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

    result = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
                                  .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "perf_out": str(perf_path),
        "perf_format": fmt,
        "conf": conf_path,
        "pmu": conf.get("PMU") or None,
        "slots": slots,
        "events": {r["name"]: {"value": r["value"], "pcnt_running": r["pcnt_running"],
                               "cgroup": r["cgroup"]} for r in by_name.values()},
        "topdown": None,
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
            print("   （五个里只要有一个不对，整组 {} 都开不起来。）")
            print("   如果是所有事件都 <not supported>，先跑 probe_pmu.sh 看 PMU 有没有被识别。")
        if missing:
            print(f"❌ perf 输出里缺事件: {', '.join(missing)}")
            print("   要么 -e 里的 name= 和本脚本的名单对不上（两边必须一致），")
            print("   要么 perf 压根没跑起来 —— 看一眼 perf 输出文件的原始内容。")
        result["checks"]["counters_present"] = False
        return dump(1)
    result["checks"]["counters_present"] = True

    vals = {name: by_name[name]["value"] for _, name, _ in L1_EVENTS}

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

    quad = {
        "Retiring":      vals["op_retired"] / denom,
        "BadSpec":       (vals["op_spec"] - vals["op_retired"]) / denom,
        "FrontendBound": vals["stall_slot_frontend"] / denom,
        "BackendBound":  vals["stall_slot_backend"] / denom,
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
        print(f"  {k:14s} {v * 100:6.2f}%  {bar}")
    # 「求和」2 个汉字占 4 个显示列，按字符数补齐会和上面的 ASCII 标签错开，手动补
    print(f"  求和{' ' * 10} {total * 100:6.2f}%")
    print()
    print(f"  参考：OP_RETIRED / CPU_CYCLES = {ipc:.3f}（每周期退休微操作数）")

    result["topdown"] = {k: quad[k] for k in quad}
    result["topdown"]["sum"] = total
    result["topdown"]["op_retired_per_cycle"] = ipc

    # ── 两个自检 ──
    print()
    print("── 自检 ────────────────────────────────────────────────────")
    ok = True

    sum_ok = abs(total - 1.0) <= SUM_TOL
    result["checks"]["sum_in_tolerance"] = sum_ok
    result["checks"]["sum_tolerance"] = SUM_TOL
    if sum_ok:
        print(f"  ✅ 四象限求和 {total:.4f}，在 1 ± {SUM_TOL} 内")
    else:
        ok = False
        print(f"  ❌ 四象限求和 {total:.4f}，超出 1 ± {SUM_TOL}")
        print("     四象限本该按定义正好铺满全部 slot，求和不等于 1 只有三种原因：")
        print(f"       1. SLOTS 不对（当前用的是 {slots}）—— 最常见。"
              " 配置里写死过常数？换核了？")
        print("       2. 事件号不对 —— 五个里有一个指到了别的事件，按 TRM 再核一遍")
        print("       3. 发生了计数器复用 —— 看下面那条占比自检")
        if total > 0:
            print(f"     反推：若事件号无误，SLOTS 应约为 {slots * total:.2f}")

    pcnts = [by_name[n]["pcnt_running"] for _, n, _ in L1_EVENTS]
    known = [p for p in pcnts if p is not None]
    if not known:
        ok = False
        result["checks"]["min_pcnt_running"] = None
        result["checks"]["no_multiplexing"] = None
        print("  ❌ perf 输出里没有调度占比字段，无法判断是否发生计数器复用")
        print("     （换个 perf 版本，或改用 PERF_OUTPUT=json 再采一次）")
    else:
        lo = min(known)
        mux_ok = lo > PCNT_MIN
        result["checks"]["min_pcnt_running"] = lo
        result["checks"]["no_multiplexing"] = mux_ok
        if mux_ok:
            print(f"  ✅ 最低调度占比 {lo:.2f}% > {PCNT_MIN}%，没有发生计数器复用")
        else:
            ok = False
            n_ev = len(L1_EVENTS) + len(extras)
            print(f"  ❌ 最低调度占比 {lo:.2f}% ≤ {PCNT_MIN}% —— 发生了计数器复用")
            print("     复用之下每个事件只在一部分时间真在计数，其余靠外推，"
                  "四象限不再可信。")
            print(f"     本轮一共开了 {n_ev} 个事件（5 个必需 + EV_EXTRA {len(extras)} 个）。")
            if n_ev > 6:
                print("     多半就是事件开多了：Neoverse 一般只有 6 个通用计数器。")
                print("     去掉 topdown.conf 里的 EV_EXTRA 分两轮采。")
            else:
                print("     事件数不算多，那问题在别处：本机通用计数器可能少于 6 个，")
                print("     或者同一时刻还有另一个 perf 会话在抢计数器（ps aux | grep perf）。")
            print("     本机计数器个数看 probe_pmu.sh 的「计数器个数」一行。")

    print()
    print("  " + ("✅ 两个自检都过，这组四象限可用。"
                  if ok else "❌ 自检未全过，上面这组四象限不要直接用。"))
    return dump(0 if ok else 2)


if __name__ == "__main__":
    sys.exit(main())
