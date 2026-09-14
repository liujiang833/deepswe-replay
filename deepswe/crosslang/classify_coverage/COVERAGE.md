# 分类器覆盖率：改动前 vs 改动后

- 命令来源：`/home/river/projects/agent/deepswe/crosslang/full_trials` 下全部 trace（replay.py load_trace，跳过哨兵，strip_cd）
- before：`before (0288c83)` = `git show 0288c83:deepswe/summarize_replay.py` 导出到临时文件（跑完即删，由 `classify_coverage/reproduce.sh` 重新生成）
- after：`after (工作区)` = 工作区的 `deepswe/summarize_replay.py`
- 「其他%」= 主类别落进「其他」的命令占比；「未识别」= 其中细类是 `未识别:X`（程序不在任何表里）的条数；
  「含未识别语句」= 整条命令里至少有一条语句是 `未识别:X`（主类别可能已被别的语句决定，
  比如 `sed -i … && go test` 在改动前被判成「写文件」）。

## 总览

| 语言 | trial | 命令 | 其他% before | 其他% after | 未识别 before | 未识别 after | 含未识别语句 before | 含未识别语句 after |
|---|---|---|---|---|---|---|---|---|
| python | 34 | 1347 | 2.7% (36) | 2.3% (31) | 4 | 0 | 33 | 1 |
| go | 35 | 1040 | 6.9% (72) | 0.2% (2) | 64 | 0 | 392 | 0 |
| rust | 5 | 311 | 8.7% (27) | 0.3% (1) | 27 | 0 | 76 | 0 |
| typescript | 34 | 1556 | 12.8% (199) | 0.4% (7) | 189 | 0 | 524 | 1 |
| javascript | 5 | 152 | 12.5% (19) | 1.3% (2) | 19 | 0 | 55 | 0 |
| 全部 | 113 | 4406 | 8.0% (353) | 1.0% (43) | 303 | 0 | 1080 | 2 |

## 主类别迁移（before → after，只列变了的）

- **python**：其他 → 跑测试 5 条
- **go**：写文件 → 跑测试 238 条；其他 → 跑测试 70 条；版本控制 → 跑测试 44 条；读文件 → 跑测试 29 条；搜索 → 跑测试 12 条
- **rust**：写文件 → 跑测试 35 条；其他 → 跑测试 26 条；搜索 → 跑测试 9 条；读文件 → 跑测试 7 条；版本控制 → 跑测试 2 条
- **typescript**：写文件 → 跑测试 204 条；其他 → 跑测试 188 条；版本控制 → 跑测试 30 条；读文件 → 跑测试 19 条；搜索 → 跑测试 9 条；其他 → 写文件 4 条
- **javascript**：写文件 → 跑测试 18 条；其他 → 跑测试 17 条；读文件 → 跑测试 2 条
- **全部**：写文件 → 跑测试 495 条；其他 → 跑测试 306 条；版本控制 → 跑测试 76 条；读文件 → 跑测试 57 条；搜索 → 跑测试 30 条；其他 → 写文件 4 条

## 主类别分布（after）

| 语言 | 跑测试 | 搜索 | 读文件 | 写文件 | 版本控制 | 其他 |
|---|---|---|---|---|---|---|
| python | 370 (27.5%) | 322 (23.9%) | 339 (25.2%) | 177 (13.1%) | 108 (8.0%) | 31 (2.3%) |
| go | 393 (37.8%) | 334 (32.1%) | 162 (15.6%) | 57 (5.5%) | 92 (8.8%) | 2 (0.2%) |
| rust | 79 (25.4%) | 90 (28.9%) | 90 (28.9%) | 29 (9.3%) | 22 (7.1%) | 1 (0.3%) |
| typescript | 450 (28.9%) | 413 (26.5%) | 365 (23.5%) | 195 (12.5%) | 126 (8.1%) | 7 (0.4%) |
| javascript | 37 (24.3%) | 35 (23.0%) | 27 (17.8%) | 36 (23.7%) | 15 (9.9%) | 2 (1.3%) |
| 全部 | 1329 (30.2%) | 1194 (27.1%) | 983 (22.3%) | 494 (11.2%) | 363 (8.2%) | 43 (1.0%) |

## 落进「其他」的 top 细类 / program

### python

- before 细类：`接口探查` 24、`环境查询` 8、`未识别:(ruff` 3、`未识别:(python` 1
- after 细类：`接口探查` 24、`环境查询` 7
- after program：`python` 17、`pip install` 3、`pip list` 3、`python -m bandit` 2、`python -m unittest` 2、`pip show` 1、`python -m ruff` 1、`python -m numba.runtests` 1、`python -m sqlite_utils` 1

### go

- before 细类：`未识别:go` 49、`未识别:gofmt` 8、`子 shell` 6、`未识别:task` 3、`未识别:prometheus` 2、`未识别:setsid` 1、`curl` 1、`未识别:kill` 1、`sleep` 1
- after 细类：`curl` 1、`sleep` 1
- after program：`curl` 1、`sleep` 1

### rust

- before 细类：`未识别:cargo` 26、`未识别:ps` 1
- after 细类：`ps` 1
- after program：`ps` 1

### typescript

- before 细类：`未识别:npx` 146、`未识别:pnpm` 15、`子 shell` 7、`未识别:node` 6、`未识别:bun` 4、`未识别:npm` 3、`未识别:tsc` 3、`未识别:pgrep` 2、`curl` 2、`未识别:⏎sed` 2、`未识别:deno` 1、`未识别:pkill` 1、`未识别:ps` 1、`echo` 1、`未识别:⏎perl` 1
- after 细类：`curl` 2、`pgrep` 1、`ps` 1、`接口探查` 1、`echo` 1、`[` 1
- after program：`curl` 2、`pgrep` 1、`ps` 1、`node` 1、`echo` 1、`[` 1

### javascript

- before 细类：`未识别:node` 6、`未识别:npx` 6、`未识别:(timeout` 4、`未识别:npm` 3
- after 细类：`接口探查` 2
- after program：`node` 2

### 全部

- before 细类：`未识别:npx` 152、`未识别:go` 49、`未识别:cargo` 26、`接口探查` 24、`未识别:pnpm` 15、`子 shell` 13、`未识别:node` 12、`环境查询` 8、`未识别:gofmt` 8、`未识别:npm` 6、`未识别:bun` 4、`未识别:(timeout` 4、`curl` 3、`未识别:(ruff` 3、`未识别:tsc` 3
- after 细类：`接口探查` 27、`环境查询` 7、`curl` 3、`ps` 2、`pgrep` 1、`echo` 1、`sleep` 1、`[` 1
- after program：`python` 17、`pip install` 3、`node` 3、`curl` 3、`pip list` 3、`python -m bandit` 2、`python -m unittest` 2、`ps` 2、`pgrep` 1、`pip show` 1、`echo` 1、`python -m ruff` 1、`python -m numba.runtests` 1、`sleep` 1、`python -m sqlite_utils` 1
