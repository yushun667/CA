# 决策：FTS5 符号搜索

Status: active
Date: 2026-10-17

## 背景

符号子串查询（`query_functions`）目前使用 `LIKE '%pattern%'` 全表扫描：50 万函数合成库上 p50 为 60–75ms。阶段 2 目标是不改变查询语义与返回键的前提下，把子串查询 p50 降到 10ms 以下。

## 决策

为 functions 的 `name`、`qualified_name` 增加 SQLite FTS5 虚表，在 `finalize()` 时填充，使用 trigram tokenizer 以支持任意子串匹配。`id`/`name`/`qualified_name` 精确查询保留现有索引路径且优先；FTS 只用于子串解析。FTS5 或 trigram 不可用时回退 `LIKE`。已有数据库在 `finalize()` 时自动升级。公开查询 API 与返回 dict 键不变。

## 原因

FTS5 随 SQLite（含 Python 内置 SQLite）提供，无需外部服务；trigram 索引能匹配任意子串，而非仅 token 前缀。在 `finalize()` 建索引可保持批量写入速度。

## 考虑过的替代方案

- **unicode61/前缀 FTS**：快但无法回答任意子串（如标识符中间片段），不选为主 tokenizer。
- **外部搜索引擎**：本地 CLI 不值得承担运维重量。
- **缩小候选集的 LIKE**：任意子串无可靠缩小方式，否决。
- **内存名字缓存**：内存开销大且不跨进程持久。

## 后果

- 数据库体积与 `finalize()` 时间小幅增加；测量写入吞吐确认无显著回退。
- FTS5 可用性取决于 SQLite 构建；运行时检测 + `LIKE` 回退保证行为正确。
- 符号行变化时需维护 FTS 索引（当前流水线只在写入/finalize 阶段变更）。

## 验证与风险

验收：50 万函数子串 p50 < 10ms、精确匹配结果不变、回退测试、写入/DB 体积影响有实测。风险：非 ASCII 标识符与特殊字符下的 trigram 行为；用测试覆盖。

## 关联与取代

- 相关架构：[overview.md](../../architecture/overview.zh.md)
- 相关决策：[存储引擎](2026-10-17-storage-engine.zh.md)
- 取代的决策：无
- 被以下决策取代：无
