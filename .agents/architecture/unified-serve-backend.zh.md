# 统一后端（`ca serve`）

Status: 已交付（阶段 3/4 基础设施）；AI 解释功能已移除（见
`../decisions/active/2026-10-18-remove-ai-explain.md`）。

## 组件

| 组件 | 职责 |
|---|---|
| `ca serve`（`src/ca/server/app.py`）| 单个 `ThreadingHTTPServer` 进程：`/api/*` 只读查询端点（复用 `ca.ui.server`）、无状态 MCP JSON-RPC `/mcp`、`/events` SSE 与通用 `/api/jobs*`；默认 loopback、无认证 |
| `ca.server.service`（`ServeService`）| 持有查询服务（单库/联邦 shard）、通用 job 服务与一个无状态 `McpSession`；job 创建/列表/领取/结果/失败端点；无 AI/explain/sampling 状态 |
| `ca.server.jobs`（`JobService`）| `ca.store.jobs.JobQueue` 的每线程薄适配（独立可写状态库，WAL）+ 进程内 `EventBus`，把新追加的 job 事件转发给 SSE 订阅者 |
| `ca.store.jobs`（`JobQueue`）| 持久通用 `jobs` + `job_events`（单调 id）、原子 claim/lease、重启恢复、事件重放；独立 state DB |
| `ca mcp --serve-url URL` | stdio JSON-RPC 代理：每行 stdin POST 到后端 `/mcp`；无状态、18 个工具 |
| `ca ui --serve-url URL` | 浏览器薄客户端（ui-engineer）：本地静态服务把 `/api/*`+`/events` 同源反代到后端（无 CORS）；`--db` 内嵌模式不变 |

## 数据流

```text
index shards（只读） ──────┐
                           ├─> ca serve（单进程，loopback）
job state DB（可写） ──────┘        |-> /api/* + /events（SSE） <- 浏览器 UI
                                     |-> /mcp（无状态）          <- AI agent / ca mcp --serve-url
                                     |-> /api/jobs + JobQueue   <- 通用后台任务
```

查询服务只读：联邦 shard 每请求独立只读连接、永不写入。写操作（仅通用 jobs）进独立
状态库（联邦模式 `<db-dir>/.ca/jobs.db`，单 DB `<db>.serve-state.db`）。服务是
job 单写者；claim/complete 是原子 `BEGIN IMMEDIATE` 事务，每个 server 线程各自持有
`JobQueue` 连接。

## 通用 jobs

`/api/jobs` 是通用后台任务队列，`kind` 为自由字符串（如 `ensure_deep`、`prewarm`、
`update`），状态 `queued → running → done | failed`，可 `requeue`。事件
（`job_events`，单调 id）通过 `/events` SSE 按 `Last-Event-ID`/`?since_id=` 重放。
不内置 worker/执行器：消费者通过 HTTP claim/complete；索引/深析/update 仍由 CLI
执行。队列跨重启（`recover_running()` 重排中断任务）。

## MCP 端点

`/mcp` 是无状态 JSON-RPC 端点（initialize/tools/list/tools/call），暴露 18 个只读
工具；`ca mcp --serve-url` 把 stdio 代理到它。没有 MCP 会话状态、服务端反向请求、
sampling 或 explain 工具。

## 限制

- 无认证：仅 loopback（远程用 SSH 隧道），与 `ca ui`/`ca mcp` 相同信任模型。
- 无跨 shard 事务；shard 永不写入。单文件 shard 副本使用只读 `immutable=1`
  回退，查询期间必须静止。
- 不内置 job 执行器；队列是未来 worker 的基础设施。
