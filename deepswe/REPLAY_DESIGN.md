# DeepSWE trace 容器侧负载重放：流程设计

**目标**：从 agent trace 复现**容器侧真实 CPU/IO 负载**，且负载要**包含 agent 的探索过程**
（反复 grep/读文件/改代码/跑测试/失败重试），而不只是"打补丁跑测试"那段干净负载。

**非目标**：复现 LLM 推理负载；复现 agent 的决策过程（不需要 LLM 参与）。

---

## 1. 可行性前提（已在 `gql-incremental-graphql-delivery__nnFNKRL` 上验证）

| 前提 | 实测 | 对设计的影响 |
|---|---|---|
| 命令自包含 | 418/439 条带 `cd /app &&` 前缀；mini-swe-agent 每条命令跑在新 subshell，cwd/env 不继承 | 可以开环顺序重放，不需要 LLM |
| 写操作内容内联 | heredoc / `sed -i` / `python3 -` ，补丁内容全在命令字面量里 | 重放能确定性地复现文件状态 |
| **跨 step 状态依赖** | 49 个 `/tmp` 路径、**59 次跨 step 引用**（如 `git commit -F /tmp/commit_message.txt`） | **必须长驻容器串行 exec，不能一条命令一个 `docker run`** |
| 失败要保留 | 439 条里 15 条非 0：`rc=1`×10、`rc=-1`×3（timeout 打死）、`rc=2`×1、`rc=128`×1 | 重放**不能** `set -e`，失败是负载的一部分 |
| 无随机/无交互 | date/uuidgen/$RANDOM/shuf 命中 0；vim/less 命中 0；后台任务 0 | 确定性好 |
| 网络 | 仅 1 条 `pip install --dry-run`；原环境 `allow_internet=false` 本就失败 | 重放用 `--network=none`，行为一致 |

## 2. 硬校验：重放保真度可自动判定

任务定义里的 `pre_artifacts.sh` 规定提交物就是：

```bash
git diff --binary <base_commit_hash> HEAD > /logs/artifacts/model.patch
```

所以**重放结束后做同样的 diff，与下载到的 `model.patch` 逐字节比对**：

- 一致 → 重放忠实，该 trace 入库
- 不一致 → 进人工检查队列，不入库

这是整条流程的地基。**先在一条 trace 上跑通这个校验，再谈规模化**——校验不过的话，采集到的负载数据没有意义。

注意 diff 是 `base..HEAD`，只算**已提交**的改动，所以重放必须把 trace 里的 `git add` / `git commit`
一并执行（本例 5 条）。

## 3. 五段流程

### S1 采集：怎么拿到 trace

榜单页 `https://deepswe.datacurve.ai/data/v1.1/trials/{trial_name}` 直接 curl 是 33 MB HTML，
但**里面没有 trace**——那是 TanStack Start 的 SSR 壳，内嵌的只有全站 trial 的元数据。
真 trace 由前端二次请求 CloudFront 取得。链路是从 `assets/use-artifact-*.js` 反查出来的：

**第 1 步：拿产物路径模板**（562 B）

```bash
curl -s https://deepswe.datacurve.ai/artifacts/v1.1/release.json
```
```json
{
  "release_id": "v1.1",
  "artifact_base_url": "https://d3ujjcmjq6o8v6.cloudfront.net",
  "artifact_patterns": {
    "trajectory":      "v1.1/trial-artifacts/{trial_name}/agent/trajectory.json",
    "model_patch":     "v1.1/trial-artifacts/{trial_name}/artifacts/model.patch",
    "agent_log":       "v1.1/trial-artifacts/{trial_name}/agent/mini-swe-agent.txt",
    "verifier_output": "v1.1/trial-artifacts/{trial_name}/verifier/test-stdout.txt",
    "verifier_file":   "v1.1/trial-artifacts/{trial_name}/verifier/{file}"
  }
}
```

**第 2 步：填模板直接 GET**——纯 GET，**无鉴权**，不需要 cookie / token / referer：

```bash
curl -O https://d3ujjcmjq6o8v6.cloudfront.net/v1.1/trial-artifacts/<trial_name>/agent/trajectory.json
```

**第 3 步：批量枚举用全量索引**（3.2 MB gzip / 47 MB 解压，29357 行）：

```bash
curl -s https://deepswe.datacurve.ai/artifacts/v1.1/trials.json
```

每行含 `trial_name` `task_name` `model` `provider` `harness` `config` `reasoning_effort`
`reward` `outcome` `n_agent_steps` `cost_usd` `n_input/cache/output_tokens` `peak_context_tokens`
`started_at` `finished_at` `agent_duration_seconds` `has_trajectory` `verifier_files`。
**很多横向统计不用下 trace，光靠索引就能算。**

**第 4 步：任务环境定义**（重放必需，每个约 22-28 KB，113 个共 31.9 MB）：

```bash
curl -s https://deepswe.datacurve.ai/artifacts/v1.1/tasks/<task_id>.json
```

返回 11 个文件的**内容**（不是链接）：`instruction.md` `task.toml` `pre_artifacts.sh`
`environment/Dockerfile` `tests/Dockerfile` `tests/test.sh` `tests/test.patch` `tests/config.json`
`tests/grader.py` `solution/solution.patch` `solution/solve.sh`。
其中 `task.toml` 给出 `docker_image`、`base_commit_hash`、`repository_url` 和 `cpus/memory_mb/allow_internet` 规格。

#### 覆盖度与体量（v1.1）

| 项 | 数量 |
|---|---|
| trial 总数 | 29357（113 个 task × 约 260 个 config） |
| has_trajectory | 29356（99.997%） |
| has_agent_log | 29335 |
| has_model_patch | 28815 |
| harness | 全部 `mini-swe-agent` |

全量 trajectory 实测：中位 **886 B/step**（gzip），252 万 steps 合计
**约 2.2 GB 传输、解压后约 13 GB**；并发 16 实测 **9.3 条/秒**，跑完约 **52 分钟**。

#### 复现脚本

```bash
# 单条（trajectory + model.patch + agent log + verifier 输出）
python3 deepswe/fetch_trial_artifacts.py <trial_name> -o deepswe/data/

# 带全量索引
python3 deepswe/fetch_trial_artifacts.py <trial_name> --index -o deepswe/data/
```

#### 采集策略：建议全量拉，选样后置

下载是可逆的，筛选是不可逆的。2.2 GB / 52 分钟就能把全部 29356 条 trace 落成本地库，
之后所有选样变成一次本地查询，口径可以反复改，还能算分布和尾部。
反过来，如果在下载阶段就按口径筛（比如只要 fable-5/high/pass 的 295 条 ≈ 20 MB），
省下的那点带宽换来的是重爬的代价，而且会**系统性砍掉负载最重的尾部**：

| | fable-high-pass (295) | 全量 (29202) |
|---|---|---|
| steps 中位 / 最大 | 51 / **231** | 72 / **1398** |
| peak context 中位 / 最大 | 102 K / **384 K** | 126 K / **999 K** |

那 295 条只占全量 agent steps 的 **0.7%**。

### S2 镜像获取 ← **当前瓶颈**
- 113 个 task = **113 个互不相同的镜像**，全在 `public.ecr.aws`，单个约 800 MB 压缩
- 匿名拉取被限流：`manifest inspect` 正常，`docker pull` 秒拒 `toomanyrequests: Rate exceeded`
- 需要：AWS 账号认证 + 限速 + 断点续传 + 本地 registry mirror 缓存

### S3 重放执行
```bash
docker run -d --name replay_<trial> \
    --cpus=2 --memory=8g --memory-swap=8g \   # 对齐 task.toml：113/113 都是 2C/8G
    --network=none \                           # 对齐 allow_internet=false
    <task_image> sleep infinity

for cmd in commands:            # 严格按 trace 顺序，串行
    docker exec replay_<trial> bash -lc "$cmd"    # 不加 set -e；超时沿用命令自带的 timeout
```

**时序两种模式**（trace 只记了 step 时间戳，模型时间与命令时间**没有分开记**）：

- `--pace=none` 背靠背执行：测纯 CPU 吞吐，把 session 压缩到最短
- `--pace=trace` 重现 duty cycle（做功耗/DVFS 才需要）：
  先跑一遍 `none` 得到每条命令实测耗时 `c_i`，则模型思考时间 `m_i = gap_i - c_i`，
  第二遍在每条命令后 `sleep m_i`。`m_i < 0` 说明重放机比原机慢，记录并置 0。

### S4 指标采集
容器串行执行，所以**在每条命令前后读容器 cgroup v2 累计值做差**即可归因到单条命令，
比 `/usr/bin/time` 更准且不侵入命令本身：

| 指标 | 来源 |
|---|---|
| user / sys CPU | `cpu.stat` 的 `user_usec` / `system_usec` 差值 |
| 峰值内存 | `memory.peak`（每条命令前重置）／退化用 `memory.current` 采样 |
| 块设备 IO | `io.stat` 的 `rbytes` / `wbytes` 差值 |
| wall time / rc | exec 外层计时与退出码 |
| **微架构事件** | 外层 `perf stat -e instructions,cycles,cache-misses,branch-misses -p <pid>` |

最后一行才是"处理器负载"真正要的东西；前几项是画像和归因用的。

### S5 校验与入库
1. **硬校验**：`git diff base..HEAD` 与 `model.patch` 逐字节一致
2. **软校验**：重放的 rc 序列与 trace 里记录的 `returncode` 一致；不一致说明环境漂移
3. **弱校验**：输出比对——注意 trace 里的输出被 harness 截断（`elided_chars` + `"Output too long."`，
   上限约 11 K 字符），只能比对前 11 K

## 4. 已知风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | ECR 匿名限流，113×800MB | S2 卡死 | AWS 账号认证；本地 registry mirror |
| 2 | 原环境 CPU 型号未知（只知 2C/8G） | 绝对时间不可比 | 只对齐相对结构，不对齐绝对秒数 |
| 3 | `rc=-1` 的 timeout 命令 | 重放机快慢不同会改变 timeout 是否触发 → 可能导致 patch 不一致 | 单独标记这类 trace；必要时按原 rc 强制对齐 |
| 4 | 磁盘 | 113 镜像 + 重放产物，估 100 GB+ | 分批跑，跑完即清镜像只留指标 |
| 5 | 语言分布不均 | ts 35 / go 34 / py 34 / rust 5 / js 5 | 抽样时按语言分层，别让 rust/js 被淹没 |

## 5. 推进顺序

1. **单条打通**：`gql-incremental-graphql-delivery__nnFNKRL`（439 步，最重的一条）
   拉镜像 → 重放 → patch 硬校验。**这步不过，后面都不用谈。**
2. **跨语言验证**：ts / go / py / rust / js 各挑 1 条，验证流程通用性（各语言的构建/测试工具链差异大）
3. **定清单批量跑**：按选样口径定出 N 条，批量执行 + 入库

## 6. 选样口径（待定，见 PROJECT_HISTORY）

覆盖 113/113 task 的阶梯：fable-5/high/pass (98) → 放宽 effort (110) → gpt-5-6-sol/high (112)
→ `gql-incremental-graphql-delivery` 需换模型（fable 与 sol 全 effort 0/20，只有 sonnet-5/max 等 10/260 过）。

**但既然负载要含探索过程，"必须 pass"这个条件本身值得重新考虑**——失败轨迹是更纯粹的探索负载。
