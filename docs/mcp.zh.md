# CA MCP 查询接口

CA 跑在源码所在的机器上，AI agent 不复制源码树，而是通过 MCP（Model Context
Protocol，JSON-RPC 2.0）查询索引数据库。

提供两种传输：

| 传输 | 适用场景 | 命令 |
|---|---|---|
| `stdio`（默认） | 本地 agent 进程，或经 SSH 调用的 agent | `ca mcp --db /path/index.db` |
| `http` | 局域网客户端，或只支持 streamable HTTP 的 agent | `ca mcp --db /path/index.db --transport http --port 8765` |

`stdio` 从 stdin 读取按行分隔的 JSON-RPC 消息，向 stdout 每行写一个 JSON-RPC
响应，诊断信息走 stderr。HTTP 传输提供 `POST /mcp`（一次一个 JSON-RPC 请求或
batch）与 `GET /health`，默认绑定 `127.0.0.1`——请保持本地并通过 SSH 隧道访问，
或明确配置防火墙放行。

同一个数据库的人类可读界面由 `ca ui --db <db>` 提供（同样只绑回环地址）；见
[ui.zh.md](ui.zh.md)。

## 服务器侧准备

```sh
# 在源码服务器上执行一次
scripts/bootstrap.sh                       # venv + 固定依赖 + editable 安装
.venv/bin/ca index --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/openharmony/out/rk3568/compile_commands.json \
    --db /home/yu/CA_OH/openharmony.db --jobs 24

# 离线批量索引与查询服务分离
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db            # stdio
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db \
    --transport http --host 127.0.0.1 --port 8765               # HTTP
```

`scripts/server_bootstrap.sh` 封装了验证：检查 venv、跑全量测试，并用临时数据库
做一次 stdio MCP 冒烟。

## 工具清单

所有工具只读。列表型工具返回 `{"items": [...], "count": N, "limit": L,
"offset": O}`；`graph` 返回下述图结构；`detail`/`stats` 直接返回对象。limit 会被
限制在 `1..1000`；`offset` 通过多取 `limit + offset` 行再切片实现（查询走索引，
小 offset 开销低）。

| 工具 | 参数 | 返回 |
|---|---|---|
| `find_symbol` | `pattern`（必填）、`limit`、`offset` | 先精确匹配 id/name/qualified name，再子串匹配函数 |
| `functions` | 同 `find_symbol` | `find_symbol` 的别名 |
| `callers` | `symbol`（必填）、`limit`、`offset` | 调用该符号的调用点（direct + 候选边） |
| `callees` | `symbol`（必填）、`limit`、`offset` | 该符号调用的调用点（direct + 候选边） |
| `graph` | `symbol`（必填）、`direction`（`forward`/`reverse`/`both`，默认 `forward`）、`depth`（0–12，默认 1）、`limit`（默认 500） | 规范化调用图，见下 |
| `branches` | `symbol`（必填）、`limit` | 函数内分支/条件事实 |
| `assignments` | `symbol`（必填）、`limit` | 函数内赋值事实 |
| `returns` | `symbol`（必填）、`limit` | 显式返回路径 |
| `resources` | `symbol`（必填）、`limit` | acquire/release 资源操作 |
| `leaks` | `kind`（默认 `leak`）、`resource_kind`、`limit` | 数据流结论，可按资源类型过滤 |
| `detail` | `symbol`（必填）、`fields`（可选投影）、`compact`（默认 false） | 函数记录 + 分支/赋值/返回/资源/结论；投影可缩减载荷 |
| `brief` | `symbol`（必填） | 紧凑摘要：签名/位置、调用者/被调者计数、分支/返回/资源计数、关键条件、deep_ready |
| `batch` | `kind`（必填）、`symbols` 或 `symbols_file`、`limit` | 一次查询多个符号；按稳定顺序返回逐符号结果 |
| `stats` | – | 各表行数、数据库大小、schema 版本，以及 `callbacks` 注册/连边统计 |
| `callbacks` | `path`（object_path 前缀）、`field`、`module`、`limit` | 跨 TU 回调注册事实（对象路径、目标函数、注册 kind、evidence） |
| `ensure_deep` | `symbols`（必填）、`jobs`、`wait`（默认 true） | L1 按需深析：解析所属 TU、分析请求函数、标记 deep_ready；返回每符号状态/延迟/事实 |
| `deep_status` | `symbols`（可选） | deep_ready 覆盖率（`total_functions/deep_ready/pending/failed/coverage`）与每符号状态 |
| `prewarm` | `limit`、`jobs`、`background` | 分析查询频次最高的待深析符号（L2 队列）；background=true 立即返回 |

### 统一后端（`ca serve`）

agent 可通过 stdio 代理连接常驻后端，而不必每进程打开数据库：
`ca mcp --serve-url http://127.0.0.1:8765`。后端 `/mcp` 是无状态 MCP HTTP
JSON-RPC 端点，工具与 `ca mcp --db` 相同的 18 个只读工具；没有会话状态、服务端
反向请求或 AI 解释功能。

### 联邦模式（`--db-dir`）

`ca mcp --db-dir <dir>`（或重复 `--db`）以只读联邦方式暴露目录下所有 shard 数据库；
合并后的每行增加 `shard` 字段，`stats` 返回汇总 totals 加 `shards`/`skipped`；`graph`
跨 shard 合并节点/边，候选边保留 `source/candidate/kind` 并带 `shard`；候选虚线边表示可能目标集合。
server 仍暴露同样 18 个工具。写型深析工具返回 `FEDERATED_READ_ONLY`（深析状态是
分片本地的）：`ensure_deep`/`prewarm` 需用单 `--db` server；`deep_status` 以只读方式
返回每个符号的 `deep_ready`/`shard` 信息。不可读 shard 跳过并 stderr 警告；没有可读
shard 时 server exit 1。shard 可原地提供（带 `-wal`/`-shm`）或单文件副本（只读
`immutable=1` 回退，略慢；查询期间 shard 必须静止）。

```sh
ca mcp --db-dir /home/yu/CA_OH/index
# HTTP: ca mcp --db-dir /home/yu/CA_OH/index --transport http --port 8765
```

### `graph` 返回结构

```json
{
  "root": "syscall",
  "direction": "forward",
  "depth": 2,
  "nodes": [{"id": "fn:..", "name": "syscall", "file": "kernel/syscall.c", "line": 131, "depth": 0, "direction": "forward"}],
  "edges": [{"caller_name": "syscall", "callee_name": "sys_open", "kind": "direct",
             "source": "direct", "candidate": false,
             "is_resolved": true, "call_file": "kernel/syscall.c", "call_line": 143,
             "depth": 1, "cycle": false}],
  "cycles": [], "truncated": false,
  "stats": {"nodes": 54, "edges": 178, "cycles": 0}
}
```

直接调用 `source = "direct"`；来自 `call_targets` 的函数指针/虚调用候选边
`source = "candidate"`、`candidate = true`，保留解析 `kind` 与可读 `reason`
（注册点、override 深度/关系、实参传递等）。候选边表示间接调用点的可能目标集合，
不是评分预测；渲染为虚线，表示无序的目标集合。节点与边排序稳定；`truncated`
表示节点上限截断了遍历。

## 协议示例

最小 stdio 会话（initialize、列工具、调用一个工具，然后 EOF）：

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"demo","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"graph","arguments":{"symbol":"syscall","depth":2}}}' \
  | .venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db
```

`detail`、`graph`、`branches`、`assignments`、`returns`、`resources` 增加
`deep_ready` 标记（事实列表在未深析时还会带 `pending: true`）；`leaks` 的
`deep_ready` 为 `null`（findings 跨函数）。server 必须带 `--repo/--compdb` 才能提供
`ensure_deep`/`prewarm`，否则这些工具返回 `DEEP_UNAVAILABLE`。分层语义详见
[layered-indexing.zh.md](layered-indexing.zh.md)。

每次 `tools/call` 的结果是一个 text content block，内含单个 JSON 信封：

```json
{"content":[{"type":"text","text":"{\"ok\": true, \"tool\": \"callers\", \"data\": {\"items\": [...], \"count\": 3, \"limit\": 50, \"offset\": 0}}"}],"isError": false}
```

工具级错误 `isError: true`，信封结构不变：

| 错误码 | 含义 |
|---|---|
| `INVALID_ARGUMENT` | 参数缺失/为空、direction/depth/limit 非法 |
| `NOT_FOUND` | `detail` 未找到符号 |
| `UNKNOWN_TOOL` | 工具名不在 `tools/list` 中 |
| `DB_NOT_FOUND` | `--db` 路径不存在 |
| `DB_ERROR` | store/数据库错误 |

JSON-RPC 层错误使用标准错误码：`-32700` 解析错误、`-32600` 非法请求、
`-32601` 方法不存在、`-32602` 参数错误、`-32603` 内部错误。

## Agent 接入方式

本地（CA 与服务在同一环境）：

```python
# 任意支持 MCP 的客户端，概念上：
client.start_stdio(["/home/yu/CA_OH/venv/bin/ca", "mcp", "--db", "/home/yu/CA_OH/openharmony.db"])
client.call_tool("find_symbol", {"pattern": "OHOS_", "limit": 20})
```

远程经 SSH、不开端口（推荐；CA 代码与 DB 都留在源码服务器）：

```sh
ssh -o BatchMode=yes yu@192.168.0.100 \
  '/home/yu/CA_OH/venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db'
```

Agent 把该 SSH 命令当作 stdio MCP server 启动即可。需要 HTTP 的客户端改用端口
转发：

```sh
ssh -N -L 8765:127.0.0.1:8765 yu@192.168.0.100
# 客户端 endpoint: http://127.0.0.1:8765/mcp ，健康检查: http://127.0.0.1:8765/health
```

如果必须绑定 `--host 0.0.0.0`，请在 8765 前加防火墙；该接口没有鉴权层。

## 全量分片

阶段 3 验收把 OpenHarmony 分成 8 个分片索引到 `/home/yu/CA_OH/index/`
（`oh_S1.db` … `oh_S8.db`，compdb 为
`/home/yu/CA_OH/corpus/openharmony/shards/shard_S<N>.json`，analyze scope=main）。
每个分片都是一个完整 CA 数据库：把 MCP server 指向包含目标模块的分片，或在不同
端口为每个分片各起一个 server：

```sh
# 服务器上（推荐 agent 用 stdio）
/home/yu/CA_OH/venv/bin/python -m ca mcp \
    --db /home/yu/CA_OH/index/oh_S1.db \
    --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/shards/shard_S1.json

# 每分片 HTTP（只绑本地；开发机通过 SSH 隧道访问）
/home/yu/CA_OH/venv/bin/python -m ca mcp --db /home/yu/CA_OH/index/oh_S1.db \
    --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/shards/shard_S1.json \
    --transport http --port 8765
# 开发机：
ssh -N -L 8765:127.0.0.1:8765 yu@192.168.0.100
# MCP endpoint: http://127.0.0.1:8765/mcp    健康检查: /health
```

可用
`python scripts/verify_shard_mcp.py --db <shard.db> --repo <repo> --compdb <shard.json> --out <report.json>`
快速验证分片：脚本用纯 stdlib 客户端驱动真实 stdio server（18 个工具、
`deep_status`、`ensure_deep` 首次与命中延迟、`graph`）以及 HTTP server
（`/health` + `POST /mcp` tools/list）。候选边抽样用
`python scripts/sample_candidates.py --db <shard.db> ...`（总量、分层样本、
direct 对照、graph/DOT 集成）。lazy deep 需要与上面完全一致的 `--repo/--compdb`；`ensure_deep`
只解析符号所属 TU，因此可在其他分片仍在索引时安全地对已完成分片使用。

## 服务器验证

```sh
rsync -az --delete --exclude .git --exclude .venv --exclude .tmp \
    ./ yu@192.168.0.100:/home/yu/CA_OH/ca/
ssh yu@192.168.0.100 'cd /home/yu/CA_OH/ca && /home/yu/CA_OH/venv/bin/python -m pytest -q'
ssh yu@192.168.0.100 'cd /home/yu/CA_OH/ca && bash scripts/server_bootstrap.sh'
```

冒烟测试使用与上面相同的 stdio 协议；`tools/list` 或 `tools/call` 不符合预期即
判定失败。

## 限制

- 查询结果来自最近一次 `ca index`/`ca update`；MCP server 自身不监听文件系统
  （离线/实时模型与 watcher 设计见 [incremental.zh.md](incremental.zh.md)）。
- 每个 server 进程对应一个数据库；多项目/工作区联合查询留待后续。
- 候选边需要由持久化 `call_targets` 的 store 版本建立的索引；旧库只有直接调用，
  所有边 `source = "direct"`。
- 分页用 offset 切片而非游标；offset 很大时会先多取若干行再切片。
