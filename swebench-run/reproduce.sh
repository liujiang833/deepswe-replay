#!/usr/bin/env bash
# 复现：跑通 SWE-bench 第一个用例 astropy__astropy-12907（infer -> eval -> report）
# 前置：docker 可用、约 3GB 磁盘、DeepSeek API key
set -euxo pipefail

ENV_PY=/home/river/miniconda3/envs/swebench/bin
RUNDIR=/home/river/projects/agent/swebench-run
REPO=/home/river/projects/agent/SWE-bench
INSTANCE=astropy__astropy-12907

# ---------- 0. 环境（只需一次） ----------
# conda create -y -n swebench python=3.12
# $ENV_PY/pip install -e "$REPO"
# $ENV_PY/pip install mini-swe-agent

# ---------- 1. 拉评测镜像（agent 和评测共用同一个镜像） ----------
docker pull swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest

export PATH="$ENV_PY:$PATH"
cd "$RUNDIR"

# ---------- 2. gold 冒烟：先证明评测链路本身没问题 ----------
swebench eval verified --gold -i "$INSTANCE" --run-id gold-smoke -j 1
# 期望：gold.gold-smoke.json 里 resolved_instances = 1

# ---------- 3. 推理：DeepSeek 驱动 mini-SWE-agent ----------
# key 取自 ~/.bashrc 的 ANTHROPIC_AUTH_TOKEN（DeepSeek 的 key，两个端点通用）
export DEEPSEEK_API_KEY=$(grep -oP '(?<=export ANTHROPIC_AUTH_TOKEN=)\S+' ~/.bashrc | head -1)
swebench infer verified -m deepseek/deepseek-v4-pro --run-id ds-v4pro -w 1 -- --filter "$INSTANCE"
# 产出：logs/inference/ds-v4pro/preds.json
#      logs/inference/ds-v4pro/$INSTANCE/$INSTANCE.traj.json

# ---------- 4. 评估模型预测 ----------
swebench eval verified -p logs/inference/ds-v4pro/preds.json --run-id ds-v4pro -i "$INSTANCE" -j 1
# 期望：deepseek__deepseek-v4-pro.ds-v4pro.json 里 resolved_instances = 1

# ---------- 5. 分析轨迹里的 bash 操作 ----------
python analyze_traj.py "logs/inference/ds-v4pro/$INSTANCE/$INSTANCE.traj.json" \
       --json traj_analysis.json --dump-commands

# 注意：`swebench report <run_id>` 只能重判 gold 运行
#      （cli/evaluate.py:report_command 硬编码 predictions_path="gold"），
#      对模型运行会把全部实例报成 error。
