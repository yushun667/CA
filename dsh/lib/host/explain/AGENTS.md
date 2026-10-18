# AGENTS.md — Host AI 解释服务

`createExplainService(ctx, {backend, config})`：用 DSH 原生模型能力解释 CA 索引中的函数。
CA 后端保持只读、不持有任何 LLM 配置（见 `.agents/decisions/active/2026-10-18-remove-ai-explain.md`）。

## 关键文件

- `index.js`：只读事实采集（`/api/function|source|callers|callees|graph`，有界裁剪）、证据编号 prompt、`llm.stream` 流聚合、超时/abort/并发限制、`ExplainError`。

## 局部规则

- 模型能力只来自 Host 注入的 `llm` 与 `agentDefaultModel`（可选服务用 `ctx.get` 退化）；缺任一时抛 `EXPLAIN_UNAVAILABLE`。
- 事实与 prompt 必须有界（源码约 200 行、字段裁剪、总字符预算）；模型只能依据给定证据回答。
- 失败一律归一为带稳定 `code` 的 `ExplainError`，由 RPC/tools 层映射；不把堆栈塞进返回值。
- 测试：`dsh/test/explain/host-explain.test.mjs`（fake backend/llm）；`cd dsh && node --test`。
