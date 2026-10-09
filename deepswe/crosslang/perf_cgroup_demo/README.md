# Docker cgroup 与进程直采 topdown 对照

这个 demo 用同一个 C 二进制、同一逻辑 CPU、同一组 ARM PMU 事件，交替做三种采集：

| 模式 | perf 目标 | 用途 |
|---|---|---|
| `native_pid` | 宿主机上的矩阵乘进程：`perf stat -p PID` | 原生基线 |
| `docker_pid` | 容器内矩阵乘进程对应的宿主机 PID：`perf stat -p PID` | 容器中的进程直采基线 |
| `docker_cgroup` | 同一容器的 cgroup：`perf stat -a -G CGROUP` | 检验正式 replay 的 cgroup 采法 |

每种模式使用**新的进程或容器**，采集**串行**进行，避免两套 perf 会话抢 PMU 计数器。
矩阵乘进程是容器 PID 1，单线程、无子进程；它完成初始化后阻塞在 FIFO，等 perf
启动并确认就绪才进入三层循环。计算结束后仍保持存活，等 perf 收尾再退出。
因此 `docker_pid` 与 `docker_cgroup` 的主要区别就是 perf 的过滤目标。

## 在 ARM 服务器上运行

先填好上级目录的 `topdown.conf`，运行 `bash ../probe_pmu.sh` 验证事件号和 PMU。
宿主机需要 `gcc`、`docker`、可用的 `perf` 和 perf 所需权限；镜像须已在本机，
脚本不会自动拉取。首次执行前运行 `sudo -v`，避免 perf 的 sudo 提示打断测量。

```bash
cd deepswe/crosslang/perf_cgroup_demo
sudo -v
python3 demo.py --image ubuntu:24.04 --repeats 3
```

主机用 `gcc -O2` 编译一次 `matmul.c`。脚本优先静态链接，使同一二进制能直接挂进
Docker；若静态链接不可用，会尝试动态链接，并通过一次 16×16 矩阵乘确认镜像能运行它。
默认矩阵大小为 `960×960`，在当前开发机计算段约 0.9 秒；ARM 服务器速度不同可手动改
`--size 768` 或 `--size 1024`，脚本不会自动调整。三种模式在同一轮始终使用同一个大小。
默认选择目标 PMU 的首个可用逻辑 CPU；也可显式指定 `--cpu 3`。

```bash
python3 demo.py --image ubuntu:24.04 --size 960 --cpu 3 --repeats 5
```

每次运行保留 `perf_argv.json`、`perf.csv`、`topdown.json`、`topdown.log`、`perf.stderr` 和 workload 的
checksum/耗时。总结果在 `runs/<时间>/summary.json`，包含二进制 SHA-256、CPU、PMU、
原始事件计数、四象限和各事件的 `pcnt-running`。终端汇报三种模式的中位数，重点看
`docker_cgroup` 相对 `docker_pid` 的差值；`summary.json` 还保存每种模式重复运行的
最大值与最小值差幅，供判断差值是否超过自然波动。若 checksum 不同或 topdown 自检失败，结果
不能用于判断 cgroup 准确性。

`OP_RETIRED` 是退休的微操作数，不等于架构指令数。比较 topdown 时先看所有必需事件
及四象限；若还要看 `INST_RETIRED`，可在 `topdown.conf` 的 `EV_EXTRA` 中填写目标核的
确切事件号，再确认 `pcnt-running` 没有因事件数增加而降到 99.9% 以下。
cycles 即使对同一程序也可能随频率、缓存和调度变化；退休计数和 checksum 是更直接的
执行路径检查。

## 当前开发机的流程验证

`--smoke` 不调用 perf，仅检查编译、原生和 Docker 运行、FIFO 同步与 checksum，
因此也能在没有 ARM PMU 的机器上运行：

```bash
python3 demo.py --smoke --repeats 1 --image ubuntu:24.04
```

当前工作区是 x86_64 WSL，`perf` 因缺少对应内核的工具包无法运行；完整的 ARM PMU
对照需要在目标 ARM 服务器执行。
