# 离线重放包设计：目标环境无网络，只能传压缩包

**约束**：目标机**没有网络**，不能 `docker pull` / `git clone` / `pip install` / `go mod download`。
唯一的输入通道是**一个压缩包**。

**结论先行**：可行，且不需要在目标机上"构建"任何东西。
构建/采集全部在联网侧完成，目标机只做"解包 + 装载 + 重放"。

---

## 1. 先把两件事分开

| | 需要网络吗 | 说明 |
|---|---|---|
| **provisioning**（拿镜像、拿 trace、拿 task 定义） | **需要** | 必须在联网侧做完 |
| **replay 执行本身** | **不需要** | 重放本来就是 `--network=none`（对齐 task.toml 的 `allow_internet=false`） |

**气隙不影响执行，只影响准备。** 而且有个额外好处：trace 里第 80 条 `pip install --dry-run`
在原始运行中就是因为无网而失败的——目标机无网时行为**天然一致**，不用刻意模拟。

## 2. 目标机的最小依赖

`replay.py` / `summarize_replay.py` 实测**只 import 标准库**
（argparse / json / pathlib / re / subprocess / threading / time / datetime / math），**零 pip 依赖**。

所以目标机需要：

| 项 | 硬性 | 备注 |
|---|---|---|
| `python3`（≥3.9） | **硬** | 只用标准库 |
| 容器运行时 | 二选一 | Docker，或走 `NODOCKER_REQUIREMENTS.md` 的 unshare/chroot 路径 |
| cgroup v2 | **硬** | 限流 + 计量的地基 |
| 磁盘 | **硬** | 见 §5 |
| **网络** | **不需要** | — |
| 编译器 / 包管理器 | **不需要** | 镜像里已经装好，不重建 |

**verifier 镜像不需要传**：硬校验是 `git diff base..HEAD` 与 model.patch 逐字节比对
（`pre_artifacts.sh` 定义的口径），不需要跑测试，所以 `tests/` 那套不进包。

## 3. 包里装什么

```
deepswe-bundle-<arch>-<date>/
  BUNDLE.json                    清单：arch、生成时间、含哪些 task/trial、全部 blob 的 digest+size
  blobs/sha256/<64位digest>      内容寻址的镜像层（registry 原样 gzip blob，不二次压缩）
  images/<task_id>.json          该 task 镜像的 OCI manifest + config digest
  tasks/<task_id>.json           任务定义（含 task.toml：base_commit_hash / cpus / memory_mb）
  trials/<trial_name>/
      trajectory.json            命令序列 + 每条的 trace 侧 returncode
      model.patch                硬校验基准
  code/replay.py                 纯 stdlib
  code/summarize_replay.py       纯 stdlib
  code/load_bundle.py            目标侧装载器（纯 stdlib）
  SHA256SUMS
```

### 为什么用内容寻址的 blob 目录，而不是直接 `docker save`

| | `docker save` 一次多镜像 + zstd | blobs/ 内容寻址目录（推荐） |
|---|---|---|
| 层去重 | 有（同一次调用内） | 有（跨包、跨批次都有） |
| 断点续传 | 无（一个大 tar） | **有**（每个 blob 独立） |
| 增量投递 | 无（每次全量） | **有**（带 `--have <已有digest清单>` 只出增量） |
| 完整性校验 | 靠外层 SHA256SUMS | **每个 blob 自校验**（文件名就是 sha256） |
| 获取方式 | 需先 `docker pull` 到本地 | **纯 HTTP GET 即可**（已实测可行，无鉴权） |
| 目标侧消费 | `docker load` | 装载器转 docker-load 流，或直接解包成 rootfs |
| 自研成本 | 0 | 约 150 行装载器 |

**关键理由是断点续传与增量**：24.3 GB 在不稳定链路上一次拉完风险高；
而且内容寻址让"第二批 task"只需要传新增 blob——113 个镜像共享 21 层 / 约 721 MB 基座，
第二个包起就不用再带它。

## 4. 目标侧装载：一个包同时服务两条路

```bash
# 路径 1：有 Docker
python3 code/load_bundle.py --docker <task_id>...     # 组 docker-load 流，边解 gzip 边喂
    # 内部等价于：构造 legacy save 格式 tar 写 stdout | docker load

# 路径 2：无 Docker（配合 NODOCKER_REQUIREMENTS.md）
python3 code/load_bundle.py --rootfs <task_id> -o /srv/rootfs/<task_id>
    # 按 manifest 顺序解各层，处理 whiteout（.wh.* / opaque dir）
```

两条路读的是**同一份 blobs/**，不需要出两个包。

装载器还要从 image config blob 里取出 `Env` / `WorkingDir`，
因为 `bash -lc` 依赖 PATH（python3 与 pytest 在 `/usr/local/bin`）——
Docker 路径由 image config 自动带上，rootfs 路径必须显式写进 chroot 的环境。

## 5. 体量与磁盘

以 113 个 task 全量、每 task 3 条 trial 计：

| 组成 | 大小 | 说明 |
|---|---|---|
| blobs/（镜像层，去重后） | **24.3 GB** | 实测：朴素求和 111.1 GB，去重率 78% |
| tasks/（113 个定义） | 31 MB | 已在本地 |
| trials/（339 × trajectory+model.patch） | 约 580 MB | 单条实测 1.7 MB |
| code/ | < 1 MB | |
| **包总计** | **约 25 GB** | |

目标机磁盘：
- 包本身 25 GB
- `docker load` 后镜像占用约 **76 GB**（按实测 840 MB 压缩 → 2.65 GB 解压，比 3.15×）
- 重放产物（每条 trial 的 commands.jsonl 约 1 MB）可忽略
- **合计约 100 GB**

只做单 task 试点的话：blobs 约 800 MB + trace 2 MB ≈ **不到 1 GB**。

## 6. ARM 的额外一步

包的格式与流程完全相同，**唯一区别是 blobs 从哪来**：

- amd64：113 个镜像已发布在 public.ecr.aws，直接 HTTP 拉（已实测）
- arm64：**没有现成镜像**，必须在一台**联网的 ARM builder**上按 task Dockerfile 构建，
  再把构建结果导出成同样的 blobs/ 结构

也就是说：**无网约束不改变 ARM 的可行性判断，但它把"必须有一台联网 ARM 构建机"这条前提变成了硬门槛。**
如果完全拿不到联网的 ARM 机器，ARM 路线只剩 QEMU 模拟（联网 x86 上 buildx 构建 arm64 包），
而 36 个 go task / 5 个 rust task 有构建期编译，模拟下会慢一个量级。

## 7. 推进顺序

1. **趁现在有网，先把 blobs 拉全**（24.3 GB）。下载可逆、筛选不可逆，
   而且一旦断网就没有第二次机会。同时拉 trace（每条 1.7 MB）。
2. **出一个单 task 试点包**（< 1 GB），端到端验证"解包 → 装载 → 重放 → 硬校验通过"。
   这一步不过，全量包没有意义。
3. **验证 `load_bundle.py --rootfs`**，确认无 Docker 路径也能走通。
4. 出全量包。

## 8. 待验证项

- `docker load` 对"边解 gzip 边喂的 legacy save 格式流"的兼容性（不同 Docker 版本行为可能不同）；
  退路是先落盘成未压缩 tar 再 load，代价是临时占 76 GB
- 27 层里是否真有 whiteout 文件，rootfs 解包路径需要按实际情况处理
- 目标机的 Docker 版本 / cgroup 驱动 / 是否有 root —— 这三项决定走哪条装载路径，需要你确认
