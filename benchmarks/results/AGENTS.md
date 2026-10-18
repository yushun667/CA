# AGENTS.md — 基准结果

`run_*.py` 生成的 JSON 报告存放处；默认不提交（见本目录 `.gitignore`），需要长期保留的结论摘录入 `docs/` 或规划，不直接引用本目录路径。

## 文件

- `accuracy.json`：精度基线（各类别 precision/recall/F1、失败样例）。
- `parse_bench.json`：真实语料解析/索引吞吐（默认 xv6，`--repo/--compdb` 可换语料）。
- `parse_bench_freertos.json`：FreeRTOS-Kernel 第二语料的同口径结果。
- `parse_bench_incremental.json`：同一语料的冷/暖两次索引，含缓存命中率与加速比。
- `parse_bench_linux_subset.json`：Linux 6.12.113 内核前 20 个 TU 的细粒度指标（含 parse/analyze/write CPU 与峰值内存）。
- `parse_bench_linux_100.json`：Linux 内核前 100 个 TU 的规模基准（语料 metadata 内嵌 kernel 版本/arch/config）。
- `scale_bench*.json`：jobs × analyze/record scope 的大语料索引规模基准与 ETA 外推；`scale_bench_oh_mainfile.json` 为 OpenHarmony 300 TU 子集实测。
- `federated_bench.json`：本地 5 shard（含 OH S7 副本）联邦查询 p50/p95 与单库基线。
- `deep_bench*.json`：分层索引基准（L0 base 成本、L1 ensure_deep 首次/命中延迟、facts 与覆盖率）；`deep_bench_oh_unique100.json` 为 OH unique-100（lib15）实测。
- `db_bench.json`：合成数据写入 rows/s 与查询 p50/p95。

## 规则

- 结果 JSON 由脚本写入，禁止手改；重跑脚本覆盖。
- 每份结果自带 machine/corpus/command 元数据，引用数字时必须同时给出语料与 jobs。
