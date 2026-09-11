#!/usr/bin/env bash
# 一键批量 topdown —— 把已建好镜像的 trial 逐条跑一遍，最后出一张横向对比表。
#
# 这是给「手上已有全量 113 条包、只建好了一部分镜像、想先快速拿一版数据」准备的
# 轻量驱动。它只负责**发现 / 选取 / 循环 / 汇总**，采集本身原样交给 topdown_trial.sh
# —— perf 的参数顺序、cgroup v1/v2 路径推导这些坑都在那里踩过一遍了，不重复实现。
#
# 用法（在全量包目录里，replay.py / topdown_trial.sh / topdown.conf 三者同级）：
#   bash topdown_quick.sh                      # 跑全部已建好的
#   bash topdown_quick.sh --per-lang 2         # 每种语言只跑 2 条
#   bash topdown_quick.sh --per-lang 1 --pick lightest   # 每种语言最轻的 1 条，最快
#   bash topdown_quick.sh --dry-run            # 只看会跑哪些，不真跑
#   bash topdown_quick.sh --summary-only       # 不跑，只把已有结果汇总成表
#
# --pick 的三种口径（默认 median）：
#   median    按命令数取中位附近 —— 快，但 trial 越轻，容器启动 + 收尾 git diff 这笔
#             **固定开销**在聚合值里占比越大，数字里掺的「容器启动 + 解释器 import」越多
#   heaviest  取最重的 —— 信噪比最好，代价是慢几倍
#   lightest  取最轻的 —— 只想验证链路通不通时用
set -uo pipefail                 # 刻意不加 -e：单条失败要继续跑下一条
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PER_LANG=0; PICK=median; DRY=0; SUMMARY_ONLY=0; OUTROOT="topdown_out"; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --per-lang) [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; PER_LANG="$2"; shift 2 ;;
    --pick)     [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; PICK="$2"; shift 2 ;;
    --out)      [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; OUTROOT="$2"; shift 2 ;;
    --dry-run)      DRY=1; shift ;;
    --summary-only) SUMMARY_ONLY=1; shift ;;
    --limit)    [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; EXTRA+=(--limit "$2"); shift 2 ;;
    -h|--help)  sed -n '2,/^set -/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "❌ 未知参数: $1"; exit 1 ;;
  esac
done
case "$PICK" in median|heaviest|lightest) ;; *) echo "❌ --pick 只能是 median / heaviest / lightest"; exit 1 ;; esac

# ── 前置检查：三个文件必须同级，且 conf 得是填过的 ────────────────────
if [ "$SUMMARY_ONLY" = 0 ]; then
  miss=0
  for f in topdown_trial.sh topdown.conf topdown_parse.py; do
    [ -f "$f" ] || { echo "❌ 缺少 $f（本脚本要和 topdown_trial.sh、topdown.conf 同级）"; miss=1; }
  done
  # replay.py 两处都认，和 topdown_trial.sh 的定位逻辑保持一致：
  # 打好的 bundle 里它与 trial 目录同级；仓库原布局里它在上一层。
  [ -f replay.py ] || [ -f ../replay.py ] || {
    echo "❌ 找不到 replay.py（本目录和上一层都没有）"; miss=1; }
  [ "$miss" = 0 ] || exit 1
fi

# ── 发现：哪些 trial 的镜像已在本地 ───────────────────────────────────
# 判断口径与 run_batch.py --skip-missing 一致：meta.json 的 image.docker_image
# —— build_arm.sh 重建 ARM 镜像时打的就是这个 tag。
echo "── 扫描已建好镜像的 trial ──────────────────────────────────"
AVAIL="$(mktemp)"; trap 'rm -f "$AVAIL"' EXIT
python3 - "$AVAIL" <<'PY'
import json, pathlib, subprocess, sys
rows = []
for m in sorted(pathlib.Path(".").glob("*/meta.json")):
    d = json.loads(m.read_text())
    img = (d.get("image") or {}).get("docker_image", "")
    if not img:
        continue
    ok = subprocess.run(["docker", "image", "inspect", img],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if ok:
        rows.append((d.get("language", "?"), d.get("n_commands", 0), m.parent.name))
with open(sys.argv[1], "w") as f:
    for lang, n, name in sorted(rows):
        f.write(f"{lang}\t{n}\t{name}\n")
PY
TOTAL_AVAIL=$(wc -l < "$AVAIL")
[ "$TOTAL_AVAIL" -gt 0 ] || { echo "❌ 一条镜像都没建好。先跑 build_arm.sh <语言或前缀>"; exit 1; }

# ── 选取 ──────────────────────────────────────────────────────────────
SEL="$(mktemp)"; trap 'rm -f "$AVAIL" "$SEL"' EXIT
python3 - "$AVAIL" "$SEL" "$PER_LANG" "$PICK" <<'PY'
import collections, sys
avail, out, per, pick = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
by = collections.defaultdict(list)
for line in open(avail):
    lang, n, name = line.rstrip("\n").split("\t")
    by[lang].append((int(n), name))
sel, report = [], []
for lang in sorted(by):
    v = sorted(by[lang])                      # 命令数升序，同数按名字 → 确定性
    if per <= 0 or len(v) <= per:
        chosen, note = v, ("" if per <= 0 else
                           (f"（可用的不够 {per} 条）" if len(v) < per else ""))
    elif pick == "heaviest":
        chosen, note = sorted(v, reverse=True)[:per], ""
    elif pick == "lightest":
        chosen, note = v[:per], ""
    else:                                     # median：取中间 per 条
        lo = max(0, (len(v) - per + 1) // 2)
        chosen, note = v[lo:lo + per], ""
    report.append((lang, len(v), len(chosen), note,
                   sorted(chosen, key=lambda x: (-x[0], x[1]))))
    sel += [name for _, name in chosen]
with open(out, "w") as f:
    f.write("\n".join(sel) + ("\n" if sel else ""))
for lang, navail, ntake, note, chosen in report:
    print(f"  {lang:<12}{navail:>3} 条可用 → 取 {ntake} 条{note}")
    for n, name in chosen:
        print(f"                  {name}  ({n} 条命令)")
PY
N_SEL=$(grep -c . "$SEL" || true)
echo
echo "  策略  --pick $PICK$([ "$PER_LANG" -gt 0 ] && echo " --per-lang $PER_LANG" || echo "（不限条数）")"
echo "  合计  $N_SEL 条（可用 $TOTAL_AVAIL 条）"
echo

summarize() {
  echo
  echo "── 横向汇总 ────────────────────────────────────────────────"
  python3 - "$OUTROOT" <<'PY'
import json, pathlib, sys, collections
root = pathlib.Path(sys.argv[1])
rows = []
for f in sorted(root.glob("*/topdown.json")):
    try:
        d = json.loads(f.read_text())
    except Exception as e:
        rows.append((f.parent.name, None, f"读不了: {e}")); continue
    td = d.get("topdown")
    if not td:
        rows.append((f.parent.name, None, "没算出四象限")); continue
    bad = [k for k, v in (d.get("checks") or {}).items()
           if v is False and k.startswith(("C1", "C2", "C3", "C4", "C5", "X", "sum"))]
    rows.append((f.parent.name, td, "✅" if not bad else "❌ " + ",".join(sorted(bad))))
if not rows:
    print("  （还没有结果）"); raise SystemExit
w = max(len(r[0]) for r in rows)
print(f"  {'trial':<{w}}  {'Retir':>7}{'BadSp':>7}{'FE':>7}{'BE':>7}  {'ops/cyc':>8}  校验")
for name, td, status in rows:
    if td is None:
        print(f"  {name:<{w}}  {'—':>7}{'—':>7}{'—':>7}{'—':>7}  {'—':>8}  {status}"); continue
    print(f"  {name:<{w}}  "
          f"{td['Retiring']*100:6.1f}%{td['BadSpec']*100:6.1f}%"
          f"{td['FrontendBound']*100:6.1f}%{td['BackendBound']*100:6.1f}%  "
          f"{td.get('op_retired_per_cycle', 0):8.3f}  {status}")
meth = {r[1].get("backend_method") for r in rows if r[1]}
if meth:
    print(f"\n  后端口径：{'/'.join(sorted(meth))}"
          + ("   ⚠️ 残差法下 BE = 1−其余三项，求和自检失效" if "residual" in meth else ""))
PY
}

if [ "$SUMMARY_ONLY" = 1 ]; then summarize; exit 0; fi
if [ "$DRY" = 1 ]; then echo "（--dry-run：到此为止，没有真跑）"; exit 0; fi

# ── 循环 ──────────────────────────────────────────────────────────────
i=0; NOK=0; NFAIL=0
while read -r d; do
  [ -n "$d" ] || continue
  i=$((i+1))
  echo "════════ [$i/$N_SEL] $d ════════"
  if bash topdown_trial.sh "$d" --no-metrics -o "$OUTROOT" ${EXTRA[@]+"${EXTRA[@]}"}; then
    NOK=$((NOK+1))
  else
    rc=$?; NFAIL=$((NFAIL+1))
    echo "  ⚠️  [$d] 退出码 $rc —— 继续下一条"
    # rc=2 是「数采到了但校验没过」，结果文件仍在，汇总表里会标出来
  fi
  echo
done < "$SEL"

echo "════════ 跑完 ════════"
echo "  成功 $NOK / 失败 $NFAIL / 共 $N_SEL"
summarize
