#!/usr/bin/env bash
# ARM PMU 探针 —— 目标机上跑的**第一件事**，只读 + 起一个临时容器，不改任何东西。
#
# 目标机是 **baremetal ARM**，所以「虚机 vPMU 没透传」不是这里的风险。
# baremetal 上真正会出事的是下面三样，任何一样不对，后面建镜像那几分钟和
# 整条重放（开发机上 222s，ARM 上只会更久）都是白做：
#
#   1. **事件号在这颗核上到底有没有效**。ARM PMUv3 只把一小部分事件号定为架构必需，
#      其余各家核自己编号。号不对的表现分两种：perf 直接给 <not supported>（好办），
#      或者**有数、但数是别的东西**（难办 —— 只有「四象限求和 ≈ 1」能兜住）。
#   2. **通用计数器余量够不够**。这是 baremetal 上最隐蔽的一条：NMI/hardlockup
#      watchdog 会**常驻占掉一个通用计数器**，6 个变 5 个。我们正好要 5 个事件，
#      卡在边界上 —— 再往 EV_EXTRA 里加一个就必然复用。复用之下每个事件只在一部分
#      时间真在计数，其余靠外推，四象限求和随之不等于 1，而现象看起来像「事件号错了」，
#      能把人带到完全错误的方向上去。
#   3. **`perf stat -a -G <cgroup>` 真能按容器过滤出数**。和虚拟化无关：docker 的
#      cgroup driver 有 cgroupfs / systemd 两种落点，路径推错时 perf **不报错**，
#      只是安安静静给你一串 0。
#
# 所以本脚本不查配置、只做**活体验证**：真起一个容器、真在里面烧 CPU、
# 真在宿主侧按 cgroup 过滤采一次，再把采回来的数交给 topdown_parse.py 做三项判定。
#
# 用法：
#   bash probe_pmu.sh                      # 用 ubuntu:24.04 做活体测试
#   bash probe_pmu.sh <镜像>               # 用一个本地已有的镜像（没网时用这个）
#   bash probe_pmu.sh --emit-conf          # 额外打印可直接粘进 topdown.conf 的片段
#   bash probe_pmu.sh --secs 10 <镜像>     # 采样窗口拉长（默认 3 秒）
#
# 刻意**不开 `set -e`**：探针的价值在于把所有检查跑完再汇总，第一个 ❌ 就退出
# 等于后面几项什么都没验到。这和同目录 preflight.sh 的取舍一致。
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/topdown.conf"

IMAGE=""; EMIT_CONF=0; SECS=3
while [ $# -gt 0 ]; do
  case "$1" in
    --emit-conf) EMIT_CONF=1; shift ;;
    --secs) [ $# -ge 2 ] || { echo "--secs 缺少值"; exit 1; }; SECS="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -[eu]/p' "$0" | sed '$d'; exit 0 ;;
    -*) echo "未知参数: $1"; exit 1 ;;
    *) IMAGE="$1"; shift ;;
  esac
done

PASS=0; FAIL=0; WARN=0
# printf 的 %-Ns 按**字符数**补齐，而汉字占 2 个显示列 —— 中英混排的表格必然错位。
# 按字节数反推 CJK 字符数，自己算显示宽度再补空格。
dispw() { local c b; c=$(printf '%s' "$1" | wc -m); b=$(printf '%s' "$1" | LC_ALL=C wc -c); echo $(( c + (b - c) / 2 )); }
row()   { local pad=$(( 22 - $(dispw "$1") )); [ "$pad" -lt 1 ] && pad=1; printf '  %s%*s%s\n' "$1" "$pad" "" "$2"; }
ok()   { echo "  ✅ $*"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️  $*"; WARN=$((WARN+1)); }
hdr()  { echo; echo "── $* ────────────────────────────────────────────"; }

CNAME="topdown_probe_$$"
TMPD="$(mktemp -d)"
# trap 必须罩住 ^C：活体测试里有一个死循环容器在烧 CPU，中途按 ^C 走掉
# 会留下一个永远在跑的孤儿容器，而且名字带 PID、下次跑还看不出是谁留的。
cleanup() {
  docker rm -f "$CNAME" >/dev/null 2>&1
  rm -rf "$TMPD"
}
trap cleanup EXIT
# INT/TERM 要清理完就退，不能清完再往下跑：容器已经被删掉了，后面那段 perf 采集
# 会对着一个不存在的 cgroup 采出一串 0，反而给出「-G 不工作」的错误结论。
trap 'echo; echo "  ⚠️  收到 SIGINT，中止探测"; cleanup; exit 130' INT
trap 'echo; echo "  ⚠️  收到 SIGTERM，中止探测"; cleanup; exit 143' TERM

echo "=============================================================="
echo " ARM PMU 探针   $(date -u +%FT%TZ)"
echo " 主机: $(uname -srm)"
echo "=============================================================="

# ── 1. 基础环境 ────────────────────────────────────────────────
hdr "1. 基础环境"
ARCH="$(uname -m)"
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
  ok "架构 $ARCH"
else
  warn "架构是 $ARCH，不是 aarch64 —— 本套脚本的事件号与 slot 模型是 ARM PMUv3 的，
       在 x86 上跑只能验到流程，四象限的公式与事件号都不适用"
fi

# 版本号形如 "perf version 6.5.g57d9b1b"、"perf version 5.15.123"，只取前两段
PERF_VER_RAW=""; PERF_MAJ=""; PERF_MIN=""
if command -v perf >/dev/null 2>&1; then
  PERF_VER_RAW="$(perf --version 2>/dev/null | head -1)"
  PERF_MAJ="$(printf '%s' "$PERF_VER_RAW" | sed -nE 's/.*version ([0-9]+)\.([0-9]+).*/\1/p')"
  PERF_MIN="$(printf '%s' "$PERF_VER_RAW" | sed -nE 's/.*version ([0-9]+)\.([0-9]+).*/\2/p')"
fi
PERF_INSTALL_HINT="Ubuntu/Debian:  sudo apt install linux-tools-common linux-tools-\$(uname -r)
       RHEL/CentOS:    sudo dnf install perf
       （云镜像上 linux-tools-\$(uname -r) 这个具体版本常常不在源里，
         退一步装 linux-tools-generic，功能够用）"
if [ -n "$PERF_MAJ" ]; then
  ok "$PERF_VER_RAW"
elif command -v perf >/dev/null 2>&1; then
  # 这一条是本机实测踩到的：Debian/Ubuntu 的 /usr/bin/perf 其实是个 **wrapper 脚本**，
  # 它按 uname -r 去找 linux-tools-<内核版本>/perf。包没装时 wrapper 本身还在，
  # `command -v perf` 照样成功，只有真去跑才会吐一段 "perf not found for kernel ..."。
  # 所以判断 perf 可不可用**必须真跑一次 --version**，不能只看命令在不在。
  bad "perf 命令在，但跑不起来（多半是 Debian/Ubuntu 的 perf wrapper 找不到对应内核的真身）：
$(perf --version 2>&1 | head -3 | sed 's/^/       /')
       $PERF_INSTALL_HINT"
else
  bad "没有 perf —— $PERF_INSTALL_HINT"
fi

PARANOID="$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo 未知)"
echo "  kernel.perf_event_paranoid = $PARANOID"
case "$PARANOID" in
  -1|0) echo "     （≤0：非 root 也能做系统级采样。本套脚本仍走 sudo，更稳）" ;;
  未知) warn "读不到 perf_event_paranoid" ;;
  *)    echo "     （≥1：\`perf stat -a\` 必须 root。topdown_trial.sh 会自动加 sudo；
       先跑一次 \`sudo -v\` 把密码缓存起来，免得采集跑到一半卡在密码提示上）" ;;
esac

if ! docker version >/dev/null 2>&1; then
  bad "docker 不可用 —— 活体测试做不了，探针到此为止"
  echo; echo "结论：先把 docker 弄通再回来。"; exit 1
fi
ok "docker $(docker version -f '{{.Server.Version}}' 2>/dev/null)（cgroup driver: $(docker info -f '{{.CgroupDriver}}' 2>/dev/null)，v$(docker info -f '{{.CgroupVersion}}' 2>/dev/null)）"

# ── 2. PMU ─────────────────────────────────────────────────────
hdr "2. PMU"
EVSRC=/sys/bus/event_source/devices

# topdown.conf **只在这里 source 一次**。
# 踩过的坑：原先第 3 节临用前又 source 了一遍，而配置里 PMU= / SLOTS= 正是推荐留空的，
# 于是第 2 节刚探测出来的值被两个空字符串盖掉，事件组拼成了 `/event=0x0011,...`
# （PMU 前缀没了），SLOTS 也丢了。更坏的是它不报错：perf 收到一个没有 PMU 前缀的
# 事件名照样能解析成默认 PMU，屏幕上还是一片正常。
# 所以配置里的原始值先另存成 CONF_*，PMU/SLOTS 之后由本脚本独占。
CONF_PMU=""; CONF_SLOTS=""
if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  source "$CONF"
  CONF_PMU="${PMU:-}"; CONF_SLOTS="${SLOTS:-}"
else
  bad "找不到 $CONF —— 事件号全在那里面，第 3 节的活体验证做不了"
fi
ALL_PMU="$(ls "$EVSRC" 2>/dev/null | grep '^armv8' || true)"
if [ -z "$ALL_PMU" ]; then
  bad "$EVSRC 下没有 armv8* PMU —— 内核没注册 ARM PMU 驱动"
  echo "     看到的全部 event source："
  ls "$EVSRC" 2>/dev/null | sed 's/^/       /'
  echo "     baremetal 上出现这个，一般是：DT/ACPI 里没描述 PMU 中断（固件/DTB 问题），"
  echo "     或者内核没编 CONFIG_ARM_PMU / CONFIG_ARM_PMUV3。"
  echo "     先看一眼：sudo dmesg | grep -iE 'pmu|perfevents'"
  echo "     （如果这台机其实是虚机，那就是 hypervisor 没给 vPMU —— 但按前提它不是。）"
  echo; echo "结论：❌ 到此为止，后面的活体测试没有意义。"; exit 1
fi
N_PMU="$(printf '%s\n' "$ALL_PMU" | wc -l)"
echo "  armv8* PMU 共 $N_PMU 个："
printf '%s\n' "$ALL_PMU" | sed 's/^/     /'

# 配置里指定了就用配置里的，否则取第一个（和 topdown_trial.sh 的探测口径完全一致）
if [ -n "$CONF_PMU" ]; then
  PMU="$CONF_PMU"
  echo "  选定 $PMU（来自 topdown.conf 的 PMU=）"
  [ -d "$EVSRC/$PMU" ] || bad "topdown.conf 指定的 PMU 不存在: $EVSRC/$PMU"
else
  PMU="$(printf '%s\n' "$ALL_PMU" | head -1)"
  echo "  选定 $PMU（topdown.conf 的 PMU= 留空 → 自动取第一个）"
fi

if [ "$N_PMU" -gt 1 ]; then
  warn "检测到 $N_PMU 个 armv8* PMU —— **这是异构核（big.LITTLE / 多簇）**。
       每个 PMU 对应一簇核，事件号、SLOTS 都可能不同，而且
       \`-e '{$PMU/.../}'\` 只会在**那一簇核**上打开事件：
       进程被调度到别的簇时，那段时间一个数都采不到（不报错，只是计数偏小）。
       这一版脚本只处理单簇。要在异构机上用，两条路：
         a) 用 taskset / docker --cpuset-cpus 把重放容器钉在同一簇上（推荐）；
         b) 每个 PMU 各开一组事件分别采，再自己合并（本脚本不支持）。"
fi

# caps/slots —— 四象限的公共分母，也是最不能猜的一个数
CAPS_SLOTS=""
if [ -r "$EVSRC/$PMU/caps/slots" ]; then
  CAPS_SLOTS="$(cat "$EVSRC/$PMU/caps/slots" 2>/dev/null | tr -dc '0-9')"
fi
# 取值优先级必须和 topdown_trial.sh 完全一致（配置里写了就用配置的），
# 否则探针验的是一套数、正式采集用的是另一套，探针就白验了。
SLOTS="$CAPS_SLOTS"; SLOTS_SRC="caps/slots（内核导出，运行时读）"
if [ -n "$CONF_SLOTS" ]; then
  SLOTS="$CONF_SLOTS"; SLOTS_SRC="topdown.conf 写死的值"
  if [ -z "$CAPS_SLOTS" ]; then
    warn "SLOTS=$CONF_SLOTS 来自 topdown.conf（本机 caps/slots 读不到，只能这样）。
       请确认它就是目标核 TRM 上的值 —— 错了四个比值会一起静默偏移。"
  elif [ "$CONF_SLOTS" != "$CAPS_SLOTS" ]; then
    bad "冲突：topdown.conf 写死 SLOTS=$CONF_SLOTS，本机 caps/slots 读出来是 $CAPS_SLOTS。
       topdown_trial.sh 会听配置的（$CONF_SLOTS），本探针也按同样口径往下验 ——
       但内核导出的 $CAPS_SLOTS 才是这颗核的真值。
       把 topdown.conf 里那一行清空（留空 = 每次运行现读），这正是推荐留空的原因。"
  else
    warn "SLOTS=$CONF_SLOTS 来自 topdown.conf（值和 caps/slots 一致，本机没问题）。
       但它是写死的：换机器/换核不会自动跟着变。建议清空该行改成运行时读。"
  fi
fi
if [ -n "$SLOTS" ] && [ "$SLOTS" -gt 0 ] 2>/dev/null; then
  ok "SLOTS = $SLOTS（每周期 issue slot 数，四象限的公共分母）← $SLOTS_SRC"
else
  bad "读不到 $EVSRC/$PMU/caps/slots
       这个核的驱动没导出 slots（老内核常见）。**不要随便填一个常数** ——
       SLOTS 错了四个比值会一起静默偏移，看着还都在 0~1 之间。
       只有拿到目标核 TRM 的确切值（V1=8，N2=5，别的核另查）才填进 topdown.conf。"
fi

# format/event —— 事件号字段宽度。config:0-15 说明支持 16 位事件号（PMUv3 的常规）
if [ -r "$EVSRC/$PMU/format/event" ]; then
  FMT="$(cat "$EVSRC/$PMU/format/event")"
  if [ "$FMT" = "config:0-15" ]; then
    ok "format/event = $FMT（16 位事件号，0x003a 这类写法直接可用）"
  else
    warn "format/event = $FMT（期望 config:0-15）—— 事件号字段宽度和预期不同，
       topdown.conf 里的 0x00xx 写法可能要调整"
  fi
else
  warn "读不到 $EVSRC/$PMU/format/event"
fi

# 通用计数器个数 —— 决定一次最多能开几个事件，超了就会复用
# 通用计数器个数 —— 决定一次最多能开几个事件。余量的账在第 3 节算。
DMESG_LINE="$(dmesg 2>/dev/null | grep -i pmuv3 | tail -1 || true)"
NCTR=""
if [ -n "$DMESG_LINE" ]; then
  echo "  dmesg:  $DMESG_LINE"
  NCTR="$(printf '%s' "$DMESG_LINE" | sed -nE 's/.*[^0-9]([0-9]+) counters available.*/\1/p')"
else
  echo "  dmesg:  取不到（kernel.dmesg_restrict=1 或缓冲区已被覆盖）"
  echo "     手动看一眼：sudo dmesg | grep -i pmuv3"
fi

# ── 3. 计数器余量：谁在占计数器 ────────────────────────────────
# baremetal 上最容易被忽略、又最难排查的一条。复用（multiplexing）不会报错，
# 它只会让「四象限求和 ≈ 1」这条自检失败，而那个现象看起来跟「事件号写错了」
# 一模一样 —— 不先把余量的账算清楚，很容易一路去抠 TRM 事件号，方向全错。
hdr "3. 计数器余量（谁在占计数器）"

# 需要开几个：5 个 L1 必需 + EV_EXTRA
N_EXTRA=$(printf '%s\n' ${EV_EXTRA:-} | grep -c . || true)
N_NEED=$((5 + N_EXTRA))

# 有几个可用：dmesg 报的总数里有 1 个是专用 cycle counter，其余才是通用的
N_GP=""
if [ -n "$NCTR" ]; then
  N_GP=$((NCTR - 1))
  echo "  内核报 $NCTR 个计数器，其中 1 个是专用 cycle counter → **通用计数器 $N_GP 个**"
else
  warn "拿不到通用计数器个数（dmesg 读不到）——
       余量算不了，只能靠第 4 节活体验证里的「调度占比」事后判断有没有复用。"
fi

# ── NMI / hardlockup watchdog ──
# arm64 上如果内核用的是 CONFIG_HARDLOCKUP_DETECTOR_PERF（而不是 buddy / cpuidle
# 那几种不吃 PMU 的实现），watchdog 会**常驻占用一个通用计数器**：6 个变 5 个。
# 我们正好要 5 个，卡在边界上 —— 再加一个 EV_EXTRA 就必然复用。
WD="$(cat /proc/sys/kernel/nmi_watchdog 2>/dev/null || echo 未知)"
WD_TAKES=0
# 顺手判一下 hardlockup detector 的实现方式：是 perf 版才真占计数器。
# 拿不到内核 config 时**按最坏情况算**（当它占），宁可多提醒一句。
KCONF_HIT=""
for kc in /proc/config.gz "/boot/config-$(uname -r)"; do
  [ -r "$kc" ] || continue
  case "$kc" in
    *.gz) KCONF_HIT="$(zcat "$kc" 2>/dev/null | grep -E '^CONFIG_HARDLOCKUP_DETECTOR(_PERF|_BUDDY|_ARCH)?=' || true)" ;;
    *)    KCONF_HIT="$(grep -E '^CONFIG_HARDLOCKUP_DETECTOR(_PERF|_BUDDY|_ARCH)?=' "$kc" 2>/dev/null || true)" ;;
  esac
  [ -n "$KCONF_HIT" ] && break
done
echo "  kernel.nmi_watchdog = $WD"
if [ -n "$KCONF_HIT" ]; then
  printf '%s\n' "$KCONF_HIT" | sed 's/^/     /'
fi
if [ "$WD" = "0" ]; then
  ok "watchdog 已关，不占计数器"
elif [ "$WD" = "未知" ]; then
  warn "读不到 kernel.nmi_watchdog，按最坏情况算它占掉 1 个通用计数器"
  WD_TAKES=1
else
  if printf '%s' "$KCONF_HIT" | grep -q 'CONFIG_HARDLOCKUP_DETECTOR_PERF=y'; then
    WD_TAKES=1
    bad "nmi_watchdog=$WD 且内核用的是 **perf 版 hardlockup detector** ——
       它常驻占掉 1 个通用计数器。采集前临时关掉：
         sudo sysctl kernel.nmi_watchdog=0
       采完记得改回去（它是死锁检测，长期关着等于少一层保护）：
         sudo sysctl kernel.nmi_watchdog=1"
  elif printf '%s' "$KCONF_HIT" | grep -qE 'CONFIG_HARDLOCKUP_DETECTOR_(BUDDY|ARCH)=y'; then
    ok "nmi_watchdog=$WD，但用的是 buddy/arch 版 detector，不吃 PMU 计数器"
  else
    WD_TAKES=1
    warn "nmi_watchdog=$WD，但查不到 hardlockup detector 的实现方式（内核 config 读不到）。
       **按最坏情况算它占掉 1 个通用计数器**。要排除嫌疑就临时关掉：
         sudo sysctl kernel.nmi_watchdog=0       # 采完 sudo sysctl kernel.nmi_watchdog=1 改回"
  fi
fi

# ── 同机还有没有别的 perf 会话 ──
# 注意锚定 ^perf：`pgrep -a perf` 是在进程名上做**子串**正则，实测会把 iperf3
# 也匹进来，凭空报一条「有别的 perf 会话在抢计数器」。
OTHER_PERF="$(pgrep -a '^perf' 2>/dev/null | grep -v "probe_pmu" || true)"
if [ -n "$OTHER_PERF" ]; then
  bad "同机还有别的 perf 会话在跑，会和我们抢计数器："
  printf '%s\n' "$OTHER_PERF" | sed 's/^/       /'
  echo "       等它跑完，或确认无用后 kill 掉，再来采。"
else
  ok "没有别的 perf 会话在跑（pgrep -a '^perf' 为空）"
fi

# ── 余量总账 ──
echo
echo "  ── 余量 ──"
row "本次要开的事件数" "$N_NEED   （5 个 L1 必需 + EV_EXTRA $N_EXTRA 个）"
if [ -n "$N_GP" ]; then
  N_AVAIL=$((N_GP - WD_TAKES))
  row "通用计数器总数" "$N_GP"
  row "watchdog 占用" "$WD_TAKES"
  row "实际可用" "$N_AVAIL"
  if [ "$N_NEED" -lt "$N_AVAIL" ]; then
    ok "余量够（要 $N_NEED，可用 $N_AVAIL），EV_EXTRA 还能再加 $((N_AVAIL - N_NEED)) 个"
  elif [ "$N_NEED" -eq "$N_AVAIL" ]; then
    warn "刚好用满（要 $N_NEED，可用 $N_AVAIL）——
       现在不会复用，但 **EV_EXTRA 再加一个事件就必然复用**。
       要腾地方：$([ "$WD_TAKES" = 1 ] && echo '先 sudo sysctl kernel.nmi_watchdog=0 关掉 watchdog' || echo '只能分两轮采')。"
  else
    bad "余量不够：要开 $N_NEED 个，实际只有 $N_AVAIL 个可用 → **一定会发生复用**，
       四象限求和会跟着偏。两条路：清空/减少 topdown.conf 的 EV_EXTRA 分两轮采；
       或者按上面的提示把 watchdog 临时关掉腾出 1 个。"
  fi
else
  row "通用计数器总数" "未知（dmesg 读不到）"
fi

# ── 4. 活体验证：-G 真的能在容器上算出数 ───────────────────────
hdr "4. 活体验证：perf stat -a -G <cgroup> 能不能采到容器里的数"
if [ -z "${PERF_MAJ:-}" ]; then
  bad "perf 不可用，活体验证跳过"
else

# 事件组：和 topdown_trial.sh 用的是同一套拼法（两边必须保持一致，改一处要改两处）
# 整组用 {} 包住 → perf 保证这 5 个事件同时上下同时下，比值才有意义。
build_evspec() {
  local sep="" spec="{"
  # shellcheck disable=SC2154
  for pair in "$EV_CPU_CYCLES:cpu_cycles" "$EV_OP_RETIRED:op_retired" \
              "$EV_OP_SPEC:op_spec" "$EV_STALL_SLOT_FE:stall_slot_frontend" \
              "$EV_STALL_SLOT_BE:stall_slot_backend"; do
    spec="${spec}${sep}${PMU}/event=${pair%%:*},name=${pair##*:}/"; sep=","
  done
  for kv in ${EV_EXTRA:-}; do
    [ -n "$kv" ] || continue
    spec="${spec}${sep}${PMU}/event=${kv#*=},name=${kv%%=*}/"; sep=","
  done
  printf '%s}' "$spec"
}

if [ ! -f "$CONF" ]; then
  echo "  （$CONF 不在，活体验证跳过）"
else
  EVSPEC="$(build_evspec)"
  echo "  事件组（$N_NEED 个）："
  printf '%s\n' "$EVSPEC" | fold -w 68 | sed 's/^/     /'

  # 镜像：默认 ubuntu:24.04，不在本地就让用户指定一个已有的
  [ -n "$IMAGE" ] || IMAGE="ubuntu:24.04"
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    bad "本地没有镜像 $IMAGE"
    echo "     探针不联网去 pull（目标机常常拉不到，而且拉一个基础镜像也要几分钟）。"
    echo "     换一个本地已有的镜像再跑：  bash probe_pmu.sh <镜像>"
    echo "     本地现有镜像（前 10 个）："
    docker images --format '       {{.Repository}}:{{.Tag}}' 2>/dev/null | head -10
    echo; echo "     实在一个都没有：docker pull ubuntu:24.04"
  else
    echo "  测试镜像  $IMAGE"
    # 容器里烧满一个核。用 sh 的死循环而不是别的：任何镜像都有 sh，
    # 而且解释器死循环是一段稳定的「高 Retiring / 少 stall」负载，采出来的数好判断。
    if ! docker run -d --name "$CNAME" "$IMAGE" \
           sh -c 'while :; do :; done' >/dev/null 2>"$TMPD/run.err"; then
      bad "临时容器起不来：$(head -2 "$TMPD/run.err" | tr '\n' ' ')"
    else
      CID="$(docker inspect -f '{{.Id}}' "$CNAME" 2>/dev/null)"
      echo "  临时容器  $CNAME  (${CID:0:12})"

      # cgroup 相对路径：perf -G 要的是**相对 cgroupfs 挂载点**的路径，不是绝对路径。
      # 两种 docker cgroup driver 落点完全不同，都得试；试过哪些路径要打出来，
      # 不然「采到一串 0」这个现象根本看不出是路径推错了。
      CG=""; TRIED=""
      for cand in "system.slice/docker-${CID}.scope" "docker/${CID}"; do
        TRIED="$TRIED
       /sys/fs/cgroup/$cand"
        if [ -d "/sys/fs/cgroup/$cand" ]; then CG="$cand"; break; fi
      done
      if [ -z "$CG" ]; then
        # 兜底：自定义 cgroup-parent / rootless 的落点不在上面两条里，搜一次
        FOUND="$(find /sys/fs/cgroup -maxdepth 6 -type d -name "*${CID}*" -print -quit 2>/dev/null)"
        if [ -n "$FOUND" ]; then
          CG="${FOUND#/sys/fs/cgroup/}"
          TRIED="$TRIED
       $FOUND   (find 兜底命中)"
        fi
      fi

      if [ -z "$CG" ]; then
        bad "找不到容器的 cgroup 目录，试过："
        printf '%s\n' "$TRIED"
        echo "     /sys/fs/cgroup 类型: $(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo 未知)"
        echo "     rootless docker 会落在 user.slice 下；自定义 cgroup-parent 则任意。"
        echo "     注意 perf -G **不会**因为路径不存在而报错，它只会给你一串 0 ——"
        echo "     所以这一步必须先确认路径存在，再去采。"
      else
        ok "cgroup 相对路径  $CG"

        SUDO="sudo"; [ "$(id -u)" = 0 ] && SUDO=""
        # 输出格式：perf >= 5.17 才有 -j。auto 时按版本挑，配置里写死了就听配置的。
        OUTFMT="${PERF_OUTPUT:-auto}"
        if [ "$OUTFMT" = auto ]; then
          if [ "${PERF_MAJ:-0}" -gt 5 ] 2>/dev/null || \
             { [ "${PERF_MAJ:-0}" -eq 5 ] && [ "${PERF_MIN:-0}" -ge 17 ]; } 2>/dev/null; then
            OUTFMT=json
          else
            OUTFMT=csv
          fi
        fi
        if [ "$OUTFMT" = json ]; then OUTOPT=(-j); PERFOUT="$TMPD/probe.json"
        else                          OUTOPT=(-x,); PERFOUT="$TMPD/probe.csv"; fi
        echo "  输出格式  $OUTFMT（perf ${PERF_MAJ:-?}.${PERF_MIN:-?}）"
        echo "  采样 ${SECS}s …（容器正在烧一个核）"

        $SUDO perf stat -a -G "$CG" "${OUTOPT[@]}" -o "$PERFOUT" \
              -e "$EVSPEC" -- sleep "$SECS" 2>"$TMPD/perf.err"
        PERF_RC=$?
        if [ "$PERF_RC" != 0 ] || [ ! -s "$PERFOUT" ]; then
          bad "perf stat 没跑成（rc=$PERF_RC）"
          sed 's/^/       /' "$TMPD/perf.err" | head -12
          echo "     常见原因：sudo 没通过；事件号在这个核上不合法；"
          echo "     perf 版本不认 -j（把 topdown.conf 的 PERF_OUTPUT 改成 csv 再试）。"
        else
          echo
          # 三项判定全部交给 topdown_parse.py：它是正式采集用的同一份代码，
          # 在这里顺带把解析器本身也验了一遍（省得到正式采集才发现解析对不上）。
          python3 "$HERE/topdown_parse.py" "$PERFOUT" --conf "$CONF" \
                  ${SLOTS:+--slots "$SLOTS"} \
                  --json-out "$TMPD/probe_topdown.json" \
                  --title "活体 -G 验证（$SECS 秒，容器 ${CID:0:12}）"
          PRC=$?
          echo
          case "$PRC" in
            0) ok "活体验证通过：五个事件号在这颗核上都有效、事件组没被复用、四象限求和 ≈ 1
       → -G 按 cgroup 过滤确实能采到容器里的数，可以往下走了" ;;
            2) bad "计数采到了，但自检没过（见上面 ❌ 那几条）
       数是真的，只是口径不对：多半是 SLOTS 或某个事件号不对，
       也可能是事件开多了导致复用。把上面的提示逐条过一遍再重跑本探针。" ;;
            1) if [ -z "$SLOTS" ]; then
                 bad "SLOTS 未知，四象限算不出来（计数本身采到没采到见上面的原始计数）。
       这是第 2 节那条 ❌ 的连锁反应 —— 先把 SLOTS 解决掉再回来。"
               else
                 bad "连基本计数都没拿到（见上面的说明）
       如果是 <not supported>：**这个事件号在这颗核上无效** —— 号写错了，
         或这颗核压根不实现这个事件，逐个对照目标核 TRM 改 topdown.conf。
       如果是一串 0：cgroup 路径虽然存在但滤空了，核对 $CG 是不是这个容器。"
               fi ;;
            *) bad "连基本计数都没拿到（见上面的说明）
       如果是 <not supported>：**这个事件号在这颗核上无效** —— 要么号写错了，
         要么这颗核压根不实现这个事件。逐个对照目标核 TRM 的 PMU 事件表改
         topdown.conf（五个里只要有一个不对，整组都开不起来）。
       如果是一串 0：cgroup 路径虽然存在但滤空了，核对 $CG 是不是这个容器。" ;;
          esac
        fi
      fi
    fi
  fi
fi
fi

# ── 5. 汇总 ────────────────────────────────────────────────────
hdr "5. 汇总"
echo "  ✅ $PASS    ❌ $FAIL    ⚠️  $WARN"
if [ "$FAIL" = 0 ]; then
  echo
  echo " 下一步：  bash build_arm.sh returns-validated    # 建镜像（最耗时的一步）"
  echo "           bash topdown_trial.sh returns-validated-error-accumula__8JQj5gw"
else
  echo
  echo " 有 ❌，先按上面的提示处理 —— 事件号 / 计数器余量 / cgroup 路径这三样任何一样不对，
 后面建镜像和整条重放都是白做。"
fi

if [ "$EMIT_CONF" = 1 ]; then
  echo
  echo "── 可直接粘进 topdown.conf 的片段 ──────────────────────────"
  echo
  echo "PMU=$PMU"
  if [ -n "$CAPS_SLOTS" ]; then
    # 刻意 emit 成空值：探测到的数字只放在注释里供核对。
    # 写死它会让脚本在换机器后静默用错分母 —— 这正是 topdown.conf 里反复强调的那件事。
    echo "SLOTS=                      # 本机 caps/slots 探测值 ${CAPS_SLOTS:-未知}；刻意留空，让脚本每次运行时现读"
  else
    echo "SLOTS=                      # 本机 caps/slots 读不到，必须查目标核 TRM 后手填"
  fi
  echo
  echo "# 五个 L1 事件号请按目标核 TRM 再核对一遍；上面的活体验证过了就说明这组能用。"
  echo
fi

[ "$FAIL" = 0 ]
