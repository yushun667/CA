# 决策：移除间接调用解析的 confidence

Status: active
Date: 2026-10-18

## 背景

回调/函数指针与 C++ 虚派发解析会在候选 `CallTargetRecord`/`RegistrationFact`
上输出 `confidence` 浮点（启发式 0.4-0.95），并按它过滤/排序。这些数值是手工
指定、未经 ground truth 校准的，却被下游（CLI 表格、MCP 输出、graph DOT、LLM
agent）当作概率使用，UI 事后只能“隐藏”它。用户明确要求：分析层不应有置信度，
间接解析只输出**可能调用目标集合** + 可审计证据，而不是带分数的预测。

## 决策

从分析契约端到端删除 confidence：

1. core schema（v2）：删除 `CallTargetRecord.confidence` 与
   `RegistrationFact.confidence`；记录只带候选目标集合与可审计 `reason`
   （注册点、override 关系、参数传递、getter 返回等）。docstring 明确候选集合
   语义。
2. analyze：`callbacks.py`、`registrations.py`、`virtual_dispatch.py` 不再计算、
   过滤、减分或按 confidence 排序。保留所有签名兼容、qualifier 匹配的候选；
   上限只用计数（`max_candidates_per_call`、`max_targets`、override 深度 ≤3）。
   去重与代表选择使用 natural key 与确定顺序；重复证据合并进 `reason`。
   `AnalyzeOptions` 删除 `call_target_min_confidence`。
3. store writer：候选行不再写 confidence 绑定；本改动保持
   `STORE_SCHEMA_VERSION=7`。删除 `call_targets.confidence`/
   `callback_registrations.confidence` 列与查询侧 confidence 由 storage 层
   schema v8 作为独立逻辑改动完成。
4. 消费端：CLI（`ca graph`、`query callees/callers`、`resolve-callbacks`）、
   全部 18 个 MCP 工具、UI/API、脚本与当前文档都不再暴露 confidence 键/列/数字；
   `--min-confidence` 删除；虚线候选边表示可能目标集合。候选行是无序集合，按
   位置/id 确定性排序，但不是排名。

## 原因

- 这些分数是编造的启发式，当作概率呈现会夸大精度并让消费端偏向“高分”候选。
- 候选集合 + 证据是诚实的契约：每个目标都能由注册点、override 关系、参数传递或
  getter 返回辩护；歧义交给消费端，而不是用一个分数决定。
- 去掉按置信度过滤也去掉了隐式的召回/精度旋钮；显式计数上限控制体积，同时保留
  全部证据。
- 直接调用语义不变：候选仍是新增行，绝不替换静态解析的直接边。

## 考虑过的替代方案

- **保留字段但 UI/CLI 隐藏**：用户明确拒绝；隐藏不改变分析契约或下游 agent 行为。
- **保留内部分数但不持久化**：仍泄漏进 `AnalysisResult`/`CallTargetRecord` 且不可
  审计；不采用。
- **用标注数据校准启发式**：没有标注 ground truth 且超出当前阶段；即便校准仍是
  用户不想要的预测。
- **直接丢弃歧义候选**（无分数但精度优先）：丢失合法多目标证据（如同一表槽位注册
  两个实现）；不采用，改为有界候选集合。

## 后果

- 消费端必须把 `indirect_candidate`/`virtual_candidate` 行视为“可能”而非“概率”；
  顺序确定但不是排名。
- 候选数量在原先被阈值砍掉的弱匹配处会上升；按调用点/函数的计数上限控制输出。
- storage schema v8 删除物理列与查询键；旧库 open 时幂等升级。CLI/MCP/UI/API/
  脚本/文档清理已完成，任何地方都不再输出 confidence 键。历史 benchmark/evaluation
  报告可能仍提旧分数，但已不是当前契约。
- `reason` 记录证据（注册路径/file/line、override 深度、参数下标、getter），去重时
  合并 reason，而不是按 confidence 选择。

## 验证与风险

`tests/test_analyze_callbacks.py`、`tests/test_analyze_crosstu.py`、
`tests/test_analyze_virtual.py` 断言候选集合、证据字符串、去重与上限（无数字
confidence）。`tests/test_store_*` 不再用 confidence 构造 `CallTargetRecord`，
导出/合并边按集合断言；CLI/MCP/UI e2e 断言输出无 `confidence` 键；全仓通过。
报告与文档（`cross-tu-callbacks`、`cross-tu-virtual-dispatch`、`mcp`、`usage`、
`ui`、`functional-verification`）描述候选集合语义。
风险：仍读取旧字段的外部消费端会在 schema v8 后失效——这是有意的 API 变更。

## 关联与取代

- 相关架构或问题：docs/cross-tu-callbacks.md、docs/cross-tu-virtual-dispatch.md
- 取代的决策：2026-10-18-virtual-dispatch.md 中 override 深度置信度与 2026-10-18-readonly-web-ui.md 中 confidence 展示部分（候选规则/UI 其余部分仍有效）
- 被以下决策取代：无
