#!/usr/bin/env bash
# 打一个自包含的 tarball，scp 到服务器解开就能跑。入口是包里的 README.md。
#
# 默认精简：只带重放真正需要的输入（trajectory / model.patch / task.json / meta.json）
# 加上基线判定（replay/verdict.json）用于跨机对比。agent 原始日志与 verifier 输出
# 各约 1MB×5，重放用不到，默认不带；要完整归档用 --full。
#
# 用法：  bash make_bundle.sh              # → deepswe-replay-bundle-<日期>.tar.gz
#         bash make_bundle.sh --full      # 连 agent 日志、per-command 指标一起带
#         bash make_bundle.sh -o /tmp/x.tar.gz
#         bash make_bundle.sh --trials-dir full_trials   # 打全量 113 条（见 make_full_trials.py）

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FULL=0; OUT=""; TRIALS="$HERE"
while [ $# -gt 0 ]; do
  case "$1" in
    --full) FULL=1; shift ;;
    --trials-dir) TRIALS="$(cd "$2" && pwd)"; shift 2 ;;
    -o) OUT="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done
[ -d "$TRIALS" ] || { echo "trial 目录不存在: $TRIALS"; exit 1; }
[ -n "$OUT" ] || OUT="$HERE/deepswe-replay-bundle-$(date +%Y%m%d).tar.gz"

REPLAY="$HERE/../replay.py"
[ -f "$REPLAY" ] || { echo "找不到 $REPLAY"; exit 1; }
# 命令分类器：cmd_stats.py（run_batch 收尾自动调）要 import 它。仓库里它和 replay.py 一样在上一层。
CLASSIFIER="$HERE/../summarize_replay.py"
[ -f "$CLASSIFIER" ] || { echo "找不到 $CLASSIFIER"; exit 1; }
[ -f "$HERE/cmd_stats.py" ] || { echo "找不到 $HERE/cmd_stats.py"; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/deepswe-replay-bundle"
mkdir -p "$ROOT"

# 顶层：脚本与手册。replay.py / summarize_replay.py 从上一层复制进来，bundle 从此自包含
# （平铺后 cmd_stats.py 先找同级的 summarize_replay.py，与 run_batch.py 找 replay.py 同一个顺序）。
cp "$REPLAY" "$ROOT/"
cp "$CLASSIFIER" "$ROOT/"
for f in run_batch.py cmd_stats.py preflight.sh check_sources.sh build_arm.sh get_ca_cert.sh detect_mitm.sh README.md RUNBOOK.md INDEX.md release.json; do
  [ -f "$HERE/$f" ] && cp "$HERE/$f" "$ROOT/"
done

N=0
for d in "$TRIALS"/*/; do
  name="$(basename "$d")"
  [ -f "$d/meta.json" ] || continue          # 只收合规的 trial 目录
  mkdir -p "$ROOT/$name/replay"
  for f in meta.json trajectory.json model.patch task.json; do
    cp "$d/$f" "$ROOT/$name/"
  done
  # 基线判定：run_batch.py 用它做跨机对比
  [ -f "$d/replay/verdict.json" ] && cp "$d/replay/verdict.json" "$ROOT/$name/replay/"
  if [ "$FULL" = 1 ]; then
    for f in mini-swe-agent.txt test-stdout.txt; do
      [ -f "$d/$f" ] && cp "$d/$f" "$ROOT/$name/"
    done
    for f in commands.jsonl replayed.patch; do
      [ -f "$d/replay/$f" ] && cp "$d/replay/$f" "$ROOT/$name/replay/"
    done
  fi
  N=$((N+1))
done
[ "$N" -gt 0 ] || { echo "没找到任何 trial 目录"; exit 1; }

# 版本标识：文件名只带日期，同一天重打会同名覆盖、跨天又会多出一个包，
# 光看文件名分不清手上这份是哪一版。服务器上 `cat BUILD_INFO` 一眼可辨。
GITSHA=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)
# 只看进了包的那些路径：仓库里别处的未提交改动与本包无关，算进来会让标识长期显示"脏"而失去意义
# 末尾 || true 不能省：set -euo pipefail 下，grep -v 无匹配时返回 1（正是"干净"的情况），
# 会把整个脚本打断
GITDIRTY=$(git -C "$HERE" status --porcelain -- "$HERE" "$REPLAY" "$CLASSIFIER" 2>/dev/null | grep -v '\.tar\.gz$' | head -1 || true)
{
  echo "built_utc   $(date -u +%FT%TZ)"
  echo "git_commit  $GITSHA${GITDIRTY:+ (工作区有未提交改动)}"
  echo "host        $(uname -srm)"
  echo "trials      $N"
  echo "trials_src  $([ "$TRIALS" = "$HERE" ] && echo "crosslang/（已验证 5 条）" || echo "$TRIALS")"
  echo "mode        $([ "$FULL" = 1 ] && echo full || echo slim)"
  echo
  echo "scripts:"
  for f in replay.py run_batch.py cmd_stats.py summarize_replay.py preflight.sh check_sources.sh build_arm.sh get_ca_cert.sh detect_mitm.sh; do
    [ -f "$ROOT/$f" ] && printf '  %-20s %s\n' "$f" "$(sha256sum "$ROOT/$f" | cut -c1-12)"
  done
} > "$ROOT/BUILD_INFO"

# 随包留一份指纹：解包后可核对传输完整性，也便于日后追溯跑的是哪一版
( cd "$ROOT" && find . -type f ! -name SHA256SUMS -print0 \
    | sort -z | xargs -0 sha256sum > SHA256SUMS )

mkdir -p "$(dirname "$OUT")"
tar -czf "$OUT" -C "$STAGE" deepswe-replay-bundle

echo "打包完成"
echo "  trial 数  $N$([ "$FULL" = 1 ] && echo '（--full：含 agent 日志与 per-command 指标）')"
echo "  文件数    $(find "$ROOT" -type f | wc -l)"
echo "  解包体积  $(du -sh "$ROOT" | cut -f1)"
echo "  归档      $OUT  ($(du -h "$OUT" | cut -f1))"
echo
echo "拷到服务器："
echo "  scp $OUT <server>:~/"
echo "  ssh <server> 'tar xzf $(basename "$OUT") && cd deepswe-replay-bundle && bash preflight.sh'"
