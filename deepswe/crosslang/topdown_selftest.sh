#!/usr/bin/env bash
# 单个测试用例：不跑重放，只用一个烧 CPU 的临时容器，把 topdown 这条链路验穿。
#
# 为什么要有它：整条 trial 要跑 200+ 秒，而 SLOTS / 事件号 / cgroup 路径 / perf 输出
# 格式这几样任何一个不对，都要等到最后才暴露。这个脚本 ~15 秒跑完，覆盖的是完全
# 相同的那条链路（perf stat -a -G → perf 输出文件 → topdown_parse.py），
# 唯一不同的是被测对象换成了一个可预测的死循环。
#
# 死循环的好处：它的四象限是**可预判**的 —— 一个纯整数循环应该是高 Retiring、
# 低 FrontendBound。如果跑出来 Backend 90%，那多半不是"这个负载后端受限"，
# 而是 SLOTS 或事件号不对。
#
# 用法：
#   bash topdown_selftest.sh                 # 用 topdown.conf 里的配置
#   bash topdown_selftest.sh --slots 8       # 临时覆盖 SLOTS（不改 conf）
#   bash topdown_selftest.sh --secs 5        # 采样时长，默认 3
#   bash topdown_selftest.sh --image <镜像>  # 换个容器镜像（默认找一个已建好的）
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$HERE"
CONF="$HERE/topdown.conf"
SECS=3; SLOTS_OVERRIDE=""; IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --slots) SLOTS_OVERRIDE="$2"; shift 2 ;;
    --secs)  SECS="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "❌ 未知参数: $1"; exit 1 ;;
  esac
done
[ -f "$CONF" ] || { echo "❌ 找不到 $CONF"; exit 1; }
# shellcheck disable=SC1090
source "$CONF"
EVSRC=/sys/bus/event_source/devices

echo "══════════════════════════════════════════════════════════════"
echo " topdown 自检 —— 不跑重放，只验链路"
echo "══════════════════════════════════════════════════════════════"

# ── PMU ───────────────────────────────────────────────────────────
[ -n "${PMU:-}" ] || PMU="$(ls "$EVSRC" 2>/dev/null | grep -m1 armv8 || true)"
[ -n "$PMU" ] || { echo "❌ 没有 armv8* PMU"; exit 1; }
echo "  PMU        $PMU"

# ── SLOTS：三个来源并排打出来，不一致要能一眼看见 ────────────────
RAW="$(tr -d '[:space:]' < "$EVSRC/$PMU/caps/slots" 2>/dev/null || true)"
case "$RAW" in
  0[xX]*[!0-9a-fA-FxX]*) SYS="" ;;
  0[xX]*)                SYS=$(( RAW )) ;;
  *[!0-9]*|"")           SYS="" ;;
  *)                     SYS=$(( 10#$RAW )) ;;
esac
printf '  caps/slots 原始 [%s] → 解析成 %s\n' "$RAW" "${SYS:-解析失败}"
[ -n "${SLOTS:-}" ] && echo "  topdown.conf   SLOTS=$SLOTS"
SLOTS_USE="${SLOTS_OVERRIDE:-${SLOTS:-$SYS}}"
[ -n "$SLOTS_USE" ] || { echo "❌ 三个来源都拿不到 SLOTS"; exit 1; }
echo "  本次使用   SLOTS=$SLOTS_USE$([ -n "$SLOTS_OVERRIDE" ] && echo '（命令行覆盖）')"
if [ -n "$SYS" ] && [ "$SLOTS_USE" != "$SYS" ]; then
  echo "  ⚠️  和 caps/slots 读出来的 $SYS **不一致** —— 这正是要查的点"
fi
echo "  CPU part   $(grep -m1 'CPU part' /proc/cpuinfo | awk '{print $NF}')  (0xd0c=N1 0xd40=V1 0xd49=N2 0xd4f=V2)"

# ── 事件组：和正式采集用的是同一套拼装规则 ───────────────────────
SEP=""; EVSPEC=""
for pair in "${EV_CPU_CYCLES}:cpu_cycles" "${EV_OP_RETIRED}:op_retired" \
            "${EV_OP_SPEC}:op_spec" "${EV_STALL_SLOT_FE}:stall_slot_fe"; do
  v="${pair%%:*}"
  case "$v" in 0[xX]*) ;; *) echo "❌ 事件号必须 0x 开头: $v"; exit 1 ;; esac
  EVSPEC="${EVSPEC}${SEP}${PMU}/event=${v},name=${pair##*:}/"; SEP=","
done
[ -n "${EV_STALL_SLOT_BE:-}" ] && EVSPEC="${EVSPEC},${PMU}/event=${EV_STALL_SLOT_BE},name=stall_slot_be/"
[ -n "${EV_STALL_SLOT:-}"    ] && EVSPEC="${EVSPEC},${PMU}/event=${EV_STALL_SLOT},name=stall_slot/"
echo "  事件组     {$EVSPEC}"

# ── 找个镜像 ──────────────────────────────────────────────────────
if [ -z "$IMAGE" ]; then
  for d in */; do
    [ -f "${d}meta.json" ] || continue
    im=$(python3 -c "import json;print((json.load(open('${d}meta.json')).get('image') or {}).get('docker_image',''))" 2>/dev/null)
    [ -n "$im" ] && docker image inspect "$im" >/dev/null 2>&1 && { IMAGE="$im"; break; }
  done
fi
[ -n "$IMAGE" ] || IMAGE=ubuntu:24.04
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "❌ 镜像不在本地: $IMAGE（用 --image 指一个）"; exit 1; }
echo "  镜像       $IMAGE"

CNAME="topdown_selftest_$$"
cleanup() { docker rm -f "$CNAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

# ── 起容器烧 CPU ──────────────────────────────────────────────────
# 纯整数死循环：可预判为高 Retiring / 低 FrontendBound。
docker run -d --name "$CNAME" --cpus=2 "$IMAGE" \
  sh -c 'i=0; while :; do i=$((i+1)); done' >/dev/null 2>&1 \
  || { echo "❌ 容器起不来"; exit 1; }
sleep 1
CPID="$(docker inspect -f '{{.State.Pid}}' "$CNAME")"

# ── cgroup 路径：v1 走 perf_event 独立层级 ────────────────────────
CGFS_T="$(stat -fc %T /sys/fs/cgroup)"
if [ "$CGFS_T" = cgroup2fs ]; then
  CG="$(awk -F: '$1==0 {print substr($3,2); exit}' "/proc/$CPID/cgroup")"; CGABS="/sys/fs/cgroup/$CG"
else
  CG="$(awk -F: '$2 ~ /(^|,)perf_event(,|$)/ {print substr($3,2); exit}' "/proc/$CPID/cgroup")"
  CGABS="/sys/fs/cgroup/perf_event/$CG"
fi
echo "  cgroup     $([ "$CGFS_T" = cgroup2fs ] && echo v2 || echo v1)  $CG"
[ -n "$CG" ] && [ -d "$CGABS" ] || { echo "❌ cgroup 路径推不出来: $CGABS"; cat "/proc/$CPID/cgroup"; exit 1; }

# ── 采 ────────────────────────────────────────────────────────────
SUDO=sudo; [ "$(id -u)" = 0 ] && SUDO=""
OUT="$(mktemp -d)"; trap 'cleanup; rm -rf "$OUT"' EXIT
case "${PERF_OUTPUT:-auto}" in
  csv)  OPT=(-x,); PF="$OUT/perf.csv" ;;
  json) OPT=(-j);  PF="$OUT/perf.json" ;;
  *)    if perf --version 2>/dev/null | grep -qE 'perf version (5\.(1[7-9]|[2-9][0-9])|[6-9]\.)'; then
          OPT=(-j); PF="$OUT/perf.json"; else OPT=(-x,); PF="$OUT/perf.csv"; fi ;;
esac
echo
echo "  采样 ${SECS}s …"
echo "  $SUDO perf stat -a ${OPT[*]} -o $PF -e '{…}' -G $CG -- sleep $SECS"
$SUDO perf stat -a "${OPT[@]}" -o "$PF" -e "{$EVSPEC}" -G "$CG" -- sleep "$SECS" 2>"$OUT/err"
PRC=$?
echo "  perf 退出码 $PRC"
[ -s "$OUT/err" ] && { echo "  perf stderr："; sed 's/^/     /' "$OUT/err" | head -10; }
[ -s "$PF" ] || { echo "❌ perf 没写出结果 —— 链路断在 perf 这一步"; exit 1; }

# ── 解析 ──────────────────────────────────────────────────────────
echo
python3 "$HERE/topdown_parse.py" "$PF" --conf "$CONF" --slots "$SLOTS_USE" \
        --json-out "$OUT/topdown.json" --title "自检（整数死循环）"
RC=$?

echo
echo "── 怎么读这个结果 ──────────────────────────────────────────"
echo "  被测的是一个纯整数死循环，**预期是高 Retiring、低 FrontendBound**。"
echo "  若 BackendBound 很高（>60%）而 Retiring 很低，多半不是负载本身的问题，"
echo "  而是 SLOTS 偏大 或 事件号不对 —— 换个 SLOTS 再跑一次对比："
echo "      bash topdown_selftest.sh --slots 8"
echo "      bash topdown_selftest.sh --slots 5"
echo "  哪个值让 Retiring 落在合理区间（死循环通常 30~70%），哪个就更可能是对的。"
exit $RC
