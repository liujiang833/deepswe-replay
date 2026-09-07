#!/usr/bin/env bash
# 在目标机上跑：探测「从 mars-base 重建这 5 个 task 镜像」所需的每一个上游源。
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
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -L "$url" 2>/dev/null || echo 000)
  if [ "$code" = "200" ] || [ "$code" = "206" ]; then
    printf '  ✅ %-26s %s   (%s)\n' "$label" "$code" "$who"; OK=$((OK+1))
  else
    printf '  ❌ %-26s %s   (%s)\n' "$label" "$code" "$who"; NO=$((NO+1)); MISSING+=("$label → $who")
  fi
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
# 基座必须已在本地：它是 5 个 task 镜像的 FROM
for tag in mars-base:arm64 mars-base:latest public.ecr.aws/x8v8d7g8/mars-base:latest; do
  if docker image inspect "$tag" >/dev/null 2>&1; then
    a=$(docker image inspect "$tag" -f '{{.Architecture}}')
    s=$(docker image inspect "$tag" -f '{{.Size}}')
    echo "  基座        ✅ $tag  ($a, $((s/1024/1024)) MB)"
    BASE_TAG="$tag"; BASE_ARCH="$a"
  fi
done
[ -n "${BASE_TAG:-}" ] || echo "  基座        ❌ 本地没有 mars-base（重建 task 镜像的 FROM）"
if [ -n "${BASE_ARCH:-}" ] && [ "$BASE_ARCH" = "arm64" ] && [ "$ARCH" != "aarch64" ]; then
  echo "  ⚠️  基座是 arm64 但本机是 $ARCH —— 没有 qemu binfmt 就跑不起来"
fi

echo
echo "── 1. github.com：5/5 都需要（git clone + 问默认分支）──"
probe "github git-upload-pack" \
  "https://github.com/dry-python/returns/info/refs?service=git-upload-pack" "全部 5 条"
probe "codeload(打包下载)" \
  "https://codeload.github.com/yjs/yjs/tar.gz/refs/heads/main" "submodule / release 资产"

echo
echo "── 2. python：returns 那条需要 ────────────────────"
probe "pypi.org/simple" "https://pypi.org/simple/anyio/" "python"
probe "files.pythonhosted.org" "https://files.pythonhosted.org/" "python(取 wheel)"

echo
echo "── 3. go：actionlint 那条需要 ─────────────────────"
probe "proxy.golang.org" \
  "https://proxy.golang.org/github.com/ctrf-io/go-ctrf-json-reporter/@v/list" "go"
probe "sum.golang.org" \
  "https://sum.golang.org/lookup/github.com/ctrf-io/go-ctrf-json-reporter@v0.1.0" "go(校验)"

echo
echo "── 4. npm：true-myth / yjs / fd 的报告器都需要 ─────"
probe "registry.npmjs.org" "https://registry.npmjs.org/junit-to-ctrf" "ts, js, rust(报告器)"

echo
echo "── 5. rust：fd 那条需要 ───────────────────────────"
probe "index.crates.io" "https://index.crates.io/config.json" "rust"
probe "static.crates.io" "https://static.crates.io/crates/libc/libc-0.2.155.crate" "rust(取 crate)"
# nextest 的下载 URL 按架构分叉，原 Dockerfile 写死的是 x86_64 那个
probe "get.nexte.st (x86_64)" "https://get.nexte.st/0.9.97/linux" "rust(原 Dockerfile 写死这个)"
probe "get.nexte.st (aarch64)" "https://get.nexte.st/0.9.97/linux-arm" "rust(ARM 上要换成这个)"

echo
echo "── 6. debian apt：这 5 条都不需要，但全量 113 条里有 5 条要 ──"
probe "deb.debian.org" "https://deb.debian.org/debian/dists/stable/Release" "全量里的 5 条"

echo
echo "=============================================================="
echo " 通 $OK / 不通 $NO"
echo "=============================================================="
if [ "$NO" -gt 0 ]; then
  echo " 不通的："
  printf '   - %s\n' "${MISSING[@]}"
fi
echo
echo " 按源需求，各条 task 的可建性："
echo "   python (returns)    需要 github + pypi                          ← 依赖最少，先建这条"
echo "   go (actionlint)     需要 github + proxy.golang.org + sum.golang.org"
echo "   js (yjs)            需要 github + npmjs"
echo "   ts (true-myth)      需要 github + npmjs"
echo "   rust (fd)           需要 github + crates.io + get.nexte.st + npmjs  ← 最难，最后建"
echo
echo " 下一步：  bash build_arm.sh --list        # 看会怎么改写 Dockerfile"
echo "           bash build_arm.sh python       # 建最容易的那条"
