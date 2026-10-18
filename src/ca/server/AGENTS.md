# AGENTS.md — server（统一后端 `ca serve`）

持有只读查询服务（单库/联邦）、可写任务状态库与事件总线；HTTP `/api/*`、`/events` SSE、`/mcp` JSON-RPC 与 stdio 代理共用同一进程状态。

## 关键文件

- `app.py`：`ThreadingHTTPServer`；查询端点复用 `ca.ui.server` 的 `api_*` 与 `QueryService`，新增 `/api/jobs*`、`/events`、`/mcp`；统一 `{ok,data|error}` 信封；优雅停止。
- `service.py`：`ServeConfig`/`ServeService`（查询服务、`McpSessionRegistry`、`JobService`、`McpSession`）；同步 explain（sampling）；MCP 额外 job 工具（legacy 兼容）；job 创建/领取/提交/失败与 explain 宽限自动完成。
- `mcp_sessions.py`：`McpSessionRegistry` 标准 MCP HTTP 会话（`Mcp-Session-Id`、`capabilities.sampling`）与 `GET /mcp` SSE 反向通道；`sampling/createMessage` 请求/响应配对。
- `agent_explain.py`：`explain_via_sampling`（收集事实→标准 MCP sampling→同步返回；无会话/超时/错误→heuristic 200）。
- `app.py` 的 `/mcp` 支持标准会话：`initialize` 返回 `Mcp-Session-Id`，`GET /mcp` SSE 接收服务端 sampling 请求，`DELETE /mcp` 关闭；`explain_function` 工具走同一通道。
- `jobs.py`：`JobService` 薄封装 `ca.store.jobs.JobQueue`（每线程独立连接、单写者）+ `EventBus` 发布已追加事件。
- `__init__.py`：导出 `ServeConfig`/`make_server`/`serve`。

## 局部规则

- 深析/索引任务只在服务内受控执行；shard 数据库只读，jobs 只写独立状态库。
- 修改 SSE 事件/sampling 端点/结果字段需同步 `docs/server.md/.zh` 与 `tests/test_server_serve.py`。
- 测试：`.venv/bin/python -m pytest tests/test_server_serve.py -q`。
