# AGENTS.md — 开发脚本

环境引导与仓库级开发工具；不包含分析逻辑。

## 关键文件

- `bootstrap.sh`：创建 `.venv`、按 `requirements.txt` 安装依赖、`pip install -e .` 安装 `ca` console script，校验 libclang 与脚本可用；在仓库根执行。
- `server_bootstrap.sh`：源码服务器部署校验（venv/editable/pytest/MCP stdio 冒烟）；可传已有 DB，否则用合成小样建临时库；输出 `SERVER_OK`。
- `check_artifacts.py`：CI/交付前的产物一致性检查（双语文档 i18n SHA-256、agent_eval JSON、benchmark results JSON）。
- `verify_shard_mcp.py`：分片 DB 的 MCP 验收（stdio/HTTP、15 工具、ensure_deep 首次/命中延迟、deep_status）；输出 JSON。
- `sample_candidates.py`：OH `call_targets` 候选边按 kind/reason 抽样（totals/samples/对照/graph 集成/TU 健康度），供 analyze 精度复核。

## 局部规则

- 脚本幂等、失败即退出（`set -euo pipefail`），路径相对仓库根解析。
- 新增脚本前先确认没有更合适的 Python 入口（`python -m ca`）。
