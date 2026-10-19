# AGENTS.md — server（统一后端 `ca serve`）

持有只读查询服务（单库/联邦）、可写通用 job 状态库与事件总线；HTTP `/api/*`、`/events` SSE、无状态 `/mcp` JSON-RPC 与 stdio 代理共用同一进程状态。

## 关键文件

- `app.py`：`ThreadingHTTPServer`；查询端点复用 `ca.ui.server` 的 `api_*` 与 `QueryService`，新增 `/api/jobs*`、`/events`、无状态 `/mcp`；统一 `{ok,data|error}` 信封；优雅停止。
- `service.py`：`ServeConfig`/`ServeService`（查询服务、`JobService`、无状态 `McpSession`）；通用 job 创建/列表/领取/结果/失败端点；无 explain/sampling。
- `jobs.py`：`JobService` 薄封装 `ca.store.jobs.JobQueue`（每线程独立连接、单写者）+ `EventBus` 发布已追加 job 事件。
- `__init__.py`：导出 `ServeConfig`/`make_server`/`serve`。

## 局部规则

- shard 数据库只读，jobs 只写独立状态库；服务不内置 job 执行器。
- Windows HTTP 监听使用独占地址，拒绝与旧服务复用同一端口；POSIX 保留地址重用行为。
- 修改 `/events` 事件形状/端点/工具数需同步 `docs/server.md/.zh` 与 `tests/test_server_serve.py`。
- 测试：`.venv/bin/python -m pytest tests/test_server_serve.py -q`。
