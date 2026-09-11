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
#
# 跑之前先跑 probe_pmu.sh —— 那一步才是判断这台机器能不能采的地方。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/topdown.conf"

TRIAL=""; OUTDIR="$HERE/topdown_out"; LIMIT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--outdir) [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; OUTDIR="$2"; shift 2 ;;
    --limit)     [ $# -ge 2 ] || { echo "❌ $1 缺少值"; exit 1; }; LIMIT="$2"; shift 2 ;;
    -h|--help)   sed -n '2,/^set -[eu]/p' "$0" | sed '$d'; exit 0 ;;
    -*)          echo "❌ 未知参数: $1"; exit 1 ;;
    *)           TRIAL="$1"; shift ;;
  esac
done
[ -n "$TRIAL" ] || { echo "❌ 用法: bash topdown_trial.sh <trial目录> [-o 输出目录] [--limit N]"; exit 1; }
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
  SLOTS="$(cat "$EVSRC/$PMU/caps/slots" 2>/dev/null | tr -dc '0-9' || true)"
  SLOTS_SRC="$EVSRC/$PMU/caps/slots"
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

# 事件组。整组用 {} 包住 —— 保证这几个事件被内核当成一个调度单元同时上同时下，
# 否则它们各自在不同时间窗口里计数，四象限的比值就没有意义了。
# （这段拼法和 probe_pmu.sh 里的 build_evspec 是同一套，改一处要两处一起改。）
SEP=""; EVSPEC="{"
for pair in "$EV_CPU_CYCLES:cpu_cycles" "$EV_OP_RETIRED:op_retired" \
            "$EV_OP_SPEC:op_spec" "$EV_STALL_SLOT_FE:stall_slot_frontend" \
            "$EV_STALL_SLOT_BE:stall_slot_backend"; do
  EVSPEC="${EVSPEC}${SEP}${PMU}/event=${pair%%:*},name=${pair##*:}/"; SEP=","
done
N_EV=5
for kv in ${EV_EXTRA:-}; do
  [ -n "$kv" ] || continue
  case "${kv%%=*}" in
    *[!A-Za-z0-9_]*|"") echo "❌ EV_EXTRA 里的事件名不合法: ${kv%%=*}（perf 的 name= 只收 [A-Za-z0-9_]）"; exit 1 ;;
  esac
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

# 非 root 时这个 test 返回 1 —— set -e 下必须写成 if，不能用 `test && 赋值`
SUDO="sudo"
if [ "$(id -u)" = 0 ]; then SUDO=""; fi

echo "=============================================================="
echo " ARM topdown 采集（整条 trial 聚合）  $(date -u +%FT%TZ)"
echo "=============================================================="
echo "  trial      $TNAME"
echo "  replay.py  $REPLAY"
echo "  输出       $TD"
echo "  PMU        $PMU（$PMU_SRC）"
echo "  SLOTS      $SLOTS（$SLOTS_SRC）"
echo "  perf       ${PERF_VER_RAW:-未知} → $OUTFMT"
echo "  事件       $N_EV 个"
printf '%s\n' "$EVSPEC" | fold -w 66 | sed 's/^/             /'
if [ -n "$LIMIT" ]; then
  echo "  ⚠️  --limit $LIMIT：只重放前 $LIMIT 条命令，这是冒烟不是正式采集"
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

# ── 4. 推 cgroup 相对路径 ──────────────────────────────────────
# perf -G 要的是相对 /sys/fs/cgroup 的路径。docker 的两种 cgroup driver
# 落点完全不同，都得试。**路径不存在时 perf 不报错，只给你一串 0** ——
# 所以这里必须先确认目录真的在，找不到就直接停，别去采一堆 0 回来。
CG=""; TRIED=""
for cand in "system.slice/docker-${CID}.scope" "docker/${CID}"; do
  TRIED="$TRIED
     /sys/fs/cgroup/$cand"
  if [ -d "/sys/fs/cgroup/$cand" ]; then CG="$cand"; break; fi
done
if [ -z "$CG" ]; then
  FOUND="$(find /sys/fs/cgroup -maxdepth 6 -type d -name "*${CID}*" -print -quit 2>/dev/null || true)"
  if [ -n "$FOUND" ]; then
    CG="${FOUND#/sys/fs/cgroup/}"
    TRIED="$TRIED
     $FOUND   (find 兜底命中)"
  fi
fi
if [ -z "$CG" ]; then
  echo
  echo "❌ 找不到容器的 cgroup 目录。试过："
  printf '%s\n' "$TRIED"
  echo "     /sys/fs/cgroup 类型: $(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo 未知)"
  echo "     rootless docker 落在 user.slice 下；自定义 cgroup-parent 则任意。"
  exit 1
fi
echo "  cgroup      $CG"

# ── 5. 采集 ────────────────────────────────────────────────────
# tail --pid=$RPID -f /dev/null 只是个「跟着 replay 一起活」的空壳子进程：
# perf 采的是 -a -G 系统级 + cgroup 过滤，跟这个子进程本身的负载无关，
# 它唯一的作用是给 perf 一个准确的结束时刻。
echo
echo "── 采集中 ──────────────────────────────────────────────────"
echo "  $SUDO perf stat -a -G $CG ${OUTOPT[*]} -o $PERFOUT -e '<事件组>' -- tail --pid=$RPID -f /dev/null"
echo "  （重放跑完 perf 自动收尾；进度看 tail -f $RLOG）"
echo
PERF_RC=0
$SUDO perf stat -a -G "$CG" "${OUTOPT[@]}" -o "$PERFOUT" \
      -e "$EVSPEC" -- tail --pid="$RPID" -f /dev/null 2>"$TD/perf.stderr" || PERF_RC=$?

# ── 6. 收重放的退出码 ──────────────────────────────────────────
RRC=0
wait "$RPID" || RRC=$?
RPID=""          # 已经收尸，cleanup 不用再动它
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
  exit 1
fi
PRC=0
python3 "$HERE/topdown_parse.py" "$PERFOUT" --conf "$CONF" --slots "$SLOTS" \
        --json-out "$TD/topdown.json" \
        --title "ARM L1 Topdown · $TNAME（整条 trial 聚合）" || PRC=$?

echo
echo "── 覆盖范围提醒 ────────────────────────────────────────────"
echo "  这组数覆盖的是**整条 trial**：容器启动 + 全部重放命令 + 收尾 git diff。"
echo "  sidecar（${CNAME}-sink）是独立 cgroup，天然不在内。"
echo "  短命令里 timeout + /bin/sh 的进程启动与动态链接开销**也算在里面**，"
echo "  详见 TOPDOWN.md「短命令的数据有效性」一节。"

# 重放没过 / 自检没过，都要让退出码带出来，便于串到脚本里
[ "$RRC" = 0 ] || exit "$RRC"
exit "$PRC"
