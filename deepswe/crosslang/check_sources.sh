#!/usr/bin/env bash
# 在目标机上跑：探测「从 mars-base 重建这 113 个 task 镜像」所需的每一个上游源。
#
# 为什么要先探而不是直接 build：探测几秒，build 最慢的一条（rust 的
# `cargo nextest --no-run` 全量编译）可能几十分钟。先知道哪些源通，
# 才能决定建哪几条、按什么顺序建。
#
# 为什么不能只 ping：DeepSWE 原环境的 allow_internet=false 就是靠 squid 代理
# 返 403 实现的——TCP 通、DNS 通，但 HTTP 被拒。所以这里一律打**真实端点**
# 看 HTTP 状态码，而不是看能不能连上。

set -uo pipefail
OK=0; NO=0
declare -a MISSING

probe() {  # probe <标签> <URL> <谁需要它>
  local label="$1" url="$2" who="$3" code
  # --range 0-0 只要首字节：这些端点里有几个是几 MB 的二进制（.crate、nextest tar），
  # 整包拉会撞 --max-time 变成假阴性（表现为状态码拼成 "200000" 这种）。
  # tail -c 3 再兜一层：跟随重定向时 curl 可能对多个 hop 各写一次 %{http_code}。
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -L --range 0-0 "$url" 2>/dev/null | tail -c 3)
  case "$code" in
    200|206) printf '  ✅ %-26s %s   (%s)\n' "$label" "$code" "$who"; OK=$((OK+1)) ;;
    # 部分 CDN 不认 Range，退回整包但只等 8 秒，够判断"通不通"
    *) code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 -L "$url" 2>/dev/null | tail -c 3)
       if [ "$code" = "200" ] || [ "$code" = "206" ]; then
         printf '  ✅ %-26s %s   (%s)\n' "$label" "$code" "$who"; OK=$((OK+1))
       else
         printf '  ❌ %-26s %-6s (%s)\n' "$label" "${code:-000}" "$who"
         NO=$((NO+1)); MISSING+=("$label → $who")
       fi ;;
  esac
}

echo "=============================================================="
echo " 构建期上游源连通性探测   $(date -u +%FT%TZ)"
echo "=============================================================="
echo
echo "── 0. 本机 ──────────────────────────────────────────"
ARCH=$(uname -m)
echo "  架构        $ARCH"
echo "  内核        $(uname -r)"
if docker version >/dev/null 2>&1; then
  echo "  docker      $(docker version -f '{{.Server.Version}}' 2>/dev/null)"
else
  echo "  docker      ❌ 不可用"
fi
# 基座必须已在本地：它是 113 个 task 镜像的 FROM
# 候选顺序与 build_arm.sh 保持一致，命中即停——否则两个脚本可能选到不同的基座
for tag in mars-base:arm64 mars-base:latest public.ecr.aws/x8v8d7g8/mars-base:latest; do
  if docker image inspect "$tag" >/dev/null 2>&1; then
    a=$(docker image inspect "$tag" -f '{{.Architecture}}')
    s=$(docker image inspect "$tag" -f '{{.Size}}')
    echo "  基座        ✅ $tag  ($a, $((s/1024/1024)) MB)"
    BASE_TAG="$tag"; BASE_ARCH="$a"
    break
  fi
done
[ -n "${BASE_TAG:-}" ] || echo "  基座        ❌ 本地没有 mars-base（重建 task 镜像的 FROM）"
if [ -n "${BASE_ARCH:-}" ] && [ "$BASE_ARCH" = "arm64" ] && [ "$ARCH" != "aarch64" ]; then
  echo "  ⚠️  基座是 arm64 但本机是 $ARCH —— 没有 qemu binfmt 就跑不起来"
fi

echo
echo "── 1. github.com：113/113 都需要（git clone + 问默认分支）──"
probe "github git-upload-pack" \
  "https://github.com/dry-python/returns/info/refs?service=git-upload-pack" "全部 113 条"
probe "codeload(打包下载)" \
  "https://codeload.github.com/yjs/yjs/tar.gz/refs/heads/main" "submodule / release 资产"
# release 资产不落在 github.com 上：请求会 302 到 *.githubusercontent.com。
# 企业代理常按域名放行，所以上一条通**不代表**这条通，单独打一次真实产物。
probe "github release 下载域" \
  "https://github.com/denoland/deno/releases/download/v2.0.0/deno-aarch64-unknown-linux-gnu.zip" \
  "cliffy(下 deno 二进制)"

echo
echo "── 2. python：34 条 python 需要 ───────────────────"
probe "pypi.org/simple" "https://pypi.org/simple/anyio/" "python"
probe "files.pythonhosted.org" "https://files.pythonhosted.org/" "python(取 wheel)"

echo
echo "── 3. go：34 条 go 需要 ───────────────────────────"
probe "proxy.golang.org" \
  "https://proxy.golang.org/github.com/ctrf-io/go-ctrf-json-reporter/@v/list" "go"
probe "sum.golang.org" \
  "https://sum.golang.org/lookup/github.com/ctrf-io/go-ctrf-json-reporter@v0.1.0" "go(校验)"

echo
echo "── 4. npm：ts/js 全部 40 条 + 各语言的报告器 ───────"
probe "registry.npmjs.org" "https://registry.npmjs.org/junit-to-ctrf" "ts, js, rust(报告器)"

echo
echo "── 5. rust：5 条 rust 需要 ────────────────────────"
probe "index.crates.io" "https://index.crates.io/config.json" "rust"
probe "static.crates.io" "https://static.crates.io/crates/libc/libc-0.2.155.crate" "rust(取 crate)"
# nextest 的下载 URL 按架构分叉，5 条 rust 的 Dockerfile 都写死了 x86_64 那个
probe "get.nexte.st (x86_64)" "https://get.nexte.st/0.9.97/linux" "rust(原 Dockerfile 写死这个)"
probe "get.nexte.st (aarch64)" "https://get.nexte.st/0.9.97/linux-arm" "rust(ARM 上要换成这个)"

echo
echo "── 6. deno：cliffy 那条需要 ───────────────────────"
# 二进制走上面那条 github release；这里是 deno cache 要取的模块源
# （jsr:@std/* 走 jsr.io，npm:sinon / npm:@types/node 走上面的 npmjs）
probe "jsr.io" "https://jsr.io/@std/assert/meta.json" "cliffy(deno cache jsr:@std/*)"

echo
echo "── 7. apt：装系统包的 5 条需要 ────────────────────"
probe "deb.debian.org" "https://deb.debian.org/debian/dists/stable/Release" "装系统包的那 5 条"
# 下面三个只有 eicrud 那条要，但缺一个它就整条建不成
probe "deb.nodesource.com" "https://deb.nodesource.com/setup_22.x" "eicrud(装 node 22)"
probe "www.mongodb.org" "https://www.mongodb.org/static/pgp/server-7.0.asc" "eicrud(mongodb 签名密钥)"
# 真实 URL 里的发行版与代号是构建期用 /etc/os-release + lsb_release 现算的，
# 这里固定探 ubuntu/jammy —— 目的是判主机与路径前缀通不通，不是核对代号
probe "repo.mongodb.org" \
  "https://repo.mongodb.org/apt/ubuntu/dists/jammy/mongodb-org/7.0/Release" "eicrud(装 mongodb-org)"

echo
echo "=============================================================="
echo " 通 $OK / 不通 $NO"
echo "=============================================================="
if [ "$NO" -gt 0 ]; then
  echo " 不通的："
  printf '   - %s\n' "${MISSING[@]}"
fi
echo
echo " 按源需求，各语言的可建性（113 条按这个顺序建最省事）："
echo "   python      34 条   github + pypi                              ← 依赖最少，先建"
echo "   go          34 条   github + proxy.golang.org + sum.golang.org"
echo "   javascript   5 条   github + npmjs"
echo "   typescript  35 条   github + npmjs"
echo "                       （eicrud 那条另需 apt + nodesource + mongodb；"
echo "                         cliffy 那条另需 github release 下载域 + jsr.io）"
echo "   rust         5 条   github + crates.io + get.nexte.st + npmjs  ← 最难，最后建"
echo
echo " 上面某个源不通，只挡掉用它的那几条，其余照常建 —— 不必等全绿再开工"
echo " （run_batch.py 的 --skip-missing 支持边建边跑）。只有 github 不通才是真的没法开始。"
echo
echo " 下一步：  bash build_arm.sh --list        # 看会怎么改写 Dockerfile"
echo "           bash build_arm.sh python       # 先建依赖最少的那批"
