#!/usr/bin/env bash
# 检测公司内网是否对 HTTPS 做 TLS 中间人，并指出内网 CA 在哪。
#
# 判据：看各站点递过来的**叶子证书的签发者**。是公共 CA 就没被劫持；
# 是别的东西（公司名/Zscaler/Netskope/...）就是中间人在重签。
#
# 为什么探多个站点：企业代理常对部分域名放行（金融/医疗、或白名单），
# 只探 github 可能得到「没劫持」的假象，而 pypi/npm 其实是被劫的。
#
# 用法：  bash detect_mitm.sh                    # 默认 4 个站点
#         bash detect_mitm.sh pypi.org           # 指定站点
#         export HTTPS_PROXY=... 后再跑，会自动带上 -proxy
#
# 拿到证书用 get_ca_cert.sh；A 段非空时优先用 A（无 TOFU 问题）。
HOSTS="${*:-github.com pypi.org registry.npmjs.org static.crates.io}"
P="${HTTPS_PROXY:-${https_proxy:-}}"
[ -n "$P" ] && { PP="${P#*://}"; PP="${PP#*@}"; PP="${PP%/}"; PX=(-proxy "$PP"); } || PX=()

echo "代理  ${P:-（未设置）}"
echo
echo "── A. 系统信任库里本地添加的 CA（有的话，这就是内网 CA）──"
found=0
for d in /usr/local/share/ca-certificates /etc/pki/ca-trust/source/anchors \
         /etc/ca-certificates/trust-source/anchors; do
  for f in "$d"/*.crt "$d"/*.pem "$d"/*.cer; do
    [ -f "$f" ] && grep -q 'BEGIN CERT' "$f" 2>/dev/null || continue
    echo "  ✅ $f"
    echo "     $(openssl x509 -in "$f" -noout -subject | sed 's/^subject=//')"
    found=1
  done
done
[ "$found" = 0 ] && echo "  （空）"

echo
echo "── B. 各站点实际递过来的签发者 ──"
for H in $HOSTS; do
  iss=$(echo | timeout 20 openssl s_client -connect "$H:443" -servername "$H" "${PX[@]}" 2>/dev/null \
        | openssl x509 -noout -issuer 2>/dev/null | sed 's/^issuer=//')
  if [ -z "$iss" ]; then echo "  $H  ❌ 握手失败/不可达"; continue; fi
  case "$iss" in
    *DigiCert*|*"Let's Encrypt"*|*Sectigo*|*GlobalSign*|*Amazon*|*Google*|*ISRG*|*COMODO*|*USERTrust*|*Entrust*|*GoDaddy*)
      echo "  $H  ⭕ 公共 CA  ← 未被劫持" ;;
    *)
      echo "  $H  ⚠️  中间人  ← $iss" ;;
  esac
done
