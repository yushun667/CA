# Agent 效用评估 harness（benchmarks/agent_eval）

回答：**agent 拿到 CA 事实数据，比直接读源码，到底有什么提升？**
pilot 结论见 [REPORT.md](REPORT.md)，原始结果见 `results/pilot_xv6.json`。

## 布局

| 文件 | 作用 |
|---|---|
| `run_eval.py` | 三条件受控 harness：构建 GT、执行 A/B/C、评分、聚合、写 JSON |
| `tasks.json` | 15 个 xv6 任务 + 322 条源码校验过的 GT 事实 + rubric（由 `--build-tasks` 生成） |
| `manual_traces.json` | 3 个人工受控 trace（D3 链路、D5 误报、C5 syscall 盲区） |
| `score_matrix.py` | 真实 LLM A/B/C 全矩阵评分（45 runs；引用/调用者/effort/失败模式/paired delta） |
| `OPTIMIZATION_BACKLOG.md` | 按 LLM 证据排序的 CA 优化 backlog（含收益/成本/优先级） |
| `OH_EXTENSION_DESIGN.md` | OpenHarmony 扩展评估设计草案（6–9 任务，待 shard DB） |
| `score_llm.py` | 真实 LLM A/C 答案评分（引用抽取 + GT 覆盖 + 人工 rubric + effort） |
| `results/pilot_xv6.json` | 90 条原始 run（15 任务 × 3 条件 × 2 次，随机顺序） |
| `results/llm_pilot_xv6.json` | 6 条真实 LLM subagent 答案评分（3 任务 × A/C） |
| `results/llm_matrix_xv6.json` | 45 条真实 LLM 全矩阵（15 任务 × A/B/C；含 paired delta/失败模式/effort） |
| `REPORT.md` | 方法、聚合表、量化 delta、失败案例、推荐、局限 |

## 条件

- **A source-only**：只有源码树，`grep -rn` + 读文件；按命中文件/函数 span 记账字节。
- **B CA basic**：`ca query functions/callers/callees/detail`。
- **C CA deep**：B + `branches/returns/resources/assignments/leaks` + `ca graph --depth`。

三条件共用同一事实抽取规则，输出 file:line:类别 或调用者函数名集合，
与 GT 做事实级 P/R/F1；同时记录读取字节、工具调用数、延迟。

## 运行

```bash
# 需要 xv6 DB 与源码（默认 .tmp/lead/final_xv6.db / .tmp/corpus/xv6-riscv）
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py --build-tasks
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py \
  --out benchmarks/agent_eval/results/pilot_xv6.json --reps 2 --seed 18
# 真实 LLM 答案评分（答案目录 .tmp/llm_eval/ 与 .tmp/llm_eval/full/）
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/score_llm.py
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/score_matrix.py \
  --answers .tmp/llm_eval/full --out benchmarks/agent_eval/results/llm_matrix_xv6.json
```

换语料/DB：`--db <db> --corpus <src-root>`；`results/` 同名文件会被覆盖，换语料请换 `--out`。

## 已知限制

- 本 harness 是 **deterministic policy pilot**，不调用 LLM：测“事实获取成本/可得性”，
  不测 LLM 推理。真实 LLM A/C 小实验见 REPORT.md §6（6 runs，结果 `results/llm_pilot_xv6.json`），下一步协议见 §7。
- GT 由 DB 枚举后逐条源码行校验；C 的满分接近同义反复（见 REPORT.md §8）。
- `call_targets` 为空；syscall 函数指针表等间接边缺失，影响面分析需人工补（C5）。
