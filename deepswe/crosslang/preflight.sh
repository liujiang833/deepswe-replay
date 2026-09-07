#!/usr/bin/env bash
# 服务器环境预检：把 replay.py 依赖的每项能力都真跑一遍，而不是只看版本号。
#
# 之所以要活体测试而不是查配置：cgroup 路径、netns 共享、资源限额这三样在
# rootless docker / podman 兼容层 / gVisor / 嵌套容器下都可能"看着有、用起来没有"。
# 本脚本会真起容器、真读 cgroup、真建 sidecar，失败就当场报出来。
#
# 用法：  bash preflight.sh            # 用 bundle 里第一个镜像做活体测试
#         bash preflight.sh <镜像>     # 指定镜像

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0; WARN=0
ok()   { echo "  ✅ $*"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $*"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️  $*"; WARN=$((WARN+1)); }
hdr()  { echo; echo "── $* ────────────────────────────────────────────"; }

echo "=============================================================="
echo " DeepSWE 重放环境预检   $(date -u +%FT%TZ)"
echo " bundle: $HERE"
echo "=============================================================="

# ── 1. 基础环境 ────────────────────────────────────────────────
hdr "1. 基础环境"
echo "  内核      $(uname -r)"
echo "  发行版    $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo 未知)"
echo "  CPU       $(nproc) 核"
echo "  内存      $(free -g 2>/dev/null | awk '/^Mem:/{print $2" GB"}' || echo 未知)"

if command -v python3 >/dev/null 2>&1; then
  ok "python3 $(python3 -V 2>&1 | awk '{print $2}')（replay.py 只用标准库）"
else
  bad "没有 python3 —— replay.py / run_batch.py 都跑不了"
fi

# 重放会在 /app 里编译、往 /tmp 写产物；镜像本身也要落盘
AVAIL=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$AVAIL" ]; then
  if [ "$AVAIL" -lt 20 ]; then bad "根分区可用 ${AVAIL}GB —— 5 个镜像约 4-5GB，重放还要写 /tmp，建议 ≥20GB"
  elif [ "$AVAIL" -lt 50 ]; then warn "根分区可用 ${AVAIL}GB，够跑这 5 条，但扩到全量 113 个镜像（去重后 24GB）会不够"
  else ok "根分区可用 ${AVAIL}GB"; fi
fi

# ── 2. cgroup ──────────────────────────────────────────────────
hdr "2. cgroup（性能指标的唯一来源）"
FSTYPE=$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo 未知)
if [ "$FSTYPE" = "cgroup2fs" ]; then
  ok "/sys/fs/cgroup 是 cgroup2fs（v2 统一层级）"
else
  bad "/sys/fs/cgroup 是 $FSTYPE，不是 cgroup2fs —— cpu.stat/memory.current 口径只适用 v2"
fi
if [ -r /sys/fs/cgroup/cgroup.controllers ]; then
  echo "  可用控制器  $(cat /sys/fs/cgroup/cgroup.controllers)"
  for c in cpu memory io; do
    grep -qw "$c" /sys/fs/cgroup/cgroup.controllers \
      && ok "$c 控制器已启用" \
      || { [ "$c" = io ] && warn "io 控制器未启用 → rbytes/wbytes 记 0（不影响保真度结论）" \
                        || bad "$c 控制器未启用 → 对应指标采不到"; }
  done
fi
[ -e /sys/fs/cgroup/memory.peak ] \
  && ok "内核有 memory.peak（6.8+）" \
  || echo "  ·  无 memory.peak（<6.8 内核）→ 用 20ms 轮询 memory.current，与基线口径一致"

# ── 3. docker ──────────────────────────────────────────────────
hdr "3. docker"
if ! docker version >/dev/null 2>&1; then
  bad "docker 不可用（未装 / daemon 没起 / 当前用户不在 docker 组）"
  echo; echo "预检中止：docker 不可用，后面的活体测试无法进行。"; exit 1
fi
ok "docker $(docker version -f '{{.Server.Version}}' 2>/dev/null)"
echo "  cgroup driver   $(docker info -f '{{.CgroupDriver}}' 2>/dev/null)"
echo "  cgroup version  $(docker info -f '{{.CgroupVersion}}' 2>/dev/null)"
echo "  storage driver  $(docker info -f '{{.Driver}}' 2>/dev/null)"
if docker info -f '{{.SecurityOptions}}' 2>/dev/null | grep -q rootless; then
  warn "rootless docker —— cgroup 落在 user.slice 下，replay.py 有对应候选路径，但请留意活体测试结果"
fi

# ── 4. 镜像清单 ────────────────────────────────────────────────
hdr "4. bundle 里的 trial 与镜像"
IMAGES=$(python3 - "$HERE" <<'PY' 2>/dev/null
import json, pathlib, sys
for d in sorted(pathlib.Path(sys.argv[1]).iterdir()):
    m = d / "meta.json"
    if m.is_dir() or not m.exists():
        continue
    j = json.loads(m.read_text())
    print(f"{j.get('language','?')}\t{d.name}\t{(j.get('image') or {}).get('docker_image','')}")
PY
)
if [ -z "$IMAGES" ]; then
  bad "没找到任何 trial（本脚本应放在含 <trial>/meta.json 的目录里）"
else
  N_IMG=0; N_HIT=0
  while IFS=$'\t' read -r lang name img; do
    [ -z "$img" ] && continue
    N_IMG=$((N_IMG+1))
    if docker image inspect "$img" >/dev/null 2>&1; then
      SZ=$(docker image inspect "$img" -f '{{.Size}}' 2>/dev/null)
      ok "$(printf '%-12s' "[$lang]")已就位（$((SZ/1024/1024)) MB）"
      N_HIT=$((N_HIT+1)); FIRST_IMG="${FIRST_IMG:-$img}"
    else
      bad "$(printf '%-12s' "[$lang]")缺失: $img"
    fi
  done <<< "$IMAGES"
  echo "  → $N_HIT/$N_IMG 个镜像就位"
fi

# ── 5. 活体测试 ────────────────────────────────────────────────
hdr "5. 活体测试（真起容器，验证 replay.py 依赖的每项能力）"
IMG="${1:-${FIRST_IMG:-}}"
if [ -z "$IMG" ]; then
  warn "没有可用镜像，跳过活体测试"
else
  echo "  用镜像 $IMG"
  C="preflight_probe_$$"; S="${C}-sink"
  docker rm -f "$C" "$S" >/dev/null 2>&1
  # 与 replay.py 完全相同的启动参数
  if docker run -d --name "$C" --cpus=2 --memory=8192m --memory-swap=8192m \
       --network=none "$IMG" sleep 120 >/dev/null 2>&1; then
    ok "容器可启动（--cpus=2 --memory=8192m --network=none）"

    CID=$(docker inspect -f '{{.Id}}' "$C")
    PID=$(docker inspect -f '{{.State.Pid}}' "$C")
    # 与 replay.py 同一套三级探测
    CGDIR=""
    # 与 replay.py 同一套三级探测，含同一条防假阳性的复核：
    # /proc 读到的路径必须能认出容器 ID，否则就是别人的 cgroup。
    # （WSL 实测过：.State.Pid 在宿主 /proc 里对上了另一个进程，读出 init.scope）
    if [ -n "$PID" ] && [ -r "/proc/$PID/cgroup" ]; then
      REL=$(sed -n 's/^0:://p' "/proc/$PID/cgroup" | head -1)
      case "$REL" in
        *"${CID:0:12}"*) [ -e "/sys/fs/cgroup${REL}/cpu.stat" ] \
            && CGDIR="/sys/fs/cgroup${REL}" && HOW="/proc/<pid>/cgroup" ;;
      esac
    fi
    if [ -z "$CGDIR" ]; then
      for c in "/sys/fs/cgroup/docker/$CID" \
               "/sys/fs/cgroup/system.slice/docker-$CID.scope" \
               "/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/user.slice/docker-$CID.scope"; do
        [ -e "$c/cpu.stat" ] && CGDIR="$c" && HOW="已知 driver 候选路径" && break
      done
    fi
    if [ -z "$CGDIR" ]; then
      CGDIR=$(find /sys/fs/cgroup -maxdepth 6 -type d -name "*$CID*" -print -quit 2>/dev/null)
      [ -n "$CGDIR" ] && HOW="cgroup 树搜索"
    fi
    if [ -n "$CGDIR" ] && [ -r "$CGDIR/cpu.stat" ]; then
      ok "cgroup 可读：$CGDIR"
      echo "     （探测方式：$HOW）"
      grep -q usage_usec "$CGDIR/cpu.stat" && ok "cpu.stat 有 usage_usec" || bad "cpu.stat 缺 usage_usec"
      [ -r "$CGDIR/memory.current" ] && ok "memory.current 可读" || bad "memory.current 不可读"
      [ -r "$CGDIR/io.stat" ] && ok "io.stat 可读" || warn "io.stat 不可读 → rbytes/wbytes 记 0"
    else
      bad "找不到容器 cgroup 目录 —— 性能指标会采不到（replay.py 会明确报错退出，不会静默写 0）"
    fi

    # 资源限额是否真生效（不生效则跨机数字不可比）
    QUOTA=$(docker exec "$C" cat /sys/fs/cgroup/cpu.max 2>/dev/null)
    [ "${QUOTA%% *}" = "200000" ] && ok "CPU 限额生效（cpu.max=$QUOTA → 2 核）" \
                                  || warn "容器内 cpu.max=$QUOTA（期望 '200000 100000'）"
    MMAX=$(docker exec "$C" cat /sys/fs/cgroup/memory.max 2>/dev/null)
    [ "$MMAX" = "8589934592" ] && ok "内存限额生效（8192 MB）" \
                               || warn "容器内 memory.max=$MMAX（期望 8589934592）"

    # 镜像内必须有的东西：replay.py 用容器内的 timeout 控制单命令超时，
    # sinkhole sidecar 用容器内的 python3，保真校验用容器内的 git
    for b in timeout python3 git sh; do
      docker exec "$C" sh -c "command -v $b" >/dev/null 2>&1 \
        && ok "镜像内有 $b" || bad "镜像内没有 $b —— replay.py 依赖它"
    done
    docker exec "$C" sh -c 'echo ok' >/dev/null 2>&1 \
      && ok "/bin/sh -c 执行器可用（对齐原 harness 的 dash 口径）" \
      || bad "/bin/sh -c 不可用"
    docker exec -w /app "$C" git rev-parse --show-toplevel >/dev/null 2>&1 \
      && ok "/app 是 git 仓库（保真校验 git diff 的前提）" \
      || bad "/app 不是 git 仓库 —— patch_identical 校验做不了"

    # sidecar 共享 netns：403 sinkhole 的实现基础
    if docker run -d --name "$S" --network=container:"$C" --memory=256m \
         "$IMG" python3 -c "
import socket,threading,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('127.0.0.1',3128)); s.listen(8); sys.stderr.write('up\n'); sys.stderr.flush()
while True:
    c,_=s.accept(); c.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n'); c.close()
" >/dev/null 2>&1; then
      HIT=0
      for _ in $(seq 40); do
        docker exec "$C" python3 -c "
import socket; socket.create_connection(('127.0.0.1',3128),2).close()" >/dev/null 2>&1 \
          && { HIT=1; break; }
        sleep 0.25
      done
      [ "$HIT" = 1 ] && ok "sidecar 共享 netns 可用（403 sinkhole 的实现基础）" \
                     || bad "被测容器连不上 sidecar 的 127.0.0.1:3128 —— sinkhole 起不来，只能退回 --net-mode=none"
    else
      bad "sidecar 容器（--network=container:）启不来 —— 只能退回 --net-mode=none"
    fi
    docker exec "$C" sh -c 'command -v curl >/dev/null && curl -sI -m 3 https://github.com >/dev/null 2>&1' \
      && bad "容器竟能连外网 —— --network=none 没生效，重放会做原始运行没做过的功" \
      || ok "容器确实无外网（--network=none 生效）"
  else
    bad "容器启动失败 —— 检查镜像可用性与 docker 权限"
  fi
  docker rm -f "$C" "$S" >/dev/null 2>&1
fi

# ── 汇总 ───────────────────────────────────────────────────────
echo
echo "=============================================================="
echo " 预检结果：$PASS 通过 / $WARN 警告 / $FAIL 失败"
echo "=============================================================="
if [ "$FAIL" -gt 0 ]; then
  echo " ❌ 有失败项，先解决再跑重放。"
  exit 1
fi
[ "$WARN" -gt 0 ] && echo " ⚠️  有警告，可以跑，但请确认警告不影响你要的结论。"
echo " ✅ 可以开跑：  python3 run_batch.py"
echo "    先冒烟：    python3 run_batch.py --smoke 5"
exit 0
