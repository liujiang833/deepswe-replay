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
# 代理默认继承环境变量；--proxy 可显式覆盖
PROXY="${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}"
NOPROXY="${NO_PROXY:-${no_proxy:-localhost,127.0.0.1,::1}}"
BUILD_NET=""
# 公司内网做 TLS 中间人时用：装内网 CA（推荐）或干脆关掉校验（有残留代价，见下）
CA_CERT="${DEEPSWE_CA_CERT:-}"
INSECURE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --list) LIST=1; shift ;;
    --base) BASE="$2"; shift 2 ;;
    --proxy) PROXY="$2"; shift 2 ;;
    --no-proxy) NOPROXY="$2"; shift 2 ;;
    --build-network) BUILD_NET="$2"; shift 2 ;;
    --ca-cert) CA_CERT="$2"; shift 2 ;;
    --insecure) INSECURE=1; shift ;;
    -o) OUT="$2"; shift 2 ;;
    -*) echo "未知参数: $1"; exit 1 ;;
    *) TARGETS+=("$1"); shift ;;
  esac
done

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
echo

mkdir -p "$OUT"
CTX="$OUT/.emptyctx"; mkdir -p "$CTX"      # 原 Dockerfile 零 COPY/ADD，空上下文即可
# 唯一会进上下文的东西：内网 CA 证书（要 COPY 进镜像的信任库）
rm -f "$CTX"/*.crt
[ -n "$CA_CERT" ] && cp "$CA_CERT" "$CTX/$(basename "$CA_CERT")"

N_OK=0; N_FAIL=0
for lang in "${SELECTED[@]}"; do
  dir="${DIR_OF[$lang]}"
  work="$OUT/$lang"; mkdir -p "$work"

  # 从 task.json 里取出 Dockerfile 与目标 tag，并做架构 / 证书改写
  python3 - "$HERE/$dir/task.json" "$work" "$BASE" "${BASE_ARCH:-}" \
           "$([ -n "$CA_CERT" ] && basename "$CA_CERT" || echo '')" "$INSECURE" <<'PY'
import json, pathlib, re, sys
task_json, work, base, base_arch, ca_name, insecure = sys.argv[1:7]
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

# 3) 公司内网 TLS 中间人：装内网 CA，或（退而求其次）关掉校验
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
  NET_ARG=(); [ -n "$BUILD_NET" ] && NET_ARG=(--network "$BUILD_NET")
  t0=$(date +%s)
  if docker build "${NET_ARG[@]}" "${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}" \
       -f "$work/Dockerfile" -t "$TAG" "$CTX" >"$work/build.log" 2>&1; then
    dt=$(( $(date +%s) - t0 ))
    sz=$(docker image inspect "$TAG" -f '{{.Size}}')
    echo "  ✅ 成功  ${dt}s，$((sz/1024/1024)) MB"
    # 保真度自检：代理绝不能留在镜像里。运行期是 --network=none + 403 sinkhole，
    # 镜像 Env 里多一个 *_proxy 就会改变 agent 命令的联网报错文本。
    if docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' \
         | grep -qiE '(^|[^A-Za-z_])(https?_proxy|no_proxy)='; then
      echo "  ❌ 镜像 Env 里残留了代理变量 —— 会污染运行期行为，必须排查后重建"
      docker image inspect "$TAG" -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -i proxy | sed 's/^/       /'
      N_FAIL=$((N_FAIL+1)); echo; continue
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
    docker tag "$TAG" "deepswe-local/$lang:${BASE_ARCH:-local}" 2>/dev/null
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
