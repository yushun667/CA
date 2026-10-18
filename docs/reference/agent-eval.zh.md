# Agent 效用评估（xv6 Pilot）

状态：xv6 pilot + 6-run/45-run 真实 LLM 评估已完成（2026-10-18）；OpenHarmony 扩展待做。产物见
[benchmarks/agent_eval/](../../benchmarks/agent_eval/README.md)。

## 目的

度量 AI agent 从 CA 事实相对直接读源码获得的增益，覆盖三类使用者：debug、
coding（影响面分析）与 test-gen。重点：跨文件调用链（上下文大、预期价值高）
vs 函数内 CFG/数据流（预期边际价值低）。

## 方法

同任务三条件：**A** 仅源码（grep/read）；**B** CA 基础索引（functions、
callers、callees、detail/位置）；**C** CA 基础 + 深度事实（branches、returns、
resource ops、leaks、递归 graph）。15 个 xv6 任务（每类 5 个），重复 2 次，
条件顺序随机，共用同一事实抽取规则；ground truth = 322 条源码校验过的位置
事实；指标 = 事实级 P/R/F1 + 读取字节/工具调用/延迟。本 pilot 为确定性检索
策略运行，不是 LLM-agent 实跑（见“局限”）。

## 主要结果（均值 F1 / 字节 / 工具调用）

| 类别 | A 源码 | B 基础 | C 深度 |
|---|---|---|---|
| debug | 0.966 / 12.1 KB / 2.2 | 0.667 / 3.7 KB / 1.4 | 1.000 / 10.3 KB / 5.2 |
| coding | 0.997 / 38.2 KB / 9.6 | 1.000 / 4.7 KB / 3.2 | 1.000 / 7.7 KB / 1.0 |
| test-gen | 0.991 / 14.4 KB / 2.0 | 0.579 / 3.7 KB / 1.0 | 1.000 / 11.7 KB / 4.0 |

## 结论

1. **调用图与符号/位置事实价值最高。** 直接调用者集合仅需 4.7 KB / 1 次查询，
   源码阅读需 38.2 KB / 9.6 次 shell 调用（上下文 −88 %、调用数 −90 %）。
   2-hop 反向影响面：1 次 graph 查询 vs 12 次基础查询或 23 次源码调用。
2. **函数内 CFG/数据流信息增量很小**（xv6 规模函数）：读函数体已达 F1
   0.97–0.99；深析事实 1.0，但字节成本相近、查询更多（debug +3.4pt、
   test-gen +0.9pt）。其价值是结构化、可机器消费的枚举，而非新事实。
3. **仅 B 不足以支撑 debug/test-gen**（recall 0.51/0.41）；B 适合作为 coding
   影响面的默认入口与图遍历起点。
4. **默认提供**：函数列表/detail/签名/位置、直接 callers/callees、限深/反向
   graph、函数摘要（分支数、错误返回数、锁配对）。**按需提供**：完整
   branches/returns/resource 列表、assignments、CFG block、findings。
   findings 只能作线索（pilot 中唯一的 leak 告警是误报）。

## 失败案例

- syscall 分发是函数指针表，`call_targets` 为空，`sys_open`/`fork` 入口边
  在调用图中缺失。
- `proc_mapstacks` 的 kalloc 泄漏 finding 是误报（内核页表与内核同生命周期），
  agent 可能据此报假 bug。
- 深析事实完整 JSON 冗长（含 id/block/col），单函数场景字节节省可能消失；
  需要字段投影与批量查询。

## 局限与下一步

确定性策略 pilot，未跑 LLM（本会话无 subagent 深度）。GT 由 DB 枚举再逐条
源码校验，故 C 满分接近同义反复；xv6 很小，对 A 有利。下一步：真实 LLM
agent A/C 实验（每类 2 任务 × 2 次），随后用同一协议在 OpenHarmony 大仓复测。

## 复现

```bash
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py --build-tasks
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py \
  --out benchmarks/agent_eval/results/pilot_xv6.json
```

**45-run 真实 LLM 全矩阵**（15 任务 × A/B/C，REPORT.md §7）进一步细化：
结构事实覆盖 B≈C>A（combined 0.898/0.900/0.869；A→B、A→C +2.9/+3.1 pt，
sign test p=0.065），45 份答案 0 个幻觉 file:line；debug 类 C 最优（F1 +4.4 pt
且 context 比 A 少 39%），test-gen 类 B 最优（F1 +10.9 pt vs A），coding 类
B/C 达天花板。CA 总体 context 仍更高（中位 A 23.8 KB < C 31.2 KB < B 56.3 KB），
原因是 agent 全量 dump `detail`——compact/投影与批量查询是最高优先优化项
（OPTIMIZATION_BACKLOG.md）。人工子集中源码阅读仍保持语义优势。

逐任务 delta 与完整报告：[benchmarks/agent_eval/REPORT.md](../../benchmarks/agent_eval/REPORT.md)。
