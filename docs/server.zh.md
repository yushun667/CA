# CA 统一后端服务（`ca serve`）

Web UI 与 MCP 是同一后端的两个客户端，而不是两套后端。`ca serve` 是常驻的
读写服务：进程内持有只读查询服务（单库或联邦 shard）、可写任务状态库
（`JobQueue`）和事件总线；浏览器通过 `/api/*` + `/events` 接入，AI agent 通过
`/mcp`（或 `ca mcp --serve-url` stdio 代理）接入，两端通过任务队列与 SSE 通信。

## 启动

```sh
# 单库：查询只读，jobs 默认写到 <db>.serve-state.db
.venv/bin/ca serve --db /home/yu/CA_OH/openharmony.db --port 8765

# 联邦 shard：查询只读，jobs 默认写到 <db-dir>/.ca/jobs.db
.venv/bin/ca serve --db-dir /home/yu/CA_OH/index --port 8765

# 自定义状态库 / 深析工具需要的仓库上下文
.venv/bin/ca serve --db ca.db --state-db /tmp/ca-jobs.db --repo /path/to/repo
```

- 默认绑定 `127.0.0.1:8765`；`--host 0.0.0.0` 仅供受信网络，**无认证**，不要暴露公网。
- shard 数据库始终只读；jobs 写独立的 SQLite 状态库（WAL），不修改 shard。
- 服务处理 SIGINT/SIGTERM：停止接收请求、关闭连接、任务状态落盘后退出。
- 重启时自动把上次遗留的 `running` 任务恢复为 `queued`（清 claim/lease，保留 attempts）。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 状态、数据库、`features:{jobs,sse,mcp,federated}` |
| GET | `/api/search?q=&limit=&offset=` | 符号搜索（与本地 UI 相同） |
| GET | `/api/function/<name_or_id>` 或 `/api/function?symbol=` | 函数详情 |
| GET | `/api/callers`、`/api/callees`、`/api/graph`、`/api/stats`、`/api/callbacks` | 既有查询 API |
| GET | `/api/jobs?type=&status=&limit=&offset=` | 任务列表 + `stats` |
| GET | `/api/jobs/<id>` | 单任务 |
| POST | `/api/jobs` | 创建任务 `{type|kind, symbol?, payload?}` |
| POST | `/api/jobs/<id>/claim` | 领取任务（agent/自动化） |
| POST | `/api/jobs/<id>/result` | 提交结果 `{explanation, evidence?, worker?}` |
| POST | `/api/jobs/<id>/fail` | 标记失败 `{error}` |
| POST | `/mcp` | MCP JSON-RPC（单条或数组） |

成功信封 `{"ok":true,"data":...}`，失败 `{"ok":false,"error":{"code","message"}}`；
`POST /api/jobs` 返回 201，未知任务 404，状态冲突 409。

```sh
curl -s http://127.0.0.1:8765/api/health
curl -s -X POST http://127.0.0.1:8765/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"type":"explain","symbol":"kfree","payload":{"lang":"en"}}'
curl -s -N http://127.0.0.1:8765/events
```

## Jobs 与 SSE

任务类型：`explain`（首个完整闭环）、`ensure_deep`、`prewarm`、`update`（占位，
后续接执行器；可由 MCP agent 或服务内 worker 执行）。状态：
`queued → running → done | failed`，失败可用 `requeue` 重排。job 对象键：
`id,type,kind,status,symbol,payload,result,error,claimed_by,attempts,created_at,updated_at`。

`GET /events` 是 SSE 事件流：

```
id: 1
data: {"type":"job.updated","event":"created","event_id":1,"job":{...}}

id: 2
data: {"type":"job.updated","event":"claimed","event_id":2,"job":{...}}

id: 3
data: {"type":"job.updated","event":"done","event_id":3,"job":{...}}
```

- `event` ∈ `created|claimed|done|failed|requeued`，`job` 始终是完整快照。
- 每 15s 发送 `: ping` 注释心跳；断线重连带 `Last-Event-ID` 头或
  `?since_id=N`，服务从 `job_events` 表重放缺失事件。
- 事件 id 全局单调；默认保留最近 10000 条。

## MCP 客户端

`/mcp` 是标准 JSON-RPC 端点，工具与本地 `ca mcp --db` 相同（18 个），另加服务端
任务工具：

- `explain_jobs {action:"list"|"claim", worker?, status?, limit?}`：列表或原子领取
  最旧的 queued explain 任务（claim 后返回 `running` 快照）；
- `submit_explanation {job_id, explanation, evidence?, worker?}`：写回说明并把任务
  置为 `done`（触发 SSE `done`，UI 立即显示）。

浏览器薄客户端（UI 本地同源反代 `/api/*` + `/events`，无 CORS）：

```sh
ca ui --serve-url http://127.0.0.1:8765 --port 8766
# 内嵌只读模式仍可用：ca ui --db ca.db
```

stdio 客户端代理到常驻服务（agent 无需自己打开数据库）：

```sh
ca mcp --serve-url http://127.0.0.1:8765
# 或 host:port 简写：ca mcp --serve-url 127.0.0.1:8765
```

## 跨客户端闭环

1. 浏览器 UI 对函数点击“AI 解释” → `POST /api/jobs {type:"explain",symbol}`；
2. MCP agent 用 `explain_jobs {action:"claim"}` 领取任务，生成解释后
   `submit_explanation` 写回；
3. 服务把任务置为 `done` 并追加 `done` 事件，UI 的 `/events` SSE 收到后渲染结果；
4. 若配置了本地 LLM/heuristic 执行器（后续里程碑），服务可直接在 worker 内完成
   explain；当前最小闭环由 agent 领取，失败后 UI 回退同步 `/api/explain`。

## 验证与性能

- `tests/test_server_serve.py`（5 条）：健康/查询/MCP 端点、explain 跨客户端
  job 闭环与 SSE 重放、重启恢复、`ca mcp --serve-url` 代理、联邦 shard 统计。
- 全仓测试：`424 passed / 3 skipped`（本地 macOS arm64，libclang18）。
- 本地基准（xv6 DB，`benchmarks/results/server_bench.json`）：
  `GET /api/health` p50 2.0ms / p95 6.2ms、`/api/search` 1.7/2.3ms、
  `/api/stats` 1.8/4.8ms、`/api/jobs` 1.7/2.3ms；MCP `/mcp` detail
  p50 4.6ms / p95 15.3ms；跨客户端闭环（HTTP create → MCP claim →
  submit → done）p50 5.5ms。SSE 重放 5 个生命周期事件正常。
- `JobQueue` 层（storage 基准）：10k jobs create ≈9,950/s，claim+complete
  ≈5,161/s，4 线程并发 claim 无重复领取。

## 限制与安全

- 单写者：jobs 写入由服务进程串行化（`BEGIN IMMEDIATE`），查询只读并发；
  联邦 shard 不参与写事务，无跨 shard 事务。
- 深析/索引任务（ensure_deep/prewarm/update）当前只有 queued 占位，执行器接入前
  请继续用 CLI；MCP 的 `ensure_deep`/`prewarm` 在联邦后端返回 `FEDERATED_READ_ONLY`。
- 无登录/认证：仅面向单机工作站或 SSH 隧道；默认只绑定 loopback。
- 单文件副本 shard 用 `immutable=1` 只读回退，查询期间 shard 必须静止。
- 状态库是服务自有 SQLite 文件（WAL）；备份/迁移时连同 `-wal/-shm` 一起复制。
