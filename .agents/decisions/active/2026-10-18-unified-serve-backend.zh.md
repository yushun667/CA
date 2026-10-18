# 决策：UI 与 MCP 共用一个后端服务

Status: active
Date: 2026-10-18

## 背景

`ca ui` 与 `ca mcp` 原本各自打开数据库：浏览器 UI 一个进程，每个 agent 一个 MCP
进程。两者读同一索引但看不到彼此的工作，跨客户端流程（人请求 AI 解释、agent 产出）
只能靠文件轮询或带外部地 IPC。索引 shard 必须只读；唯一需要持久化的可变状态是
“待处理任务队列”。

## 决策

运行单一常驻后端 `ca serve`：持有查询访问（单库或联邦 shard）与独立可写 SQLite
状态库中的持久 job 队列；客户端保持薄：

- 浏览器/UI 用 `/api/*` 与 `GET /events`（SSE，支持 `Last-Event-ID` 重放）；
- agent 用 HTTP `/mcp`，或 `ca mcp --serve-url` 把 stdio 代理过来；
- `ca ui --serve-url` 把本地 UI 同源反代到后端，`--db` 内嵌模式保留；
- MCP 在常规工具外看到 `explain_jobs`/`submit_explanation`，形成闭环：UI 创建
  `explain` job → agent 领取并提交 → SSE `done` 更新 UI。

job 使用 storage 的 `JobQueue`（原子 claim/lease、重启恢复、单调 `job_events` id），
保证服务是唯一写者。

## 原因

- 单一查询路径与连接缓存，避免每进程重复启动；所有客户端看到同一 job 状态。
- 独立状态库保持 shard 只读、联邦查询严格只读。
- 持久 job + 事件日志可跨重启，SSE 无需额外 broker 即可重放；复用既有 store schema。
- UI/MCP 内嵌模式保留用于单次本地使用与测试，整体是增量演进而非切换日。

## 考虑过的替代方案

- **UI/MCP 各自后端 + 轮询**：无持久队列与重放、连接重复、难以做跨客户端体验。
- **只有 MCP 后端**：浏览器无法直接消费 JSON-RPC 工具，需要额外协议桥；UI 查询是
  只读的，用 HTTP 更简单。
- **内存 job 队列**：崩溃丢 claim、重启后无法重放 SSE。

## 后果

- `ca serve` 是单写者；索引/深析/update 执行器必须在服务内（或走 CLI），长任务要
  续租 lease。
- 默认 loopback/无认证模型不变；对外仍需 SSH 隧道。
- UI 保留内嵌与 `--serve-url` 两种模式，由 UI 测试覆盖。
- `ensure_deep`/`prewarm`/`update` job 在执行器落地前是占位；文档已说明限制。

## 验证与风险

explain job 后来由 [2026-10-18-remove-ai-explain.zh.md](2026-10-18-remove-ai-explain.zh.md)
细化为同步 MCP sampling（无 job/claim）；jobs 保留给其它任务。

第一里程碑由 `tests/test_server_serve.py`（HTTP 查询、MCP `/mcp`、explain 跨客户端
闭环、SSE 重放、重启恢复、stdio 代理、联邦 shard 统计）与
`benchmarks/results/server_bench.json` 验证：xv6 上 health/search p50 2.0/1.7ms、
MCP detail p50 4.6ms、跨客户端闭环 p50 5.5ms。风险：单进程可用性、默认无认证、
job 执行器落地前部分任务仍需手工。
