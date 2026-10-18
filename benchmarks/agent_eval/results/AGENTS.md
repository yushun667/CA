# AGENTS.md — 评估结果

`benchmarks/agent_eval` 的原始评估结果 JSON：每条件 run 的 claims、命令、字节、工具调用与延迟。

## 文件

- `pilot_xv6.json`：xv6 pilot 的 90 条 run（15 任务 × A/B/C × 2 次，seed=18，2026-10-18）。**作为证据保留并跟踪**，勿覆盖。
- `llm_pilot_xv6.json`：真实 LLM subagent A/C 小实验评分（3 任务 × A/C × 单次；含 auto F1、manual rubric、effort、信息缺口）。
- `llm_matrix_xv6.json`：真实 LLM 全矩阵（15 任务 × A/B/C × 单次；含 per-cell 指标、effort、幻觉、失败模式、paired delta/sign test）。

## 复现

```bash
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py --build-tasks
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py \
  --out benchmarks/agent_eval/results/pilot_xv6.json
# 真实 LLM 答案评分（.tmp/llm_eval/ 与 .tmp/llm_eval/full/）
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/score_llm.py
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/score_matrix.py \
  --answers .tmp/llm_eval/full --out benchmarks/agent_eval/results/llm_matrix_xv6.json
```

## 局部规则

- 保留少量有结论价值的 JSON 作证据；大体积/临时结果用不同 `--out` 命名，避免覆盖证据文件。
- JSON 与 `tasks.json`、`REPORT.md` 的结论必须同源；换语料时文件名带语料标识（如 `pilot_oh.json`）。
