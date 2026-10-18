# 决策：header 依赖感知增量索引

Status: active
Date: 2026-10-18

## 背景

`ca update`/`ca watch` 每个 TU 缓存一份 `ParseResult`，键只含主源文件内容与编译
参数，头文件内容刻意不参与。因此任何头文件（或构建文件）变更都会清空整个增量
缓存并重解析全部 TU。对 OpenHarmony 规模树，小头文件改动也会触发全量重建，
`ca watch` 无法在普通 header 变更后增量刷新。

## 决策

parser 记录每个 TU 的 repo 内 include 事实：

- `ParseResult.include_files`：TU 主文件的直接 include（repo 相对 POSIX、排序去重、
  排除系统头、不含主文件自身）；
- `ParseResult.include_edges`：repo 内 `(parent, child)` include 边，含 header→header。

`ca index` 把传递闭包持久化到 store 表 `tu_includes(tu_id, include_path)`（schema
bump、幂等 `CREATE TABLE IF NOT EXISTS` 升级；`include_path` 建索引）。
`ca update --changed-files` 把头文件映射到依赖 TU，只删除这些 TU 的缓存条目；
无关 TU 保持缓存命中；未被任何已索引 TU include 的头文件是 no-op。`ca watch`
使用同一映射，报告 `dependency_aware`、`fallback_full`、affected TU 与 cache
hits/misses。

若索引没有 include 事实（旧库/parser 未采集），回退旧行为：清空整个缓存、警告并
重解析全部 TU。构建文件（Makefile/CMakeLists 等）仍触发全量重建；传递闭包由
store writer 从直接 include + include 边计算。

## 原因

- header 变更是最常见的增量场景；按依赖 TU 裁剪重解析可让 update/watch 保持快速，
  且不改变记录语义。
- 一次持久化传递闭包让查询保持简单（`include_path -> tu_id`），避免 update 时
  递归 SQL 或维护 header 图；间接 header 变更必须依赖闭包才能正确失效。
- 保留全缓存回退保证旧索引/未知构建文件变更的正确性，而不是静默漏更新。
- parser 继续拥有 include 提取；pipeline/store 只持久化与消费冻结契约。

## 考虑过的替代方案

- **把 header 内容纳入缓存键**：正确但会让所有包含该 header 的 TU 失效（仍是
  当前全量成本），且没有 per-TU 映射。
- **update 时对 header 边表做递归 SQL**：schema/查询复杂度更高、收益很小；闭包
  每 TU 很小且 ingest 时一次算完。
- **只记录直接 include**：会漏掉“header 被另一个 header include”的变更；正确性
  优先，接受少量闭包计算。

## 后果

- `tu_includes` 随唯一 (TU, 传递 include) 对增长；ingest 增加少量闭包计算与磁盘
  占用。
- 变更前建立的索引在重索引前会回退全缓存清理；警告会提示。
- 构建文件变更与未知/未匹配路径仍保守全量重建。
- 增量缓存键版本已 bump，避免回放缺少 include 字段的旧缓存（否则会写不出依赖）。

## 验证与风险

`tests/test_store_includes.py` 覆盖直接/传递闭包、依赖查询与旧库幂等升级；
`tests/test_e2e_includes.py` 用三 TU/header 链 fixture 验证：index 写入直接+传递
include；改被包含 header 只重解析两个依赖 TU（`cache hits=1/3 misses=2`）；传递
深层 header 映射回 TU；无关 header no-op；无 include 行时回退全量并警告；
`ca watch --once` 报告 dependency-aware 指标。风险：升级后未重索引的旧库会先用
回退行为（有警告）；构建文件变更仍全量重建。
