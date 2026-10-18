# AGENTS.md — Agent 效用评估

评估 CA 事实数据相对“直接读源码”的 agent 增益：任务集、评分 rubric、可复现三条件 harness（源码 / 基础索引 / 深度事实）与结果报告。

## 关键文件

- `run_eval.py`：受控 A/B/C harness（GT 构建 + 源码校验 + 评分 + 聚合）。
- `tasks.json`：15 个 xv6 任务、322 条源码校验过的 GT 事实、rubric。
- `manual_traces.json`：人工受控 trace（调用链、finding 误报、syscall 盲区）。
- `score_llm.py` / `score_matrix.py`：真实 LLM A/C 与 A/B/C 全矩阵评分（GT 覆盖/引用/effort/paired delta）。
- `OPTIMIZATION_BACKLOG.md`：按证据排序的 CA 优化项；`OH_EXTENSION_DESIGN.md`：OH 扩展设计草案。
- [results/AGENTS.md](results/AGENTS.md)：原始结果 JSON（`pilot_xv6.json` 作证据保留；大体积/临时结果另名不覆盖）。
- `REPORT.md`：pilot 结论、量化 delta、失败案例、局限与下一步。

## 局部规则

- 只读 `src/` 公开接口与 `ca` CLI/MCP；**不修改 `src/` 业务代码**，结果不 git add/commit。
- GT 必须可追溯到源码/人工标注；新增任务须证明能区分 A（直接读码）与 B/C（CA 事实）。
- 三条件必须同任务、同模型、随机顺序、≥2 次重复；记录字节/工具调用/延迟与答案。
- 区分“deterministic policy pilot”与真实 LLM-agent 实验，报告中不得混用结论。
- `results/` 同名 JSON 会覆盖；换语料/DB 用 `--out` 指定不同文件名。
- 新增正式文档按 [.agents/i18n.md](../../.agents/i18n.md) 双语三件套维护。
