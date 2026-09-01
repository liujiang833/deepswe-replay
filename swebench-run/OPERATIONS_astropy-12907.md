# `astropy__astropy-12907` 单次运行的操作统计（DeepSeek v4-pro 驱动 mini-SWE-agent）

- 数据集：`SWE-bench/SWE-bench_Verified` test split 第 1 条
- 模型：`deepseek/deepseek-v4-pro`（litellm → api.deepseek.com）
- 镜像：`docker.io/swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`
- 退出状态：`Submitted`

## 1. 宏观计数

| 指标 | 值 |
|---|---|
| LM 调用（api_calls） | 12 |
| 消息总数 | 28（system 1 + user 1 + assistant 12 + tool 13 + exit 1） |
| **bash 工具调用** | **14** |
| bash 内部拆出的**独立 shell 操作** | **30** |
| 观察回灌总字节 | 23,750 |
| 单次观察最大 / 中位 | 7,977 / 791 字节（无一超过 10,000 的截断阈值） |
| 输入 token 累计 | 102,926 |
| 输出 token 累计 | 3,790（其中 reasoning 2,218） |
| 成本 | **$0.0318** |
| agent 墙钟时间 | 65.7 s（模型 54.2 s / docker exec 11.6 s） |
| 提交 patch | 504 字符，1 文件 1 行 |

**为什么 14 次 bash 调用只对应 12 次 LM 调用**：配置开了 `parallel_tool_calls: true`，
第 2 轮和第 4 轮各在一次回复里发了 2 个 bash tool call。

**为什么 13 个 tool 观察对应 14 次调用**：第 14 次是提交命令，
`DockerEnvironment._check_finished()` 在拼观察消息之前就抛了 `Submitted`，所以没有第 14 条观察。
分析脚本里它显示为 `rc=None`。

## 2. bash 内部到底做了什么 —— 按意图分类（30 个 shell 操作）

| 意图 | 次数 | 说明 |
|---|---|---|
| `navigate` | 12 | 全部是 `cd /testbed`。每条命令都得重新 cd，因为每次 `docker exec bash -c` 都是新子 shell |
| `read_file` | 5 | `sed -n 'a,bp'`（3）、`cat patch.txt`（2） |
| `make_patch` | 3 | `git diff -- <file>`，其中 1 次重定向到 patch.txt |
| `run_tests` | 3 | 3 次 pytest |
| `repro_script` | 2 | `python - <<'PY'` 只打印不写盘（#5 复现、#8 边界验证） |
| `search_files` | 1 | `find . -maxdepth 3 -name 'separable.py' ...` |
| `search_content` | 1 | `grep -R "separability_matrix" -n astropy/modeling` |
| `edit_file` | 1 | **全程只有这一次真正改了源码**（#7 的 `python - <<'PY'` + `write_text`） |
| `vcs` | 1 | `git status --short` |
| `submit` | 1 | `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` |

按调用的**程序**统计（管道每段各计一次，heredoc 整块算一个 op、头部程序是 `cd`）：
`cd`×14、`git`×4、`sed`×3、`pytest`×3、`head`×2、`cat`×2、`ls`×1、`find`×1、`grep`×1、`echo`×1。

注意：**`python` 没有单独出现在这张表里**。3 次 `python - <<'PY'` 因为写在 `cd /testbed && python - <<'PY'`
里、且 heredoc 正文必须整块保留，所以头部程序被记成 `cd`。这 3 次恰恰是整个任务里信息量最大的操作，下面单独讲。
区分它们的办法是看 heredoc 正文是否有写操作（`write_text` / `open(...,'w')` 等）：
只有 #7 有，所以 `edit_file` 只有 1 次，另外 2 次是纯打印的复现脚本。

被引用的文件：`astropy/modeling/separable.py`(6)、`astropy/modeling/tests/test_separable.py`(4)、
`patch.txt`(3)、`astropy/modeling/tests/test_core.py`(2)。

返回码分布：`rc=0` 12 次，`rc=1` 1 次，`rc=None`（提交，无观察）1 次。

## 3. 逐步剖析：bash 内部的真实语义

### 阶段 A｜定位（#1–#4，4 次调用，0 次写操作）
```bash
cd /testbed && ls -la && find . -maxdepth 3 -name 'separable.py' -o -name 'separable*.py' | head -20
cd /testbed && sed -n '1,260p' astropy/modeling/separable.py          # 读前 260 行
cd /testbed && grep -R "separability_matrix" -n astropy/modeling | head -50
cd /testbed && sed -n '260,380p' astropy/modeling/separable.py        # 接着读 260-380 行
```
要点：
- 用 `sed -n 'a,bp'` 分段读而不是 `cat` 整个文件 —— prompt 的观察模板对 >10000 字符会截断，
  分段读是对这个限制的直接适应。最大一次观察 7,977 字节，正好压在阈值下。
- `grep -R ... | head -50` 主动截流。
- `find ... -name 'separable.py' -o -name 'separable*.py'` 两个模式是冗余的
  （后者完全包含前者），`-o` 分支没有意义但无害。`-maxdepth` 是 find 的全局选项，
  对两个分支同时生效（已实测确认），所以不存在深度约束漏掉半支的问题。

### 阶段 B｜复现（#5，`repro_script`：只打印，不落盘）
```bash
cd /testbed && python - <<'PY'
from astropy.modeling import models as m
from astropy.modeling.separable import separability_matrix
cm = m.Linear1D(10) & m.Linear1D(5)
print(separability_matrix(cm))
print(separability_matrix(m.Pix2Sky_TAN() & m.Linear1D(10) & m.Linear1D(5)))
print(separability_matrix(m.Pix2Sky_TAN() & cm))
print(repr(m.Pix2Sky_TAN() & cm))
PY
```
- **完全照抄 issue 正文里的三段代码**，第四行 `repr(...)` 是自己加的，用来看嵌套模型的实际结构。
- 用 `python - <<'PY'` 而不是先 `cat > repro.py` 再 `python repro.py`：不落盘，
  也就不会污染最后的 `git diff`。prompt 明确禁止把复现脚本提交进 patch，这种写法天然规避。
- 这次是全程最慢的一轮（LM 侧 17.6s，因为模型在这轮花了 1,119 个 reasoning token）。

### 阶段 C｜改代码（#7，核心动作）
```bash
cd /testbed && python - <<'PY'
from pathlib import Path
p = Path('astropy/modeling/separable.py')
s = p.read_text()
old = "        cright = np.zeros((noutp, right.shape[1]))\n        cright[-right.shape[0]:, -right.shape[1]:] = 1\n"
new = "        cright = np.zeros((noutp, right.shape[1]))\n        cright[-right.shape[0]:, -right.shape[1]:] = right\n"
assert old in s
p.write_text(s.replace(old, new))
PY
git diff -- astropy/modeling/separable.py
```
这一条命令里藏着四个值得注意的工程细节：
1. **用 Python 而不是 `sed -i` 改文件**。目标行含 `[`、`]`、`:`、`-`，sed 正则要转义得很难看；
   Python 的字面量替换没有转义问题。
2. **带 2 行上下文的锚点**：`old` 串包含上一行 `cright = np.zeros(...)`，避免误伤同名模式。
3. **`assert old in s`**：替换前先断言锚点存在。如果文件内容和预期不符，脚本非零退出，
   模型会在观察里看到 AssertionError 而不是"命令成功但什么都没改"。这是 heredoc 编辑里最关键的一句。
4. **同一次 tool call 里紧跟 `git diff` 自检** —— 一次往返就同时完成"改"和"确认改对了"。

产出的 diff 与 gold patch 的**代码改动完全相同**（同一 hunk、同一行）。
文本上并非逐字符相同：模型的 patch 多一行 `index a308e2729..45bea3608 100755`，
因为 `git diff` 默认会输出 blob 哈希行，而数据集里的 gold patch 把它剥掉了（470B vs 504B）。
这一行对 `git apply` 无影响，评测照常通过。

### 阶段 D｜验证（#8–#11）
```bash
# #8 扩展复现：加了 is_separable、左嵌套、双层嵌套共 6 种组合
cd /testbed && python - <<'PY' ... PY
# #9 先跑窄范围
cd /testbed && pytest -q astropy/modeling/tests/test_separable.py astropy/modeling/tests/test_core.py \
      -k 'separable or separability' --disable-warnings --maxfail=1        # rc=0
# #10 再跑宽范围
cd /testbed && pytest -q astropy/modeling/tests/test_separable.py astropy/modeling/tests/test_core.py \
      --disable-warnings --maxfail=1                                        # rc=1  ← 唯一一次失败
# #11 收窄回来确认
cd /testbed && pytest -q astropy/modeling/tests/test_separable.py --disable-warnings --maxfail=1   # rc=0
```
- #8 是**自己扩展的边界测试**（`cm & Pix2Sky_TAN()` 左嵌套、`Linear1D & (Pix2Sky_TAN & cm)` 双层嵌套），
  prompt 的 "Test edge cases" 那一步确实被执行了。
- #10 的 `rc=1` 是 `test_core.py::test_prepare_outputs_single_entry_vector` 失败，
  原因是浮点最后一位不等（`Max absolute difference: 1.11e-16`），**与本次改动无关**（预先存在的环境噪声）。
  模型在下一轮明确写下 "The only failure in the broader test is an unrelated floating-point precision mismatch"，
  然后收窄范围重跑 —— 没有为了让测试变绿去动 `test_core.py`。这一点很关键：改测试会被评测脚本的
  `git checkout <base> <test_file>` 冲掉，而且会破坏 P2P。
- `--maxfail=1` 让失败早停，省 token；`-q --disable-warnings` 压缩输出。

### 阶段 E｜提交（#12–#14，严格三步）
```bash
cd /testbed && git status --short && git diff -- astropy/modeling/separable.py   # 确认工作区只有这一个改动
cd /testbed && git diff -- astropy/modeling/separable.py > patch.txt && cat patch.txt   # 生成并核对
cd /testbed && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt              # 提交
```
- `git status --short` 确认没有多余的新建文件（因为复现脚本走的 stdin，工作区确实干净）。
- 第三条的输出首行恰为 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 且 rc=0，
  `DockerEnvironment._check_finished()` 抛 `Submitted`，其余行（即 patch.txt 内容）成为 `submission`。
- `patch.txt` 虽然写进了 `/testbed`，但 `git diff -- <path>` 限定了路径，所以它不会进 patch。

## 4. 从 bash 视角能看出的行为模式

1. **每条命令都以 `cd /testbed &&` 开头**（14/14）。这不是冗余，是被 `bash -c` 无状态子 shell 逼出来的。
2. **读文件全部用范围读**（`sed -n`、`| head -50`），没有一次无界 `cat` 源码文件 —— 对观察截断阈值的适应。
3. **写文件全部用 `python - <<'PY'` heredoc**，不用 `sed -i`，且都带 `assert` 前置校验。
4. **每次修改后立刻 `git diff` 自检**，把"修改"和"验证修改"塞进同一次往返，省一轮 LM 调用。
5. **不落盘的复现脚本**：`python - <<'PY'` 而非 `cat > repro.py`，从源头保证最终 diff 干净。
6. **测试范围先窄后宽再窄**：定位噪声、确认无关、收敛结论，全程没有动测试文件。
7. **成本几乎全在输入 token**：102,926 in vs 3,790 out。因为每轮都要重发全部历史消息，
   而观察被模板压得很小（总共才 23,750 字符）。这也是为什么 SWE-bench agent 的成本主要由轮数决定。
