#!/usr/bin/env bash
# 打一个自包含的 ARM topdown 采集包，scp 到服务器解开就能跑。入口是包里的 TOPDOWN.md。
#
# **为什么不复用 make_bundle.sh**：那是 113 条主重放包的路径，已经在服务器上跑通过，
# 给它加分支（"要不要带 topdown 脚本"、"要不要只带一条 trial"）等于拿一条已验证的
# 路径去冒险。两个包的内容差得也远：这里只带 1 条 trial，但多带 topdown 那一套，
# 还要带 replay/commands.jsonl 做基线对照（主包默认不带，因为 113 条会撑到几十 MB）。
# 约定（BUILD_INFO 记 built_utc/git_commit/host、SHA256SUMS 记全量指纹、
# tarball 根目录名固定）与 make_bundle.sh 保持一致，两个包可以用同一套手法核对。
#
# 用法：  bash make_topdown_bundle.sh                 # → deepswe-topdown-bundle-<日期>[后缀].tar.gz
#         bash make_topdown_bundle.sh -o /tmp/x.tar.gz          # 完全指定输出名
#         bash make_topdown_bundle.sh --tag b                   # 强制后缀 → …-<日期>b.tar.gz
#         bash make_topdown_bundle.sh --trial <trial目录名>     # 换一条 trial

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT=""; TAG=""
TRIAL_NAME="returns-validated-error-accumula__8JQj5gw"
while [ $# -gt 0 ]; do
  case "$1" in
    -o)      [ $# -ge 2 ] || { echo "❌ -o 缺少值"; exit 1; }; OUT="$2"; shift 2 ;;
    --tag)   [ $# -ge 2 ] || { echo "❌ --tag 缺少值"; exit 1; }; TAG="$2"; shift 2 ;;
    --trial) [ $# -ge 2 ] || { echo "❌ --trial 缺少值"; exit 1; }; TRIAL_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -e/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "❌ 未知参数: $1"; exit 1 ;;
  esac
done
# 默认输出名：同一天重打**不覆盖**，自动往后加一位字母（…-20260911.tar.gz → …-20260911b.tar.gz）。
# 为什么不直接覆盖：用户手上往往已经 scp 过一份旧包，文件名一样、内容不一样，
# 到时候「服务器上跑的到底是哪一版」就只能靠 BUILD_INFO 猜。宁可多一个文件，也不要同名两份。
# （真想覆盖就显式 -o 指定同名。）
if [ -z "$OUT" ]; then
  BASE="$HERE/deepswe-topdown-bundle-$(date +%Y%m%d)"
  if [ -n "$TAG" ]; then
    OUT="${BASE}${TAG}.tar.gz"
  else
    OUT="${BASE}.tar.gz"
    for suf in b c d e f g h i j k l m n o p q r s t u v w x y z; do
      [ -e "$OUT" ] || break
      OUT="${BASE}${suf}.tar.gz"
    done
    if [ -e "$OUT" ]; then
      echo "❌ $BASE{,b..z}.tar.gz 全都存在了 —— 今天打得够多了，用 -o 显式指定输出名"
      exit 1
    fi
  fi
fi

SRC="$HERE/$TRIAL_NAME"
[ -d "$SRC" ] || { echo "❌ trial 目录不存在: $SRC"; exit 1; }
REPLAY="$HERE/../replay.py"
[ -f "$REPLAY" ] || { echo "❌ 找不到 $REPLAY"; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/deepswe-topdown-bundle"
mkdir -p "$ROOT"

# 顶层：replay.py 从上一层复制进来，复制后它和 trial 目录同级 ——
# topdown_trial.sh 的 replay.py 定位逻辑（先 ./replay.py，再 ../replay.py）就是为这个布局写的。
cp "$REPLAY" "$ROOT/"

# topdown 这一套 + 建镜像要用的那几个脚本。
# 建镜像的脚本一个都不能少：check_sources.sh 先探源、build_arm.sh 真建，
# detect_mitm.sh / get_ca_cert.sh 是内网 TLS 中间人时的解法，preflight.sh 验重放环境。
MUST=(topdown.conf probe_pmu.sh topdown_trial.sh topdown_parse.py TOPDOWN.md
      build_arm.sh check_sources.sh preflight.sh get_ca_cert.sh detect_mitm.sh)
MISSING=()
for f in "${MUST[@]}"; do
  if [ -f "$HERE/$f" ]; then cp "$HERE/$f" "$ROOT/"; else MISSING+=("$f"); fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "❌ 这些文件不在 $HERE，包不完整，拒绝打包：${MISSING[*]}"
  exit 1
fi

# trial：只带重放真正要用的 4 个输入 + 基线对照 2 个。
# **不带 mini-swe-agent.txt**（285KB 的 agent 原始日志）—— 重放用不到，纯占体积。
mkdir -p "$ROOT/$TRIAL_NAME/replay"
for f in meta.json trajectory.json model.patch task.json; do
  [ -f "$SRC/$f" ] || { echo "❌ 缺 $SRC/$f"; exit 1; }
  cp "$SRC/$f" "$ROOT/$TRIAL_NAME/"
done
# 基线对照：verdict.json 是判定，commands.jsonl 是逐条命令的耗时分布
#（TOPDOWN.md「短命令的数据有效性」一节让用户在目标机上复算的就是它）。
for f in verdict.json commands.jsonl; do
  [ -f "$SRC/replay/$f" ] || { echo "❌ 缺基线 $SRC/replay/$f"; exit 1; }
  cp "$SRC/replay/$f" "$ROOT/$TRIAL_NAME/replay/"
done

# 包内可执行位统一成 755。源仓库里几个文件是 644（历史原因），`cp` 会把 644 原样带过来，
# 结果包里 build_arm.sh 是 755 而 probe_pmu.sh 是 644 —— 文档一律写 `bash xxx.sh` 所以
# 不影响使用，但用户敲 `./probe_pmu.sh` 会 Permission denied，没必要留这个坑。
chmod 755 "$ROOT"/*.sh "$ROOT"/*.py

# 版本标识：文件名只带日期（同一天多次重打会带 b/c/d… 后缀），光看文件名
# 分不清手上这份在功能上是哪一版。服务器上 `cat BUILD_INFO` 一眼可辨。
GITSHA=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)
# 只看进了包的那些路径：仓库里别处的未提交改动与本包无关，算进来会让标识长期显示"脏"而失去意义。
# 末尾 || true 不能省：set -euo pipefail 下 grep -v 无匹配时返回 1（正是"干净"的情况），
# 会把整个脚本打断。
GITDIRTY=$(git -C "$HERE" status --porcelain -- "$HERE" "$REPLAY" 2>/dev/null \
           | grep -v '\.tar\.gz$' | head -1 || true)
{
  echo "built_utc   $(date -u +%FT%TZ)"
  echo "git_commit  $GITSHA${GITDIRTY:+ (工作区有未提交改动)}"
  echo "host        $(uname -srm)"
  echo "kind        topdown（单条 trial 的 ARM PMU 采集）"
  echo "bundle      $(basename "$OUT")"
  # 同一天可能打出好几个包（…-20260911.tar.gz / …-20260911b.tar.gz），光看日期分不清。
  # 这一行写死这一版**在功能上**是什么，服务器上 `cat BUILD_INFO` 一眼可辨新旧。
  echo "features    后端双口径（EV_STALL_SLOT_BE 留空=残差法 / 填=直接法）"
  echo "            残差法下求和自检失效 → 改跑 C1~C5；可选 EV_STALL_SLOT 开 X 交叉校验"
  echo "            事件号强制 0x 前缀校验；perf 的 -G 排在 -e 之后（must define events before cgroups）"
  echo "            cgroup v1/v2 都支持：路径改为读 /proc/<pid>/cgroup，不猜 docker driver"
  echo "            topdown_trial.sh 支持 --no-metrics 透传给 replay.py（v1 上绕开 replay 指标）"
  echo "trial       $TRIAL_NAME"
  echo "baseline    $(python3 -c "
import json,sys
v=json.load(open(sys.argv[1]))
print('patch_identical=%s n_replayed=%s rc_match=%s elapsed_s=%s（开发机 x86_64）'
      % (v.get('patch_identical'), v.get('n_replayed'), v.get('rc_match'), v.get('elapsed_s')))
" "$ROOT/$TRIAL_NAME/replay/verdict.json" 2>/dev/null || echo 未知)"
  echo
  echo "scripts:"
  for f in replay.py probe_pmu.sh topdown_trial.sh topdown_parse.py topdown.conf \
           build_arm.sh check_sources.sh preflight.sh get_ca_cert.sh detect_mitm.sh; do
    # 必须写成 if：set -e 下 `[ -f ... ] && printf` 在文件不存在时整条返回 1，
    # 会把打包脚本在「生成 BUILD_INFO」这一步静默打断（上面的 MUST 校验保证了
    # 这些文件都在，但别让正确性依赖另一段代码的副作用）。
    if [ -f "$ROOT/$f" ]; then
      printf '  %-20s %s\n' "$f" "$(sha256sum "$ROOT/$f" | cut -c1-12)"
    fi
  done
} > "$ROOT/BUILD_INFO"

# 随包留一份指纹：解包后可核对传输完整性，也便于日后追溯跑的是哪一版
( cd "$ROOT" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )

mkdir -p "$(dirname "$OUT")"
tar -czf "$OUT" -C "$STAGE" deepswe-topdown-bundle

echo "打包完成"
echo "  trial     $TRIAL_NAME（1 条）"
echo "  文件数    $(find "$ROOT" -type f | wc -l)"
echo "  解包体积  $(du -sh "$ROOT" | cut -f1)"
echo "  归档      $OUT  ($(du -h "$OUT" | cut -f1))"
echo
echo "拷到服务器："
echo "  scp $OUT <server>:~/"
echo "  ssh <server> 'tar xzf $(basename "$OUT") && cd deepswe-topdown-bundle && bash probe_pmu.sh'"
echo
echo "  解包后第一条命令就是 bash probe_pmu.sh —— 先验事件号、计数器余量、cgroup 路径，"
echo "  这三样任何一样不对，后面建镜像和整条重放都是白做。详见包里的 TOPDOWN.md。"
