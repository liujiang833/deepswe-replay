#!/usr/bin/env bash
# 复现 classify_coverage/ 下的全部结果（2026-09-14 分类器跨语言扩展的验收）：
#   1. python 回归：改动前（BEFORE_REV 的 summarize_replay.py）与当前版本，对 python jsonl 的
#      默认输出 / --audit / --json 逐字 diff，结论写进 python_regression.txt；
#   2. 覆盖率：full_trials/ 113 条 trace 的命令，before / after 各跑一遍，再出 COVERAGE.md。
#
# 用法：bash deepswe/crosslang/classify_coverage/reproduce.sh [BEFORE_REV]
# 依赖：full_trials/*/trajectory.json（入库）；deepswe/replay_out/ 与 crosslang/*/replay/commands.jsonl
#       被 .gitignore 排除，不在本机就跳过对应那份 python 回归。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CL="$(cd "$HERE/.." && pwd)"              # deepswe/crosslang
DS="$(cd "$CL/.." && pwd)"                # deepswe
BEFORE_REV="${1:-0288c83}"                # 分类器改动之前的最后一个提交

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git -C "$DS" show "$BEFORE_REV:deepswe/summarize_replay.py" > "$TMP/summarize_replay_before.py"

REG="$HERE/python_regression.txt"
{
  echo "# python 回归：$BEFORE_REV 的 summarize_replay.py vs 工作区版本"
  echo "# 生成：$(date -u +%FT%TZ)"
  for J in "$DS/replay_out/gql-incremental-graphql-delivery__nnFNKRL/commands.jsonl" \
           "$CL/returns-validated-error-accumula__8JQj5gw/replay/commands.jsonl"; do
    if [ ! -f "$J" ]; then echo "SKIP（文件不在本机）$J"; continue; fi
    echo "## ${J#$DS/}"
    for MODE in default audit json; do
      case "$MODE" in
        default) A=(); ;;
        audit)   A=(--audit) ;;
        json)    A=(--json "$TMP/MODE.json") ;;
      esac
      python3 "$TMP/summarize_replay_before.py" --jsonl "$J" "${A[@]}" > "$TMP/b.txt"
      [ "$MODE" = json ] && mv "$TMP/MODE.json" "$TMP/b.txt"
      python3 "$DS/summarize_replay.py" --jsonl "$J" "${A[@]}" > "$TMP/a.txt"
      [ "$MODE" = json ] && mv "$TMP/MODE.json" "$TMP/a.txt"
      if cmp -s "$TMP/b.txt" "$TMP/a.txt"; then
        echo "  $MODE: IDENTICAL ($(wc -l < "$TMP/a.txt") 行)"
      else
        echo "  $MODE: DIFFERENT"
        diff "$TMP/b.txt" "$TMP/a.txt" | sed 's/^/    /' || true
      fi
    done
  done
} > "$REG"
cat "$REG"

cd "$CL"
python3 classify_coverage.py run --classifier "$TMP/summarize_replay_before.py" \
    --classifier-source "\`git show $BEFORE_REV:deepswe/summarize_replay.py\` 导出到临时文件（跑完即删，由 \`classify_coverage/reproduce.sh\` 重新生成）" \
    --label "before ($BEFORE_REV)" -o "$HERE/before.json"
python3 classify_coverage.py run --classifier-source "工作区的 \`deepswe/summarize_replay.py\`" \
    --label "after (工作区)" -o "$HERE/after.json"
python3 classify_coverage.py compare "$HERE/before.json" "$HERE/after.json" -o "$HERE/COVERAGE.md"
