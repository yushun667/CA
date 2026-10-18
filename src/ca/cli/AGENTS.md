# AGENTS.md — cli（索引编排与命令行）

组织 build → parse → analyze → store 的并行索引流程，提供 `index`、`query`、`stats`、`export`、`version` 子命令，是用户可见行为的入口。

## 关键文件（已实现）

- `main.py`：`main(argv=None) -> int`；argparse 子命令、退出码（0 成功/1 错误/2 无结果）、数据走 stdout、进度走 stderr。
- `commands.py`：子命令实现；`index` 解析 compile database 来源，支持增量缓存、`--analyze-scope`（大语料默认 main）与 `--record-scope auto|all|main_file`（>200 TU 默认 main_file）、传输统计；`update` 刷新 DB；`graph`/`mcp` 复用对应模块；查询映射 store `query_*`，`export` 用 `iter_records()`。
- `dbsource.py`：`--db`/`--db-dir` 与只读联邦 store 打开（单 DB 语义不变）。
- `serve`/`cmd_serve`（见 `ca/server/`）：常驻后端；`ca mcp --serve-url` 把 stdio 代理到 `/mcp`。
- `format.py`：表格/JSON/KV 输出与 `IndexSummary` 指标渲染；列优先用 schema 字段名，缺失时回退到返回行的键。
- `graph.py`：调用图遍历（优先 store `query_call_graph`，旧库回退 calls BFS）、稳定排序/环/上限、DOT；CLI 与 MCP 共用。
- `mcp.py`：MCP JSON-RPC server（stdio + HTTP）、18 个只读工具（含 callbacks/deep/batch/brief）、`--serve-url` stdio 代理、稳定信封与错误码。
- `watcher.py`：离线/增量规划辅助（`plan_changes`/`snapshot`/`diff_snapshots`/`update_command`）。
- `watch.py`：`ca watch` 轮询增量 daemon（状态/单实例锁/防抖/失败重试/`compdb_stale` 跳过），见 `docs/incremental.md`。
- `callbacks.py`：跨 TU 回调后处理（`ca resolve-callbacks`/索引 `--resolve-callbacks`）：提取注册事实/间接调用点、解析候选边并写 callback 表；幂等只增。
- `deep.py`：L0/L1/L2 分层 `ensure_deep`/`prewarm`、deep_status 适配；详见 `docs/layered-indexing.md`。

## 局部规则

- 命令行行为变化需同步 `docs/` 用户文档（双语三件套）。
- 大仓索引默认多进程；进度与 `IndexSummary` 指标写 stderr，数据写 stdout。
- 测试：`.venv/bin/python -m pytest tests/test_e2e_*.py -q`。
