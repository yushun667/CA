# 统一后端（`ca serve`）

Status: 阶段 4（单 DB + HTTP 查询 + `/mcp` + job 队列/SSE + 同步 MCP sampling
explain；UI 薄客户端已落地）。

## 组件

| 组件 | 职责 |
|---|---|
| `ca serve`（`src/ca/server/app.py`）| 单个 `ThreadingHTTPServer` 进程：`/api/*` 只读查询端点（复用 `ca.ui.server`）、`/mcp` JSON-RPC、`/events` SSE、`/api/jobs*`；默认 loopback、无认证 |
| `ca.server.service`（`ServeService`）| 持有查询服务（单库/联邦 shard）、sampling gateway、job 服务与一个 `McpSession`；同步 explain、兼容 MCP job 工具、job 创建/领取/提交/失败端点 |
| `ca.server.mcp_sessions`（`McpSessionRegistry`）| 标准 MCP HTTP 会话（`Mcp-Session-Id`、`capabilities.sampling`）与 `GET /mcp` SSE 反向通道；发送 `sampling/createMessage` 并把 client 响应与等待中的 HTTP explain 请求配对 |
| `ca.server.jobs`（`JobService`）| `ca.store.jobs.JobQueue` 的每线程薄适配（独立可写状态库，WAL）+ 进程内 `EventBus`，把新追加的 store 事件转发给 SSE 订阅者 |
| `ca.store.jobs`（`JobQueue`）| 持久 `jobs` + `job_events`（单调 id）、原子 claim/lease、重启恢复、事件重放；既可独立 state DB，也可挂到可写 `SqliteStore` |
| `ca mcp --serve-url URL` | stdio JSON-RPC 代理：每行 stdin POST 到后端 `/mcp`；agent 继续用标准 MCP 工具接口 |
| `ca ui --serve-url URL` | 浏览器薄客户端（ui-engineer）：本地静态服务把 `/api/*`+`/events` 同源反代到后端（无 CORS）；`--db` 内嵌模式不变 |

## 数据流

```text
index shards（只读） ──────┐
                           ├─> ca serve（单进程，loopback）
job state DB（可写） ──────┘        |-> /api/* + /events（SSE） <- 浏览器 UI
                                     |-> /mcp                    <- AI agent（ca mcp --serve-url）
                                     |-> /api/explain/agent      <- 同步 sampling explain（无队列）
                                     |-> GET /mcp (SSE)         <- 标准 MCP 服务端→客户端通道
                                     |-> /api/jobs + JobQueue    <- deep/update 任务（保留 legacy explain job）
```

查询服务只读：联邦 shard 每请求独立只读连接、永不写入。写操作（仅 jobs）进独立
状态库（联邦模式 `<db-dir>/.ca/jobs.db`，单 DB `<db>.serve-state.db`）。服务是
单写者；claim/complete 是原子 `BEGIN IMMEDIATE` 事务，每个 server 线程各自持有
`JobQueue` 连接。

## 同步 explain（MCP sampling）

Web UI 的 AI 解释不再创建 job/等待 claim。`POST /api/explain/agent {symbol,lang}`
同步完成：

1. 后端收集函数事实（detail/源码/branches/returns/resources/findings/call 计数/
   CFG 摘要）。
2. sampling-capable MCP client 保持标准会话：`initialize` 带
   `capabilities.sampling` 返回 `Mcp-Session-Id`；`GET /mcp`（SSE）作为服务端→
   客户端反向通道。
3. 后端向最早连接的 sampling session 发送标准 JSON-RPC
   `sampling/createMessage`（messages/systemPrompt/includeContext/modelPreferences/
   temperature/maxTokens/metadata）；agent 用自己的模型作答。
4. client 把 JSON-RPC response POST 回 `/mcp`（同会话头）；后端与等待中的请求
   配对，在同一 HTTP 响应返回 `provider:"agent"`、client 的 `model`、
   explanation/evidence。`tools/call explain_function` 复用同一 sampling 通道。
5. 无会话/超时（默认 30s，`--sampling-timeout`）或 agent 错误 → HTTP 200 +
   本地 heuristic + `fallback_reason`，页面不阻塞。

client 必须声明 sampling 才能得到 `provider:"agent"`；不再提供额外
agent-session CLI。Jobs/SSE 仍供其它任务（`ensure_deep`/`prewarm`/`update` 占位与 legacy
`explain` 兼容 `ca agent-worker`），UI explain 不使用。该路径无后端 LLM 配置
（不涉及 `CA_AGENT_URL`/`CA_LLM_*`）。SIGINT/SIGTERM 优雅停止并关闭 sampling
session 与队列连接；启动时 `recover_running()` 把中断的 `running` job 重新排队。

1. 浏览器 UI 创建 `POST /api/jobs {type:"explain", symbol}`；
2. MCP agent 调 `explain_jobs {action:"claim", worker}`；job 变 `running`（带 lease、
   `attempts+1`）；
3. agent 调 `submit_explanation {job_id, explanation, evidence}`；job 变 `done` 并追加
   `done` 事件；
4. `GET /events` 推送 `{type:"job.updated", event:"created|claimed|done|failed|
   requeued", job:{...}}`，`id:` 为事件 id；客户端带 `Last-Event-ID`/`?since_id=`
   重连并重放缺失事件。

`ensure_deep`/`prewarm`/`update` job 在执行器落地前是 queued 占位；这些操作仍走
CLI。SIGINT/SIGTERM 优雅停止并关闭队列连接；启动时 `recover_running()` 把中断的
`running` job 重新排队。

## 限制

- 无认证：仅 loopback（远程用 SSH 隧道），与 `ca ui`/`ca mcp` 相同信任模型。
- 无跨 shard 事务；shard 永不写入。单文件 shard 副本使用只读 `immutable=1`
  回退，查询期间必须静止。
- 无后端 LLM：同步 explain 需要已连接的 sampling agent；无 agent 时端点返回
  本地 heuristic（HTTP 200）。jobs 仅保留给 deep/update 与 legacy explain 自动化。
