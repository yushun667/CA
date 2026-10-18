# AGENTS.md — 测试

测试是各模块的验收依据；文件按所有者前缀命名，避免并行写入冲突。

## 文件与命名

- `test_core_schema.py`（Lead）：数据契约与稳定 ID。
- `test_parse_*.py` / `fixtures/ast_*`：AST 提取。
- `test_analyze_*.py` / `fixtures/df_*`：CFG/数据流/资源分析。
- `test_store_*.py`：数据库写入与查询。
- `test_build_*.py` / `fixtures/build_*`：编译参数获取。
- `test_e2e_*.py` / `fixtures/golden_*`：端到端与精度基线。

## 局部规则

- 在仓库根运行 `.venv/bin/python -m pytest -q`；测试必须无网络可重复运行。
- 不修改他人夹具；共享夹具规则见 [fixtures/AGENTS.md](fixtures/AGENTS.md)。
- 大语料与构建产物放 `.tmp/`（已忽略），不提交生成文件。
