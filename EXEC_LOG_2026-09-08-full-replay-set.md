# Task Execution Log: 全量 replay 用例集（113 → 118 条）

**Date:** 2026-09-08
**Goal:** 把 replay 从 5 条样例扩到全量，目标是在服务器上跑通 go / python / javascript，
rust 与 typescript 预期也能过。

## Problems to Solve

1. `deepswe/data/` 是「下载态」布局，replay.py / run_batch.py 要的是另一种布局，中间缺一层装配
2. 113 条里**没有一条带已验证基线**——crosslang 那 5 条是另一批 trial，不能直接复用
3. `build_arm.sh` 是「一种语言一个目录」的映射，全量集下会静默只留最后一条
4. `run_batch.py` 缺一个镜像就整批拒绝启动，而 113 个镜像不可能一次建齐
5. 完整 `task.json` 30.4 MB，且含参考解，不该整包传到服务器

## Steps Log

### Step 1: 摸清数据布局与语言归属
- **Status:** success
- **Result location:** `deepswe/data/{trajectories,tasks,build_env.json}`
- **Success result:**
  - `trajectories/<task_name>/` 有 trajectory.json + model.patch + meta.json（下载清单，
    与 crosslang 的 meta.json **不是同一套 schema**）
  - `tasks/<task_name>.json` 是整包 task 定义（11 个文件）
  - `build_env.json` 的静态分析里有全部 113 个的 `language` / `repository_url` /
    `base_commit_hash` / `environment.docker_image` —— 语言不用靠猜 Dockerfile 里的包管理器
    （typescript 与 javascript 都是 node，`pnpm install` / `npm ci` 区分不可靠）
  - 语言分布：typescript 35 / go 34 / python 34 / rust 5 / javascript 5
- **关键发现:** **replay.py 完全不读 meta.json**，只读 trial 目录 + task.json；
  而 task.json 里它只用 `task.toml`，build_arm.sh 只用 `environment/Dockerfile`。
  这决定了装配只需要生成 5 个字段的 meta.json，且 task.json 可以裁剪 98.7%。

### Step 2: 发现两批 trial 不是同一次运行
- **Status:** success（这是个「幸好查了」的发现）
- **Success result:** `TRAJECTORY_SELECTION.json` 选的与 `crosslang/` 下那 5 条
  **trial_name 不同、模型不同、model.patch 大小也不同**：

  | task | crosslang（已验证） | selection（新选） |
  |---|---|---|
  | returns-… | `__8JQj5gw` moonshot/vellise-0716, 63009B | `__sT7wC5g` claude-fable-5, 32687B |
  | actionlint-… | `__23b2uyq` openai/gpt-5.6-luna, 31161B | `__f62DK3Z` claude-fable-5, 26408B |
  | fd-… | `__fK6jc93` vertex_ai/claude-opus, 42279B | `__tnQJDgA` claude-fable-5, 17736B |
  | true-myth-… | `__BBLS6Fy` anthropic/claude-opus, 39714B | `__zVw8vPV` claude-fable-5, 29797B |
  | yjs-… | `__gSSidka` openai/gpt-5.5, 32899B | `__cAHUYjt` claude-fable-5, 17714B |

  **含义：那 113 条里一条已验证基线都没有。** 若不处理，服务器上某条失败时无法区分
  「这个 task 有问题」和「整套流程有问题」。
- **对策:** 装配时把这 5 条一并带上当**回归对照组** → 118 条。同一 task 用同一镜像
  （已核对 docker_image 完全一致），**不增加任何构建成本**。

### Step 3: 写 make_full_trials.py
- **Status:** success
- **Result location:** `deepswe/crosslang/make_full_trials.py`
- **Success result:** 118 条，4891 条命令，113 个不同镜像，零缺失文件。三个设计点：
  1. `n_commands` 直接 import replay.py 的 `load_trace()`，不另写解析——口径必须与实际重放一致
  2. task.json 裁剪到 `task.toml` + `environment/Dockerfile`：30.4 MB → 0.4 MB，
     顺带把参考解 `solution/solution.patch` 挡在包外
  3. 语言取自 `build_env.json`，不猜 Dockerfile

### Step 4: 修 build_arm.sh 的「一语言一目录」bug
- **Status:** success
- **Failure reason（修复前）:** `DIR_OF[$lang]="$dir"` 覆盖式赋值，35 条 go 只会留下最后一条，
  且**不报错**——在 5 条样例集上完全看不出来
- **Success result:** 改为 `DIRS_OF[$lang]` 存空格分隔列表，`SELECTED` 改装 trial 目录名；
  work 目录、便捷别名 tag 都改为按 trial（别名还要转小写，docker 仓库名不允许大写）；
  加 `[i/N]` 进度、失败清单与单条重试提示。`--list all` 验证：118 条，go×35。

### Step 5: 给 run_batch.py 加 --skip-missing
- **Status:** success
- **Success result:** 缺镜像的 trial 被剔除而非让整批拒绝启动，可以边建边跑；
  缺镜像提示从 `docker pull`（registry 本来就够不到，是死路）改为 `build_arm.sh`；
  问题清单截断到 8 条（118 条会把真正的问题冲掉）；trial 列表 >12 条时改为分语言统计。

### Step 6: 端到端验证
- **Status:** in_progress
- **Result location:** `scratchpad/verify_run/returns-validated-error-accumula__sT7wC5g/verdict.json`
- **做法:** 打包 → 解包 → 用**新装配的**那条（裁剪版 task.json + 生成的 meta.json，
  本身没有基线）实跑一次 replay，确认装配出来的数据真的能重放。

## 产出

| 文件 | 说明 |
|---|---|
| `crosslang/make_full_trials.py` | 新增。data/ → trial 目录的装配器 |
| `crosslang/build_arm.sh` | 改。按 trial 构建而非按语言 |
| `crosslang/run_batch.py` | 改。`--skip-missing` + 全量集下的输出裁剪 |
| `crosslang/make_bundle.sh` | 改。`--trials-dir` |
| `crosslang/RUNBOOK.md` | §7 重写为全量集操作说明 + 实测成本 |

包体：27 MB 解包 / **4.8 MB 压缩**（55 KB/s 下约 25 分钟）。
