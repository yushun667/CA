# AGENTS.md — store（数据库与查询）

批量写 SQLite（WAL），提供查询/图/多仓/深析/回调/联邦/jobs API；延迟可度量。

## 关键文件（实现后登记）

- `schema.sql`：22 张表；列与 `ca.core.schema` 对应（sloc 平铺、tuple JSON）；FTS 由 finalize 建。
- `writer.py`：批量写、`finalize()`（占位、38 索引+FTS5、ANALYZE、深析回填）；`STORE_SCHEMA_VERSION=5`；旧库幂等补建。
- `queries.py`：查询/导出；callers/callees 合并 direct+call_targets；detail 带 deep_ready。
- `graph.py`：图 BFS/路径/环。
- `deep.py`：`deep_status` CRUD。
- `callbacks.py`：`callback_registrations`/`callback_target_links`（批量写/前缀/字段/模块/stats/link/`add_call_targets`）。
- `federated.py`：`FederatedStore`/`discover_databases`（多 shard 只读并联；去重/分页/聚合）。
- `jobs.py`：`JobQueue`（jobs/job_events CRUD/claim/lease/事件重放/prune；`JobQueue.open(state_db)`；SqliteStore 委托）。
- `workspace.py`：三表 CRUD
- `sqlite_store.py`：生命周期/PRAGMA 与 API 委托；模块 docstring 是键名契约。
- `tests/test_store*.py`：round-trip/FTS/graph/workspace/deep/callbacks/jobs/federated/升级。

## 局部规则

- 写入事务批处理；查询参数绑定，禁止拼接 SQL。
- 返回键为 CLI 契约：变更同步 cli/tests/docstring。
- 图遍历有界；基准用 `.tmp`。
- 测试：`.venv/bin/python -m pytest tests/test_store_*.py -q`。
