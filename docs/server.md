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

## 同步 AI 解释（MCP sampling，无队列，UI 默认路径）

Web UI 的“AI 解释”不再创建 job/claim：后端收集函数事实后，**同步**向已连接的
MCP sampling agent 发送标准 JSON-RPC `sampling/createMessage` 请求，等待 agent
用自己的模型作答，直接把结果返回给页面。后端不配置 LLM，也不需要 `CA_AGENT_URL`。

### Agent 会话协议

1. agent 连接 `POST /agent/session`，body `{name, capabilities:{"sampling":{}}}`；
   返回 `{session_id, events_url, response_url, heartbeat_url}`（201）。
2. agent 打开 `GET /agent/session/<id>/events`（SSE）保持长连接；服务端推送
   `event: sampling`，`data` 为标准请求：

   ```json
   {"jsonrpc":"2.0","id":"smp:...","method":"sampling/createMessage",
    "params":{"messages":[{"role":"user","content":{"type":"text","text":"..."}}],
              "systemPrompt":"...","includeContext":"none",
              "modelPreferences":{"costPriority":0.5,"speedPriority":0.5,
                                  "intelligencePriority":0.5},
              "temperature":0.2,"maxTokens":2000,
              "metadata":{"source":"ca-serve","symbol":"kfree","language":"en"}}}
   ```

3. agent 用自己的模型生成回答后，`POST /agent/session/<id>/response`：

   ```json
   {"jsonrpc":"2.0","id":"smp:...",
    "result":{"role":"assistant","content":{"type":"text","text":"..."},
              "model":"<agent-side-model>","stopReason":"endTurn"}}
   ```

4. 心跳 `POST /agent/session/<id>/heartbeat`（SSE ping 也会续期）；断开时
   `DELETE /agent/session/<id>`；空闲 session 超过 120s 清理。
5. 服务端把 `sampling/createMessage` 的 response 与等待中的页面请求配对，
   `POST /api/explain/agent` 同步返回。

### 页面端点

```sh
curl -s -X POST http://127.0.0.1:8765/api/explain/agent   -H 'Content-Type: application/json'   -d '{"symbol":"kfree","lang":"en"}'
```

返回 data：`symbol,function,provider,model,language,explanation,evidence,
fallback_reason,elapsed_ms,facts_summary`。

- 有 sampling agent 会话：`provider:"agent"`，`model` 为 agent 返回的模型名；
- 无会话/超时/agent 错误：HTTP 200 + `provider:"heuristic"`、`fallback:true`、
  `fallback_reason`（例如 `no connected MCP sampling agent session` /
  `sampling request timed out after 30.0s`），页面不阻塞；
- 未知 symbol 404、缺 symbol 400；**不创建 job、不产生 queued/claim**；
- 超时默认 30s（`ca serve --sampling-timeout N` 或 `CA_SAMPLING_TIMEOUT`，
  上限 120s）；页面 fetch 建议 60s 上限。
- 隐私：只把该函数的有界源码/事实发给**已连接的 agent**；未连接 agent 时不发任何
  外部请求。后端不保存解释内容。

### 启动一个 sampling agent 会话

```sh
# 参考客户端（demo handler；真实 agent 用自己的模型实现同一 JSON-RPC 流程）
ca agent-session --serve-url http://127.0.0.1:8765 --model my-agent

# 外部程序处理 sampling 请求（stdin=params JSON，stdout=解释文本或 JSON）
ca agent-session --serve-url http://127.0.0.1:8765 --agent-command ./my-agent
```

`ca agent-session --once` 处理一次后退出（测试/演示）；SIGINT/SIGTERM 会关闭
session。没有 agent 时 UI 仍可用（heuristic 回退），配置即用。

## Explain jobs（兼容/后台自动化，UI 不使用）

没有常驻 agent 时页面点“AI 解释”不应一直 queued：

- explain job 创建后启动宽限期，默认 **2000 ms**；可用 `--explain-grace-ms N`
  或环境变量 `CA_EXPLAIN_GRACE_MS=N` 覆盖，`0` 关闭（完全交给 agent）。
- 宽限期内没有 agent claim，服务自动用现有 heuristic/LLM 解释逻辑完成 job：
  `provider:"heuristic"`（配置 `CA_LLM_*` 时 `"llm"`）、`fallback:true`、
  `auto_completed:true`、`claimed_by:"server:auto"`；事件照常走 SSE `done`。
- agent 先 claim/submit 则自动完成不会覆盖（原子 claim；先到先得）；宽限期内
  agent claim 会取消该 job 的定时器。
- `ca agent-worker --serve-url http://127.0.0.1:8765 [--once --worker ID
  --interval 1.0 --limit N --language en|zh]`：真实 MCP agent worker，轮询
  `explain_jobs` claim → 调后端 `/api/explain` 生成解释（heuristic 或
  `CA_LLM_*`）→ `submit_explanation` 写回，结果
  `provider:"agent", model:"mcp-agent", evidence:[...]`；SIGINT/SIGTERM 优雅退出。
- HTTP `/api/jobs/<id>/result` 同样补 `provider`（默认 `"agent"`，可显式覆盖）。
- UI 无需改动：无 agent 时很快收到 heuristic `done`；有 agent 时显示
  MCP agent + evidence。

```sh
ca serve --db ca.db --port 8765                      # 默认 2s 宽限
ca agent-worker --serve-url http://127.0.0.1:8765    # 常驻 agent 消费
CA_EXPLAIN_GRACE_MS=0 ca serve --db ca.db            # 关闭自动完成
```

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

- `tests/test_server_serve.py`（17 条）：健康/查询/MCP 端点、同步 sampling explain
  （mock agent 成功/超时/无会话回退/无 job）、sampling session 生命周期、
  `ca agent-session --once`、explain job 兼容闭环、`ca mcp --serve-url` 代理、
  联邦 shard 统计。
- 同步 sampling explain：无会话 heuristic 回退 p50 ≈4 ms（10 次，xv6）；
  mock sampling agent 成功路径、超时回退、无 job 创建均有测试覆盖。
- 全仓测试：`439 passed / 3 skipped`（本地 macOS arm64，libclang18）。
- 本地基准（xv6 DB，`benchmarks/results/server_bench.json`）：
  `GET /api/health` p50 2.0 ms / p95 6.2 ms、`/api/search` 1.7/2.3 ms、
  `/api/stats` 1.8/4.8 ms、`/api/jobs` 1.7/2.3 ms；MCP `/mcp` detail
  p50 4.6 ms / p95 15.3 ms；跨客户端闭环（HTTP create → MCP claim →
  submit → done）p50 5.5 ms。SSE 重放 5 个生命周期事件正常。
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
