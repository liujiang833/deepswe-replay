#!/usr/bin/env bash
# 诊断 per-step topdown 对齐问题
# 用法：bash diag_perstep.sh <topdown_out 目录>/<trial名>
set -euo pipefail

TRIAL_DIR="${1:?用法: bash diag_perstep.sh <trial输出目录>}"
TD="$TRIAL_DIR/topdown"

echo "=== 1. perf 输出文件 ==="
ls -la "$TD"/perf.* 2>/dev/null || echo "  (no perf files)"

echo
echo "=== 2. perf 输出前 20 行（看 interval 时间戳格式）==="
head -20 "$TD/perf.json" 2>/dev/null || head -20 "$TD/perf.csv" 2>/dev/null || echo "  (no perf output)"

echo
echo "=== 3. perf_start_mono.txt ==="
cat "$TD/perf_start_mono.txt" 2>/dev/null || echo "  (missing)"

echo
echo "=== 4. verdict.json 的 t_start_mono ==="
python3 -c "
import json, sys
try:
    v = json.load(open('$TRIAL_DIR/verdict.json'))
    print('t_start_mono =', v.get('t_start_mono'))
    print('elapsed_s =', v.get('elapsed_s'))
    print('n_replayed =', v.get('n_replayed'))
except Exception as e:
    print('ERROR:', e)
"

echo
echo "=== 5. commands.jsonl 前 3 行（看 step / abs_start_s / wall_s）==="
head -3 "$TRIAL_DIR/commands.jsonl" 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        print(f'  i={d.get(\"i\")} step={d.get(\"step\")} abs_start_s={d.get(\"abs_start_s\")} wall_s={d.get(\"wall_s\")} cmd={d.get(\"cmd_stripped\",\"\")[:60]}')
    except: pass
" || echo "  (no commands.jsonl or parse error)"

echo
echo "=== 6. commands.jsonl 最后 3 行 ==="
tail -3 "$TRIAL_DIR/commands.jsonl" 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        print(f'  i={d.get(\"i\")} step={d.get(\"step\")} abs_start_s={d.get(\"abs_start_s\")} wall_s={d.get(\"wall_s\")} cmd={d.get(\"cmd_stripped\",\"\")[:60]}')
    except: pass
" || echo "  (parse error)"

echo
echo "=== 7. perf 输出格式和 interval 时间范围 ==="
python3 -c "
import json, sys, pathlib
td = pathlib.Path('$TD')
# 尝试 JSON
jf = td / 'perf.json'
cf = td / 'perf.csv'
if jf.exists():
    text = jf.read_text(errors='replace')
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    print(f'格式: JSON, {len(lines)} 行')
    intervals = []
    for l in lines:
        try:
            o = json.loads(l)
            iv = o.get('interval')
            if iv is not None:
                intervals.append(float(iv))
        except: pass
    if intervals:
        print(f'interval 数量: {len(intervals)}')
        print(f'interval 时间范围: [{min(intervals):.6f}, {max(intervals):.6f}]')
        print(f'前 5 个 interval: {intervals[:5]}')
        print(f'后 5 个 interval: {intervals[-5:]}')
    else:
        print('没有 interval 字段！查看前几行：')
        for l in lines[:5]:
            print(f'  {l[:200]}')
elif cf.exists():
    text = cf.read_text(errors='replace')
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    print(f'格式: CSV, {len(lines)} 行')
    print('前 5 行:')
    for l in lines[:5]:
        print(f'  {l[:200]}')
else:
    print('找不到 perf.json 或 perf.csv')
"

echo
echo "=== 8. topdown_steps.py 的输出（如果有 topdown_steps.json）==="
python3 -c "
import json, sys
try:
    r = json.load(open('$TD/topdown_steps.json'))
    print(f'n_intervals: {r.get(\"n_intervals\")}')
    print(f't_start_mono: {r.get(\"t_start_mono\")}')
    print(f'perf_start_mono: {r.get(\"perf_start_mono\")}')
    print(f'offset_s: {r.get(\"offset_s\")}')
    agg = r.get('aggregate', {})
    counts = agg.get('counts', {})
    print(f'aggregate counts: {dict(list(counts.items())[:4])}')
    steps = r.get('steps', [])
    print(f'steps: {len(steps)} 条')
    for s in steps[:5]:
        c = s.get('counts', {})
        print(f'  step={s.get(\"step\")} wall={s.get(\"wall_s\")} cycles={c.get(\"cpu_cycles\",0)} t_perf=[{s.get(\"t_start_perf\")},{s.get(\"t_end_perf\")}]')
except Exception as e:
    print('ERROR:', e)
"
