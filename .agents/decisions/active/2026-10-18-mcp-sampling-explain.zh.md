# 决策：用 MCP sampling 做同步 explain（无后端 LLM、无 job 队列）

Status: active
Date: 2026-10-18

## 背景

Web UI 原先把 explain 做成 job：浏览器创建 job、MCP agent claim 并提交、UI 通过
SSE 更新。用户否定了这种交互 explain 流程（排队/claim 增加延迟且需要常驻 agent），
也否定了后续在后端配置 LLM 端点（`CA_AGENT_URL`/`CA_LLM_*`）的方案：模型应当是
用户 MCP agent 已在使用的那个。agent 是客户端；服务端不应持有模型凭据或 provider
配置。

## 决策

explain 改为**服务端→客户端 MCP sampling 同步调用**：

1. sampling-capable MCP client 用标准 `/mcp` 会话：`initialize` 带
   `capabilities.sampling` 返回 `Mcp-Session-Id`，`GET /mcp`（SSE）作为服务端→
   客户端反向通道；
2. Web UI 调 `POST /api/explain/agent {symbol,lang}`；
3. 服务端收集有界函数事实/源码，向最早连接的 sampling session 发送标准 JSON-RPC
   `sampling/createMessage`；
4. client 用自己的模型回答，POST 回 `/mcp`（同会话头，
   `CreateMessageResult`）；
5. 服务端在同一 HTTP 响应返回 `provider:"agent"`、agent 的 `model`、explanation 与
   evidence。

无已连接 sampling agent、超时或 agent 错误时，端点返回 HTTP 200 + 本地 heuristic
与 `fallback_reason`。explain 不创建 job、不 claim、不出现 queued；后端不配置 LLM。
job 队列仍供其它任务（`ensure_deep`/`prewarm`/`update` 占位与 legacy explain
自动化 `ca agent-worker`）。

## 原因

- agent 已持有模型与凭据；转发 sampling 请求让服务端无需凭据且与 provider 无关。
- 同步请求/响应去掉队列延迟与“没有 agent 时卡住”的状态；heuristic 回退保证页面
  始终响应。
- 标准 MCP sampling 让真实 MCP agent 无需自定义 API；不再提供 agent-session CLI
  或 demo 客户端。
- MCP sampling 是服务端发起的请求，普通 HTTP `/mcp` 请求/响应没有服务端→客户端的
  通道，因此需要长连接会话 + SSE 侧通道。

## 考虑过的替代方案

- **explain job + claim（旧方案）**：用户否定其交互 explain 用途；仅保留 legacy/
  后台自动化。
- **后端配置 OpenAI 兼容端点（`CA_AGENT_URL`）**：否定，重复 agent 模型/凭据且让
  后端调用外部模型；已从 serve 路径移除。
- **直接同步 HTTP 调用 agent URL**：同样因凭据/模型归属被否定；sampling 让 agent
  保持控制。

## 后果

- 交互 explain 需要已连接 sampling agent 才有 `provider:"agent"`；否则用户得到
  本地 heuristic（200）和明确的 fallback reason。
- 会话长期存在并由 heartbeat/TTL 管理（空闲 >120s 清理）；agent 断开期间 explain
  回退，重连后恢复。
- 服务端可能把有界源码/事实发给已连接 agent；隐私文档必须说明，且无 agent 时不发
  外部请求。
- Job/SSE API 对其他任务保持稳定，现有客户端/测试不回归。

## 验证与风险

`tests/test_server_serve.py` 覆盖 mock sampling 成功（断言 MCP 请求形状）、超时
回退、无会话回退、会话生命周期、无 job 创建（标准 MCP client）。无会话
heuristic 回退在 xv6 上 p50 ≈4ms。风险：不实现 sampling 的 agent 只能看到 heuristic；
慢 agent 会占用一个 HTTP worker 至超时；会话状态仅进程内（重启后 agent 需重连）。
