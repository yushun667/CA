# AGENTS.md — store（数据库与查询）

批量写 SQLite（WAL），提供查询/图/多仓/深析/回调/虚拟/联邦/jobs API。

## 关键文件（实现后登记）

- `schema.sql`：25 张表；列对应 `ca.core.schema`（sloc 平铺）；FTS 由 finalize 建。
- `writer.py`：批量写、`finalize()`（占位/索引/FTS5/深析回填）；`STORE_SCHEMA_VERSION=8`。
- `migrations.py`：v8 删 confidence（DROP→安全重建→保留旧列+warning；open 幂等调用）。
- `queries.py`：查询/导出；callers/callees 合并 direct/call_target/virtual（natural-key/reason 合并）。
- `graph.py`：图 BFS/路径/环；`virtual_dispatch.py`：虚分派。
- `deep.py`：deep_status；`callbacks.py`：callbacks/links。
- `federated.py`：FederatedStore（shard 只读）。
- `workspace.py`：三表 CRUD；`sqlite_store.py`：生命周期/迁移/API 委托；docstring 是键名契约。
- `incremental.py`：`remove_units`/`replace_units`/`replace_unit`（按 TU 级联删/单事务替换 + FTS5 增量维护）。
- `jobs.py`：JobQueue（jobs/job_events、claim/lease、重放、open）。
- `tests/test_store*.py`：round-trip/FTS/graph/deep/callbacks/virtual/jobs/federated/incremental/迁移。

## 局部规则

- 写入批处理；查询参数绑定，禁止拼接 SQL。
- 返回键为 CLI 契约：变更同步 cli/tests/docstring。
- 图遍历有界；基准用 `.tmp`。
- 测试：`.venv/bin/python -m pytest tests/test_store_*.py -q`。
