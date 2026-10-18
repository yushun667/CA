# 跨 TU C++ 虚函数派发候选

C++ 虚调用只能静态解析到声明目标，因此一个 override 的调用方在看图时像是
不可达的（`Base::run()` 的调用点不会连到 `Derived::run()`）。本文说明新增的
虚派发元数据、候选边规则与当前限制。增量索引使用的 include 依赖契约见
[incremental.md](incremental.md)。

## 数据

解析器在既有 core 记录上新增字段（schema v2）：

| 记录 | 字段 | 含义 |
|---|---|---|
| `FunctionRecord` | `is_virtual`、`is_pure_virtual` | 方法派发标记 |
| `FunctionRecord` | `overrides` | 直接 override 的声明函数 id（`clang_getOverriddenCursors`） |
| `TypeRecord` | `base_types` | 直接基类类型 id（多继承按声明顺序） |
| `CallRecord` | `kind="virtual"`、`is_virtual` | 虚调用点，静态目标在 `callee_id` |

store schema v7 持久化为 `functions.is_virtual`、`functions.is_pure_virtual`、
`functions.overrides_json`、`types.bases_json`，以及直接边表
`virtual_overrides(function_id, base_function_id)` 与
`type_bases(type_id, base_type_id)`。升级是幂等的：打开旧库时就地补建表/列。

## 候选规则

- **同 TU**：`ca.analyze.virtual_dispatch` 把虚调用点变成
  `virtual_candidate` 的 `CallTargetRecord`，覆盖该 TU 内可见的静态目标的
  所有 override（直接 override 置信度 0.6，第 2 层 0.5，第 3 层 0.45，
  阈值 `AnalyzeOptions.call_target_min_confidence`）。
- **跨 TU**：查询时 `store.load_forward_edges`/`load_reverse_edges` 基于全部已索引
  shard 的 override 关系，在直接调用边上叠加 `virtual_candidate` 边；无需额外
  物化步骤，旧库缺表时退化为直接边。
- 经由 `Base::m` 的调用，候选是 `m` 的已知 override（override 链最多 3 层）。
  `callees` 以 `kind="virtual_candidate"`、低于直接边的 `confidence` 和可读
  `reason` 返回；`Derived::m` 的 `callers` 会包含静态目标是其祖先方法的虚调用点。
- 直接调用边从不被替换或升级：候选边是新增行（`source != "call"`），id 仍用
  `ca.core.ids.call_target_id`。

## 限制

- 动态类型未知，候选只是可能性而非证明目标；没有 override 元数据时不产生候选
  （不猜测边）。
- 多继承、模板与依赖类型会保守地扩大候选集合；每函数候选有上限
  （`max_call_targets`，默认 64）并去重。
- 只有目标为 C++ method 的调用被识别为虚调用；虚析构不会产生候选边。
- 每次 callers/callees 查询会多执行一个有界递归 CTE；虚表缺失或旧布局只读库
  会跳过该步骤。

## 测试

`tests/test_parse_virtual.py`、`tests/test_analyze_virtual.py`、
`tests/test_store_virtual.py`（元数据、同/跨 TU 候选、无 override 不产假边、
旧库幂等升级）。
