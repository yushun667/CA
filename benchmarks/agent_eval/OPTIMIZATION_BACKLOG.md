# CA 优化 Backlog（按 LLM 效用评估证据排序）

输入证据：`results/pilot_xv6.json`（90-run deterministic pilot）、
`results/llm_pilot_xv6.json`（6-run 真实 LLM 人工评分）、
`results/llm_matrix_xv6.json`（**45-run 真实 LLM A/B/C 全矩阵**）。
结论细节见 [REPORT.md](REPORT.md) §6–§7。

优先级 = 证据强度 × agent 效用 / 实现成本。成本：S ≤1 天、M 2–5 天、L >1 周
（单人，含测试/文档，按本仓库现状粗估）。

| # | 优化项 | 矩阵证据（量化） | 预期收益 | 成本 | 优先级 |
|---|---|---|---|---|---|
| 1 | **compact 输出 + 字段投影**（`--fields`，去 id/block/col/params；默认紧凑 JSON） | 全矩阵 context 中位：A 23.8 KB < C 31.2 KB < B 56.3 KB；B 的 debug 中位 **81.9 KB**（detail 全量 dump）；C 有 2 次跑出 **5.4 MB** JSONL（整库导出）；`detail` 单函数 13–23 KB | 让 CA 真正省 context（目标：C/B 中位 ≤ A 的 23.8 KB），消除 5.4 MB 级取数；间接降低 caveat/复述 token | S–M（CLI 输出层 + 默认投影 + 文档） | **P0** |
| 2 | **批量查询 / 多符号一次返回**（callers/callees/branches 支持多 symbol；graph 多根；batch JSON） | C 条件 tool calls 25.0 中位/26.4 均值 vs A 21/23.2；单任务最高 41 次（C2）、33 次（C3/D3）、32（D3_C） | tool calls −30–50%，wall time/token 同步下降；配合 #1 把“查询成本”压回 deterministic pilot 水平 | M（CLI 参数 + store 批查询 + MCP 同步） | **P0** |
| 3 | **summary-first 入口**（`ca brief <fn>`：签名/位置、callers 计数、branch/return/resource 计数、前 N 调用者；一屏 ≤2 KB） | CA 条件 caveat 提及 **B 16.5 / C 9.7 次/答案** vs A 0.33——agent 在“缺什么/该怎么查”上大量自我限定；debug B 因逐函数 detail 导致 context 81.9 KB | 减少探索性/防御性查询与解释 token；给 agent 一致的查询路径 | S（组合现有查询 + MCP tool） | **P0** |
| 4 | **头文件声明索引**（defs.h 等原型：`is_definition=false` 入 functions；头文件作为声明来源） | C2/C5 的 coding 答案明确标注“CA 只有定义、无原型/声明记录”，C2_C caveat 28 次；coding 类“改签名”影响面缺声明点 | coding 影响面完整性；避免“改定义漏改声明”；对 OH 头文件密集仓更重要 | M（头文件扫描/声明抽取；compile DB 未列头文件时按 include 图） | **P0** |
| 5 | **returns 的 exit-block 塌缩修复**（每 return 保留所属 block/终止符 + 可选 post-dominator） | D3_C/T3_C/T2_C 均报告“returns 全部落在同一 exit block”，无法配对“返回点↔清理路径”；test-gen 的清理断言依赖手工邻接推断 | debug/test-gen 的清理路径正确率；减少推断错误与 caveat | M（CFG 构建/store schema；cfg_edges 已有） | **P1** |
| 6 | **锁/资源操作与调用点的支配关系**（`resources --at-call` / holding-lock 查询） | debug 是 C 条件唯一全面占优的类别（F1 0.738 vs A 0.694/B 0.668，context 中位 14.1 KB vs A 23.2）；但 C2 类答案仍无法证明“调用点持锁”（只能给行号区间） | 锁 bug/死锁/丢失唤醒任务的核心问题；把 debug 优势从“列事实”提升到“给结论” | M–L（支配分析 + 查询接口） | **P1** |
| 7 | **未解析/间接调用边**（call_targets：syscall 表、函数指针、宏包装） | C5 三条件均指出 `callers sys_open` 为空、syscall 表分发不可见；`call_targets` 表为空 | coding 影响面（syscall/回调入口）与 debug 调用链完整性；OH 上更关键 | M（候选边生成 + 置信度；表结构已有） | **P1** |
| 8 | **常量/宏参数入库**（MAXPATH/NDEV/NOFILE/NFILE、enum、`#define`） | T3 答案多次标注“常量不在 CA 结果，无法量化错误边界”；test-gen 边界断言需要具体值 | test-gen 边界用例、debug 错误码解释 | M（预处理常量表 + 查询面） | **P1** |
| 9 | **引用计数资源建模**（iget/idup/iput 纳入 resource/leak 配对） | T2_C/D3_C：`leaks namex` 无结果且 `deep_ready=false`，不能判“无泄漏”；引用生命周期只能从 callees 间接看 | 资源泄漏/引用计数 debug；findings 可靠性 | M（matcher 扩展 + refcount 配对事实） | **P2** |
| 10 | **IRQ/上下文属性**（中断处理函数、可睡眠上下文） | C2 答案无法判断 uartintr/clockintr 等中断上下文，锁序/死锁结论靠命名猜测 | 锁序/deadlock debug；编码规范检查 | M（入口标注/调用链自根分析） | **P2** |
| 11 | **findings 仅线索化**（confidence/evidence/“候选”措辞；默认不当 bug） | D5 三条件（A/B/C）**全部判对误报**并解释所有权转移 → 本轮未观察到误导；但仍属单点、且 B/C 需自行复核 | 保持/明确“线索”语义，降低未来假阳性风险 | S（文案/字段 + 文档） | **P2** |
| 12 | **graph 默认深度/输出紧凑化**（文件聚类、每层 top-N、节点名+位置） | B→C 的 F1 delta −0.25 pt、combined +0.23 pt（无正收益）；B（detail L1）在 test-gen 反而优于 C 7.8 pt；graph 只在 debug 有间接贡献 | 低（当前证据不支持扩大投入）；保留按需使用与深度限制即可 | S | **P3 / 观察** |
| 13 | **MCP 常驻/批处理**（避免每查询进程启动） | 45-run 矩阵中 CA 条件均用 CLI（20–26 calls/task），未测 MCP 常驻收益；deterministic pilot 单查询 40–200 ms vs 源码 5 ms | 交互 wall time；配合 #2 批量后总收益变小 | S（已有 MCP server） | **P2** |

## 不做 / 后置

- **默认注入全量 CFG blocks/edges**：单函数内事实源码可读，且体积大；仅按需。
- **默认全量 deep facts / 无限 graph**：矩阵中 C 相对 B 无显著增益，且 context 更高；
  深析事实应按需 + 摘要。
- **扩大 finding 规则而不先修 precision/措辞**：会放大误报噪声。
- **把 C（deep）作为默认 agent 条件**：本矩阵中 B≈C > A 幅度有限，成本更高；
  优先做接口优化（#1–#3）再复测。

## 验收方法

- 优化后用**同一 45-run 协议**复测（`score_matrix.py --answers .tmp/llm_eval/full`）：
  验收门槛 = context 中位 ≤ A（23.8 KB）、tool calls 中位 ≤ 20、F1 不低于当前
  （A .826 / B .860 / C .857）、0 幻觉位置。
- deterministic 侧用 `run_eval.py` 验证查询延迟/输出字节不回退。
