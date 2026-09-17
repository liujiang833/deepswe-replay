#!/usr/bin/env bash
# 单条 trial 的 ARM topdown 采集 —— 在宿主侧按 cgroup 过滤，采整条重放的 PMU 事件。
#
# 为什么必须在宿主侧按 cgroup 采，而不是 `perf stat -- <命令>`：
#   replay.py 是用 `docker exec` 一条条重放的，而 `docker exec` 的真实进程是
#   **containerd-shim fork 出来的**，根本不在我们这个 perf 的子进程树里。
#   `perf stat -- docker exec ...` 采到的只有 docker 客户端那点 RPC 开销，
#   被测的负载一个周期都不会进来。所以只能走 `perf stat -a -G <cgroup>`：
#   系统级采样 + 按容器的 cgroup 过滤，不管进程是谁 fork 的都能框进来。
#
# 采集窗口 = perf 的生命周期。这里用 `-- tail --pid=<replay 的 PID> -f /dev/null`
# 给 perf 当子进程：replay.py 一退出 tail 就退出，perf 跟着收尾并打印结果。
# 比起「后台起 perf 再找准时机发信号」，这条路没有竞态，也不用管信号语义。
#
# ⚠️ 采到的是**整条 trial 的聚合值**：容器启动 + 全部命令 + 收尾的 git diff 都在内。
#    sidecar（<容器名>-sink）是独立 cgroup，天然被 -G 滤掉，不在统计里。
#    per-command 归因是后续工作，这一版没有。
#
# 用法：
#   bash topdown_trial.sh <trial目录>
#   bash topdown_trial.sh <trial目录> -o <输出目录>
#   bash topdown_trial.sh <trial目录> --limit 5      # 只重放前 5 条命令，冒烟用
#   bash topdown_trial.sh <trial目录> --no-metrics   # 关掉 replay.py 自己那套 cgroup 指标
#   bash topdown_trial.sh <trial目录> --cmd-timeout 30   # 透传给 replay.py 的单命令超时
#   bash topdown_trial.sh <trial目录> --per-step       # per-step topdown（perf stat -I 10 + 事后按 step 归并）
#
# 批量：不要自己写循环。`python3 run_batch.py --topdown` 就是对本脚本逐条调用
# （它负责排程、跳过没建镜像的、汇总四象限），perf 那套逻辑只有这里这一份。
#
# 什么时候要 --no-metrics：replay.py 启动时报「sinkhole cgroup 初始化失败」，
# 整条采集根本起不来。它那套指标（usage_usec / mem_peak）是**自己去读容器 cgroup 文件**
# 拿的，和本脚本在宿主侧用 perf -G 采 PMU 完全是两条独立的路 ——
# 关掉它不影响四象限一个数，只是 commands.jsonl 里那几列记成 null。
# 换句话说：cgroup 指标采不到 ≠ topdown 采不到，别因为前者放弃整轮采集。
#
# 跑之前先跑 probe_pmu.sh —— 那一步才是判断这台机器能不能采的地方。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/topdown.conf"

TRIAL=""; OUTDIR="$HERE/topdown_out"; LIMIT=""; NO_METRICS=0; CMD_TIMEOUT=""; PER_STEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--outdir) [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; OUTDIR="$2"; shift 2 ;;
    --limit)     [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; LIMIT="$2"; shift 2 ;;
    # --cmd-timeout 只是原样透传给 replay.py。之所以要有这个口子：批量入口
    # （run_batch.py）一直在给 replay.py 传 --cmd-timeout 30（对齐原 harness 口径），
    # 改走本脚本之后如果这个值传不下去，同一批里 topdown 的那几条就换了口径，
    # 耗时和 rc_match 都不再能跟历史批次比 —— 而且这种偏差在报告里完全看不出来。
    --cmd-timeout) [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; CMD_TIMEOUT="$2"; shift 2 ;;
    --per-step)  PER_STEP=1; shift ;;
    --no-metrics) NO_METRICS=1; shift ;;
    -h|--help)   sed -n '2,/^set -[eu]/p' "$0" | sed '$d'; exit 0 ;;
    -*)          echo "❌ 未知参数: $1"; exit 1 ;;
    *)           TRIAL="$1"; shift ;;
  esac
done
[ -n "$TRIAL" ] || { echo "❌ 用法: bash topdown_trial.sh <trial目录> [-o 输出目录] [--limit N] [--cmd-timeout N] [--no-metrics] [--per-step]"; exit 1; }
[ -d "$TRIAL" ] || { echo "❌ trial 目录不存在: $TRIAL"; exit 1; }
TRIAL="$(cd "$TRIAL" && pwd)"
TNAME="$(basename "$TRIAL")"
[ -f "$TRIAL/task.json" ] || { echo "❌ 缺 $TRIAL/task.json"; exit 1; }

# replay.py 的定位：打包后它和 trial 目录同级（就在本脚本旁边）；
# 开发机上的 crosslang/ 布局则是上一层。两个都找不到才报错。
REPLAY=""
for c in "$HERE/replay.py" "$HERE/../replay.py"; do
  if [ -f "$c" ]; then REPLAY="$(cd "$(dirname "$c")" && pwd)/replay.py"; break; fi
done
[ -n "$REPLAY" ] || { echo "❌ 找不到 replay.py（试过 $HERE/replay.py 和 $HERE/../replay.py）"; exit 1; }
[ -f "$CONF" ] || { echo "❌ 找不到 $CONF —— 事件号全在那里面"; exit 1; }

# ── 1. 读配置，把 PMU 和 SLOTS 定下来 ──────────────────────────
# shellcheck disable=SC1090
source "$CONF"
EVSRC=/sys/bus/event_source/devices

if [ -z "${PMU:-}" ]; then
  PMU="$(ls "$EVSRC" 2>/dev/null | grep -m1 armv8 || true)"
  [ -n "$PMU" ] || { echo "❌ $EVSRC 下没有 armv8* PMU —— 先跑 bash probe_pmu.sh"; exit 1; }
  PMU_SRC="自动探测"
else
  PMU_SRC="topdown.conf"
fi
[ -d "$EVSRC/$PMU" ] || { echo "❌ PMU 不存在: $EVSRC/$PMU"; exit 1; }

# SLOTS：**永远优先运行时读**。配置里留空是推荐用法，写死常数换机器就会静默算错。
if [ -z "${SLOTS:-}" ]; then
  # caps/slots 的进制不统一：arm64 的 armv8_pmuv3 导出的是**十六进制**（`0x8`），
  # x86 那边是十进制。早先这里写的是 `tr -dc '0-9'`，它把 `x` 剥掉 ——
  #   0x8 → "08" → 8    碰巧对
  #   0xa → "0"  → 0    **分母归零**，解析器除零崩溃，连 topdown.json 都落不下来
  # 而且失败得毫无征兆：perf 照常退出 0、perf.json 照常有内容。
  # 所以必须按进制解析，并且解析不出来要明确拒绝，绝不能降级成某个数。
  SLOTS_RAW="$(tr -d '[:space:]' < "$EVSRC/$PMU/caps/slots" 2>/dev/null || true)"
  SLOTS_SRC="$EVSRC/$PMU/caps/slots"
  case "$SLOTS_RAW" in
    0[xX]*[!0-9a-fA-FxX]*) SLOTS="" ;;                      # 含非法字符
    0[xX]*)                SLOTS=$(( SLOTS_RAW )) ;;        # 十六进制，$(( )) 认 0x
    *[!0-9]*|"")           SLOTS="" ;;                      # 含非数字，或空
    *)                     SLOTS=$(( 10#$SLOTS_RAW )) ;;    # 十进制。必须加 10# ——
                                                            # 否则 "08" 会被当八进制而报错
  esac
  [ "${SLOTS:-0}" -gt 0 ] 2>/dev/null || SLOTS=""           # 0 和负数一律当没读到
  if [ -z "$SLOTS" ]; then
    echo "❌ 读不到 $EVSRC/$PMU/caps/slots，topdown.conf 里也没填 SLOTS。"
    echo "   没有 SLOTS 就算不出四象限，而**猜一个常数比算不出更糟**：四个比值会"
    echo "   一起按同一比例静默偏移，每一项看着都还正常。"
    echo "   拿目标核 TRM 的确切值填进 topdown.conf 的 SLOTS= 再来。"
    exit 1
  fi
else
  SLOTS_SRC="topdown.conf（⚠️ 写死的值，换机器/换核请复核）"
fi

# ── cgroup 版本：v1 和 v2 下 perf -G 的路径口径完全不同 ────────
# v2（统一层级）：-G 的路径相对 /sys/fs/cgroup/
# v1（多层级）：perf 用的是 **perf_event 这个独立层级**，路径相对
#              /sys/fs/cgroup/perf_event/
# 这件事必须在第一屏就告诉用户：v1 上路径推错的表现是
# `no access to cgroup /sys/fs/cgroup/perf_event/xxx`，
# 而同一台 v1 机器上 replay.py 还会另外报一句「sinkhole cgroup 初始化失败」
# （它那套指标只认 v2 的 cpu.stat / memory.current）—— 两个报错看着不相干，
# 根因是同一个。不把版本打出来，人就会当成两个独立问题去查。
CGFS_T="$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo 未知)"
if [ "$CGFS_T" = cgroup2fs ]; then CGVER=v2; else CGVER=v1; fi

# ── 事件号校验：必须是 0x 开头的十六进制 ──────────────────────
# 为什么值得专门拦一道：perf 的 `event=` 字段按 **C 风格**解析数字 ——
# 配置里写 `11`，perf 读到的是**十进制 11**，也就是事件 0xb（BR_MIS_PRED），
# 而你想要的 CPU_CYCLES 是 0x11。这个错误 perf **完全不报错**：0xb 是合法事件，
# 照样有数，只是数的是别的东西 —— 四象限整套静默算错，而且「看起来很正常」。
# 之前这里是把 conf 里的值**原样**拼进 event=，零校验，这就是那个坑。
# （这段校验和 probe_pmu.sh 里的 evcode_bad 是同一套，改一处要两处一起改。）
evcode_bad() {   # $1=配置键名  $2=值；不合法时打印原因并返回 0（真）
  local key="$1" val="$2"
  if [[ "$val" =~ ^0[xX][0-9a-fA-F]+$ ]]; then return 1; fi
  echo "❌ $key=$val —— 事件号必须写成 0x 开头的十六进制（如 0x0011 / 0x11）"
  echo "   perf 的 event= 按 C 风格解析数字：写 \`11\` 它读的是**十进制 11**（= 0xb），"
  echo "   数到的是另一个合法事件，**不报任何错**，四象限静默算错。"
  echo "   改成 0x${val} 很可能就是你想要的（但请按目标核 TRM 核对一遍）。"
  return 0
}

# 四个必需事件：不能空，且必须 0x 打头
for key in EV_CPU_CYCLES EV_OP_RETIRED EV_OP_SPEC EV_STALL_SLOT_FE; do
  val="${!key:-}"          # bash 间接展开，比 eval 干净也安全
  [ -n "$val" ] || { echo "❌ topdown.conf 里 $key 是空的 —— 这四个事件是必需的，不能留空"; exit 1; }
  if evcode_bad "$key" "$val"; then exit 1; fi
done
# 两个可选事件：留空合法（留空各有含义），填了就必须合格
for key in EV_STALL_SLOT_BE EV_STALL_SLOT; do
  val="${!key:-}"
  [ -n "$val" ] || continue
  if evcode_bad "$key" "$val"; then exit 1; fi
done

# ── 事件组 ────────────────────────────────────────────────────
# 整组用 {} 包住 —— 保证这几个事件被内核当成一个调度单元同时上同时下，
# 否则它们各自在不同时间窗口里计数，四象限的比值就没有意义了。
#
# ⚠️ EV_STALL_SLOT_BE 留空时**绝对不能**把它拼进去：拼出来会是
#    `.../event=,name=stall_slot_backend/`，perf 直接拒绝解析，**整组都开不起来**，
#    连另外四个事件都采不到。留空 = 走残差法（BackendBound 由 1 减出来），
#    这是没实现 STALL_SLOT_BACKEND 的核上的正常用法，不是降级。
#    EV_STALL_SLOT 同理（它只用于 topdown_parse.py 的 X 交叉校验）。
# （这段拼法和 probe_pmu.sh 里的 build_evspec 是同一套，改一处要两处一起改。）
PAIRS=("$EV_CPU_CYCLES:cpu_cycles" "$EV_OP_RETIRED:op_retired" \
       "$EV_OP_SPEC:op_spec" "$EV_STALL_SLOT_FE:stall_slot_frontend")
if [ -n "${EV_STALL_SLOT_BE:-}" ]; then
  PAIRS+=("$EV_STALL_SLOT_BE:stall_slot_backend")
  BE_MODE="直接法（BackendBound = STALL_SLOT_BACKEND / 分母）"
else
  BE_MODE="残差法（BackendBound = 1 − 其余三项；求和自检失效，改跑 C1~C5）"
fi
# name= 取 stall_slot_total 而不是 stall_slot：后者是另外两个名字的前缀，
# topdown_parse.py 按 name 做子串兜底匹配时会出歧义。
if [ -n "${EV_STALL_SLOT:-}" ]; then
  PAIRS+=("$EV_STALL_SLOT:stall_slot_total")
  X_MODE="开（EV_STALL_SLOT=$EV_STALL_SLOT）"
else
  X_MODE="关（EV_STALL_SLOT 留空 —— 残差法下就没有任何一条校验能抓「SLOTS 偏大」）"
fi
SEP=""; EVSPEC="{"
for pair in "${PAIRS[@]}"; do
  EVSPEC="${EVSPEC}${SEP}${PMU}/event=${pair%%:*},name=${pair##*:}/"; SEP=","
done
N_MAIN=${#PAIRS[@]}
N_EV=$N_MAIN
for kv in ${EV_EXTRA:-}; do
  [ -n "$kv" ] || continue
  case "${kv%%=*}" in
    *[!A-Za-z0-9_]*|"") echo "❌ EV_EXTRA 里的事件名不合法: ${kv%%=*}（perf 的 name= 只收 [A-Za-z0-9_]）"; exit 1 ;;
  esac
  # EV_EXTRA 的事件号同样要校验 —— 和五个主事件同一个坑，一样静默数错东西
  if evcode_bad "EV_EXTRA 里的 ${kv%%=*}" "${kv#*=}"; then exit 1; fi
  EVSPEC="${EVSPEC}${SEP}${PMU}/event=${kv#*=},name=${kv%%=*}/"; SEP=","
  N_EV=$((N_EV + 1))
done
EVSPEC="${EVSPEC}}"

# 输出格式：perf >= 5.17 才有 -j。auto 就按版本挑；配置里写死了就听配置的。
# 判断 perf 可不可用必须**真跑一次 --version**，不能只看 `command -v`：
# Debian/Ubuntu 的 /usr/bin/perf 是个按 uname -r 找真身的 wrapper 脚本，
# linux-tools-<内核版本> 没装时 wrapper 照样在，只有跑起来才吐
# "perf not found for kernel ..."。本机就是这个情况，所以这里按版本号解析结果判。
command -v perf >/dev/null 2>&1 || { echo "❌ 没有 perf —— 先跑 bash probe_pmu.sh 看装法"; exit 1; }
PERF_VER_RAW="$(perf --version 2>/dev/null | head -1 || true)"
PERF_MAJ="$(printf '%s' "$PERF_VER_RAW" | sed -nE 's/.*version ([0-9]+)\.([0-9]+).*/\1/p')"
PERF_MIN="$(printf '%s' "$PERF_VER_RAW" | sed -nE 's/.*version ([0-9]+)\.([0-9]+).*/\2/p')"
if [ -z "$PERF_MAJ" ]; then
  echo "❌ perf 命令在，但跑不起来 / 版本号解析不出来："
  perf --version 2>&1 | head -3 | sed 's/^/     /'
  echo "   多半是 Debian/Ubuntu 的 perf wrapper 找不到对应内核的真身。"
  echo "   先跑 bash probe_pmu.sh，那里有装法。"
  exit 1
fi
OUTFMT="${PERF_OUTPUT:-auto}"
if [ "$OUTFMT" = auto ]; then
  if [ "${PERF_MAJ:-0}" -gt 5 ] 2>/dev/null || \
     { [ "${PERF_MAJ:-0}" -eq 5 ] && [ "${PERF_MIN:-0}" -ge 17 ]; } 2>/dev/null; then
    OUTFMT=json
  else
    OUTFMT=csv
  fi
fi

TD="$OUTDIR/$TNAME/topdown"
mkdir -p "$TD"
if [ "$OUTFMT" = json ]; then PERFOUT="$TD/perf.json"; OUTOPT=(-j)
else                          PERFOUT="$TD/perf.csv";  OUTOPT=(-x,); fi
RLOG="$TD/replay.log"
STATUS="$TD/run_status.json"

# ── 机读状态文件：把三个退出码分开记下来 ─────────────────────
# 为什么必须分开：本脚本最后只能吐**一个**退出码，而它把两件正交的事压成了一个数 ——
#   重放退出码 RRC   → 这条 trial 到底重放成没成（patch_identical 那条线，保真度）
#   解析退出码 PRC   → 四象限的自检过没过（数据可信度，2 = 数拿到了但自检没过）
# 调用方（run_batch.py）看到一个非零退出码，分不清是「trial 失败了」还是
# 「trial 好好的，只是这轮 PMU 数不可信」。把 trial 判成失败是**错的**：
# topdown 采废了不影响 git diff 逐字节相等这个结论。
# 所以这里额外落一份 run_status.json，调用方按字段各取各的。
#
# 注意：早于重放启动的失败（事件号不合法、等不到容器…）**不会**写这个文件 ——
# 那种情况 trial 确实没跑起来，调用方就该按进程退出码判失败，这是有意的。
RRC=null; PERF_RC=0
write_status() {   # $1 = 解析退出码（还没跑到解析就传 null）
  printf '{\n  "trial": "%s",\n  "replay_rc": %s,\n  "perf_rc": %s,\n  "parse_rc": %s,\n  "topdown_json": "%s",\n  "verdict_json": "%s"\n}\n' \
    "$TNAME" "$RRC" "$PERF_RC" "$1" "$TD/topdown.json" "$OUTDIR/$TNAME/verdict.json" > "$STATUS"
}

# 非 root 时这个 test 返回 1 —— set -e 下必须写成 if，不能用 `test && 赋值`
SUDO="sudo"
if [ "$(id -u)" = 0 ]; then SUDO=""; fi

echo "=============================================================="
if [ "$PER_STEP" = 1 ]; then
  echo " ARM topdown 采集（per-step · interval 10ms）  $(date -u +%FT%TZ)"
else
  echo " ARM topdown 采集（整条 trial 聚合）  $(date -u +%FT%TZ)"
fi
echo "=============================================================="
echo "  trial      $TNAME"
echo "  replay.py  $REPLAY"
echo "  输出       $TD"
echo "  cgroup     $CGVER（/sys/fs/cgroup 类型 $CGFS_T）"
if [ "$CGVER" = v1 ]; then
  echo "             ⚠️ v1：perf -G 走 perf_event 独立层级；replay.py 的 per-command"
  echo "                cgroup 指标在 v1 上不可用，起不来就加 --no-metrics（不影响 topdown）"
fi
echo "  PMU        $PMU（$PMU_SRC）"
echo "  SLOTS      $SLOTS（$SLOTS_SRC）"
echo "  perf       ${PERF_VER_RAW:-未知} → $OUTFMT"
echo "  后端口径   $BE_MODE"
echo "  X 交叉校验 $X_MODE"
echo "  事件       $N_EV 个（主事件 $N_MAIN + EV_EXTRA $((N_EV - N_MAIN))）"
printf '%s\n' "$EVSPEC" | fold -w 66 | sed 's/^/             /'
if [ -n "$LIMIT" ]; then
  echo "  ⚠️  --limit $LIMIT：只重放前 $LIMIT 条命令，这是冒烟不是正式采集"
fi
if [ "$NO_METRICS" = 1 ]; then
  echo "  --no-metrics：replay.py 的 cgroup 指标关闭（commands.jsonl 里那几列记 null）"
  echo "                不影响 topdown —— PMU 是宿主侧 perf -G 采的，两条路互不相干"
fi
if [ -n "$CMD_TIMEOUT" ]; then
  echo "  单命令超时   ${CMD_TIMEOUT}s（透传给 replay.py）"
fi
echo

# ── 2. 后台起重放 ──────────────────────────────────────────────
RPID=""
CLEANED=0
# 判断后台重放是不是真的还活着。
# 只用 `kill -0` 是不够的：子进程退出后、被 wait 收尸前是**僵尸**状态，
# 这段时间 `kill -0` 照样返回成功。实测表现是明明 replay 已经按 SIGINT 正常退出了，
# 清理逻辑还是白等满 10 秒，再对着僵尸补一刀 SIGKILL，然后打印一段吓人的
# 「残留容器请手动清」——全是假的。所以要再看一眼 /proc 里的进程状态。
# （comm 字段可能带空格和括号，所以按最后一个 ")" 切，不能直接 awk 取第 3 列。）
alive() {
  kill -0 "$1" 2>/dev/null || return 1
  local st
  st="$(sed -E 's/^.*\) //' "/proc/$1/stat" 2>/dev/null | cut -d' ' -f1)"
  # 写成 if 而不是 `[ ... ] && return 1`：后者在条件不成立时整条返回 1，
  # 虽然 bash 对 && 列表里非末位命令的失败有豁免（实测不会被 set -e 打断），
  # 但那条规则太容易记错，不值得在清理路径上赌。
  if [ "$st" = "Z" ]; then return 1; fi
  return 0
}
cleanup() {
  if [ "$CLEANED" = 1 ]; then return 0; fi
  CLEANED=1
  if [ -n "$RPID" ] && alive "$RPID"; then
    echo
    echo "  清理：重放进程 $RPID 还活着，先发 SIGINT 让 replay.py 走它自己的 finally"
    echo "        （直接 SIGKILL 会把主容器和 -sink 两个容器都留成孤儿）"
    kill -INT "$RPID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      alive "$RPID" || break
      sleep 1
    done
    if alive "$RPID"; then
      echo "        10s 没退，改 SIGKILL；残留容器请手动清："
      echo "        docker ps -a --filter name=^replay_ "
      kill -KILL "$RPID" 2>/dev/null || true
    fi
  fi
}
# 三个 trap 分开写，不能图省事合成一个：
#   - EXIT 只负责兜底清理，不改退出码；
#   - INT/TERM 必须**清理完就 exit**。合成一个的话，信号被处理完之后 bash 会从被打断
#     的地方继续往下跑，于是一次中途 ^C 最后会打印出一份「✅ 自检都过」的完整报告、
#     退出码还是 0 —— 看起来像采成功了，其实重放只跑了一半。
#
# 补充：在真终端里按 ^C，信号发给的是整个前台进程组，replay.py 作为本脚本的后台子进程
# 同样在这个进程组里，会自己收到 SIGINT 并走它的 finally 清容器。下面的 cleanup 是给
# 「信号只发给本脚本」的场合兜底（`kill -INT <pid>`、批处理调度器发的 SIGTERM）。
trap cleanup EXIT
trap 'echo; echo "  ⚠️  收到 SIGINT，中止采集"; cleanup; exit 130' INT
trap 'echo; echo "  ⚠️  收到 SIGTERM，中止采集"; cleanup; exit 143' TERM

echo "── 起重放（后台）──────────────────────────────────────────"
RCMD=(python3 "$REPLAY" "$TRIAL" "$TRIAL/task.json" -o "$OUTDIR")
if [ -n "$LIMIT" ]; then RCMD+=(--limit "$LIMIT"); fi
if [ -n "$CMD_TIMEOUT" ]; then RCMD+=(--cmd-timeout "$CMD_TIMEOUT"); fi
# replay.py 的 cgroup 指标和本脚本的 PMU 采集互不相干，关掉不影响四象限
if [ "$NO_METRICS" = 1 ]; then RCMD+=(--no-metrics); fi
echo "  ${RCMD[*]}"
echo "  日志  $RLOG"
# ⚠️ 这里的 `set -m` 不能删，删了清理逻辑会静默失效。
#
# 非交互 shell 在**没开作业控制**时，会把后台任务的 SIGINT / SIGQUIT 置成 SIG_IGN
# （POSIX 规定的行为），而且这个处置会被 exec 继承下去 —— 子进程里 python 看到的
# signal.getsignal(SIGINT) 直接就是 SIG_IGN，它连装处理器的机会都没有。
# 后果很实在：`kill -INT $RPID` 打过去毫无反应，清理逻辑白等 10 秒再补 SIGKILL，
# 而 SIGKILL 不会让 replay.py 走它的 finally，主容器和 -sink 两个容器双双变孤儿。
# 终端里按 ^C 也一样 —— 信号确实发到了整个前台进程组，但 replay.py 照样忽略。
#
# `set -m` 让后台任务拿到独立进程组 + 默认信号处置，`kill -INT` 才真的能送进去。
# 代价是终端 ^C 不再自动传播到它（进程组不同了），所以必须靠上面的 INT trap
# 显式转发 —— 这反而更确定：清理路径只有一条，不依赖进程组怎么分。
# 紧接着 `set +m` 关掉，免得作业状态通知（[1]+ Done …）混进输出里。
set -m
"${RCMD[@]}" >"$RLOG" 2>&1 &
RPID=$!
set +m
echo "  PID   $RPID"

# ── 3. 等主容器出现 ────────────────────────────────────────────
# replay.py 的容器名是 replay_<trial名 sanitize 后前 44 字符>_<它自己的 PID>。
# 它是我们 fork 出来的，所以 $RPID 就是它的 os.getpid() —— 名字可以精确算出来，
# 不用去 docker ps 里猜。这一点很重要：机器上可能同时有别人的重放在跑。
SAN="$(printf '%s' "$TNAME" | sed 's/[^A-Za-z0-9_.-]/_/g' | cut -c1-44)"
EXPECT="replay_${SAN}_${RPID}"
# docker 的 name filter 是**正则**，不是字面量。sanitize 规则允许保留 `.` 和 `-`，
# 而 `.` 在正则里是通配符 —— trial 名带点时 `^replay_a.b_123$` 会匹上 `replay_axb_123`。
# 当前这条 trial 名里没有点，但别把正确性押在数据上，过滤用的那份把 `.` 转义掉。
EXPECT_RE="$(printf '%s' "$EXPECT" | sed 's/\./\\./g')"
echo
echo "── 等主容器（最多 60s）────────────────────────────────────"
echo "  期望容器名  $EXPECT"
CID=""; CNAME=""
for i in $(seq 1 60); do
  if ! alive "$RPID"; then
    echo
    echo "❌ 重放进程已经退出，容器还没起来。最后 40 行日志："
    tail -40 "$RLOG" | sed 's/^/     /'
    echo
    echo "  完整日志：$RLOG"
    echo "  最常见的两种：镜像不在本地（先 bash build_arm.sh $TNAME）；"
    echo "  同 trial 有存量容器占着名字（docker ps -a --filter name=^replay_）。"
    exit 1
  fi
  CID="$(docker ps --filter "name=^${EXPECT_RE}$" --format '{{.ID}}' 2>/dev/null | head -1 || true)"
  if [ -n "$CID" ]; then CNAME="$EXPECT"; break; fi
  # 兜底：万一 replay.py 改了命名规则，退回按前缀找。
  # **必须排掉 -sink**：那是 403 sinkhole sidecar，独立容器、独立 cgroup，
  # 采它等于采了个空气（而且会让人误以为「采到了，只是数很小」）。
  # 这不是理论风险 —— 本机用真容器实测过：同时起主容器和 <名字>-sink 之后，
  # `docker ps --filter name=^replay_` **先列出的是 -sink**，没有这个 grep，
  # head -1 拿到的就是 sidecar。（同一次实测也确认了 name=^<全名>$ 这种精确过滤
  # 不会误命中 -sink，所以上面那条精确路径才是首选。）
  LINE="$(docker ps --filter "name=^replay_" --format '{{.ID}} {{.Names}}' 2>/dev/null \
          | grep -v -- '-sink' | head -1 || true)"
  if [ -n "$LINE" ]; then
    CID="${LINE%% *}"; CNAME="${LINE#* }"
    echo "  ⚠️  没等到 $EXPECT，按前缀兜底命中 $CNAME —— 确认这是你要采的那个"
    break
  fi
  sleep 1
  if [ $((i % 10)) = 0 ]; then echo "  … 等了 ${i}s"; fi
done
if [ -z "$CID" ]; then
  echo
  echo "❌ 60s 内没等到重放容器。最后 40 行日志："
  tail -40 "$RLOG" | sed 's/^/     /'
  echo
  echo "  完整日志：$RLOG"
  exit 1
fi
# docker ps 给的是短 ID，cgroup 目录名用的是**完整 64 位 ID**，必须再 inspect 一次。
# 失败要显式报错：set -e 下不加兜底的话，容器在 `docker ps` 与这一行之间被删掉
# （窗口很窄但存在）会让脚本**零输出直接退出**，用户完全看不出发生了什么。
FULLID="$(docker inspect -f '{{.Id}}' "$CID" 2>/dev/null || true)"
if [ -z "$FULLID" ]; then
  echo
  echo "❌ 取不到容器完整 ID（docker inspect $CID 失败）"
  echo "   多半是容器在 docker ps 之后、这一步之前就没了 —— 看重放日志："
  tail -20 "$RLOG" | sed 's/^/     /'
  exit 1
fi
CID="$FULLID"
echo "  ✅ $CNAME  ($CID)"

# ── 4. 推 cgroup 路径 ──────────────────────────────────────────
# **不猜 docker 的 cgroup driver，直接问内核**：读容器 1 号进程的 /proc/<pid>/cgroup，
# 那里写的就是这个进程当前所在的 cgroup，driver 是 systemd 还是 cgroupfs、
# 有没有自定义 cgroup-parent、是不是 rootless，一概不用管。
#
# 为什么把原来那套「猜两条路径 + find 兜底」整个换掉：**它在 cgroup v1 上会静默推错**。
# v1 的 /sys/fs/cgroup/ 下是 blkio/ memory/ perf_event/ … 一堆并列的控制器目录，
# 每个下面都有 docker/<id>。原来的 `find -maxdepth 6 -name "*<id>*"` 会命中其中
# **随便一个**（字母序大概率是 blkio），于是 CG 变成 `blkio/docker/<id>`；
# perf 再把它拼到自己的 perf_event 挂载点下 → 一个根本不存在的路径，
# 报错是 `no access to cgroup /sys/fs/cgroup/perf_event/blkio/docker/<id>`，
# 完全看不出是「推导选错了控制器目录」。
#
# v1 / v2 的口径差别：
#   v2  统一层级，-G 的路径相对 /sys/fs/cgroup/      ，/proc/<pid>/cgroup 里是 `0::/...`
#   v1  perf 走 perf_event 独立层级，相对 /sys/fs/cgroup/perf_event/，
#       /proc/<pid>/cgroup 里是 `<n>:perf_event:/...`
#       （v1 上控制器常常是 co-mount 的，那一列会是 `cpu,cpuacct` 这种逗号列表，
#         所以只能用正则按边界匹配 perf_event，不能整列相等比较。）
#
# **路径不存在时 perf 未必报错，可能只给你一串 0** —— 所以这里必须先确认目录真的在，
# 推不出来就直接停，别去采一堆 0 回来。
CPID="$(docker inspect -f '{{.State.Pid}}' "$CID" 2>/dev/null || true)"
if [ -z "$CPID" ] || [ "$CPID" = 0 ]; then
  echo
  echo "❌ 取不到容器主进程 PID（docker inspect -f '{{.State.Pid}}' $CID）"
  echo "   PID 为 0 一般意味着容器已经退出了 —— 看重放日志："
  tail -20 "$RLOG" | sed 's/^/     /'
  exit 1
fi
PROCCG="/proc/$CPID/cgroup"
if [ ! -r "$PROCCG" ]; then
  echo
  echo "❌ 读不到 $PROCCG —— 容器主进程（PID $CPID）可能刚退出。"
  echo "   没有它就推不出 cgroup 路径。重放日志："
  tail -20 "$RLOG" | sed 's/^/     /'
  exit 1
fi
# awk 说明：$3 是以 / 开头的 cgroup 路径，substr($3,2) 去掉前导 /（perf -G 要相对路径）；
# 加 exit 是保证只取第一条匹配，多条时不会拼成带换行的值。
if [ "$CGVER" = v1 ]; then
  CG="$(awk -F: '$2 ~ /(^|,)perf_event(,|$)/ {print substr($3,2); exit}' "$PROCCG")"
  CGABS="/sys/fs/cgroup/perf_event/$CG"
else
  CG="$(awk -F: '$1==0 {print substr($3,2); exit}' "$PROCCG")"
  CGABS="/sys/fs/cgroup/$CG"
fi
if [ -z "$CG" ] || [ ! -d "$CGABS" ]; then
  echo
  echo "❌ 推不出容器的 cgroup 路径（cgroup $CGVER）。"
  echo "   相对路径  ${CG:-<空>}"
  echo "   绝对路径  $CGABS   $([ -d "$CGABS" ] && echo '(存在)' || echo '(不存在)')"
  echo "   $PROCCG 原文（这是排查这件事的唯一线索）："
  sed 's/^/     /' "$PROCCG"
  if [ "$CGVER" = v1 ]; then
    echo "   v1 上要找的是 perf_event 那一行。如果压根没有这一行，说明内核没挂载"
    echo "   perf_event 控制器：ls /sys/fs/cgroup/ 看一眼，没有 perf_event/ 就采不了。"
  else
    echo '   v2 上要找的是 `0::/...` 那一行。'
  fi
  exit 1
fi
echo "  cgroup      $CG"
echo "  绝对路径    $CGABS（cgroup $CGVER，由 $PROCCG 推出，不猜 driver）"

# ── 5. 采集 ────────────────────────────────────────────────────
# tail --pid=$RPID -f /dev/null 只是个「跟着 replay 一起活」的空壳子进程：
# perf 采的是 -a -G 系统级 + cgroup 过滤，跟这个子进程本身的负载无关，
# 它唯一的作用是给 perf 一个准确的结束时刻。
#
# --per-step 模式：加 -I 10（每 10ms 打印一组计数器增量），事后用
# topdown_steps.py 按 step 时间窗口归并。需要在 perf 启动前记录
# perf_start_mono，在 replay 的 verdict.json 里读 t_start_mono，
# 两者之差就是时钟对齐的常数偏移。
echo
echo "── 采集中 ──────────────────────────────────────────────────"
PERF_INTERVAL_OPT=()
PERF_START_MONO=""
if [ "$PER_STEP" = 1 ]; then
  PERF_INTERVAL_OPT=(-I 10)
  PERF_START_MONO=$(python3 -c "import time; print(f'{time.monotonic():.6f}')")
  echo "$PERF_START_MONO" > "$TD/perf_start_mono.txt"
  echo "  $SUDO perf stat -a -I 10 ${OUTOPT[*]} -o $PERFOUT -e '<事件组>' -G $CG -- tail --pid=$RPID -f /dev/null"
  echo "  perf_start_mono = $PERF_START_MONO（写入 $TD/perf_start_mono.txt）"
else
  echo "  $SUDO perf stat -a ${OUTOPT[*]} -o $PERFOUT -e '<事件组>' -G $CG -- tail --pid=$RPID -f /dev/null"
fi
echo "  （重放跑完 perf 自动收尾；进度看 tail -f $RLOG）"
echo
PERF_RC=0
# ⚠️⚠️ **`-G` 必须排在 `-e` 后面**，顺序反了 perf 直接拒绝启动：
#     `must define events before cgroups`
#   原因在 perf 自己的 util/cgroup.c：parse_cgroups() 解析 -G 时会检查 evlist
#   是不是空的，空就报这句然后退出。man perf-stat 的原话是 cgroup
#   "always refer to events defined earlier on the command line" ——
#   也就是 -G 是**按位置**绑到它前面那些 -e 上的，不是一个全局开关。
#   这个报错信息完全没提「参数顺序」，不翻 man page 很难联想到，
#   所以这行的顺序不要「顺手整理」成看着更顺眼的样子。
#   （上面那行 echo 打给用户看的命令必须和这里**逐字一致**，否则排查时会把人带偏。）
#   -I 10 也必须排在 -e 前面：它是全局选项，不是事件属性。
$SUDO perf stat -a "${PERF_INTERVAL_OPT[@]}" "${OUTOPT[@]}" -o "$PERFOUT" \
      -e "$EVSPEC" -G "$CG" -- tail --pid="$RPID" -f /dev/null 2>"$TD/perf.stderr" || PERF_RC=$?

# ── 6. 收重放的退出码 ──────────────────────────────────────────
RRC=0
wait "$RPID" || RRC=$?
RPID=""          # 已经收尸，cleanup 不用再动它
# 先落一版（parse_rc 还不知道，记 null）：万一下面解析这一步自己炸了，
# 调用方至少还能知道重放本身成没成。
write_status null
echo "  重放退出码  $RRC"
echo "  perf 退出码 $PERF_RC"
if [ "$PERF_RC" != 0 ]; then
  echo "  perf stderr："
  head -12 "$TD/perf.stderr" | sed 's/^/     /'
fi

# verdict.json 是判定环境等价的唯一硬标准，PMU 数只是旁证，所以这里先把它亮出来
VERDICT="$OUTDIR/$TNAME/verdict.json"
if [ -f "$VERDICT" ]; then
  echo
  echo "── 重放判定（这才是环境等价的硬标准）────────────────────"
  python3 - "$VERDICT" <<'PYV'
import json, sys
v = json.load(open(sys.argv[1]))
for k in ("patch_identical", "n_replayed", "rc_match", "rc_match_semantic", "elapsed_s"):
    if k in v:
        print(f"  {k:20s} {v[k]}")
PYV
  echo "  完整判定  $VERDICT"
fi

# ── 7. 解析 ────────────────────────────────────────────────────
echo
if [ ! -s "$PERFOUT" ]; then
  echo "❌ perf 没写出任何结果：$PERFOUT"
  head -12 "$TD/perf.stderr" | sed 's/^/     /'
  # parse_rc 记 1：解析根本没跑（没东西可解析）。重放的结论仍然在 replay_rc 里。
  write_status 1
  exit 1
fi
PRC=0
if [ "$PER_STEP" = 1 ]; then
  CMDS_JSONL="$OUTDIR/$TNAME/commands.jsonl"
  python3 "$HERE/topdown_steps.py" "$PERFOUT" \
          --conf "$CONF" --slots "$SLOTS" \
          --commands "$CMDS_JSONL" \
          --verdict "$VERDICT" \
          --perf-start-mono "$TD/perf_start_mono.txt" \
          --json-out "$TD/topdown_steps.json" \
          --title "ARM L1 Topdown (per-step) · $TNAME" || PRC=$?
else
  python3 "$HERE/topdown_parse.py" "$PERFOUT" --conf "$CONF" --slots "$SLOTS" \
          --json-out "$TD/topdown.json" \
          --title "ARM L1 Topdown · $TNAME（整条 trial 聚合）" || PRC=$?
fi
write_status "$PRC"

echo
echo "── 覆盖范围提醒 ────────────────────────────────────────────"
if [ "$PER_STEP" = 1 ]; then
  echo "  per-step 模式：每 10ms 一个 interval，按 step 时间窗口归并。"
  echo "  step 边界误差 ≤ 10ms（一个 interval）。短 step（<100ms）的 topdown"
  echo "  可能因计数不足而不稳定——看 topdown_steps.json 的 cycles 列判断可信度。"
else
  echo "  这组数覆盖的是**整条 trial**：容器启动 + 全部重放命令 + 收尾 git diff。"
  echo "  sidecar（${CNAME}-sink）是独立 cgroup，天然不在内。"
  echo "  短命令里 timeout + /bin/sh 的进程启动与动态链接开销**也算在里面**，"
  echo "  详见 TOPDOWN.md「短命令的数据有效性」一节。"
fi

# 重放没过 / 自检没过，都要让退出码带出来，便于串到脚本里
[ "$RRC" = 0 ] || exit "$RRC"
exit "$PRC"
