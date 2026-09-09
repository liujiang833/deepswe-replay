#!/usr/bin/env bash
# 服务器环境预检：把 replay.py 依赖的每项能力都真跑一遍，而不是只看版本号。
#
# 之所以要活体测试而不是查配置：cgroup 路径、netns 共享、资源限额这三样在
# rootless docker / podman 兼容层 / gVisor / 嵌套容器下都可能"看着有、用起来没有"。
# 本脚本会真起容器、真读 cgroup、真建 sidecar，失败就当场报出来。
#
# ⚠️ 先建镜像再跑本脚本：活体测试要真起一个容器，手里一个镜像都没有时整段会跳过，
#    等于什么都没验到。包里不带镜像，所以顺序是 build_arm.sh → preflight.sh。
#
# 用法：  bash preflight.sh            # 用 bundle 里第一个已建好的镜像做活体测试
#         bash preflight.sh <镜像>     # 指定镜像
#         bash preflight.sh --metrics  # 连 cgroup 性能指标一起验（默认不验）

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --metrics 与 run_batch.py 的 need_cgroup 门控是同一个口径：只跑重放 + 保真校验
# 时压根不碰 cgroup，所以默认就不该因为 cgroup 读不到而把预检判失败——rootless
# docker / cgroup v1 / 嵌套容器的机器跑重放没问题，不能被挡在门外。
METRICS=0
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --metrics) METRICS=1; shift ;;
    -h|--help) echo "用法: bash preflight.sh [--metrics] [<镜像>]"; exit 0 ;;
    -*) echo "未知参数: $1（本脚本只有 --metrics）"; exit 1 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- ${ARGS[@]+"${ARGS[@]}"}
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
  if [ "$AVAIL" -lt 20 ]; then bad "根分区可用 ${AVAIL}GB —— 光基座就约 5GB，加几条 task 镜像和 /tmp 产物就满，建议 ≥20GB"
  elif [ "$AVAIL" -lt 50 ]; then warn "根分区可用 ${AVAIL}GB，够先建一批边建边跑，但全量 113 个镜像（共享基座后约 25~35GB）会不够"
  else ok "根分区可用 ${AVAIL}GB"; fi
fi

# ── 2. cgroup ──────────────────────────────────────────────────
hdr "2. cgroup（性能指标的唯一来源）"
FSTYPE=$(stat -fc %T /sys/fs/cgroup 2>/dev/null || echo 未知)
if [ "$METRICS" = 0 ]; then
  # 不采指标就不碰 cgroup，所以这里连查都不查——查了也只能是噪声，
  # 更不该判失败。要采指标时加 --metrics，下面那套硬校验才会回来。
  echo "  /sys/fs/cgroup   $FSTYPE"
  echo "  ·  未加 --metrics → 本轮不采性能指标，整节跳过判定"
  echo "     （口径与 run_batch.py 一致：cgroup v2 只在 --metrics 下才是硬要求，"
  echo "       rootless docker / cgroup v1 的机器照样能跑重放与保真校验）"
else
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
fi

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

# 代理：构建期需要、运行期绝不能有。两者来源不同，要分开看。
if [ -n "${HTTPS_PROXY:-${https_proxy:-}}" ]; then
  echo "  shell 代理      $(printf '%s' "${HTTPS_PROXY:-$https_proxy}" | sed -E 's#(//)[^/@]*@#\1***@#')"
  echo "                  （仅 build_arm.sh 会读它并显式 --build-arg 传给构建；"
  echo "                    docker build/run 都不会自动继承 shell 变量）"
fi
# 这个才是隐患：config.json 里的 proxies 会被 docker **自动注入到每个 docker run**，
# 包括 replay.py 起的重放容器，从而和 403 sinkhole 抢同一批环境变量。
DOCKER_CFG="${DOCKER_CONFIG:-$HOME/.docker}/config.json"
if [ -f "$DOCKER_CFG" ] && grep -q '"proxies"' "$DOCKER_CFG" 2>/dev/null; then
  warn "$DOCKER_CFG 里配了 proxies —— docker run 会自动把它注入**运行期**容器。
       replay.py 已显式覆盖 HTTP(S)_PROXY 与 NO_PROXY 指向 sinkhole，正常能压住；
       但若重放时看到外连报错不是 403 而是连接失败/超时，先来查这里。"
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
  N_IMG=0; N_HIT=0; MISS=()
  while IFS=$'\t' read -r lang name img; do
    [ -z "$img" ] && continue
    N_IMG=$((N_IMG+1))
    if docker image inspect "$img" >/dev/null 2>&1; then
      SZ=$(docker image inspect "$img" -f '{{.Size}}' 2>/dev/null)
      ok "$(printf '%-12s' "[$lang]")已就位（$((SZ/1024/1024)) MB）"
      N_HIT=$((N_HIT+1)); FIRST_IMG="${FIRST_IMG:-$img}"
    else
      # 缺镜像不判失败：113 个镜像不可能一次建齐，**边建边跑是设计上的正常流程**
      # （run_batch.py 的 --skip-missing 就是为它准备的）。判失败的话新机器上
      # 113 个全缺 → 预检必然 exit 1，反而把它真正的价值（活体测试）挡在门外。
      MISS+=("[$lang] $img")
    fi
  done <<< "$IMAGES"
  echo "  → $N_HIT/$N_IMG 个镜像就位"
  if [ "${#MISS[@]}" -gt 0 ]; then
    # 只列前 5 条：全量集下缺几十上百个是常态，全打出来会把真正的问题冲没
    warn "还缺 ${#MISS[@]} 个镜像 —— 不算失败，用 build_arm.sh 建、run_batch.py --skip-missing 跳过没建好的"
    printf '       %s\n' "${MISS[@]:0:5}"
    [ "${#MISS[@]}" -gt 5 ] && echo "       …… 另有 $(( ${#MISS[@]} - 5 )) 个"
  fi
  if [ "$N_HIT" = 0 ]; then
    echo
    echo "  ⛔ 一个镜像都没有 —— 下面的活体测试会整段跳过，等于什么都没验到。"
    echo "     先建至少一条再回来跑预检：  bash build_arm.sh python"
    echo "     （预检必须在有镜像之后跑，这是它与 check_sources.sh 的分工："
    echo "       建之前探包源用 check_sources.sh，建之后验能力用本脚本）"
  fi
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

    if [ "$METRICS" = 0 ]; then
      # 同上：不采指标就不需要宿主侧能读到容器 cgroup，探它只会制造假警报
      echo "  ·  未加 --metrics → 跳过容器 cgroup 目录探测（重放本身不读它）"
    else
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
    fi   # /--metrics

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
# 一律带 --skip-missing：边建边跑时镜像本来就没齐，不加它 run_batch.py 会整批
# 拒绝启动——这段是要被照抄的，不能给一条抄了就失败的命令。
echo " ✅ 可以开跑：  python3 run_batch.py --skip-missing --keep-going"
echo "    先冒烟：    python3 run_batch.py --smoke 5 --skip-missing"
echo "    （--skip-missing：镜像还没建好的自动跳过，而不是整批拒绝启动）"
exit 0
