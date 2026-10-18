# AGENTS.md — 性能基准

解析吞吐、分析耗时、数据库写入与查询延迟的可重复测量；结果验证“足够快”的目标并防止回归。

## 关键文件（已实现）

- `run_parse_bench.py`：真实语料（默认 xv6，`--repo/--compdb` 换语料）的 TU/s、LOC/s、单 TU 平均耗时、峰值内存、DB 大小；`--incremental` 先冷后暖，报告命中率与加速比。
- `run_db_bench.py`：合成数据批量写入 rows/s 与 functions/callers/callees/branches/assignments/returns/resources/detail/findings 查询 p50/p95。
- `run_scale_bench.py`：大语料索引规模基准（jobs × analyze scope）的 CPU/wall/序列化/DB 指标与全量 ETA 外推。
- `run_deep_bench.py`：L0 基础索引耗时/DB、L1 ensure_deep 首次/命中延迟、事实产出与 deep_ready 覆盖率。
- `run_accuracy.py`：用 `tests/fixtures/golden_os_mini.*` 夹具按类别输出 precision/recall/F1。
- `_common.py`：仓库路径、机器/语料元数据、结果 JSON 写出与 `PYTHONPATH=src` 引导。
- [agent_eval/AGENTS.md](agent_eval/AGENTS.md)：Agent 效用评估；`agent_eval/run_eval.py` 为源码 vs CA 基础/深度事实的 A/B/C harness（任务集、GT 校验、评分、聚合），`agent_eval/REPORT.md` 为 pilot 结论。
- [results/AGENTS.md](results/AGENTS.md)：本地生成的 JSON 报告（默认忽略；重要结论手工摘录入 `docs/` 或规划）。

## 局部规则

- 脚本必须记录机器、语料版本、编译参数来源与命令，结果可复现。
- 不放业务逻辑；复用 `ca.pipeline`/`ca.store` 公开接口。
- 区分首次索引与热缓存；不用小样本外推大仓结论。
- `results/` 同名 JSON 会被覆盖；并发跑不同语料/基准时用 `--out` 指定不同文件名，不要并发写同一文件。
