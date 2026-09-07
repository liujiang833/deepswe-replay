#!/usr/bin/env bash
# 从目标机上把「公司内网 CA」抠出来，产出可直接喂给 build_arm.sh --ca-cert 的 PEM 文件。
#
# 背景：公司代理做 TLS 中间人，用内网 CA 重签所有 HTTPS。宿主机通常已被 IT 装好这张
# CA（所以你在宿主上 curl/git 是通的），但**容器里没有**，于是 docker build 里的
# git clone 报 "server certificate verification failed"。
#
# 两条获取路径，脚本都试：
#   A. 从宿主系统信任库里挑出「本地额外添加的」CA —— 最可靠，就是 IT 装的那张
#   B. 从一次真实 TLS 握手里抓证书链 —— A 拿不到时的兜底
#
# 用法：
#   bash get_ca_cert.sh                    # 输出到 ./corp-ca.crt
#   bash get_ca_cert.sh -o /path/ca.crt
#   bash get_ca_cert.sh --host pypi.org    # 换一个目标站点探（默认 github.com）

set -uo pipefail
OUT="./corp-ca.crt"
HOST="github.com"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

PROXY="${HTTPS_PROXY:-${https_proxy:-}}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo "=============================================================="
echo " 提取公司内网 CA"
echo "=============================================================="
echo "  探测站点  $HOST:443"
[ -n "$PROXY" ] && echo "  代理      $(printf '%s' "$PROXY" | sed -E 's#(//)[^/@]*@#\1***@#')"
echo

# ── 0. 先确认宿主到底通不通，这决定了后面哪条路可行 ────────────
echo "── 0. 宿主机的 HTTPS 是否正常 ──"
if curl -sS -o /dev/null --max-time 15 "https://$HOST" 2>"$TMP/curl.err"; then
  echo "  ✅ 宿主机 curl 正常 —— 说明系统信任库里已有这张内网 CA，走路径 A 基本能拿到"
  HOST_OK=1
else
  echo "  ❌ 宿主机 curl 也失败：$(head -1 "$TMP/curl.err" | cut -c1-100)"
  echo "     宿主自己都没装 CA，路径 A 多半为空；路径 B 仍可能抓到，但拿不准哪张是根"
  HOST_OK=0
fi

# ── A. 从系统信任库里挑「本地额外添加的」CA ───────────────────
echo
echo "── A. 系统信任库里本地添加的 CA ──"
# 发行版放「额外 CA」的位置就这几个；系统自带的根证书不在这里，所以这里的
# 东西基本就是 IT 装进去的内网 CA
FOUND=""
for d in /usr/local/share/ca-certificates \
         /etc/pki/ca-trust/source/anchors \
         /etc/ca-certificates/trust-source/anchors; do
  [ -d "$d" ] || continue
  for f in "$d"/*.crt "$d"/*.pem "$d"/*.cer; do
    [ -f "$f" ] || continue
    if grep -q 'BEGIN CERTIFICATE' "$f" 2>/dev/null; then
      subj=$(openssl x509 -in "$f" -noout -subject 2>/dev/null | sed 's/^subject=//')
      echo "  ✅ $f"
      echo "     $subj"
      cat "$f" >> "$TMP/from_store.pem"
      FOUND=1
    fi
  done
done
[ -n "$FOUND" ] || echo "  （空 —— 这些目录里没有本地添加的证书）"

# ── B. 从真实 TLS 握手里抓链 ──────────────────────────────────
echo
echo "── B. 从 TLS 握手抓证书链 ──"
SC=(openssl s_client -showcerts -connect "$HOST:443" -servername "$HOST")
if [ -n "$PROXY" ]; then
  # openssl 1.1.1+ 支持 -proxy；剥掉 scheme 只留 host:port
  P="${PROXY#*://}"; P="${P#*@}"; P="${P%/}"
  SC+=(-proxy "$P")
fi
if "${SC[@]}" </dev/null >"$TMP/sc.out" 2>"$TMP/sc.err"; then :; fi
if grep -q 'BEGIN CERTIFICATE' "$TMP/sc.out"; then
  awk '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/' "$TMP/sc.out" > "$TMP/chain.pem"
  # 按证书拆开，逐张看 subject/issuer；自签的（subject==issuer）就是根
  csplit -sz -f "$TMP/c_" -b '%02d.pem' "$TMP/chain.pem" '/-----BEGIN CERTIFICATE-----/' '{*}' 2>/dev/null
  i=0
  for c in "$TMP"/c_*.pem; do
    [ -f "$c" ] || continue
    s=$(openssl x509 -in "$c" -noout -subject 2>/dev/null | sed 's/^subject=//')
    is=$(openssl x509 -in "$c" -noout -issuer 2>/dev/null | sed 's/^issuer=//')
    mark=""
    if [ "$s" = "$is" ]; then mark="  ← 自签，是根 CA"; cat "$c" >> "$TMP/from_chain.pem"
    elif [ "$i" -gt 0 ]; then mark="  ← 中间 CA"; cat "$c" >> "$TMP/from_chain.pem"
    fi
    echo "  [$i] $s$mark"
    [ "$i" = 0 ] && echo "      签发者: $is"
    i=$((i+1))
  done
  # 叶子证书的签发者若不是真实 CA，就是中间人的铁证
  LEAF_ISSUER=$(openssl x509 -in "$TMP/c_00.pem" -noout -issuer 2>/dev/null | sed 's/^issuer=//')
  case "$LEAF_ISSUER" in
    *DigiCert*|*"Let's Encrypt"*|*Sectigo*|*GlobalSign*|*Amazon*|*Google*)
      echo
      echo "  ℹ️  叶子证书由公共 CA 签发 —— 这条链**没有**被中间人替换。"
      echo "     那么证书报错可能另有原因（比如容器里 ca-certificates 过期）。" ;;
    *)
      echo
      echo "  ⚠️  叶子证书的签发者不是常见公共 CA —— 确认存在 TLS 中间人。" ;;
  esac
else
  echo "  ❌ 抓不到证书：$(head -1 "$TMP/sc.err" | cut -c1-100)"
fi

# ── 汇总产出 ──────────────────────────────────────────────────
echo
echo "── 产出 ──"
: > "$TMP/final.pem"
[ -f "$TMP/from_store.pem" ] && cat "$TMP/from_store.pem" >> "$TMP/final.pem"
[ -f "$TMP/from_chain.pem" ] && cat "$TMP/from_chain.pem" >> "$TMP/final.pem"

if ! grep -q 'BEGIN CERTIFICATE' "$TMP/final.pem" 2>/dev/null; then
  echo "  ❌ 两条路都没拿到 CA。"
  echo
  echo "  还能怎么办："
  echo "    1. 直接问 IT 要「内网根 CA 证书」（PEM 格式，.crt）"
  echo "    2. 从同网段一台已配好的机器上取 /usr/local/share/ca-certificates/*.crt"
  echo "    3. 实在拿不到 → build_arm.sh --insecure（但 rust 那条仍会失败，cargo 没有该开关）"
  exit 1
fi

# 去重：路径 A 和 B 可能抓到同一张
awk '/-----BEGIN CERTIFICATE-----/{c++} {print > "'"$TMP"'/split_" c ".pem"}' "$TMP/final.pem" 2>/dev/null
: > "$OUT"
declare -A SEEN
for f in "$TMP"/split_*.pem; do
  [ -f "$f" ] && grep -q 'BEGIN CERTIFICATE' "$f" || continue
  fp=$(openssl x509 -in "$f" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
  [ -z "$fp" ] && continue
  [ -n "${SEEN[$fp]:-}" ] && continue
  SEEN[$fp]=1
  openssl x509 -in "$f" -outform PEM >> "$OUT" 2>/dev/null
done

N=$(grep -c 'BEGIN CERTIFICATE' "$OUT" 2>/dev/null || echo 0)
if [ "$N" -eq 0 ]; then echo "  ❌ 写出失败"; exit 1; fi
echo "  ✅ $OUT（$N 张证书）"
openssl crl2pkcs7 -nocrl -certfile "$OUT" 2>/dev/null | openssl pkcs7 -print_certs -noout 2>/dev/null \
  | grep '^subject' | sed 's/^subject=/     /' | head -10

# ── 验证：拿这份 CA 能不能真的握上手 ──────────────────────────
echo
echo "── 验证（用这份 CA 重试 HTTPS）──"
if curl -sS -o /dev/null --max-time 15 --cacert "$OUT" "https://$HOST" 2>"$TMP/v.err"; then
  echo "  ✅ 用它能验证通过 —— 可以直接喂给 build_arm.sh"
else
  echo "  ⚠️  用它仍验证失败：$(head -1 "$TMP/v.err" | cut -c1-120)"
  echo "     可能只抓到中间 CA 而缺根。仍可一试，或找 IT 要完整的根 CA。"
fi

echo
echo "下一步："
echo "  bash build_arm.sh --ca-cert $OUT python"
echo "  （若同时要代理： export HTTPS_PROXY=... 再跑，脚本会自动带上）"
