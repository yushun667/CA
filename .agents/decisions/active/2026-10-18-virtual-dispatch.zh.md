# 决策：C++ 虚函数派发候选

Status: active
Date: 2026-10-18

## 背景

OpenHarmony dsoftbus/camera 是 C++ 代码库，调用图很大程度上依赖虚派发（头文件中的
接口类、其他 TU 中的实现）。虚调用 `base->run()` 静态解析到声明的 `Base::run`，
因此 override 实现（`Derived::run`、`Leaf::run`）在 `callers`/`callees`/图中不可见，
仅同 TU 分析也看不到其他 TU 编译的实现。项目需要可解释、跨 TU 的候选目标，同时不
改变直接调用语义，也不要求指针分析。

## 决策

1. **parser 元数据（core schema v2）**——只新增字段：
   `FunctionRecord.is_virtual` / `is_pure_virtual` / `overrides`（直接 override
   声明 id，来自 `clang_getOverriddenCursors`）与 `TypeRecord.base_types`
   （直接基类类型 id，来自 `CXX_BASE_SPECIFIER`）。TU 内类外定义仍替换头文件声明，
   但 virtual/override 元数据会合并，幸存记录保留派发事实。
2. **store schema v7**（幂等升级）：`functions.is_virtual`、
   `functions.is_pure_virtual`、`functions.overrides_json`、`types.bases_json`，
   以及带索引的边表 `virtual_overrides(function_id, base_function_id)` 与
   `type_bases(type_id, base_type_id)`。
3. **同 TU 候选**（`ca.analyze.virtual_dispatch`）：虚调用点为该 TU 内可见的静态
   目标的每个 override 生成 `CallTargetRecord(kind="virtual_candidate")`（override
   深度 ≤3）；`reason` 记录关系/深度作为证据。没有置信度分数或阈值：结果是确定、
   去重、受 `max_call_targets` 有界的候选集合。
4. **跨 TU 候选在查询时合并**：`store.load_forward_edges`/`load_reverse_edges` 基于
   所有已索引 shard 的 override 关系（递归 CTE，深度 ≤3）合并 virtual 候选。候选
   是新增行（`source != "call"`），带 `kind` 与可读 `reason`；重复时直接边优先。override 的 `callers` 同样会包含静态目标为其祖先方法的虚调用点。
5. **不物化**：跨 TU 候选在 callers/callees/graph 查询时计算，不由索引 pass 写入
   `call_targets`。

## 原因

- 直接调用语义不变；虚目标以带 kind/证据的显式候选集合呈现，agent 可区分直接事实
  与候选集合（不是概率分数）。
- 查询时合并天然使用全库数据，与 shard 划分和后续增量重索引无关；物化 pass 在
  shard 变化时需重算，且会重复直接调用关系。
- 持久化的 override/基类边很小（每个直接关系一行），同 TU 分析与跨 TU 查询共享
  `ca.core.ids` id。
- 旧库（无 v7 表）退化为直接/间接边而不是失败，联邦/遗留查询保持可用。

## 考虑过的替代方案

- **在 `finalize()` 物化候选**：多数候选边只对当前已索引 override 集合有意义；
  重索引 shard 需重写可能数百万行，且候选量取决于查询/上限；不采用。
- **复用 `CallRecord.kind` 或改写静态 callee**：丢失直接事实、破坏既有直接调用
  契约；不采用。
- **RTTI/points-to 分析**：超出 L0/L1 索引预算；候选元数据是刻意受限的替代。
- **仅同 TU**：漏掉 OpenHarmony 主场景（接口头一个 TU、实现另一个 TU）；不采用。

## 后果

- 虚函数密集库上每次 callers/callees 查询多一个有界递归 CTE；深度/上限保证可预期。
- 对模板、多继承、动态类型未知，候选集合保守；虚析构不按虚调用跟踪。
- v7 之前或未产出 header 声明的索引只是候选更少/没有，绝不凭空造直接边。
- 候选不持久化，仅消费 `call_targets` 的调用方看到同 TU analyze 候选；
  callers/callees/graph/MCP 同时看到两类。
- 治理：`directory_agents.max_chars` 从 1500 提升到 1800，因为 active 决策索引必须
  逐字列出 14 组三件套共 42 个文件名；13 组时旧预算已到上限。

## 验证与风险

`tests/test_parse_virtual.py`（元数据、声明/定义合并、跨 TU id、header 声明模式）、
`tests/test_analyze_virtual.py`（同 TU 候选、置信度/深度、确定性、无 override 不产
边）、`tests/test_store_virtual.py`（持久化、跨 TU 查询合并、反向 callers、旧库幂等
升级、无表退化）与 `tests/test_parse_includes.py`（依赖契约）随全仓通过
（454 passed, 3 skipped）。partial OpenHarmony dsoftbus_camera 索引（640 TU，
store v7/core v2）记录 2,863 次虚调用、4,175 个虚方法、3,957 个 override 方法/
关系、387 次调用（13.5%）对应 1,310 条候选边。风险：候选只是可能性而非证明目标；
task-40 的 dsoftbus/camera 全量验证观察真实覆盖与噪声。

## 关联与取代

- 相关架构或问题：docs/cross-tu-virtual-dispatch.md
- 取代的决策：无
- 被以下决策取代：无
