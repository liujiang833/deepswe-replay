#!/usr/bin/env bash
# 分级测试一个镜像能否拉取。从最便宜的探测逐级加深，卡在哪一层一目了然。
#
#   ./check_pullable.sh public.ecr.aws/x8v8d7g8/mars-base:latest
#   ./check_pullable.sh public.ecr.aws/x8v8d7g8/mars-base:latest linux/arm64
#   ./check_pullable.sh docker.io/library/debian:12
#
# 依赖：curl + python3（解析 JSON）。docker 有就多跑两级，没有也能给出前 5 级结论。
#
# 为什么分级：manifest 读得到 ≠ 拉得下来。ECR 匿名访问实测就是
# manifest 正常但 blob 被 429 限流 —— L3 过而 L5 挂，这条差别是整个脚本的意义所在。
set -uo pipefail
REF="${1:?用法: $0 <registry>/<repo>:<tag> [platform]}"
WANT="${2:-}"

case "$REF" in
  */*/*) REG="${REF%%/*}"; REST="${REF#*/}" ;;
  */*)   REG="docker.io"; REST="$REF" ;;
  *)     REG="docker.io"; REST="library/$REF" ;;
esac
REPO="${REST%:*}"; TAG="${REST##*:}"; [ "$TAG" = "$REST" ] && TAG=latest
[ "$REG" = "docker.io" ] && HOST="registry-1.docker.io" || HOST="$REG"
ACC='application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.manifest.v1+json'

pass(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail(){ printf '  \033[31m✗\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }
info(){ printf '    %s\n' "$*"; }

echo "registry=$HOST  repo=$REPO  tag=$TAG  want=${WANT:-<宿主默认>}"; echo

echo "[L1] registry API /v2/"
C=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$HOST/v2/")
case "$C" in
  200) pass "HTTP 200（无需鉴权）" ;;
  401) pass "HTTP 401（活着，需要 token —— 正常）" ;;
  000) fail "连不上（DNS/TCP/TLS 失败）"; exit 1 ;;
  *)   fail "HTTP $C"; exit 1 ;;
esac

echo "[L2] 匿名 token"
if [ "$HOST" = "registry-1.docker.io" ]; then
  TURL="https://auth.docker.io/token?service=registry.docker.io&scope=repository:$REPO:pull"
else
  TURL="https://$HOST/token/?service=$HOST&scope=repository:$REPO:pull"
fi
TOK=$(curl -s -m 20 "$TURL" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
[ -n "$TOK" ] && pass "拿到 token（${#TOK} 字符）" \
              || { warn "拿不到匿名 token —— 私有仓库需先 docker login"; TOK=""; }

echo "[L3] manifest（仓库+tag 存在、有权限）"
MF=$(mktemp)
C=$(curl -s -m 30 -o "$MF" -w '%{http_code}' ${TOK:+-H "Authorization: Bearer $TOK"} \
      -H "Accept: $ACC" "https://$HOST/v2/$REPO/manifests/$TAG")
[ "$C" = "200" ] || { fail "HTTP $C"; head -c 300 "$MF"; echo; rm -f "$MF"; exit 1; }
pass "HTTP 200（$(wc -c <"$MF") 字节）"

echo "[L4] 平台"
# 用 python 解析：输出 "kind<TAB>平台列表<TAB>目标manifest的digest或空"
read -r KIND PLATS SUBREF < <(python3 - "$MF" "$WANT" <<'PY'
import json,sys
m=json.load(open(sys.argv[1])); want=sys.argv[2]
if "manifests" in m:
    ps=[]; sub=""
    for x in m["manifests"]:
        p=x.get("platform",{}) or {}
        if p.get("os")=="unknown": continue          # attestation 条目，跳过
        s=f"{p.get('os')}/{p.get('architecture')}"+(f"/{p['variant']}" if p.get('variant') else "")
        ps.append(s)
        if want and s.split('/')[1]==want.split('/')[-1]: sub=x["digest"]
    print("index", ",".join(ps) or "-", sub or "-")
else:
    print("single", "-", "-")
PY
)
if [ "$KIND" = "index" ]; then
  pass "多架构索引：$PLATS"
  if [ -n "$WANT" ]; then
    [ "$SUBREF" != "-" ] && pass "包含请求的 $WANT" || fail "不含 $WANT —— 该 tag 没有这个架构"
  fi
else
  warn "单架构 manifest（无索引，没有可选余地）"
  ARCH=$(python3 - "$MF" <<'PY'
import json,sys; m=json.load(open(sys.argv[1])); print(m["config"]["digest"])
PY
)
  ARCH=$(curl -sL -m 30 ${TOK:+-H "Authorization: Bearer $TOK"} "https://$HOST/v2/$REPO/blobs/$ARCH" \
          | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("architecture","?"))' 2>/dev/null)
  info "config blob 声明: architecture=$ARCH"
  if [ -n "$WANT" ] && [ "${WANT#*/}" != "$ARCH" ]; then
    fail "与请求的 $WANT 不符"
    info "⚠ docker pull 对此只会 WARNING 且 rc=0，要到 docker run 才炸 exec format error"
  fi
fi

echo "[L5] blob 取样（前 1 KB，限流在这层暴露）"
# 索引要先下钻到具体平台的 manifest，才能拿到真正的 layer blob
BM="$MF"
if [ "$KIND" = "index" ] && [ "$SUBREF" != "-" ]; then
  BM=$(mktemp)
  curl -s -m 30 -o "$BM" ${TOK:+-H "Authorization: Bearer $TOK"} -H "Accept: $ACC" \
       "https://$HOST/v2/$REPO/manifests/$SUBREF"
fi
BLOB=$(python3 - "$BM" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
ls=m.get("layers") or []
print(min(ls,key=lambda l:l["size"])["digest"] if ls else "")
PY
)
if [ -z "$BLOB" ]; then info "（未能定位 layer blob，跳过）"; else
  C=$(curl -sL -m 40 -o /dev/null -w '%{http_code}' -r 0-1023 \
        ${TOK:+-H "Authorization: Bearer $TOK"} "https://$HOST/v2/$REPO/blobs/$BLOB")
  case "$C" in
    200|206) pass "HTTP $C —— blob 可取，拉取路径通畅" ;;
    429)     fail "HTTP 429 限流 —— manifest 能读不代表能拉，这层才是真门槛" ;;
    *)       fail "HTTP $C" ;;
  esac
fi
[ "$BM" != "$MF" ] && rm -f "$BM"; rm -f "$MF"

echo "[L6] docker daemon"
command -v docker >/dev/null 2>&1 || { info "（无 docker，前 5 级已足以判断网络与授权）"; exit 0; }
docker info >/dev/null 2>&1 || { fail "docker 装了但 daemon 不可用"; exit 1; }
pass "daemon 可用（$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')）"
echo "[L7] docker manifest inspect（不下载层）"
docker manifest inspect "$REF" >/dev/null 2>&1 \
  && pass "通过" || fail "失败 —— 可能需要 docker login"
