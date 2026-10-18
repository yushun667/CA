# AGENTS.md — cli（索引编排与命令行）

组织 build → parse → analyze → store 的并行索引流程，提供 `index`、`query`、`stats`、`export`、`version` 子命令，是用户可见行为的入口。

## 关键文件（已实现）

- `main.py`：`main(argv=None) -> int`；argparse 子命令、退出码（0/1/2）、stdout 数据、stderr 进度。
- `commands.py`：子命令实现；`index` 解析 compdb 来源，增量缓存、`--analyze-scope`（大语料默认 main）、`--record-scope auto|all|main_file`（>200 TU 默认 main_file）；`update` 刷新 DB；`graph`/`mcp` 复用模块；查询映射 `query_*`，`export` 用 `iter_records()`。
- `dbsource.py`：`--db`/`--db-dir` 与只读联邦 store（单 DB 语义不变）。
- `serve`/`cmd_serve`（见 `ca/server/`）：常驻后端；`ca mcp --serve-url` 代理 stdio 到 `/mcp`。
- `format.py`：表格/JSON/KV 输出与 `IndexSummary` 渲染；列优先 schema 字段名，缺失回退行键。
- `graph.py`：调用图遍历（`query_call_graph` 优先，旧库回退 calls BFS）、稳定排序/环/上限、DOT；CLI/MCP 共用。
- `mcp.py`：MCP JSON-RPC server（stdio/HTTP）、18 个只读工具、`--serve-url` 代理、稳定信封/错误码。
- `watcher.py`：离线/增量规划辅助（`plan_changes`/`snapshot`/`diff_snapshots`）。
- `watch.py`：`ca watch` 轮询增量 daemon（状态/锁/防抖/重试/`compdb_stale` 跳过、header include 依赖感知），见 `docs/incremental.md`。
- `callbacks.py`：跨 TU 回调后处理（`ca resolve-callbacks`/索引 `--resolve-callbacks`）：提取注册事实/间接调用点、写 callback 表。
- `deep.py`：L0/L1/L2 `ensure_deep`/`prewarm`、deep_status 适配；详见 `docs/layered-indexing.md`。

## 局部规则

- 命令行行为变化同步 `docs/` 双语三件套。
- 大仓索引默认多进程；`IndexSummary`/进度写 stderr，数据写 stdout。
- 测试：`.venv/bin/python -m pytest tests/test_e2e_*.py -q`。
