#!/usr/bin/env bash
# ARM PMU 探针 —— 目标机上跑的**第一件事**，只读 + 起一个临时容器，不改任何东西。
#
# 目标机是 **baremetal ARM**，所以「虚机 vPMU 没透传」不是这里的风险。
# baremetal 上真正会出事的是下面三样，任何一样不对，后面建镜像那几分钟和
# 整条重放（开发机上 222s，ARM 上只会更久）都是白做：
#
#   1. **事件号在这颗核上到底有没有效**。ARM PMUv3 只把一小部分事件号定为架构必需，
#      其余各家核自己编号。号不对的表现分两种：perf 直接给 <not supported>（好办），
#      或者**有数、但数是别的东西**（难办 —— 得靠 topdown_parse.py 那组自检兜）。
#      ⚠️ 兜底能力取决于后端口径：直接法（EV_STALL_SLOT_BE 填了）有「四象限求和 ≈ 1」；
#      **残差法（留空）下求和恒等于 1，那条自检失效**，只剩 C1~C5 这组不等式，
#      判别力弱一截。详见 topdown.conf 里 EV_STALL_SLOT_BE 那一段。
#   2. **通用计数器余量够不够**。这是 baremetal 上最隐蔽的一条：NMI/hardlockup
#      watchdog 会**常驻占掉一个通用计数器**，6 个变 5 个。本套要开的主事件是
#      残差法 4 个 / 直接法 5 个（再填 EV_STALL_SLOT 各 +1），卡在边界上 ——
#      再往 EV_EXTRA 里加就可能复用。复用之下每个事件只在一部分时间真在计数，
#      其余靠外推，自检跟着红，而现象看起来像「事件号错了」，
#      能把人带到完全错误的方向上去。
#   3. **`perf stat -a -G <cgroup>` 真能按容器过滤出数**。和虚拟化无关：docker 的
#      cgroup driver 有 cgroupfs / systemd 两种落点，路径推错时 perf **不报错**，
#      只是安安静静给你一串 0。
#
# 所以本脚本不查配置、只做**活体验证**：真起一个容器、真在里面烧 CPU、
# 真在宿主侧按 cgroup 过滤采一次，再把采回来的数交给 topdown_parse.py 跑那组自检
# （跑哪几条取决于后端口径，见第 2 节打印的「后端口径」）。
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

# ── cgroup 版本：和 PMU / perf 版本并列，属于环境摘要 ──
# 必须在最开头就报告，因为 v1 和 v2 下 perf -G 的路径口径**完全不同**：
#   v2  统一层级，-G 的路径相对 /sys/fs/cgroup/
#   v1  perf 走 **perf_event 这个独立层级**，路径相对 /sys/fs/cgroup/perf_event/
# v1 上路径推错的表现是 `no access to cgroup /sys/fs/cgroup/perf_event/xxx`，
# 而同一台 v1 机器上 replay.py 还会另外报一句「sinkhole cgroup 初始化失败」
# （它那套 per-command 指标只认 v2 的 cpu.stat / memory.current）——
# 两个报错看着不相干，根因是同一个。不把版本亮出来，人就会当成两个独立问题去查。
CGFS_T="$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo 未知)"
if [ "$CGFS_T" = cgroup2fs ]; then CGVER=v2; else CGVER=v1; fi
if [ "$CGVER" = v2 ]; then
  ok "cgroup v2（统一层级，/sys/fs/cgroup 类型 $CGFS_T）—— perf -G 路径相对 /sys/fs/cgroup/"
else
  warn "cgroup **v1**（/sys/fs/cgroup 类型 $CGFS_T）—— 两件事跟着变，都已处理，但你要知道：
       1) perf -G 走 **perf_event 独立层级**，路径相对 /sys/fs/cgroup/perf_event/。
          本脚本直接读 /proc/<容器主进程 pid>/cgroup 拿这个路径，不猜 docker driver。
       2) **replay.py 的 per-command cgroup 指标在 v1 上不可用**（它只认 v2 的
          cpu.stat / memory.current），启动时会报「sinkhole cgroup 初始化失败」。
          正式采集时加 --no-metrics 绕过：bash topdown_trial.sh <trial> --no-metrics
          —— PMU 采集完全不经过它，四象限和 patch_identical 都不受影响。"
  if [ ! -d /sys/fs/cgroup/perf_event ]; then
    bad "/sys/fs/cgroup/perf_event 不存在 —— v1 上内核没挂载 perf_event 控制器，
       perf -G 按 cgroup 过滤做不了。挂上它：
         sudo mkdir -p /sys/fs/cgroup/perf_event
         sudo mount -t cgroup -o perf_event perf_event /sys/fs/cgroup/perf_event"
  fi
fi

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

# ── 事件号格式校验：必须是 0x 开头的十六进制 ──
# 为什么要专门拦：perf 的 `event=` 按 **C 风格**解析数字 —— 配置里写 `11`，
# perf 读到的是**十进制 11**（= 事件 0xb，BR_MIS_PRED），而 CPU_CYCLES 是 0x11。
# 这个错 perf **完全不报错**：0xb 是合法事件，照样有数，只是数的是别的东西。
# 探针到这里就把它拦下来，比等正式采集跑完再去猜哪里不对划算得多。
# （这段校验和 topdown_trial.sh 里的 evcode_bad 是同一套，改一处要两处一起改。）
evcode_bad() {   # $1=配置键名  $2=值；不合法时打印原因并返回 0（真）
  local key="$1" val="$2"
  if [[ "$val" =~ ^0[xX][0-9a-fA-F]+$ ]]; then return 1; fi
  bad "$key=$val 不是 0x 开头的十六进制（应写成 0x0011 / 0x11 这样）
       perf 的 event= 按 C 风格解析数字：写 \`11\` 它读的是**十进制 11**（= 0xb），
       数到的是另一个合法事件，**不报任何错**，四象限静默算错。
       多半改成 0x${val} 就对了，但请按目标核 TRM 核对一遍。"
  return 0
}
EVCODE_OK=1
if [ -f "$CONF" ]; then
  for k in EV_CPU_CYCLES EV_OP_RETIRED EV_OP_SPEC EV_STALL_SLOT_FE; do
    v="${!k:-}"
    if [ -z "$v" ]; then
      bad "topdown.conf 里 $k 是空的 —— 这四个事件是必需的，不能留空"
      EVCODE_OK=0
    elif evcode_bad "$k" "$v"; then
      EVCODE_OK=0
    fi
  done
  # 这两个留空是合法的（各有含义），填了才校验
  for k in EV_STALL_SLOT_BE EV_STALL_SLOT; do
    v="${!k:-}"
    if [ -n "$v" ] && evcode_bad "$k" "$v"; then EVCODE_OK=0; fi
  done
  for kv in ${EV_EXTRA:-}; do
    [ -n "$kv" ] || continue
    case "${kv%%=*}" in
      *[!A-Za-z0-9_]*|"") bad "EV_EXTRA 里的事件名不合法: ${kv%%=*}（perf 的 name= 只收 [A-Za-z0-9_]）"; EVCODE_OK=0; continue ;;
    esac
    if evcode_bad "EV_EXTRA 里的 ${kv%%=*}" "${kv#*=}"; then EVCODE_OK=0; fi
  done
  [ "$EVCODE_OK" = 1 ] && ok "事件号格式都合法（0x 开头的十六进制）"
fi

# ── 后端口径：EV_STALL_SLOT_BE 有值 = 直接法，留空 = 残差法 ──
# 这个判定必须和 topdown_trial.sh / topdown_parse.py 完全一致，否则探针验的是
# 一套口径、正式采集用的是另一套，探针就白验了。
if [ -n "${EV_STALL_SLOT_BE:-}" ]; then
  BE_MODE=direct
  echo "  后端口径  直接法（EV_STALL_SLOT_BE=$EV_STALL_SLOT_BE）"
  echo "     BackendBound = STALL_SLOT_BACKEND / (CPU_CYCLES × SLOTS)"
  echo "     「四象限求和 ≈ 1」这条自检**有效**，是防 SLOTS/事件号出错的主力。"
else
  BE_MODE=residual
  echo "  后端口径  残差法（EV_STALL_SLOT_BE 留空）"
  echo "     BackendBound = 1 − (Retiring + BadSpec + FrontendBound)"
  warn "残差法下「四象限求和 ≈ 1」**恒成立，不再是校验** ——
       它原本是防「SLOTS 取错 / 事件号写错」的主安全网，现在由
       C1（残差非负）/ C2（OP_SPEC ≥ OP_RETIRED）/ C3（退休率 ≤ SLOTS）/
       C4（取值域）/ C5（复用）顶上，判别力弱一截。
       这组**抓不到「SLOTS 偏大」**：分母放大只会让残差里的 BackendBound 变大，
       每条不等式反而更宽松。要补这个窟窿，只能填 topdown.conf 的 EV_STALL_SLOT
       （STALL_SLOT，架构值 0x003f）开 X 交叉校验 —— 代价是多占一个通用计数器。"
fi
if [ -n "${EV_STALL_SLOT:-}" ]; then
  ok "X 交叉校验开着（EV_STALL_SLOT=$EV_STALL_SLOT）—— 残差法下唯一真正独立的一条校验"
else
  echo "  X 交叉校验  关（EV_STALL_SLOT 留空）"
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
# 它只会让 topdown_parse.py 的自检红（直接法是「求和 ≈ 1」，残差法是 C1/C4），
# 而那个现象看起来跟「事件号写错了」一模一样 —— 不先把余量的账算清楚，
# 很容易一路去抠 TRM 事件号，方向全错。
hdr "3. 计数器余量（谁在占计数器）"

# 需要开几个：主事件（残差法 4 / 直接法 5）+ EV_STALL_SLOT（填了才占）+ EV_EXTRA
N_EXTRA=$(printf '%s\n' ${EV_EXTRA:-} | grep -c . || true)
N_MAIN=4
MAIN_DESC="4 个（残差法：CPU_CYCLES/OP_RETIRED/OP_SPEC/STALL_SLOT_FE）"
if [ "${BE_MODE:-residual}" = direct ]; then
  N_MAIN=5
  MAIN_DESC="5 个（直接法：上面四个 + STALL_SLOT_BACKEND）"
fi
N_X=0
if [ -n "${EV_STALL_SLOT:-}" ]; then N_X=1; fi
N_NEED=$((N_MAIN + N_X + N_EXTRA))

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
row "本次要开的事件数" "$N_NEED"
row "  主事件" "$MAIN_DESC"
row "  EV_STALL_SLOT" "$N_X   （X 交叉校验用；0 = 留空没开）"
row "  EV_EXTRA" "$N_EXTRA"
if [ -n "$N_GP" ]; then
  N_AVAIL=$((N_GP - WD_TAKES))
  row "通用计数器总数" "$N_GP"
  row "watchdog 占用" "$WD_TAKES"
  row "实际可用" "$N_AVAIL"
  if [ "$N_NEED" -lt "$N_AVAIL" ]; then
    ok "余量够（要 $N_NEED，可用 $N_AVAIL），EV_EXTRA 还能再加 $((N_AVAIL - N_NEED)) 个"
  elif [ "$N_NEED" -eq "$N_AVAIL" ]; then
    warn "刚好用满（要 $N_NEED，可用 $N_AVAIL）——
       现在不会复用，但 **再加一个事件就必然复用**。
       要腾地方：$([ "$WD_TAKES" = 1 ] && echo '先 sudo sysctl kernel.nmi_watchdog=0 关掉 watchdog' || echo '只能分两轮采')。"
    # 这句只在真开了 X 的时候才有意义，写成独立的 if 而不是 $(... || echo '')——
    # 后者在不满足时会留下一整行空白，看着像输出被截断了
    if [ "$N_X" = 1 ]; then
      echo "       实在腾不出来，清掉 EV_STALL_SLOT 能省一个 —— 但那会丢掉 X 交叉校验，"
      echo "       残差法下那是唯一真正独立的一条校验，权衡清楚再动。"
    fi
  else
    bad "余量不够：要开 $N_NEED 个，实际只有 $N_AVAIL 个可用 → **一定会发生复用**，
       四象限跟着偏（C5 会红）。三条路：清空/减少 topdown.conf 的 EV_EXTRA 分两轮采；
       按上面的提示把 watchdog 临时关掉腾出 1 个；
       或者清掉 EV_STALL_SLOT（省 1 个，代价是丢掉 X 交叉校验）。"
  fi
else
  row "通用计数器总数" "未知（dmesg 读不到）"
fi

# ── 4. 活体验证：-G 真的能在容器上算出数 ───────────────────────
hdr "4. 活体验证：perf stat -a -e {事件组} -G <cgroup> 能不能采到容器里的数"
if [ -z "${PERF_MAJ:-}" ]; then
  bad "perf 不可用，活体验证跳过"
else

# 事件组：和 topdown_trial.sh 用的是同一套拼法（两边必须保持一致，改一处要改两处）
# 整组用 {} 包住 → perf 保证这组事件同时上、同时下，比值才有意义。
#
# ⚠️ EV_STALL_SLOT_BE / EV_STALL_SLOT 留空时**绝对不能**拼进去：拼出来是
#    `.../event=,name=stall_slot_backend/`，perf 直接拒绝解析，**整组都开不起来**，
#    连另外四个事件都采不到 —— 探针会报成「perf stat 没跑成」，方向全错。
build_evspec() {
  local sep="" spec="{" pair kv
  local pairs=()
  # shellcheck disable=SC2154
  pairs+=("$EV_CPU_CYCLES:cpu_cycles" "$EV_OP_RETIRED:op_retired" \
          "$EV_OP_SPEC:op_spec" "$EV_STALL_SLOT_FE:stall_slot_frontend")
  [ -n "${EV_STALL_SLOT_BE:-}" ] && pairs+=("$EV_STALL_SLOT_BE:stall_slot_backend")
  # name= 取 stall_slot_total 而不是 stall_slot：后者是另外两个名字的前缀，
  # topdown_parse.py 按 name 做子串兜底匹配时会出歧义。
  [ -n "${EV_STALL_SLOT:-}" ] && pairs+=("$EV_STALL_SLOT:stall_slot_total")
  for pair in "${pairs[@]}"; do
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
elif [ "${EVCODE_OK:-1}" != 1 ]; then
  # 事件号格式就不对的话，拼出来的事件组本身是错的 —— 再跑一次只会拿到一堆
  # 「有数但数的是别的东西」，反而给人一种「探针过了」的错觉。直接不跑。
  bad "事件号格式不合法（见第 2 节），活体验证**不跑** ——
       用一组错的事件号去采，采回来的数看着正常但全是别的事件，
       那比不采更危险。先把 topdown.conf 改对再来。"
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

      # cgroup 路径：perf -G 要的是**相对挂载点**的路径，不是绝对路径。
      # **不猜 docker 的 cgroup driver，直接问内核**：读容器主进程的 /proc/<pid>/cgroup。
      #
      # 为什么把原来那套「猜两条路径 + find 兜底」整个换掉：**它在 cgroup v1 上会静默推错**。
      # v1 的 /sys/fs/cgroup/ 下是 blkio/ memory/ perf_event/ … 一堆并列的控制器目录，
      # 每个下面都有 docker/<id>。原来的 `find -maxdepth 6 -name "*<id>*"` 会命中其中
      # **随便一个**（字母序大概率是 blkio），CG 变成 `blkio/docker/<id>`；perf 再把它
      # 拼到自己的 perf_event 挂载点下 → 一个不存在的路径，报错是
      # `no access to cgroup /sys/fs/cgroup/perf_event/blkio/docker/<id>`，
      # 完全看不出是「推导选错了控制器目录」。
      #
      # v1 上控制器常常 co-mount（那一列是 `cpu,cpuacct` 这种逗号列表），
      # 所以只能按边界正则匹配 perf_event，不能整列相等比较。
      # substr($3,2) 去掉前导 /；加 exit 保证只取第一条，不会拼成带换行的值。
      # （这段推导和 topdown_trial.sh 第 4 节是同一套，改一处要两处一起改。）
      CG=""; CGABS=""; CPID=""; PROCCG=""
      CPID="$(docker inspect -f '{{.State.Pid}}' "$CNAME" 2>/dev/null)"
      if [ -n "$CPID" ] && [ "$CPID" != 0 ]; then
        PROCCG="/proc/$CPID/cgroup"
        if [ -r "$PROCCG" ]; then
          if [ "$CGVER" = v1 ]; then
            CG="$(awk -F: '$2 ~ /(^|,)perf_event(,|$)/ {print substr($3,2); exit}' "$PROCCG")"
            CGABS="/sys/fs/cgroup/perf_event/$CG"
          else
            CG="$(awk -F: '$1==0 {print substr($3,2); exit}' "$PROCCG")"
            CGABS="/sys/fs/cgroup/$CG"
          fi
          [ -d "$CGABS" ] || CG=""
        fi
      fi

      if [ -z "$CG" ]; then
        bad "推不出容器的 cgroup 路径（cgroup $CGVER）"
        echo "       容器主进程 PID  ${CPID:-<取不到>}"
        echo "       相对路径        ${CG:-<空>}"
        echo "       绝对路径        ${CGABS:-<空>}   $([ -n "$CGABS" ] && { [ -d "$CGABS" ] && echo '(存在)' || echo '(不存在)'; })"
        if [ -n "$PROCCG" ] && [ -r "$PROCCG" ]; then
          echo "       $PROCCG 原文（排查这件事的唯一线索）："
          sed 's/^/         /' "$PROCCG"
        else
          echo "       $PROCCG 读不到（容器可能已经退出）"
        fi
        if [ "$CGVER" = v1 ]; then
          echo "       v1 上要找的是 perf_event 那一行。压根没有这一行 = 内核没挂载"
          echo "       perf_event 控制器，perf -G 按 cgroup 过滤就做不了。"
        fi
        echo "       注意 perf -G 路径不对时未必报错，也可能只给你一串 0 ——"
        echo "       所以这一步必须先确认路径存在，再去采。"
      else
        ok "cgroup 路径  $CG"
        echo "     绝对路径  $CGABS（cgroup $CGVER，由 $PROCCG 推出，不猜 driver）"

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

        # ⚠️⚠️ **`-G` 必须排在 `-e` 后面**，顺序反了 perf 直接拒绝启动：
        #     `must define events before cgroups`
        #   原因在 perf 自己的 util/cgroup.c：parse_cgroups() 解析 -G 时会检查
        #   evlist 是不是空的，空就报这句然后退出。man perf-stat 的原话是 cgroup
        #   "always refer to events defined earlier on the command line" ——
        #   -G 是**按位置**绑到它前面那些 -e 上的，不是一个全局开关。
        #   报错信息里完全没提「参数顺序」，不翻 man page 很难联想到。
        #   （这个顺序曾经是反的，后果是本节的活体验证**从来没真正跑通过** ——
        #    一直走的是下面 `perf stat 没跑成` 那个分支，于是「cgroup 路径推导对不对」
        #    这件本节唯一要验的事，其实一次都没验到。别再把顺序调回去。）
        #   topdown_trial.sh 里那条正式采集的命令必须和这里保持同样的顺序。
        $SUDO perf stat -a "${OUTOPT[@]}" -o "$PERFOUT" \
              -e "$EVSPEC" -G "$CG" -- sleep "$SECS" 2>"$TMPD/perf.err"
        PERF_RC=$?
        if [ "$PERF_RC" != 0 ] || [ ! -s "$PERFOUT" ]; then
          bad "perf stat 没跑成（rc=$PERF_RC）"
          sed 's/^/       /' "$TMPD/perf.err" | head -12
          echo "     常见原因：sudo 没通过；事件号在这个核上不合法；"
          echo "     perf 版本不认 -j（把 topdown.conf 的 PERF_OUTPUT 改成 csv 再试）。"
          if grep -q 'must define events before cgroups' "$TMPD/perf.err" 2>/dev/null; then
            echo "     ⚠️ stderr 里是 \`must define events before cgroups\` —— 这是**参数顺序**问题："
            echo "        -G 必须排在 -e 后面。本脚本里已经是对的，看到这条说明有人改回去了。"
          fi
        else
          echo
          # 判定全部交给 topdown_parse.py：它是正式采集用的同一份代码，
          # 在这里顺带把解析器本身也验了一遍（省得到正式采集才发现解析对不上）。
          # ⚠️ 它跑哪几条自检取决于后端口径 —— 残差法下**没有**求和自检
          # （那条在残差法下恒成立），所以下面的结论措辞也必须跟着分口径写，
          # 绝不能在残差法下说一句「求和 ≈ 1，通过」，那是假的安全感。
          python3 "$HERE/topdown_parse.py" "$PERFOUT" --conf "$CONF" \
                  ${SLOTS:+--slots "$SLOTS"} \
                  --json-out "$TMPD/probe_topdown.json" \
                  --title "活体 -G 验证（$SECS 秒，容器 ${CID:0:12}）"
          PRC=$?
          echo
          case "$PRC" in
            0) if [ "${BE_MODE:-residual}" = direct ]; then
                 ok "活体验证通过：$N_NEED 个事件号在这颗核上都有效、事件组没被复用、
       **四象限求和 ≈ 1**（直接法下这是一条真校验）
       → -G 按 cgroup 过滤确实能采到容器里的数，可以往下走了"
               else
                 ok "活体验证通过：$N_NEED 个事件号在这颗核上都有效、事件组没被复用、
       C1~C5 全过$([ -n "${EV_STALL_SLOT:-}" ] && echo '，X 交叉校验也过' || echo '')
       → -G 按 cgroup 过滤确实能采到容器里的数，可以往下走了"
                 if [ -z "${EV_STALL_SLOT:-}" ]; then
                   warn "但请记住这是**残差法**：求和 ≈ 1 是恒等式，上面那组自检里**没有**它。
       C1~C5 能抓「SLOTS 偏小 / 事件号指错 / 复用」，**抓不到「SLOTS 偏大」** ——
       分母放大只会让残差里的 BackendBound 变大，每条不等式反而更宽松。
       目标核若实现了 STALL_SLOT（架构值 0x003f），把 topdown.conf 的
       EV_STALL_SLOT 填上，X 交叉校验能把这个窟窿补上（代价：多占 1 个计数器）。"
                 fi
               fi ;;
            2) bad "计数采到了，但自检没过（见上面 ❌ 那几条）
       数是真的，只是口径不对：多半是 SLOTS 或某个事件号不对，
       也可能是事件开多了导致复用。把上面的提示逐条过一遍再重跑本探针。
       $([ "${BE_MODE:-residual}" = residual ] && echo '残差法下 C1 失败会直接反推出「最小自洽 SLOTS」，先看那个数。' || echo '求和自检失败会反推出「SLOTS 应约为多少」，先看那个数。')" ;;
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
  echo "# 事件号请按目标核 TRM 再核对一遍；上面的活体验证过了就说明这组能用。"
  echo "# 事件号必须写成 0x 开头的十六进制 —— 写 11 会被 perf 按十进制读成 0xb，"
  echo "# 数到的是别的事件而且不报错。"
  echo
  if [ "${BE_MODE:-residual}" = residual ]; then
    echo "EV_STALL_SLOT_BE=           # 留空 = 残差法（这颗核没实现 STALL_SLOT_BACKEND 时的正确用法）"
    echo "                            # ⚠️ 代价：四象限求和恒等于 1，那条自检失效，改跑 C1~C5"
  else
    echo "EV_STALL_SLOT_BE=$EV_STALL_SLOT_BE        # 直接法，求和自检有效"
  fi
  if [ -n "${EV_STALL_SLOT:-}" ]; then
    echo "EV_STALL_SLOT=$EV_STALL_SLOT           # X 交叉校验开着 —— 残差法下唯一真正独立的校验"
  else
    echo "EV_STALL_SLOT=              # 目标核若实现 STALL_SLOT（0x003f）强烈建议填 0x003f："
    echo "                            # 它是残差法下唯一能抓「SLOTS 偏大」的校验（多占 1 个计数器）"
  fi
  echo
fi

[ "$FAIL" = 0 ]
