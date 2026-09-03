# DeepSWE trial trace 获取方式

**结论：能拿到完整 trace，公开、无鉴权。**

问题起点：`https://deepswe.datacurve.ai/data/v1.1/trials/abs-module-cache-flags__4kU2tLe`
这个页面直接 curl 下来是 33MB 的 HTML，但里面**没有** trace ——
它是 TanStack Start 的 SSR 壳，内嵌的只有全站 29357 条 trial 的**元数据**
（model / reward / cost / n_agent_steps / has_trajectory ...）。
真正的 trace 是前端二次请求 CloudFront 拿的。

## 链路

1. `GET https://deepswe.datacurve.ai/artifacts/v1.1/release.json` （562B）

   ```json
   {
     "release_id": "v1.1",
     "artifact_base_url": "https://d3ujjcmjq6o8v6.cloudfront.net",
     "artifact_key_prefix": "v1.1/trial-artifacts",
     "artifact_patterns": {
       "trajectory":      "v1.1/trial-artifacts/{trial_name}/agent/trajectory.json",
       "model_patch":     "v1.1/trial-artifacts/{trial_name}/artifacts/model.patch",
       "agent_log":       "v1.1/trial-artifacts/{trial_name}/agent/mini-swe-agent.txt",
       "verifier_output": "v1.1/trial-artifacts/{trial_name}/verifier/test-stdout.txt",
       "verifier_file":   "v1.1/trial-artifacts/{trial_name}/verifier/{file}"
     }
   }
   ```

2. 把 `{trial_name}` 填进模板，直接 GET CloudFront。纯 GET，无 cookie / token / referer 要求。

   ```bash
   curl -O https://d3ujjcmjq6o8v6.cloudfront.net/v1.1/trial-artifacts/abs-module-cache-flags__4kU2tLe/agent/trajectory.json
   ```

3. 批量枚举用全量索引：
   `GET https://deepswe.datacurve.ai/artifacts/v1.1/trials.json` （3.2MB gzip / 47MB 解压，29357 行）
   每行含 `trial_name` `task_name` `model` `provider` `harness` `config` `reasoning_effort`
   `reward` `outcome` `n_agent_steps` `cost_usd` `n_input/cache/output_tokens`
   `peak_context_tokens` `started_at` `finished_at` `agent_duration_seconds`
   `has_trajectory` `verifier_files` 等。

## 覆盖度（v1.1）

| 项 | 数量 |
|---|---|
| trial 总数 | 29357 |
| has_trajectory | 29356（99.997%） |
| has_agent_log | 29335 |
| has_model_patch | 28815 |
| harness | 全部 `mini-swe-agent` |

模型分布（top）：gpt-5-6-sol / claude-sonnet-5 / claude-opus-4-8 / claude-fable-5 各 2260，
claude-opus-5 2256、gpt-5-6-luna 2256、gpt-5-6-terra 2254、gpt-5-5 1808、grok-4-6 1808、
gemini-3-7-flash 1356、glm-5-2 904、gemini-3-8-flash 899。

## trajectory.json 结构

```
schema_version
session_id
agent: {name: "mini-swe-agent", version: "2.4.1", model_name: "vertex_ai/claude-fable-5",
        extra.agent_config: {system_template, instance_template, step_limit, mode, ...}}
steps[]: {step_id, timestamp, source: system|user|agent,
          message,                                   # reasoning 文本
          tool_calls[]: {tool_call_id, function_name, arguments},
          observation.results[]: {content, source_call_id}}
final_metrics: {total_prompt_tokens, total_completion_tokens, total_cached_tokens,
                total_cost_usd, total_steps, extra.peak_context_tokens}
notes
```

即：**逐 step 的 reasoning 文本 + 完整 bash 命令 + 完整 stdout/returncode**，
外加 system/instance prompt 原文和逐次的 token/cost。这正是做轨迹分析要的粒度。

前端还支持 `subagent_trajectories` / `subagent_trajectory_ref` 字段（嵌套子 agent 轨迹），
mini-swe-agent 的 trial 里没有用到。

### 本例实测 `abs-module-cache-flags__4kU2tLe`

- model `claude-fable-5` / provider `vertex_ai` / config `mini_swe_agent_claude_fable_5_high`（reasoning_effort=high）
- 49 steps（索引里 `n_agent_steps` 记 47，差 system+instance 两条）
- reward 1，f2p 20/20，p2p 3/3
- cost $5.247942，prompt 2163618 tok（cache 2077852）/ completion 41965 tok，peak context 76969
- 耗时 606s（agent）/ 668s（trial）
- 产物大小：trajectory 187KB、agent log 139KB、model.patch 27KB、verifier stdout 20KB

## 复现

```bash
python3 deepswe/fetch_trial_artifacts.py abs-module-cache-flags__4kU2tLe -o out/
python3 deepswe/fetch_trial_artifacts.py --index -o out/     # 全量索引，用于批量枚举
```
