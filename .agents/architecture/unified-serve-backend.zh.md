# 统一后端（`ca serve`）

Status: 阶段 4 第一里程碑（单 DB + HTTP 查询 + `/mcp` + 一个 explain job 闭环 +
SSE；UI 薄客户端并行落地）。

## 组件

| 组件 | 职责 |
|---|---|
| `ca serve`（`src/ca/server/app.py`）| 单个 `ThreadingHTTPServer` 进程：`/api/*` 只读查询端点（复用 `ca.ui.server`）、`/mcp` JSON-RPC、`/events` SSE、`/api/jobs*`；默认 loopback、无认证 |
| `ca.server.service`（`ServeService`）| 持有查询服务（单库/联邦 shard）、job 服务与一个 `McpSession`；增加 MCP job 工具 `explain_jobs`/`submit_explanation`；job 创建/领取/提交/失败端点 |
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
                                     |-> /api/jobs + JobQueue    <- explain/deep/update 所有权
```

查询服务只读：联邦 shard 每请求独立只读连接、永不写入。写操作（仅 jobs）进独立
状态库（联邦模式 `<db-dir>/.ca/jobs.db`，单 DB `<db>.serve-state.db`）。服务是
单写者；claim/complete 是原子 `BEGIN IMMEDIATE` 事务，每个 server 线程各自持有
`JobQueue` 连接。

## 跨客户端 job 闭环

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
- 暂无服务内 LLM 执行器：explain job 由 MCP agent 完成（或后续 server worker）；
  无 agent 时 UI 回退同步 `/api/explain`。
