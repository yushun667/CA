# CA 统一后端服务（`ca serve`）

`ca serve` 是面向浏览器与 agent 客户端的常驻后端：持有只读查询（单库或联邦
shard）、独立可写状态库中的通用 job 队列，以及无状态 MCP 端点 `/mcp`；浏览器用
`/api/*` + `/events`，agent 用 `/mcp` 或 stdio 代理 `ca mcp --serve-url`。不内置
AI 解释功能。

## 启动

```sh
# 单库：查询只读；通用 jobs 默认写 <db>.serve-state.db
.venv/bin/ca serve --db /home/yu/CA_OH/openharmony.db --port 8765

# 联邦 shard：通用 jobs 默认写 <db-dir>/.ca/jobs.db
.venv/bin/ca serve --db-dir /home/yu/CA_OH/index --port 8765

# 自定义状态库 / 深析工具需要的仓库上下文
.venv/bin/ca serve --db ca.db --state-db /tmp/ca-jobs.db --repo /path/to/repo
```

- 默认绑定 `127.0.0.1:8765`；`--host 0.0.0.0` 仅供受信网络（无认证）。
- shard 数据库始终只读，服务永不写 shard。
- SIGINT/SIGTERM 优雅退出；重启时把中断的 `running` job 重新排队。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 状态、数据库、`features:{jobs,sse,mcp,federated}` |
| GET | `/api/search?q=&limit=&offset=` | 符号搜索（与本地 UI 相同） |
| GET | `/api/function/<name_or_id>` 或 `/api/function?symbol=` | 函数详情 |
| GET | `/api/callers`、`/api/callees`、`/api/graph`、`/api/stats`、`/api/callbacks` | 既有查询端点 |
| GET | `/api/jobs?kind=&status=&limit=&offset=` | 通用 job 列表 + `stats` |
| GET | `/api/jobs/<id>` | 单个 job |
| POST | `/api/jobs` | 创建 `{kind,payload?}` |
| POST | `/api/jobs/<id>/claim` | 原子领取 queued job（`worker`） |
| POST | `/api/jobs/<id>/result` | 完成 running job（`{result}`） |
| POST | `/api/jobs/<id>/fail` | 标记 running job 失败（`{error}`） |
| POST | `/mcp` | 无状态 MCP JSON-RPC（initialize/tools/list/tools/call） |

成功信封 `{"ok":true,"data":...}`；失败 `{"ok":false,"error":{"code","message"}}`。
`POST /api/jobs` 返回 201；未知 job 404；状态冲突 409。

```sh
curl -s http://127.0.0.1:8765/api/health
curl -s -X POST http://127.0.0.1:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"kind":"update","payload":{"note":"generic background task"}}'
curl -s 'http://127.0.0.1:8765/api/jobs?status=queued'
curl -s -N http://127.0.0.1:8765/events
```

## 通用 jobs 与 SSE

Jobs 是通用后台任务队列（kind 为自由字符串，例如 `ensure_deep`、`prewarm`、
`update`；不含 explain 类型/特殊 AI 语义）。状态：
`queued → running → done | failed`；`requeue` 可重新排队。job 键：
`id,kind,status,payload,result,error,claimed_by,attempts,created_at,updated_at`。
队列持久化在状态库（`JobQueue`：原子 claim/lease、重启恢复），服务只提供通用
HTTP claim/result API，不内置 worker。

`GET /events` 是 job 变更的 SSE 流：

```
id: 1
data: {"type":"job.updated","event":"created","event_id":1,"job":{...}}

id: 2
data: {"type":"job.updated","event":"claimed","event_id":2,"job":{...}}

id: 3
data: {"type":"job.updated","event":"done","event_id":3,"job":{...}}
```

- `event` ∈ `created|claimed|done|failed|requeued`，`job` 为完整快照。
- 每 15s `: ping` 心跳；重连带 `Last-Event-ID` 或 `?since_id=N` 重放缺失事件。
- 事件 id 全局单调；保留最近 10000 条。

## MCP 端点（无状态）

`/mcp` 是标准无状态 JSON-RPC 端点（initialize/tools/list/tools/call），工具与
`ca mcp --db` 相同的 18 个只读工具。`ca mcp --serve-url URL` 把 stdio JSON-RPC
代理到该端点；没有会话状态，也没有服务端发起的反向请求。

```sh
ca mcp --serve-url http://127.0.0.1:8765
```

## 验证与性能

- `tests/test_server_serve.py`：健康/查询端点、无状态 MCP（18 工具）、通用 job
  闭环 + SSE 重放、重启重排、stdio 代理、联邦 shard stats、已移除 explain 的
  CLI/API 断言。
- 全仓测试：本地 `pytest -q`（见项目测试汇总）。
- 本地基准（xv6 DB，`benchmarks/results/server_bench.json`）：health/search p50
  ≈2ms、MCP detail p50 ≈5ms、通用 job create→claim→done p50 ≈5ms。

## 限制与安全

- jobs 单写者；查询只读并发；shard 不参与写事务（无跨 shard 事务）。
- 深析/索引执行器不内置：通过 HTTP claim/complete 通用 job，或继续用 CLI 跑
  `ensure_deep`/`prewarm`/`update`。
- 无认证：仅 loopback（远程用 SSH 隧道）。单文件 shard 副本使用只读
  `immutable=1` 回退，查询期间必须静止。
- 状态库是服务自有 SQLite（WAL）；备份时连同 `-wal/-shm` 一起复制。
