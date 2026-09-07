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

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/build"
BASE=""; LIST=0; TARGETS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --list) LIST=1; shift ;;
    --base) BASE="$2"; shift 2 ;;
    -o) OUT="$2"; shift 2 ;;
    -*) echo "未知参数: $1"; exit 1 ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done

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

# 语言 → trial 目录
declare -A DIR_OF LANG_OF
while IFS=$'\t' read -r lang dir; do
  DIR_OF[$lang]="$dir"; LANG_OF[$dir]="$lang"
done < <(python3 - "$HERE" <<'PY'
import json, pathlib, sys
for d in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    m = d / "meta.json"
    if d.is_dir() and m.exists():
        print(f"{json.loads(m.read_text()).get('language','?')}\t{d.name}")
PY
)

# 目标解析：语言名、task_id 前缀、或 all
SELECTED=()
if [ ${#TARGETS[@]} -eq 0 ] || [ "${TARGETS[0]:-}" = "all" ]; then
  for l in $ORDER; do [ -n "${DIR_OF[$l]:-}" ] && SELECTED+=("$l"); done
else
  for t in "${TARGETS[@]}"; do
    if [ -n "${DIR_OF[$t]:-}" ]; then SELECTED+=("$t"); continue; fi
    hit=""
    for l in $ORDER; do
      [ -n "${DIR_OF[$l]:-}" ] && case "${DIR_OF[$l]}" in "$t"*) hit="$l" ;; esac
    done
    [ -n "$hit" ] && SELECTED+=("$hit") || { echo "❌ 认不出目标: $t（用语言名 python/go/rust/typescript/javascript，或 task 目录名前缀）"; exit 1; }
  done
fi

echo "=============================================================="
echo " 从本地基座重建 task 镜像"
echo "=============================================================="
echo "  基座        ${BASE:-（未找到）}  ${BASE_ARCH:+($BASE_ARCH)}"
echo "  本机架构    $(uname -m)"
echo "  待建        ${SELECTED[*]}"
echo "  输出        $OUT"
echo

mkdir -p "$OUT"
CTX="$OUT/.emptyctx"; mkdir -p "$CTX"      # 零 COPY/ADD，空上下文即可

N_OK=0; N_FAIL=0
for lang in "${SELECTED[@]}"; do
  dir="${DIR_OF[$lang]}"
  work="$OUT/$lang"; mkdir -p "$work"

  # 从 task.json 里取出 Dockerfile 与目标 tag，并做架构改写
  python3 - "$HERE/$dir/task.json" "$work" "$BASE" "${BASE_ARCH:-}" <<'PY'
import json, pathlib, re, sys
task_json, work, base, base_arch = sys.argv[1:5]
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

  TAG=$(cat "$work/TAG")
  if [ "$LIST" = 1 ]; then echo; continue; fi

  if docker image inspect "$TAG" >/dev/null 2>&1; then
    echo "  跳过  镜像已存在（要重建先 docker rmi $TAG）"; echo; N_OK=$((N_OK+1)); continue
  fi

  echo "  构建中…（日志 $work/build.log）"
  t0=$(date +%s)
  if docker build -f "$work/Dockerfile" -t "$TAG" "$CTX" >"$work/build.log" 2>&1; then
    dt=$(( $(date +%s) - t0 ))
    sz=$(docker image inspect "$TAG" -f '{{.Size}}')
    echo "  ✅ 成功  ${dt}s，$((sz/1024/1024)) MB"
    docker tag "$TAG" "deepswe-local/$lang:$(basename "${BASE_ARCH:-local}")" 2>/dev/null
    N_OK=$((N_OK+1))
  else
    dt=$(( $(date +%s) - t0 ))
    echo "  ❌ 失败  ${dt}s —— 最后 15 行："
    tail -15 "$work/build.log" | sed 's/^/       /'
    N_FAIL=$((N_FAIL+1))
  fi
  echo
done

if [ "$LIST" = 1 ]; then
  echo "（--list：只展示改写，未构建）"
  exit 0
fi

echo "=============================================================="
echo " 成功 $N_OK / 失败 $N_FAIL"
echo "=============================================================="
if [ "$N_OK" -gt 0 ]; then
  echo
  echo " ⚠️  重建镜像 ≠ 原 amd64 镜像。跑之前先读 $OUT/<lang>/REWRITES.md。"
  echo "     patch_identical 在重建镜像上是否成立，本身就是这轮要测的东西——"
  echo "     它失败不一定是重放流程坏了，也可能是依赖漂移或架构差异。"
  echo
  echo " 下一步：  bash preflight.sh"
  echo "           python3 run_batch.py --only $(IFS=,; echo "${SELECTED[*]}")"
fi
