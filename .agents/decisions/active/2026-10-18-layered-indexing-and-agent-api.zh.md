# 决策：分层索引与面向 Agent 的 API 策略

Status: active
Date: 2026-10-18

## 背景

对 OpenHarmony 每个 TU 都做全量深析不可行：每个 TU 含数千 header 内联定义，
header 分析占 analyze CPU 的 90.6%，全量深析估计需 5–10 天。真实 Agent 评估
（xv6 45 runs，A=源码，B=CA 事实，C=CA+深析）表明调用/符号事实有价值但并不
自动省上下文：A 中位 23.8KB、B 56.3KB、C 31.2KB，因为 `detail` 输出冗长。
coding 三类条件都接近天花板；debug 最受益于深析（C 比 A +4.4pt F1）；test-gen
用 B 最好（B 比 A +10.9pt）。45 份答案没有编造 file:line；CA 缺口（头文件声明、
返回块塌缩、锁支配/常量/引用计数缺失）被 Agent 明确标注。

## 决策

- 对唯一源文件各解析一次，构建快速基础层 L0：符号、类型、字段、直接调用与
  位置；查询立即可用，`deep_ready=0`。
- 深析事实（CFG、分支、赋值、返回、资源操作、call_targets、findings）按函数/
  模块按需计算（L1），写入 `deep_status` 缓存；高频模块用有界后台队列预热（L2）。
  离线验收仍为主文件定义预计算深析（`--analyze-scope main`），但不逐 TU 分析
  所有 header 内联函数。
- MCP/CLI 保持分层语义：基础事实与 `deep_ready` 总是返回；深析事实列表按需；
  `ensure_deep` 负责物化。
- 默认不向 Agent 暴露原始 CFG 块；返回语义事实（条件、返回/错误路径、资源生命
  周期），`detail` 作为摘要入口；按评估 backlog 优先做 compact/字段投影、批量
  查询与 `brief` 入口。
- 验收口径：唯一源文件（约 6.5 万），排除 `out/`/`prebuilts/` 生成树；variant
  矩阵保留为 metadata，不作为独立验收行。

## 备选

- 全量深析所有 TU：5–10 天、数百 GB，放弃。
- 只做基础索引：debug/test-gen 召回不足，放弃作为深析接口，保留为默认快路径。
- 暴露原始 CFG/JSON dump：评估中价值低、上下文成本高，放弃。
- 按包含关系逐 TU 分析 header 内联函数：重复冗余，放弃；all 模式用全局去重。

## 后果

- 查询语义包含 `deep_ready`/pending 状态；Agent 必须明确区分“未分析”与“无事实”。
- 深析覆盖按设计是部分的；验收报告记录覆盖度，并保证基础覆盖完整。
- compact 输出与头文件声明缺口成为阶段 3 之后的首要优化项。

## 验证

xv6 lazy deep：首次分析 10.4 ms/符号、缓存命中 5.3 ms/符号；OH unique-100
基础索引 87.5s / 359MB，`ensure_deep` 缓存命中约 1ms。45-run Agent 矩阵与优化
backlog 记录在 `benchmarks/agent_eval/`。
