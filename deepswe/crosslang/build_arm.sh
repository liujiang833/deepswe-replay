#!/usr/bin/env bash
# 从本地 mars-base 出发，在目标机上重建 task 镜像 —— 用于「拉不到 ECR 但能访问
# 各包源」的环境。
#
# 可行的前提（都已核实）：
#   - task.json 里就带着 environment/Dockerfile，不需要额外数据
#   - 113 个 Dockerfile 全部零 COPY / 零 ADD，构建上下文可以是空目录，
#     镜像内容 100% 由 Dockerfile 文本 + 网络决定
#   - 构建完打上 task.toml 里原本的 docker_image tag，replay.py 零改动
#
# ⚠️ 重建出来的镜像**不等于**原 amd64 镜像。改写项与漂移风险见 REWRITES.md
#    和脚本末尾的提示。patch_identical 在重建镜像上是否仍然成立，是开放问题。
#
# 用法：
#   bash build_arm.sh --list                 # 只看会做什么改写，不构建
#   bash build_arm.sh python                 # 建一条（语言名或 task_id 前缀）
#   bash build_arm.sh all                    # 全建，按依赖从少到多排序
#   bash build_arm.sh --base mars-base:arm64 python
#   bash build_arm.sh --registry https://registry.npmmirror.com typescript
#   bash build_arm.sh --godebug http2client=0 go
#   bash build_arm.sh --goproxy https://goproxy.cn,direct go
#   bash build_arm.sh --gomaxprocs 4 go      # 压低 go 取模块并发（代价见下方提示语）
#   bash build_arm.sh --build-env NPM_CONFIG_NETWORK_CONCURRENCY=4 typescript
#   （--build-env KEY=VALUE 可重复；是上面几个命名开关的通用形式，只注入构建期）
#   bash build_arm.sh -j 4 all              # 4 条并发；默认 1 = 逐条串行
#   bash build_arm.sh --stall-timeout 900 rust      # 放宽「日志多久不动就放弃」
#   bash build_arm.sh --stall-timeout 0 --build-timeout 3600 all  # 关停滞检测，只留总耗时上限
#   （每 60s 打一行心跳，串行/并发都打，含每条「距上次日志输出多久」——
#     这个数字逼近 --stall-timeout 才要警觉，为 0 说明在正常输出；--heartbeat <秒> 可调）

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/build"
BASE=""; LIST=0; TARGETS=()
# 并发与放弃策略。默认值刻意保守：-j 1 就是改动前的逐条串行，行为不变。
JOBS=1
# 停滞判据是「日志停止增长」而不是「总耗时」—— 这个区分是关键：cargo nextest
# --no-run 跑 20 分钟是正常的，但它一路在输出；卡死的 go mod download / pnpm
# install 是一个字都不出。0 = 关掉（一条挂死会让整批停摆，不建议）。
STALL_TIMEOUT=300
# 单条总耗时上限，停滞检测之外的兜底。默认 0（关闭）—— 有些 rust task 光
# cargo fetch 就要十几分钟，给死上限比停滞检测更容易误杀。
BUILD_TIMEOUT=0
# 多久打一行进度。**按墙钟固定间隔打，不是「静默到这个秒数才打」** —— 跑得好的
# 构建也该有进度可看，而且那一行里的「距上次日志输出 0s」本身就是健康信号。
# -j 1 与并发模式都打（调小便于自测）。
HEARTBEAT=60
# 代理默认继承环境变量；--proxy 可显式覆盖
PROXY="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}"
NOPROXY="${NO_PROXY:-${no_proxy:-localhost,127.0.0.1,::1}}"
BUILD_NET=""
# 公司内网做 TLS 中间人时用：装内网 CA（推荐）或干脆关掉校验（有残留代价，见下）
CA_CERT="${DEEPSWE_CA_CERT:-}"
TRIALS_DIR=""
INSECURE=0
# 内网取包极慢时用：构建期把 npm 系的源换到镜像站（只在构建期生效，见「包源」一节）
REGISTRY="${DEEPSWE_NPM_REGISTRY:-}"
# go 取模块失败有两种完全不同的现象，别混：
#   - `read: connection reset by peer` —— `read:` 说明 TCP 已经建起来才被掐，
#     多半是中间设备（DPI / 老式代理）对 HTTP/2 流处理不好；wget 默认 HTTP/1.1
#     所以「wget 通而 go 不通」就是这个味道。GODEBUG=http2client=0 把 go 降回 1.1。
#   - 源本身不通（连不上 / 超时）才是换源的场景（内网 Athens / Artifactory / 镜像站）。
# 三个都只在构建期生效，走 ARG 不走 ENV，理由同「包源」一节。
GOPROXY="${DEEPSWE_GOPROXY:-}"
# 与 GOPROXY 独立：换源多数不必关校验和，go 会走 <GOPROXY>/sumdb/… 把它一并代理掉
GOSUMDB="${DEEPSWE_GOSUMDB:-}"
GODEBUG="${DEEPSWE_GODEBUG:-}"
# 与上面三个同构的第四个 go 开关。它是**间接**杠杆：cmd/go 用 runtime.GOMAXPROCS(0)
# 给模块下载队列定容量，所以调小它就等于压低取模块的并发（内网中间设备拦高并发时
# 有用）。代价见「Go 取模块」一节的提示语——它同时把 go install 的编译压成单线程。
GOMAXPROCS="${DEEPSWE_GOMAXPROCS:-}"
# 通用逃生口。上面五个命名开关（registry/goproxy/gosumdb/godebug/gomaxprocs）做的
# 是同一件事：在生成的 Dockerfile 里插一行 ARG KEY，再 --build-arg KEY=值。再冒出
# 新变量（如 pnpm 的 NPM_CONFIG_NETWORK_CONCURRENCY）就不该再改脚本了。
# 命名开关一个都不删：它们自带针对性提示语与残留自检，那部分价值 --build-env 没有。
BUILD_ENV=()

# ---- 取值参数的两道关 ----------------------------------------------------
# 两个坑都是真踩过的：
#   `… -j`（取值参数落在末尾）—— set -u 下 "$2" 直接 unbound variable，用户看到的
#     是 bash 的行号，完全看不出是谁少给了值；
#   `-j true-myth`（值漏写、后面跟的其实是构建目标）—— 旧写法把目标当值吃掉，再
#     抱怨「不是数字」。抱怨的是格式，真问题是**构建目标没了**，报错必须点破。
# 「这个值其实是个构建目标」的判断不靠猜：语言名是固定的几个，trial 则直接拿 $HERE
# 下的目录核对（目标本来就按前缀匹配，所以这里也用前缀）。
looks_like_target() {
  case "$1" in python|go|javascript|typescript|rust|all) return 0 ;; esac
  compgen -G "$HERE/$1*/meta.json" >/dev/null 2>&1
}
need_val() {                        # 用法：need_val <参数名> "$@"
  local opt="$1"; shift
  if [ $# -lt 2 ]; then
    echo "❌ $opt 需要一个值，但它已经是命令行的最后一个参数了"
    echo "   写法： $opt <值> [构建目标]"
    exit 1
  fi
  case "$2" in
    -[0-9]*) echo "❌ $opt 的值不能是负数：「$2」"; exit 1 ;;
    -*)      echo "❌ $opt 需要一个值，但后面跟的是另一个参数「$2」"
             echo "   是不是 $opt 的值漏写了？"; exit 1 ;;
  esac
  # 值本身就是个能解析的构建目标 —— 几乎必然是「值漏写了，目标被参数吃掉」。
  # 不点破的话，用户丢了目标还会以为只是格式问题；更坏的是没报错的那些参数，
  # 目标被吃掉后 TARGETS 为空，脚本会当成 all 把 113 条全建一遍。
  # -o 例外：输出目录叫什么都合法，不该拿目标名去卡它。
  if [ "$opt" != "-o" ] && looks_like_target "$2"; then
    echo "❌ $opt 需要一个值，但后面跟的「$2」是一个构建目标"
    echo "   它被 $opt 当成值吃掉了 —— 目标一空，这一轮就会变成「全建」。"
    echo "   是不是 $opt 的值漏写了？ 例如： $opt <值> $2"
    exit 1
  fi
}
# 数值参数专用。min = 允许的最小值；note 用来解释「为什么不能更小」
need_num() {                        # 用法：need_num <参数名> <值> <最小值> [说明]
  local opt="$1" val="$2" min="$3" note="${4:-}"
  case "$val" in
    ''|*[!0-9]*)
      echo "❌ $opt 需要一个整数，收到「$val」"
      # 这类错九成是「数字漏写了，后面的构建目标被当成值吃掉」。只说「不是数字」会
      # 把人往格式问题上引，而真正丢掉的是构建目标，所以认出来就直说。
      if looks_like_target "$val"; then
        echo "   「$val」是一个构建目标，被 $opt 当成值吃掉了"
        echo "   是不是漏写了数字？应该是： $opt <数字> $val"
      fi
      exit 1 ;;
  esac
  if [ "$val" -lt "$min" ]; then
    echo "❌ $opt 需要 ≥$min 的整数，收到 $val"
    [ -n "$note" ] && echo "   $note"
    exit 1
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --list) LIST=1; shift ;;
    --base) need_val --base "$@"; BASE="$2"; shift 2 ;;
    --proxy) need_val --proxy "$@"; PROXY="$2"; shift 2 ;;
    --no-proxy) need_val --no-proxy "$@"; NOPROXY="$2"; shift 2 ;;
    --build-network) need_val --build-network "$@"; BUILD_NET="$2"; shift 2 ;;
    --ca-cert) need_val --ca-cert "$@"; CA_CERT="$2"; shift 2 ;;
    # trial 根目录。bundle 是平铺的（trial 就在本脚本同级），而仓库布局里
    # 全量 113 条在 full_trials/ 子目录，crosslang/ 根下只有已验证的那几条。
    # 相对路径按本脚本所在目录解释，于是 `--trials-dir full_trials` 即可。
    --trials-dir) need_val --trials-dir "$@"; TRIALS_DIR="$2"; shift 2 ;;
    --insecure) INSECURE=1; shift ;;
    --registry) need_val --registry "$@"; REGISTRY="$2"; shift 2 ;;
    --goproxy) need_val --goproxy "$@"; GOPROXY="$2"; shift 2 ;;
    --gosumdb) need_val --gosumdb "$@"; GOSUMDB="$2"; shift 2 ;;
    --godebug) need_val --godebug "$@"; GODEBUG="$2"; shift 2 ;;
    --gomaxprocs) need_val --gomaxprocs "$@"; GOMAXPROCS="$2"; shift 2 ;;
    --build-env) need_val --build-env "$@"; BUILD_ENV+=("$2"); shift 2 ;;
    -j|--jobs) need_val "$1" "$@"; JOBS="$2"; shift 2 ;;
    -j[0-9]*) JOBS="${1#-j}"; shift ;;
    --stall-timeout) need_val --stall-timeout "$@"; STALL_TIMEOUT="$2"; shift 2 ;;
    --build-timeout) need_val --build-timeout "$@"; BUILD_TIMEOUT="$2"; shift 2 ;;
    --heartbeat) need_val --heartbeat "$@"; HEARTBEAT="$2"; shift 2 ;;
    -o) need_val -o "$@"; OUT="$2"; shift 2 ;;
    -*) echo "未知参数: $1"; exit 1 ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done

# 下限逐个不同，因为 0 的语义逐个不同：
#   -j 0 没有意义（工作池要么空转、要么永不启动），所以下限是 1、不是 0；
#   两个 timeout 的 0 就是「关掉这项检查」，是合法取值（回显里会写明已关闭）；
#   心跳没有「关掉」的语义 —— 构建跑起来之后它是唯一的进度来源（串行并发都一样），
#   要少打就把间隔调大。
need_num --jobs "$JOBS" 1 "-j 0 等于不启动任何构建；串行请用 -j 1（默认值）"
need_num --stall-timeout "$STALL_TIMEOUT" 0
need_num --build-timeout "$BUILD_TIMEOUT" 0
need_num --heartbeat "$HEARTBEAT" 1 "心跳不提供关闭：构建跑起来后它是唯一的进度反馈；想少打就调大，如 --heartbeat 300"
# GOMAXPROCS=0 在 go 里是「按 CPU 数」的意思，即等于没设；用户写 0 多半是笔误
[ -n "$GOMAXPROCS" ] && need_num --gomaxprocs "$GOMAXPROCS" 1 "0 等于不限制，那就干脆别给这个参数"

# ---- --build-env 的校验 --------------------------------------------------
# KEY 会被**原样写进生成的 Dockerfile 的 ARG 行**，所以必须严格限成标识符 ——
# 放宽一点就是 Dockerfile 注入（`--build-env "X=1 && RUN evil"` 之类）。
# VALUE 不进 Dockerfile，只作为 --build-arg 的一个 argv 元素交给 docker：全程在
# 带引号的数组里传递、不经过 eval，所以不会被 shell 二次解析，无需再转义。
conflict_env() {
  echo "❌ --build-env $1=… 与 $2 冲突：两条路都会注入 $1，docker 取最后一个，"
  echo "   等于一个静默覆盖另一个。二选一 —— 优先用命名开关（它自带提示语和残留自检）"
  exit 1
}
BUILD_ENV_KEYS=()
for kv in ${BUILD_ENV[@]+"${BUILD_ENV[@]}"}; do
  case "$kv" in
    *=*) : ;;
    *) echo "❌ --build-env 要求 KEY=VALUE 形式，收到「$kv」"; exit 1 ;;
  esac
  k="${kv%%=*}"
  case "$k" in
    ''|[0-9]*|*[!A-Za-z0-9_]*)
      echo "❌ --build-env 的 KEY 只接受 [A-Za-z_][A-Za-z0-9_]*，收到「$k」"
      echo "   （KEY 会原样写进生成的 Dockerfile 的 ARG 行，放宽就是 Dockerfile 注入）"
      exit 1 ;;
  esac
  case "$k" in
    GOPROXY)    [ -n "$GOPROXY" ]    && conflict_env "$k" --goproxy ;;
    GOSUMDB)    [ -n "$GOSUMDB" ]    && conflict_env "$k" --gosumdb ;;
    GODEBUG)    [ -n "$GODEBUG" ]    && conflict_env "$k" --godebug ;;
    GOMAXPROCS) [ -n "$GOMAXPROCS" ] && conflict_env "$k" --gomaxprocs ;;
    NPM_CONFIG_REGISTRY|COREPACK_NPM_REGISTRY)
                [ -n "$REGISTRY" ]   && conflict_env "$k" --registry ;;
    HTTP_PROXY|HTTPS_PROXY|http_proxy|https_proxy|NO_PROXY|no_proxy)
                [ -n "$PROXY" ]      && conflict_env "$k" --proxy ;;
  esac
  for prev in ${BUILD_ENV_KEYS[@]+"${BUILD_ENV_KEYS[@]}"}; do
    [ "$prev" = "$k" ] && { echo "❌ --build-env 里 KEY 重复给了两次：$k"; exit 1; }
  done
  BUILD_ENV_KEYS+=("$k")
done
# 残留自检要用的正则；KEY 已限成标识符，拼进正则是安全的
BUILD_ENV_RE=""
[ ${#BUILD_ENV_KEYS[@]} -gt 0 ] && BUILD_ENV_RE=$(IFS='|'; printf '%s' "${BUILD_ENV_KEYS[*]}")
# --list 只打印改写，并发反而会把输出藏进各自的 .out 文件里，强制回到串行
[ "$LIST" = 1 ] && JOBS=1
# wait -n（工作池的核心）是 bash 4.3 才有的
if [ "$JOBS" -gt 1 ] && { [ "${BASH_VERSINFO[0]}" -lt 4 ] || \
     { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -lt 3 ]; }; }; then
  echo "❌ -j >1 需要 bash 4.3+（wait -n），当前 $BASH_VERSION"; exit 1
fi

# 监控的轮询间隔。定成 1s 不是为了让停滞判得更准（阈值是分钟级），而是因为
# **轮询间隔同时是「构建已经结束」的发现延迟**：调成 5s 的话，每条构建结束后都
# 要多空等最多 5s，113 条串起来就是白扔十分钟。代价只是每条每秒一次 stat。
POLL=1

# 打印时把 user:pass@ 抹掉——日志会被贴来贴去
redact() { printf '%s' "$1" | sed -E 's#(//)[^/@]*@#\1***@#'; }

# 自动挑本地基座
if [ -z "$BASE" ]; then
  for t in mars-base:arm64 mars-base:latest public.ecr.aws/x8v8d7g8/mars-base:latest; do
    docker image inspect "$t" >/dev/null 2>&1 && { BASE="$t"; break; }
  done
fi
if [ -z "$BASE" ] && [ "$LIST" = 0 ]; then
  echo "❌ 本地找不到 mars-base（试过 mars-base:arm64 / mars-base:latest / ECR 全名）"
  echo "   先 docker load 基座，或用 --base 指定 tag。"
  exit 1
fi
BASE_ARCH=""
[ -n "$BASE" ] && BASE_ARCH=$(docker image inspect "$BASE" -f '{{.Architecture}}' 2>/dev/null)

# 依赖从少到多——先建最可能一次成功的，把最贵最脆的 rust 放最后
ORDER="python go javascript typescript rust"

# 语言 → trial 目录。**一种语言可能对应几十条**（全量 113 条里 go/python/typescript
# 各三十多条），所以这里存的是空格分隔的列表；早先版本用的是「一语言一目录」，
# 在全量集上会静默只保留最后一条。
declare -A DIRS_OF LANG_OF
# trial 根目录解析：相对路径按本脚本所在目录算，绝对路径直接用。
if [ -n "$TRIALS_DIR" ]; then
  case "$TRIALS_DIR" in /*) TRIALS_ROOT="$TRIALS_DIR" ;; *) TRIALS_ROOT="$HERE/$TRIALS_DIR" ;; esac
  [ -d "$TRIALS_ROOT" ] || { echo "❌ --trials-dir 不是目录: $TRIALS_ROOT"; exit 1; }
else
  TRIALS_ROOT="$HERE"
fi

while IFS=$'\t' read -r lang dir; do
  DIRS_OF[$lang]="${DIRS_OF[$lang]:-} $dir"; LANG_OF[$dir]="$lang"
done < <(python3 - "$TRIALS_ROOT" <<'PY'
import json, pathlib, sys
for d in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    m = d / "meta.json"
    if d.is_dir() and m.exists():
        print(f"{json.loads(m.read_text()).get('language','?')}\t{d.name}")
PY
)

# 目标解析：语言名（展开成该语言全部 trial）、trial 目录名前缀、或 all
SELECTED=()
if [ ${#TARGETS[@]} -eq 0 ] || [ "${TARGETS[0]:-}" = "all" ]; then
  for l in $ORDER; do for d in ${DIRS_OF[$l]:-}; do SELECTED+=("$d"); done; done
else
  for t in "${TARGETS[@]}"; do
    if [ -n "${DIRS_OF[$t]:-}" ]; then
      for d in ${DIRS_OF[$t]}; do SELECTED+=("$d"); done; continue
    fi
    hit=0
    for d in $(printf '%s\n' "${!LANG_OF[@]}" | sort); do
      case "$d" in "$t"*) SELECTED+=("$d"); hit=1 ;; esac
    done
    [ "$hit" = 1 ] || { echo "❌ 认不出目标: $t（用语言名 python/go/rust/typescript/javascript，或 trial 目录名前缀）"; exit 1; }
  done
fi
[ ${#SELECTED[@]} -gt 0 ] || { echo "❌ 没有匹配到任何 trial"; exit 1; }

echo "=============================================================="
echo " 从本地基座重建 task 镜像"
echo "=============================================================="
echo "  基座        ${BASE:-（未找到）}  ${BASE_ARCH:+($BASE_ARCH)}"
echo "  本机架构    $(uname -m)"
if [ ${#SELECTED[@]} -le 6 ]; then
  echo "  待建        ${SELECTED[*]}"
else
  echo "  待建        ${#SELECTED[@]} 条：$(for d in "${SELECTED[@]}"; do echo "${LANG_OF[$d]}"; done \
                        | sort | uniq -c | awk '{printf "%s×%s ", $2, $1}')"
fi
echo "  输出        $OUT"
[ "$JOBS" -gt 1 ] && \
  echo "  并发        -j $JOBS（并发下逐条实时输出会串成乱码，改为每条只落自己的"
[ "$JOBS" -gt 1 ] && \
  echo "              build.log，开始/结束各一行，另有 ${HEARTBEAT}s 一次心跳）"
[ "$JOBS" -gt 1 ] && \
  echo "              逐条的改写说明与镜像自检输出落在 <trial>/.out"
if [ "$STALL_TIMEOUT" -gt 0 ]; then
  echo "  停滞放弃    ${STALL_TIMEOUT}s 内 build.log 不增长就终止这一条、继续下一条"
  echo "              （盯的是日志有没有动，不是总耗时；--stall-timeout 0 关闭）"
  echo "              ⚠️ BuildKit 对静默的 RUN 一个字都不打，天生沉默超过阈值的步骤会被"
  echo "                 误杀。rust 那几条有 cargo nextest --no-run（全量集里唯一的真"
  echo "                 编译步骤，可能长时间无输出），建议 --stall-timeout 900"
else
  echo "  停滞放弃    已关闭 —— 一条卡死会让整批停摆"
fi
[ "$BUILD_TIMEOUT" -gt 0 ] && echo "  单条上限    ${BUILD_TIMEOUT}s（--build-timeout，停滞检测之外的兜底）"

# ---- 代理 ----------------------------------------------------------------
# 构建期要联网（git clone / pip / npm / go / cargo），运行期不要（--network=none）。
# 用 docker 的**预定义 build-arg** 传：它们无需在 Dockerfile 里声明 ARG 就能注入
# 构建环境，而且**不会写进 image config 的 Env**（实测确认过），所以运行期镜像
# 依旧干净——原始 mars-base 的 Env 里本来也是零个 *_proxy。
BUILD_ARGS=()
if [ -n "$PROXY" ]; then
  echo "  代理        $(redact "$PROXY")"
  echo "  NO_PROXY    $NOPROXY"
  for v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
    BUILD_ARGS+=(--build-arg "$v=$PROXY")
  done
  for v in NO_PROXY no_proxy; do
    BUILD_ARGS+=(--build-arg "$v=$NOPROXY")
  done
  # 代理挂在宿主 loopback 上时，构建容器内的 127.0.0.1 是它自己，连不到宿主。
  # --network=host 让构建容器共用宿主网络栈，这是最省事的解法。
  case "$PROXY" in
    *//127.0.0.1*|*//localhost*|*//[::1]*|*//0.0.0.0*)
      if [ -z "$BUILD_NET" ]; then
        BUILD_NET="host"
        echo "  ⚠️  代理指向 loopback —— 构建容器内的 127.0.0.1 不是宿主的，"
        echo "      已自动加 --network=host（用 --build-network 可覆盖）"
      fi ;;
  esac
else
  echo "  代理        未设置"
  echo "              若目标机需要代理才能访问 github/pypi/npm，用 --proxy http://host:port"
  echo "              或先 export HTTPS_PROXY=... 再跑本脚本"
fi
[ -n "$BUILD_NET" ] && echo "  构建网络    --network=$BUILD_NET"

# ---- 证书 ----------------------------------------------------------------
if [ -n "$CA_CERT" ]; then
  if [ ! -f "$CA_CERT" ]; then
    echo "  ❌ --ca-cert 指向的文件不存在: $CA_CERT"; exit 1
  fi
  if ! grep -q 'BEGIN CERTIFICATE' "$CA_CERT" 2>/dev/null; then
    echo "  ❌ $CA_CERT 里没有 'BEGIN CERTIFICATE' —— 需要 PEM 格式；"
    echo "     DER 格式可转： openssl x509 -inform der -in x.cer -out x.crt"
    exit 1
  fi
  echo "  内网 CA     $CA_CERT（$(grep -c 'BEGIN CERTIFICATE' "$CA_CERT") 张证书）"
fi
if [ "$INSECURE" = 1 ]; then
  echo "  ⚠️  --insecure 关闭证书校验（构建末尾会还原，不留进运行期）"
  echo "      cargo 没有 insecure 开关，只认 CA 文件 —— rust 那条仍需 --ca-cert"
fi
if [ -z "$CA_CERT" ] && [ "$INSECURE" = 0 ]; then
  echo "  证书        默认（如报 server certificate verification failed，"
  echo "              说明内网做了 TLS 中间人：用 --ca-cert <内网CA.crt>，或 --insecure）"
fi

# ---- 包源 ----------------------------------------------------------------
# 内网从 registry.npmjs.org 取包极慢时换镜像站。两点实测约束决定了这里的写法：
#   - npm / pnpm 读 NPM_CONFIG_REGISTRY，corepack 只读 COREPACK_NPM_REGISTRY
#     （它**不读** .npmrc），所以两个变量都得给；
#   - 这两个都不在 docker 的预定义 build-arg 白名单里（白名单只有 *_proxy），
#     所以还必须在生成的 Dockerfile 里显式写 ARG，否则 --build-arg 传进去是空值。
# 用 ARG 而不是 ENV / .npmrc：ARG 不进 image config 的 Env（与代理同理），运行期
# 的 npm 仍指向官方源——运行期是 --network=none + 403 sinkhole，源被改写会让
# agent 命令的联网报错文本变样。
if [ -n "$REGISTRY" ]; then
  echo "  包源        $(redact "$REGISTRY")（npm/pnpm/corepack，仅构建期）"
  BUILD_ARGS+=(--build-arg "NPM_CONFIG_REGISTRY=$REGISTRY")
  BUILD_ARGS+=(--build-arg "COREPACK_NPM_REGISTRY=$REGISTRY")
else
  echo "  包源        未设置（默认 registry.npmjs.org）"
  echo "              若目标机取 npm 包极慢，用 --registry https://registry.npmmirror.com"
  echo "              或先 export DEEPSWE_NPM_REGISTRY=... 再跑本脚本"
fi

# ---- Go 取模块 ------------------------------------------------------------
# 34 条 go task 的 Dockerfile 一条都没写 GOPROXY/GODEBUG，全吃基座默认值，所以只能
# 从外面注入。约束和「包源」那节一模一样：这三个变量都不在 docker 预定义 build-arg
# 白名单里（白名单只有 *_proxy），必须在生成的 Dockerfile 里显式写 ARG，否则
# --build-arg 传进去是空值；用 ARG 而不是 ENV，是为了不进 image config 的 Env。
GO_OPTS=0
if [ -n "$GOPROXY" ]; then
  echo "  Go 模块源   $(redact "$GOPROXY")（仅构建期）"
  BUILD_ARGS+=(--build-arg "GOPROXY=$GOPROXY"); GO_OPTS=1
fi
if [ -n "$GOSUMDB" ]; then
  echo "  Go 校验和库 $GOSUMDB（仅构建期）"
  BUILD_ARGS+=(--build-arg "GOSUMDB=$GOSUMDB"); GO_OPTS=1
fi
if [ -n "$GODEBUG" ]; then
  echo "  Go GODEBUG  $GODEBUG（仅构建期）"
  BUILD_ARGS+=(--build-arg "GODEBUG=$GODEBUG"); GO_OPTS=1
fi
if [ -n "$GOMAXPROCS" ]; then
  echo "  Go 并发度   GOMAXPROCS=$GOMAXPROCS（仅构建期，同时压低下载并发与编译并行）"
  BUILD_ARGS+=(--build-arg "GOMAXPROCS=$GOMAXPROCS"); GO_OPTS=1
fi
if [ -n "$GOPROXY" ] && [ -z "$GOSUMDB" ]; then
  echo "              （未动 GOSUMDB：go 一般会走 <GOPROXY>/sumdb/… 把校验和一起代理掉，"
  echo "                该端点也不通时才需要 --gosumdb off）"
fi
if [ "$GO_OPTS" = 0 ]; then
  echo "  Go 取模块   未设置（默认 proxy.golang.org + HTTP/2）"
  echo "              若报 connection reset by peer 且错误里有 read: —— 连接是建起来之后"
  echo "              被掐的，多半是中间设备掐 HTTP/2，先试 --godebug http2client=0"
  echo "              若是源本身不通（连不上 / 超时），才换源：--goproxy https://goproxy.cn,direct"
  echo "              也可先 export DEEPSWE_GODEBUG=... / DEEPSWE_GOPROXY=... 再跑本脚本"
  echo "              还有一条**间接**杠杆：--gomaxprocs N —— cmd/go 用 runtime.GOMAXPROCS(0)"
  echo "              给下载队列定容量，调小它就等于压低取模块并发（中间设备拦高并发时有用）。"
  echo "              实测确实管用，但代价很大：同一仓库同一依赖树，不设是 35.1s / 22.6s（复测），"
  echo "              GOMAXPROCS=1 变成 95.7s / 129.1s，慢 3~6 倍且两组完全不重叠。"
  echo "              而且 34 个 go Dockerfile 全都有 go install —— 它把编译也压成单线程，双重损失。"
  echo "              所以**从 --gomaxprocs 4 开始试**，能过就往上加、不过再往下降；1 是最后手段"
fi
# ---- 额外构建期变量 ------------------------------------------------------
# 与命名开关同一套机制（ARG + --build-arg，不进 image config 的 Env），只是不预设
# 名字。典型用途：pnpm 的 NPM_CONFIG_NETWORK_CONCURRENCY（默认 16）—— 它只管网络
# 并发、不碰任何计算，比 GOMAXPROCS 精准得多。
if [ ${#BUILD_ENV[@]} -gt 0 ]; then
  echo "  额外变量    ${#BUILD_ENV[@]} 个（--build-env，仅构建期）"
  for kv in "${BUILD_ENV[@]}"; do
    echo "              $(redact "$kv")"       # 值可能含凭据，与代理/包源同样处理
    BUILD_ARGS+=(--build-arg "$kv")
  done
fi
echo

mkdir -p "$OUT"
CTX="$OUT/.emptyctx"; mkdir -p "$CTX"      # 原 Dockerfile 零 COPY/ADD，空上下文即可
# 唯一会进上下文的东西：内网 CA 证书（要 COPY 进镜像的信任库）。
# 这两行**必须留在循环之前**、全程只做一次：上下文目录是所有条共用的，-j >1 时
# 各条的 docker build 会同时读它。放进循环里每条 rm+cp 一次，并发下就会出现
# 「A 刚 rm 掉、B 正在读」的窗口，表现为随机的 COPY xxx.crt not found。
# ⚠️ 已知坑：这只在**单个** build_arm.sh 进程内安全。同时跑多个 build_arm.sh
#    且 -o 指向同一个 build/ 目录，它们会互相 rm 对方的证书；要并行跑多个进程，
#    请给每个进程一个独立的 -o。
rm -f "$CTX"/*.crt
[ -n "$CA_CERT" ] && cp "$CA_CERT" "$CTX/$(basename "$CA_CERT")"

# 构建网络参数与具体哪一条无关，循环外算一次即可
NET_ARG=(); [ -n "$BUILD_NET" ] && NET_ARG=(--network "$BUILD_NET")

# ---- 带停滞监控的 docker build -------------------------------------------
# 返回 0=成功，124=被我们杀掉（停滞或超上限，理由在 WATCH_REASON），其余=docker 的退出码。
#
# 为什么先 SIGTERM 再 SIGKILL：BuildKit 的构建会话是**客户端持有**的，客户端收到
# TERM 会通知服务端取消这次构建并回收中间产物；上来就 SIGKILL 客户端，daemon 侧
# 那条构建往往还在继续跑，CPU 和网络照占，等于没放弃。
WATCH_REASON=""; WATCH_IDLE=0
# -j 1 的心跳。并发模式由主进程的 heartbeat() 负责，串行没人管 —— 而串行恰恰最需要：
# 并发好歹每条完成时会打一行，串行卡住就是彻底静默到 --stall-timeout 触发为止。
# 内容与并发版一致：已跑多久 / 距上次日志输出多久 / 阈值多少。
# 触发条件是**每 --heartbeat 秒一次**，与静不静默无关：一路在输出的构建同样每分钟
# 打一行，只是那行显示「距上次日志输出 0s」—— 这个数字接近 --stall-timeout 才是要
# 警觉的信号，为 0 反而说明一切正常。
seq_beat() {
  local name="$1" el="$2" idle="$3"
  if [ "$STALL_TIMEOUT" -gt 0 ]; then
    printf '  ⏱  %s 已跑 %ds，距上次日志输出 %ds／%ds\n' "$name" "$el" "$idle" "$STALL_TIMEOUT"
  else
    printf '  ⏱  %s 已跑 %ds，距上次日志输出 %ds\n' "$name" "$el" "$idle"
  fi
}
kill_gently() {
  local p="$1" i=0
  kill -TERM "$p" 2>/dev/null
  while kill -0 "$p" 2>/dev/null && [ "$i" -lt 5 ]; do sleep 1; i=$((i+1)); done
  kill -KILL "$p" 2>/dev/null
}
docker_build_watched() {
  local work="$1" tag="$2"
  WATCH_REASON=""; WATCH_IDLE=0
  : >"$work/build.log"
  docker build "${NET_ARG[@]+"${NET_ARG[@]}"}" "${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}" \
       -f "$work/Dockerfile" -t "$tag" "$CTX" >"$work/build.log" 2>&1 &
  local bpid=$!
  # 落盘给主进程的中断处理用：worker 是子进程，主进程拿不到它手里的 pid
  echo "$bpid" >"$work/.buildpid"
  local t0 now sig last_sig="" last_change last_beat rc
  t0=$(date +%s); last_change=$t0; last_beat=$t0
  while kill -0 "$bpid" 2>/dev/null; do
    sleep "$POLL"
    now=$(date +%s)
    # 大小和 mtime 一起看：只看大小会漏掉原地重写等长进度行的工具，
    # 只看 mtime 会被某些文件系统的时间戳粒度糊弄
    sig=$(stat -c '%s:%Y' "$work/build.log" 2>/dev/null)
    if [ "$sig" != "$last_sig" ]; then last_sig="$sig"; last_change="$now"; fi
    if [ "$STALL_TIMEOUT" -gt 0 ] && [ $((now - last_change)) -ge "$STALL_TIMEOUT" ]; then
      WATCH_IDLE=$((now - last_change)); WATCH_REASON="stall"; break
    fi
    if [ "$BUILD_TIMEOUT" -gt 0 ] && [ $((now - t0)) -ge "$BUILD_TIMEOUT" ]; then
      WATCH_IDLE=$((now - last_change)); WATCH_REASON="timeout"; break
    fi
    if [ "$JOBS" -le 1 ] && [ $((now - last_beat)) -ge "$HEARTBEAT" ]; then
      last_beat=$now
      seq_beat "$(basename "$work")" $((now - t0)) $((now - last_change))
    fi
  done
  if [ -n "$WATCH_REASON" ]; then
    kill_gently "$bpid"; wait "$bpid" 2>/dev/null; rc=124
  else
    wait "$bpid"; rc=$?
  fi
  rm -f "$work/.buildpid"
  return "$rc"
}

# ---- 单条：生成 Dockerfile → 构建 → 自检 ---------------------------------
# 抽成函数是为了 -j >1 时能把一整条丢进后台子进程。子进程改不了主进程的变量
# （N_OK/N_FAIL/FAILED 放进 & 里就随进程一起没了），所以结果一律写
# $work/.status，跑完由主进程统一聚合。
# 格式：<ok|skip|fail|stall|timeout> <耗时秒> <MB>；stall 与 timeout 都是「被我们
# 终止」，但分开记 —— 后续处置一个调 --stall-timeout、一个调 --build-timeout。
build_one() {
  local dir="$1" I="$2"
  local lang="${LANG_OF[$dir]}"
  local work="$OUT/$dir"; mkdir -p "$work"
  rm -f "$work/.status" "$work/.buildpid"
  [ ${#SELECTED[@]} -gt 6 ] && printf '[%d/%d] ' "$I" "${#SELECTED[@]}"

  # 从 task.json 里取出 Dockerfile 与目标 tag，并做架构 / 证书改写
  python3 - "$TRIALS_ROOT/$dir/task.json" "$work" "$BASE" "${BASE_ARCH:-}" \
           "$([ -n "$CA_CERT" ] && basename "$CA_CERT" || echo '')" "$INSECURE" \
           "$REGISTRY" "$GOPROXY" "$GOSUMDB" "$GODEBUG" "$GOMAXPROCS" \
           "${BUILD_ENV_KEYS[*]+${BUILD_ENV_KEYS[*]}}" <<'PY'
import json, pathlib, re, sys
(task_json, work, base, base_arch, ca_name, insecure, registry,
 goproxy, gosumdb, godebug, gomaxprocs, build_env_keys) = sys.argv[1:13]
insecure = insecure == "1"
work = pathlib.Path(work)
files = {f["path"]: f["content"] for f in json.loads(pathlib.Path(task_json).read_text())["files"]}
df = files["environment/Dockerfile"]
toml = files["task.toml"]
tag = re.search(r'^docker_image\s*=\s*"([^"]+)"', toml, re.M).group(1)

rewrites = []
# 1) FROM 换成本地基座：ECR 拉不到，而基座已经在本地
new, n = re.subn(r'^FROM\s+\S*mars-base:\S+', f'FROM {base}', df, flags=re.M)
if n:
    rewrites.append((f"FROM …mars-base:latest", f"FROM {base}",
                     "ECR 拉不到；基座已在本地"))
    df = new
# 2) cargo-nextest 的下载 URL 按架构分叉，原文写死的是 x86_64 那个
if base_arch in ("arm64", "aarch64") and "get.nexte.st" in df:
    new, n = re.subn(r'(get\.nexte\.st/[^/"\s]+/)linux(?![-\w])', r'\1linux-arm', df)
    if n:
        rewrites.append(("get.nexte.st/<ver>/linux", "get.nexte.st/<ver>/linux-arm",
                         "原文写死 x86_64；ARM 上要换成 aarch64 那个产物"))
        df = new

# 3) deno 的 release 资产同样按架构分叉，且**没有 fallback**：装错架构的二进制不会
#    在下载时报错，而是拖到紧接着的 `RUN deno cache` 才 exec format error，很难认。
#    （cliffy 那条写死 deno-x86_64-unknown-linux-gnu.zip；实测 v2.0.0 的
#     deno-aarch64-unknown-linux-gnu.zip 存在。）
if base_arch in ("arm64", "aarch64") and "denoland/deno/releases" in df:
    new, n = re.subn(
        r'(github\.com/denoland/deno/releases/download/[^/"\s]+/deno-)x86_64(-unknown-linux-gnu)',
        r'\1aarch64\2', df)
    if n:
        rewrites.append(("deno-x86_64-unknown-linux-gnu.zip",
                         "deno-aarch64-unknown-linux-gnu.zip",
                         "原文写死 x86_64；ARM 上装进去会在 `deno cache` 时 exec format error"))
        df = new

# 4) 公司内网 TLS 中间人：装内网 CA，或（退而求其次）关掉校验
#    插入点：第一条 RUN 之前 —— git clone 是第一个联网动作，必须在它之前生效
def insert_before_first_run(text, block):
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if ln.lstrip().upper().startswith("RUN "):
            return "".join(lines[:i]) + block + "".join(lines[i:])
    return text + block

def insert_before_cmd(text, block):
    lines = text.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().upper().startswith("CMD"):
            return "".join(lines[:i]) + block + "".join(lines[i:])
    return text + block

prelude = ""
if registry:
    # 只声明、不给默认值：值由 --build-arg 注入，构建期对 RUN 可见，构建完即消失。
    # 必须排在 prelude 最前 —— 后面 CA / insecure 那两段自带 RUN，ARG 得在其之前。
    prelude += (
        "\n# [build_arm.sh] --registry：构建期改用镜像源。写 ARG 不写 ENV —— ARG 不进\n"
        "# image config 的 Env，运行期 npm 仍是官方源，联网报错文本不受影响\n"
        "ARG NPM_CONFIG_REGISTRY\n"
        "ARG COREPACK_NPM_REGISTRY\n\n")
    rewrites.append(("（无）", "ARG NPM_CONFIG_REGISTRY + ARG COREPACK_NPM_REGISTRY",
                     f"--registry={registry}；这两个变量不在预定义 build-arg 白名单里，"
                     "不声明就传不进去；corepack 只认后者、不读 .npmrc"))

# 三个 go 开关互相独立：只声明实际给了值的那个 —— 没给值的空 ARG 是纯噪音，
# 还会让 --list / REWRITES.md 看起来像做了并不存在的改写。同样必须排在 CA /
# insecure 之前，那两段自带 RUN。
go_args = [(n, v) for n, v in
           (("GOPROXY", goproxy), ("GOSUMDB", gosumdb), ("GODEBUG", godebug),
            ("GOMAXPROCS", gomaxprocs)) if v]
if go_args:
    prelude += (
        "\n# [build_arm.sh] --goproxy/--gosumdb/--godebug/--gomaxprocs：构建期改 go 的\n"
        "# 取模块方式与并发度。\n"
        "# 写 ARG 不写 ENV —— ARG 不进 image config 的 Env，运行期 go 仍是基座默认配置\n"
        + "".join(f"ARG {n}\n" for n, _ in go_args) + "\n")
    rewrites.append(("（无）", " + ".join(f"ARG {n}" for n, _ in go_args),
                     "；".join(f"--{n.lower()}={v}" for n, v in go_args)
                     + "；这些变量不在预定义 build-arg 白名单里，不声明就传不进去"))

# --build-env 的逃生口。KEY 在 bash 侧已按 [A-Za-z_][A-Za-z0-9_]* 严格校验过 ——
# 它是唯一会被原样写进 Dockerfile 文本的部分，不校验就是 Dockerfile 注入。
# 值不写进这里：同样只经 --build-arg 注入，构建期可见、构建完即消失。
# 位置与上面两组一样必须在 CA / insecure 之前，那两段自带 RUN。
be_keys = build_env_keys.split()
if be_keys:
    prelude += (
        "\n# [build_arm.sh] --build-env：构建期额外注入的变量。写 ARG 不写 ENV —— ARG 不进\n"
        "# image config 的 Env，运行期镜像不受影响\n"
        + "".join(f"ARG {k}\n" for k in be_keys) + "\n")
    rewrites.append(("（无）", " + ".join(f"ARG {k}" for k in be_keys),
                     "--build-env；不在 docker 预定义 build-arg 白名单里的变量，"
                     "不声明 ARG 就传不进去"))

if ca_name:
    # 装 CA 是**首选**：不像关校验那样改变工具行为，而且 cargo 只认这条路。
    #
    # 三步缺一不可 —— 实测过各工具的信任源，它们并不一致：
    #   git / curl / go / cargo → 读 /etc/ssl/certs/ca-certificates.crt，update-ca-certificates 就够
    #   node / npm / pnpm       → 只认内置的 146 张根证书，**不读系统 bundle**
    #   python / pip            → 用 certifi 自带的 cacert.pem，**也不读系统 bundle**
    # 所以只跑 update-ca-certificates 的话，git clone 会过，npm/pip 照样失败。
    #
    # 那几个 ENV 会留在镜像里，但值指向系统 bundle —— 是「信任库更全」，
    # 不是「不再校验」，与 --insecure 的残留性质完全不同。
    prelude += (
        "\n# [build_arm.sh] 公司内网 CA：TLS 被中间人重签，不装则所有 HTTPS 取包都验不过\n"
        f"COPY {ca_name} /usr/local/share/ca-certificates/{ca_name}\n"
        "RUN update-ca-certificates\n"
        "# node 与 python 各自带内置证书库、不读系统 bundle，必须显式指过去\n"
        "ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \\\n"
        "    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \\\n"
        "    PIP_CERT=/etc/ssl/certs/ca-certificates.crt \\\n"
        "    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \\\n"
        "    CARGO_HTTP_CAINFO=/etc/ssl/certs/ca-certificates.crt\n\n")
    rewrites.append(("（无）", f"COPY {ca_name} + update-ca-certificates + 4 个 CA 环境变量",
                     "内网 CA；node/pip 不读系统 bundle，必须显式指过去"))

if insecure:
    # 关校验只能作为退路，且**必须还原**：这些设置若留在镜像里会改变运行期行为。
    # 一律写文件而不用 ENV —— ENV 进 image config 就删不掉了。
    prelude += (
        "\n# [build_arm.sh] --insecure：临时关掉证书校验（构建末尾会还原，不留进运行期）\n"
        "RUN git config --system http.sslVerify false \\\n"
        " && printf 'insecure\\n' >> /root/.curlrc \\\n"
        " && printf '[global]\\ntrusted-host = pypi.org files.pythonhosted.org pypi.python.org\\n'"
        " > /etc/pip.conf \\\n"
        " && (npm config set strict-ssl false --global || true) \\\n"
        " && (go env -w GOFLAGS=-insecure GOSUMDB=off GOINSECURE='*' || true)\n\n")
    df = insert_before_cmd(df, (
        "\n# [build_arm.sh] 还原上面关掉的证书校验：留着会污染运行期行为\n"
        "RUN (git config --system --unset-all http.sslVerify || true) \\\n"
        " ; (sed -i '/^insecure$/d' /root/.curlrc || true) \\\n"
        " ; rm -f /etc/pip.conf \\\n"
        " ; (npm config delete strict-ssl --global || true) \\\n"
        " ; (go env -u GOFLAGS GOSUMDB GOINSECURE || true)\n\n"))
    rewrites.append(("（无）", "构建期关闭证书校验 + 末尾还原",
                     "--insecure；cargo 无此开关，rust 那条仍需 --ca-cert"))

if prelude:
    df = insert_before_first_run(df, prelude)

(work / "Dockerfile").write_text(df)
(work / "TAG").write_text(tag)
lines = ["# 相对原始 Dockerfile 的改写", "",
         f"- 目标 tag：`{tag}`", f"- 基座：`{base}` ({base_arch or '?'})", ""]
if rewrites:
    lines += ["| 原文 | 改成 | 为什么 |", "|---|---|---|"]
    lines += [f"| `{a}` | `{b}` | {c} |" for a, b, c in rewrites]
else:
    lines.append("（无改写）")
lines += ["", "## 无法通过改写消除的漂移", "",
          "重建镜像与原 amd64 镜像**不是同一个东西**，即使改写为零：",
          "",
          "- 依赖版本会漂移：`pnpm install` 未加 `--frozen-lockfile`、",
          "  `pip install` 未钉版本、`npm install -g` 只钉了直接依赖。",
          "  （例外：`cargo fetch --locked` 与 `npm ci` 是锁定的）",
          "- 用镜像源（`--registry`）时，那些没有 `--frozen-lockfile` 又没有匹配",
          "  lockfile 的 task（`pnpm install` 会做全量 resolution）可能因镜像同步",
          "  延迟解析到不同版本；有 lockfile 的按 sha512 integrity 校验，",
          "  tarball 内容不会漂",
          "- 用镜像源时 `node_modules/.modules.yaml` 里会记下镜像站 URL（实测：",
          "  `default: https://registry.npmmirror.com/` vs 官方源的 `.../registry.npmjs.org/`），",
          "  所以镜像内容与官方源建出来的**字节不同**。上面两条自检查不到这里。",
          "  实测后果是良性的：pnpm 解析新包读自己的配置而非该文件，运行期离线报错",
          "  文本与官方源镜像**逐字节一致**。即「行为不受污染」成立，「字节完全相同」不成立",
          "- 换 Go 模块代理（`--goproxy`）不会让模块内容漂：模块仍由仓库里提交的",
          "  `go.sum` 逐个 hash 校验，对不上直接构建失败。`GOSUMDB=off` 关掉的只是",
          "  「向公共透明日志（sum.golang.org）为新模块补查校验和」这一步对账，",
          "  **不是**关掉 `go.sum` 校验",
          "- 工具链是 arm64 构建，native 扩展与编译产物全部不同",
          "- 基座本身是 `:latest` tag，不可复现",
          "",
          "→ 所以 `patch_identical` 在重建镜像上**是待验证的开放问题**，",
          "  不能因为它在原 amd64 镜像上 5/5 通过就假定这里也成立。"]
(work / "REWRITES.md").write_text("\n".join(lines))
print(f"  tag   {tag}")
for a, b, c in rewrites:
    print(f"  改写  {a}  →  {b}")
if not rewrites:
    print("  改写  （无）")
PY
  local TAG; TAG=$(cat "$work/TAG")
  if [ "$LIST" = 1 ]; then echo; return 0; fi

  if docker image inspect "$TAG" >/dev/null 2>&1; then
    echo "  跳过  镜像已存在（要重建先 docker rmi $TAG）"; echo
    local sz0; sz0=$(docker image inspect "$TAG" -f '{{.Size}}' 2>/dev/null); : "${sz0:=0}"
    echo "skip 0 $((sz0/1024/1024))" >"$work/.status"; return 0
  fi

  echo "  构建中…（日志 $work/build.log）"
  local t0 rc dt sz
  t0=$(date +%s)
  docker_build_watched "$work" "$TAG"; rc=$?
  if [ "$rc" = 0 ]; then
    dt=$(( $(date +%s) - t0 ))
    sz=$(docker image inspect "$TAG" -f '{{.Size}}')
    echo "  ✅ 成功  ${dt}s，$((sz/1024/1024)) MB"
    # 保真度自检：代理绝不能留在镜像里。运行期是 --network=none + 403 sinkhole，
    # 镜像 Env 里多一个 *_proxy 就会改变 agent 命令的联网报错文本。
    if docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' \
         | grep -qiE '(^|[^A-Za-z_])(https?_proxy|no_proxy)='; then
      echo "  ❌ 镜像 Env 里残留了代理变量 —— 会污染运行期行为，必须排查后重建"
      docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -i proxy | sed 's/^/       /'
      echo "fail $dt 0" >"$work/.status"; echo; return 0
    fi
    # 同理，registry 也只能活在构建期：写成 ENV 就会进 Config.Env 带到运行期
    if [ -n "$REGISTRY" ]; then
      if docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' \
           | grep -qiE '(^|[^A-Za-z_])(npm_config_registry|corepack_npm_registry)='; then
        echo "  ❌ 镜像 Env 里残留了 registry 变量 —— 该用 ARG 而非 ENV，必须排查后重建"
        docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -i registry | sed 's/^/       /'
        echo "fail $dt 0" >"$work/.status"; echo; return 0
      fi
      # Env 干净还不够：.npmrc 之类的文件配置照样会改运行期取值，实测一遍最直接
      if REG_LEFT=$(docker run --rm "$TAG" npm config get registry 2>/dev/null); then
        case "$REG_LEFT" in
          *registry.npmjs.org*) echo "     （运行期 npm registry 已回到 $REG_LEFT）" ;;
          *) echo "  ❌ 运行期 npm registry = ${REG_LEFT:-（空）} —— 没回到官方源，"
             echo "     会改变运行期 agent 命令的联网报错文本，必须排查后重建"
             echo "fail $dt 0" >"$work/.status"; echo; return 0 ;;
        esac
      else
        echo "     （镜像里没有 npm，跳过 registry 还原实测）"
      fi
    fi
    # go 那三个同理，只能活在构建期。这里只查 Config.Env，不做 docker run 实测 ——
    # go 的默认值本来就可能由基座 / go 自身给出，实测值容易误判成「残留」。
    if [ -n "$GOPROXY" ] || [ -n "$GOSUMDB" ] || [ -n "$GODEBUG" ] || [ -n "$GOMAXPROCS" ]; then
      if docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' \
           | grep -qiE '(^|[^A-Za-z_])(goproxy|gosumdb|godebug|gomaxprocs)='; then
        echo "  ❌ 镜像 Env 里残留了 go 变量 —— 该用 ARG 而非 ENV，必须排查后重建"
        docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -iE 'goproxy|gosumdb|godebug|gomaxprocs' | sed 's/^/       /'
        echo "fail $dt 0" >"$work/.status"; echo; return 0
      fi
    fi
    # --build-env 注入的键同理：只能活在构建期，写成 ENV 就带进运行期了。
    # 正则由已校验过的标识符拼成，不会被值里的字符搞坏。
    if [ -n "$BUILD_ENV_RE" ]; then
      if docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' \
           | grep -qE "(^|[^A-Za-z_])($BUILD_ENV_RE)="; then
        echo "  ❌ 镜像 Env 里残留了 --build-env 注入的变量 —— 该用 ARG 而非 ENV，必须排查后重建"
        docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -E "($BUILD_ENV_RE)=" | sed 's/^/       /'
        echo "fail $dt 0" >"$work/.status"; echo; return 0
      fi
    fi
    # --insecure 的配置必须已被末尾那步还原，否则运行期工具行为就变了
    if [ "$INSECURE" = 1 ]; then
      LEFT=$(docker run --rm "$TAG" sh -c '
        r=""
        git config --system --get http.sslVerify >/dev/null 2>&1 && r="$r git"
        [ -f /etc/pip.conf ] && r="$r pip"
        grep -q "^insecure$" /root/.curlrc 2>/dev/null && r="$r curl"
        go env GOFLAGS 2>/dev/null | grep -q insecure && r="$r go"
        printf "%s" "$r"' 2>/dev/null)
      if [ -n "$LEFT" ]; then
        echo "  ⚠️  这些工具的证书校验没还原干净:$LEFT —— 会带进运行期，建议改用 --ca-cert"
      else
        echo "     （--insecure 的配置已还原干净，镜像未被污染）"
      fi
    fi
      # 便捷别名。用 trial 名而非语言名 —— 全量集下同语言有几十条，用语言名会互相覆盖。
    # docker 仓库名只允许小写与 . _ -，先规整一遍。
    alias_repo=$(printf '%s' "$dir" | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9._-]/-/g')
    docker tag "$TAG" "deepswe-local/$alias_repo:${BASE_ARCH:-local}" 2>/dev/null
    echo "ok $dt $((sz/1024/1024))" >"$work/.status"
  elif [ "$rc" = 124 ]; then
    # 停滞单独成一类，不混进「失败」：两者的后续处置完全不同 —— 失败多是配置/依赖
    # 问题（改 Dockerfile 或换参数），停滞多是取包网络（换源、放宽阈值，或干脆跳过）。
    dt=$(( $(date +%s) - t0 ))
    if [ "$WATCH_REASON" = timeout ]; then
      # 这里不能说「停滞」：终止原因是总耗时超上限，日志可能一直在正常输出。
      # 措辞与并发模式的结束行、以及结尾汇总保持一致，免得引到 --stall-timeout 上去
      echo "  ⏳ 超时  ${dt}s 触到 --build-timeout=${BUILD_TIMEOUT}s 上限，已终止（最后 ${WATCH_IDLE}s 无日志输出）—— 最后 15 行："
    else
      echo "  ⏳ 停滞  ${dt}s，其中最后 ${WATCH_IDLE}s 日志一个字都没动，已终止 —— 最后 15 行："
    fi
    tail -15 "$work/build.log" | sed 's/^/       /'
    echo "     继续下一条（处置建议见结尾汇总 —— 两种终止原因的处置不一样）"
    # 终止原因要一路带到汇总去：停滞该调 --stall-timeout，超上限该调 --build-timeout，
    # 两者混成一类会让汇总给出答非所问的建议（实测踩过）
    echo "$WATCH_REASON $dt 0" >"$work/.status"
  else
    dt=$(( $(date +%s) - t0 ))
    echo "  ❌ 失败  ${dt}s —— 最后 15 行："
    tail -15 "$work/build.log" | sed 's/^/       /'
    echo "fail $dt 0" >"$work/.status"
  fi
  echo
}

# ---- 计数与聚合 ----------------------------------------------------------
N_OK=0; N_FAIL=0; N_STALL=0; N_SKIP=0; I=0; T_ALL=$(date +%s)
FAILED=(); STALLED=(); TIMEDOUT=()
# 三个都显式赋空值：set -u 下「declare -A 了但没赋过值」的数组，展开 ${#a[@]}
# 会被判成未定义变量而直接退出
declare -A RUN_DIR=() RUN_T0=() RUN_I=()   # worker pid → trial 目录 / 起始时间 / 序号
SEQ_DIR=""; TICK_PID=""

C_ST=""; C_DT=0; C_MB=0
collect() {
  local dir="$1" f="$OUT/$dir/.status"
  C_ST=""; C_DT=0; C_MB=0
  [ -f "$f" ] && read -r C_ST C_DT C_MB <"$f"
  : "${C_DT:=0}" "${C_MB:=0}"
  case "${C_ST:-}" in
    ok)    N_OK=$((N_OK+1)) ;;
    skip)  N_OK=$((N_OK+1)); N_SKIP=$((N_SKIP+1)) ;;
    # 两者都计进 N_STALL（汇总里是「被终止」一大类），但分别留名单：类内要按
    # 原因分行给建议
    stall)   N_STALL=$((N_STALL+1)); STALLED+=("$dir") ;;
    timeout) N_STALL=$((N_STALL+1)); TIMEDOUT+=("$dir") ;;
    fail)  N_FAIL=$((N_FAIL+1)); FAILED+=("$dir") ;;
    # 没留下状态 = worker 自己被杀或异常退出。宁可多报也不能漏，算失败。
    *)     C_ST="fail"; N_FAIL=$((N_FAIL+1)); FAILED+=("$dir") ;;
  esac
}

# ---- 中断 ----------------------------------------------------------------
# 非交互 shell 里用 & 起的进程**默认忽略 SIGINT**（POSIX 规定），所以 Ctrl-C 打在
# 终端上进不了 worker 也进不了 docker build —— 必须由主进程显式发 SIGTERM。
# 顺序不能反：先杀 docker build 客户端（TERM 让 BuildKit 通知服务端取消），再收
# worker；反过来先杀 worker，会把 docker build 留成孤儿继续在 daemon 里跑。
active_works() {
  local p
  if [ "$JOBS" -le 1 ]; then
    [ -n "$SEQ_DIR" ] && echo "$OUT/$SEQ_DIR"
  else
    for p in "${!RUN_DIR[@]}"; do echo "$OUT/${RUN_DIR[$p]}"; done
  fi
}
on_signal() {
  trap '' INT TERM                 # 连按 Ctrl-C 不要打断清理
  echo
  echo "⚠️  收到中断：不再调度新的构建，正在终止已启动的 docker build…"
  local w p
  while read -r w; do
    [ -f "$w/.buildpid" ] || continue
    p=$(cat "$w/.buildpid" 2>/dev/null)
    [ -n "$p" ] && kill -TERM "$p" 2>/dev/null
  done < <(active_works)
  sleep 5
  while read -r w; do
    [ -f "$w/.buildpid" ] || continue
    p=$(cat "$w/.buildpid" 2>/dev/null)
    [ -n "$p" ] && kill -KILL "$p" 2>/dev/null
    rm -f "$w/.buildpid"
  done < <(active_works)
  for p in "${!RUN_DIR[@]}"; do kill -TERM "$p" 2>/dev/null; done
  [ -n "$TICK_PID" ] && kill "$TICK_PID" 2>/dev/null
  echo "   已终止。已建好的镜像仍在，重跑本命令会自动跳过它们。"
  exit 130
}
trap on_signal INT TERM

# ---- 并发工作池 ----------------------------------------------------------
# 心跳靠一个「定时子进程」实现：wait -n 没有超时参数，但它对**任一**子进程结束都
# 会返回，于是塞一个 sleep 进去当闹钟，醒来发现是它死了就打一行进度。
tick_start() {
  { [ -n "$TICK_PID" ] && kill -0 "$TICK_PID" 2>/dev/null; } && return 0
  sleep "$HEARTBEAT" & TICK_PID=$!
}
heartbeat() {
  local now p m; now=$(date +%s)
  printf '  ⏱  在建 %d ／ 已完成 %d（成功 %d 失败 %d 被终止 %d）／ 共 %d\n' \
    "${#RUN_DIR[@]}" "$((N_OK + N_FAIL + N_STALL))" "$N_OK" "$N_FAIL" "$N_STALL" \
    "${#SELECTED[@]}"
  # 「距上次日志输出」就是停滞判据本身，按它倒序排：排最前的那条离被杀最近，
  # 用户能眼看着这个数字逼近阈值，而不是等 5 分钟后才知道发生了什么。
  # （这一行每 --heartbeat 秒无条件打一次，不是「静默了才打」；数字小说明健康。）
  for p in "${!RUN_DIR[@]}"; do
    m=$(stat -c '%Y' "$OUT/${RUN_DIR[$p]}/build.log" 2>/dev/null)
    [ -n "$m" ] || m=${RUN_T0[$p]}
    printf '%d\t%s\t%d\n' "$((now - m))" "${RUN_DIR[$p]}" "$((now - RUN_T0[$p]))"
  done | sort -rn | while IFS=$'\t' read -r idle d el; do
    if [ "$STALL_TIMEOUT" -gt 0 ]; then
      printf '     %-44s 已跑 %5ds，距上次日志输出 %5ds／%ds\n' "$d" "$el" "$idle" "$STALL_TIMEOUT"
    else
      printf '     %-44s 已跑 %5ds，距上次日志输出 %5ds\n' "$d" "$el" "$idle"
    fi
  done
}
launch() {
  local dir="$1" i="$2"
  mkdir -p "$OUT/$dir"; : >"$OUT/$dir/build.log"
  printf '[%d/%d] ▶️  开始  %s\n' "$i" "${#SELECTED[@]}" "$dir"
  build_one "$dir" "$i" >"$OUT/$dir/.out" 2>&1 &
  local p=$!
  RUN_DIR[$p]="$dir"; RUN_T0[$p]=$(date +%s); RUN_I[$p]="$i"
}
finish_one() {
  local p="$1" dir="${RUN_DIR[$p]}" i="${RUN_I[$p]}" n="${#SELECTED[@]}"
  wait "$p" 2>/dev/null
  unset 'RUN_DIR[$p]' 'RUN_T0[$p]' 'RUN_I[$p]'
  collect "$dir"
  # 打印全部由主进程做，所以多行也不会串；worker 只管写文件
  case "$C_ST" in
    ok)    printf '[%d/%d] ✅ 成功  %s  %ss，%s MB\n' "$i" "$n" "$dir" "$C_DT" "$C_MB" ;;
    skip)  printf '[%d/%d] ⏭️  跳过  %s  镜像已存在\n' "$i" "$n" "$dir" ;;
    stall) printf '[%d/%d] ⏳ 停滞  %s  %ss 后被终止（日志停了 ≥%ss）—— 最后 6 行：\n' \
             "$i" "$n" "$dir" "$C_DT" "$STALL_TIMEOUT"
           tail -6 "$OUT/$dir/build.log" 2>/dev/null | sed 's/^/       /' ;;
    timeout) printf '[%d/%d] ⏳ 超时  %s  %ss，触到 --build-timeout=%ss 上限 —— 最后 6 行：\n' \
             "$i" "$n" "$dir" "$C_DT" "$BUILD_TIMEOUT"
           tail -6 "$OUT/$dir/build.log" 2>/dev/null | sed 's/^/       /' ;;
    *)     printf '[%d/%d] ❌ 失败  %s  %ss —— 最后 6 行（完整日志 %s）：\n' \
             "$i" "$n" "$dir" "$C_DT" "$OUT/$dir/build.log"
           tail -6 "$OUT/$dir/build.log" 2>/dev/null | sed 's/^/       /' ;;
  esac
}
pool_wait() {
  local p rc
  tick_start
  wait -n; rc=$?
  # wait -n 在没有可等待子进程时会立刻返回 127；真出现了就别空转
  [ "$rc" = 127 ] && sleep 1
  for p in "${!RUN_DIR[@]}"; do
    kill -0 "$p" 2>/dev/null && continue
    finish_one "$p"
  done
  if [ -n "$TICK_PID" ] && ! kill -0 "$TICK_PID" 2>/dev/null; then
    TICK_PID=""
    [ "${#RUN_DIR[@]}" -gt 0 ] && heartbeat
  fi
}

if [ "$JOBS" -le 1 ]; then
  for dir in "${SELECTED[@]}"; do
    I=$((I+1))
    SEQ_DIR="$dir"
    build_one "$dir" "$I"
    SEQ_DIR=""
    [ "$LIST" = 1 ] || collect "$dir"
  done
else
  for dir in "${SELECTED[@]}"; do
    while [ "${#RUN_DIR[@]}" -ge "$JOBS" ]; do pool_wait; done
    I=$((I+1))
    launch "$dir" "$I"
  done
  while [ "${#RUN_DIR[@]}" -gt 0 ]; do pool_wait; done
  [ -n "$TICK_PID" ] && kill "$TICK_PID" 2>/dev/null
  wait 2>/dev/null
fi

if [ "$LIST" = 1 ]; then
  echo "（--list：只展示改写，未构建）"
  exit 0
fi

echo "=============================================================="
echo " 成功 $N_OK（其中已存在跳过 $N_SKIP） / 失败 $N_FAIL / 被终止 $N_STALL   总耗时 $(( $(date +%s) - T_ALL ))s"
echo "=============================================================="
if [ "$N_FAIL" -gt 0 ]; then
  echo
  echo " 失败的 $N_FAIL 条（日志在 $OUT/<trial>/build.log）："
  for d in "${FAILED[@]}"; do echo "   $d"; done
  echo
  echo " 重试其中某一条：  bash build_arm.sh [同样的 --ca-cert/--proxy 参数] <trial 目录名前缀>"
fi
# 「被我们终止」是一大类，但**类内必须按原因分行**：两种终止的处置完全不同 ——
# 日志停滞该调 --stall-timeout，超总耗时上限该调 --build-timeout。混在一起讲，
# 就会出现「停滞检测明明关着，却建议你放宽 --stall-timeout」这种答非所问。
if [ "$N_STALL" -gt 0 ]; then
  echo
  echo " 被我们主动终止的 $N_STALL 条（日志在 $OUT/<trial>/build.log）："
  echo
  if [ ${#STALLED[@]} -gt 0 ]; then
    echo " ── 日志停滞（${STALL_TIMEOUT}s 不增长）${#STALLED[@]} 条："
    for d in "${STALLED[@]}"; do echo "      $d"; done
    echo "    先看一眼日志尾停在哪一步：若那步本来就会长时间不出声（rust 的"
    echo "    cargo nextest --no-run、大包的本地编译），这是**误杀**，该调大阈值而不是原样重试。"
    echo "    否则多半是取包的网络问题（go mod / pnpm / pip 连上了但不出数据）。三条路："
    # 重试命令填**完整** trial 目录名：脚本按前缀匹配，截短了会连带命中同前缀的邻居
    echo "      换源重试：  bash build_arm.sh --goproxy https://goproxy.cn,direct \\"
    echo "                                 --registry https://registry.npmmirror.com ${STALLED[0]}"
    echo "      放宽阈值：  bash build_arm.sh --stall-timeout 900 ${STALLED[0]}"
    echo "      直接跳过：  python3 run_batch.py --skip-missing 会自动跳过没建好的镜像"
    [ ${#STALLED[@]} -gt 1 ] && \
      echo "    （其余 $(( ${#STALLED[@]} - 1 )) 条同理，trial 名可以一次给多个）"
  fi
  if [ ${#TIMEDOUT[@]} -gt 0 ]; then
    [ ${#STALLED[@]} -gt 0 ] && echo
    echo " ── 超总耗时上限（--build-timeout=${BUILD_TIMEOUT}s）${#TIMEDOUT[@]} 条："
    for d in "${TIMEDOUT[@]}"; do echo "      $d"; done
    # 措辞只陈述「撞上了哪个上限」，不替构建断言「它一直在输出」—— 停滞检测被
    # --stall-timeout 0 关掉时，一条静默的构建同样会撞到这里
    echo "    这几条撞的是**单条总耗时上限**，不是日志停滞 —— 该动的是 --build-timeout，"
    echo "    不是 --stall-timeout。要么把上限调大，要么干脆不给（默认就没有总耗时上限）："
    echo "      调大上限：  bash build_arm.sh --build-timeout $((BUILD_TIMEOUT * 4)) ${TIMEDOUT[0]}"
    echo "      不设上限：  bash build_arm.sh ${TIMEDOUT[0]}"
    [ ${#TIMEDOUT[@]} -gt 1 ] && \
      echo "    （其余 $(( ${#TIMEDOUT[@]} - 1 )) 条同理，trial 名可以一次给多个）"
  fi
fi
if [ "$N_OK" -gt 0 ]; then
  echo
  echo " ⚠️  重建镜像 ≠ 原 amd64 镜像。跑之前先读 $OUT/<trial>/REWRITES.md。"
  echo "     patch_identical 在重建镜像上是否成立，本身就是这轮要测的东西——"
  echo "     它失败不一定是重放流程坏了，也可能是依赖漂移或架构差异。"
  echo
  echo " 下一步：  bash preflight.sh"
  echo "           python3 run_batch.py --only $(for d in "${SELECTED[@]}"; do echo "${LANG_OF[$d]}"; done | sort -u | paste -sd,) --skip-missing"
fi
