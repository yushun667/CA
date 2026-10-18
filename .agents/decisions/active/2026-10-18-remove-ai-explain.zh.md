# 决策：移除 AI 解释功能

Status: active
Date: 2026-10-18

## 背景

Web UI 曾提供“AI 解释”tab，底层依次尝试过 job/claim 队列、后端配置 LLM 端点、
向已连接 agent 做 MCP sampling。用户否定了所有这些方案：不希望 CA 持有 LLM 配置；
job 队列/claim 不适合解释交互；而要求 sampling-capable MCP client 实际不可用
（没有可用 sampling 客户端，且服务端反向 sampling 带来大量会话/协议复杂度）。
因此决定彻底移除该功能，而不是保留半可用入口。

## 决策

端到端删除整个 AI 解释功能：

- UI：删除 explain tab、explain state/请求/i18n keys 与 explain 端点代理白名单。
- 后端：删除 `/api/explain`、`/api/explain/agent`、`api_explain` 与
  `agent_explain`；删除 MCP sampling（会话、`sampling/createMessage`、SSE 反向
  通道、health/CLI/env 字段），`/mcp` 恢复无状态 JSON-RPC（18 个只读工具）。
- MCP 工具：删除 `explain_function`、`explain_jobs`、`submit_explanation`。
- CLI：删除 `ca agent-worker` 与 explain 自动完成逻辑。
- 保留通用 `JobQueue` / `/api/jobs` / `/events` SSE 基础设施，供未来后台任务
  （自由 kind，如 `ensure_deep`/`prewarm`/`update`）使用；不含 explain 类型/示例。

## 原因

- 实际没有可用 sampling 客户端；继续保留管道会形成半可用功能与维护负担。
- CA 不应持有 LLM 凭据/provider 配置；分析事实与查询才是产品面。
- 通用 job 队列有独立价值，保留基础设施；只删除 explain 专用语义。
- 删除 UI 入口，避免用户面对无法可靠工作的功能。

## 考虑过的替代方案

- **保留 MCP sampling**：否；没有 sampling-capable client，会话/反向请求协议对
  一个始终降级的功能过重。
- **后端配置 LLM**：此前已否（凭据/provider 归属，用户明确拒绝）。
- **只隐藏 UI、保留 API**：否；死代码与半可用端点造成困惑。

## 后果

- Web UI 不再有 AI 解释入口；用户直接查询事实、阅读代码或使用自己的 agent 流程。
- `/mcp` 恢复无状态（18 工具），不再需要 MCP 会话维护。
- 通用 job 队列/SSE 基础设施保留，文档说明为后台任务管道；不内置 worker。
- explain/sampling 的测试/文档/决策被删除或标记 superseded。

## 验证与风险

`tests/test_server_serve.py` 覆盖健康/查询端点、无状态 MCP（18 工具）、通用 job
闭环 + SSE 重放、重启重排、stdio 代理、联邦 stats，并断言 explain 端点/工具/CLI
已不存在。风险：未来若再要 AI 解释，需要新的设计讨论（可能放 agent 侧工具而非
CA 自有功能）。
