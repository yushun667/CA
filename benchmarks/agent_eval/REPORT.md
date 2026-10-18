# Agent 效用评估 Pilot：直接读源码 vs CA 基础/深度事实（xv6）

**任务**：task-18 / P3-8。**范围**：xv6-riscv 真实 compdb 数据库
`.tmp/lead/final_xv6.db`（phases 2 产物）+ 源码 `.tmp/corpus/xv6-riscv`。
本文件是 pilot 报告；harness、任务集、原始结果、人工 trace 均可复现。

> **English abstract** — We ran a controlled A/B/C pilot on xv6: **A** reads source
> only, **B** uses CA's basic index (symbols / direct callers / callees /
> positions), **C** adds deep facts (branches / returns / resource ops /
> findings / recursive graph). 15 tasks (5 debug, 5 coding-impact, 5
> test-gen), 2 repetitions, randomized condition order. Result: **call-graph &
> symbol/position facts are the clear win** — direct caller sets cost 4.7 KB
> context and 1 query vs 38.2 KB / 9.6 shell calls for source reading
> (−88 % context, −90 % calls), and 2-hop impact costs 1 graph call vs 23 shell
> calls. **Function-local CFG/dataflow facts add little information** for
> xv6-sized functions: source reading already reaches F1 0.97–0.99 from the
> function body; deep facts reach 1.0 with similar bytes and more queries. B
> alone is insufficient for debug/test-gen (recall 0.51/0.41). Findings are
> advisory: the single deep-analysis leak alert in the DB is a false positive.
> A **full 45-run real-LLM matrix** (15 tasks × A/B/C, §7) refines this:
> **B ≈ C > A** on structural fact coverage (combined 0.898 / 0.900 / 0.869;
> A→B / A→C paired +2.9 / +3.1 pt, sign-test p=0.065, initial), with **zero
> hallucinated file:line locations in 45 answers**. Class-specific: **debug C
> best** (+4.4 pt F1 vs A) and *cheaper* context (median 14.1 KB vs 23.2 KB);
> **test-gen B best** (+10.9 pt F1 vs A, 5/5 wins, p=0.0625) but ~2.8× context;
> **coding is at ceiling for B/C** (1.0 vs A 0.982) with no context saving. The
> theoretical CA efficiency advantage is still **not realized consistently**:
> overall median context is A 23.8 KB < C 31.2 KB < B 56.3 KB because agents dump
> verbose `detail` output (two C runs exported the whole DB, 5.4 MB each).
> Source reading kept the semantic/causal edge in the human-scored subset.
> Compact/projected output, batching and a summary-first entry point are the
> top backlog items (§8, [OPTIMIZATION_BACKLOG.md](OPTIMIZATION_BACKLOG.md)).

## 1. 方法

### 条件（同一任务、同一模型、随机顺序、重复 2 次）

| 条件 | 可见证据 | 工具 |
|---|---|---|
| **A** source-only | 源码树 | `grep -rn` + 读文件（字节/文件/调用次数记账） |
| **B** CA basic | 符号、签名、直接调用者/被调用者、源码位置 | `ca query functions/callers/callees/detail` |
| **C** CA deep | B + 分支/返回/资源操作/finding + 递归调用图 | 上述 + `branches/returns/resources/leaks`、`ca graph --depth` |

A 的检索策略是确定性的：按任务给出的符号 grep 后读取命中文件（调用者集）
或按函数体 span 读取定义文件（函数内事实）；C4 走逐层反向展开。
B/C 通过真实 CLI 子进程执行并计时。抽取器对三条件使用**同一套事实抽取
规则**（分支/返回/资源/调用/调用者集合），只由证据形态决定输入：
A 从源码文本抽取，B/C 从 CLI JSON 抽取。故本 pilot 测量的是
**证据获取成本 + 事实可得性**，不是 LLM 推理质量（限制见 §8）。

### 任务集（15 个，见 `tasks.json`）

- **Debug 5**：D1 `sleep` 锁纪律；D2 `kfork` 失败/清理路径；D3 `sys_open`
  错误返回点；D4 `bget/bread` 旧数据/睡眠等待；D5 `proc_mapstacks` 泄漏告警真伪。
- **Coding 5**：C1 `log_write` 调用者影响面；C2 `wakeup`（跨 8 文件）；
  C3 `readi`；C4 `kalloc` 2-hop 反向影响面；C5 `filealloc` + syscall 入口盲区。
- **Test-gen 5**：T1 `kfork`；T2 `namex`；T3 `sys_open`；T4 `writei`；T5 `uvmcopy`
  的分支/错误返回/资源清理测试要点。

### Ground truth 与 rubric

GT 事实 = 源码位置（`file:line`）+ 类别（branch/return/resource/call/caller/
finding），共 **322 条**，由 DB 枚举后**逐条用源码行校验**（0 条失效）。
评分：事实级 `P/R/F1`（集合命中，file:line:类别 为键；调用者集合以函数名为键）。
人工 rubric 另含解释性要点（锁纪律、误报判断、syscall 盲区等），见每个 task 的
`rubric` 字段与 `manual_traces.json`。

### 复现

```bash
# 重新生成任务集（GT 会重新做源码校验）
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py --build-tasks
# 跑 pilot：15 任务 × 3 条件 × 2 次，随机顺序（seed=18）
PYTHONPATH=src .venv/bin/python benchmarks/agent_eval/run_eval.py \
  --out benchmarks/agent_eval/results/pilot_xv6.json
```

原始结果：`results/pilot_xv6.json`（90 条 run，含每条件 claims、命令、字节、
工具调用、延迟）。**未修改 src/，未 git add/commit。**

## 2. 总体结果（2 次重复均值）

| 类别 | 条件 | F1 | Precision | Recall | 平均读入字节 | 平均工具调用 | 平均延迟 |
|---|---|---|---|---|---|---|---|
| debug | A | 0.966 | 0.990 | 0.950 | 12,095 | 2.2 | 5 ms |
| debug | B | 0.667 | 1.000 | 0.510 | 3,715 | 1.4 | 56 ms |
| debug | C | **1.000** | 1.000 | 1.000 | 10,319 | 5.2 | 207 ms |
| coding | A | 0.997 | 0.993 | 1.000 | 38,200 | 9.6 | 14 ms |
| coding | B | **1.000** | 1.000 | 1.000 | **4,748** | 3.2 | 129 ms |
| coding | C | **1.000** | 1.000 | 1.000 | 7,654 | **1.0** | 40 ms |
| testgen | A | 0.991 | 0.982 | 1.000 | 14,421 | 2.0 | 5 ms |
| testgen | B | 0.579 | 1.000 | 0.409 | 3,749 | 1.0 | 40 ms |
| testgen | C | **1.000** | 1.000 | 1.000 | 11,743 | 4.0 | 163 ms |

结论先行：**B/C 的正确性优势只出现在它们独有的信息上**（调用者集合、
分支/返回/资源/finding）；对单函数内事实，A 读函数体也能到 0.97–0.99。
CA 的真正增量是**上下文成本与结构化程度**，在跨文件/递归场景被放大。

## 3. 关键 delta（回答用户四个问题）

### 3.1 函数调用链值多少？——最高价值

| 场景 | A 源码 | B 基础 | C 深度/图 |
|---|---|---|---|
| C2 `wakeup` 直接调用者（12 个，跨 8 文件） | 48.0 KB / 10 calls / F1 1.0 | **5.3 KB / 1 call** / F1 1.0 | 5.3 KB / 1 call / F1 1.0 |
| C1 `log_write` 调用者（8 个，同文件） | 24.4 KB / 4 calls | **3.1 KB / 1 call** | 3.1 KB / 1 call |
| C4 `kalloc` 2-hop 反向影响面（28 函数） | 67.3 KB / **23 calls** / F1 0.983 | 12.2 KB / 12 calls / 484 ms | **26.8 KB / 1 call** / 41 ms / F1 1.0 |
| D3/T3 `sys_open` 完整链路 | 9.4 KB / 2 calls（但需人工沿调用继续展开 3–4 文件，估算 ~100 KB） | F1 0.55（无分支/返回） | 1 条 `graph` 即得跨 4 文件链路（depth=6，113 节点） |

- **直接调用者（1-hop）**：B 用 **~1/8 上下文、~1/3 工具调用** 达到与 A 相同
  正确率；C 与 B 同为 1 call，但 C 的 JSON 更冗长（调用图/深度事实查询），
  所以**“只要调用者”用 B，需要递归/链路用 C**。
- **2-hop 反向影响面**：C 一次 `graph --reverse --depth 2` = 1 次调用；
  B 需逐层调用 12 次（483 ms）；A 需 23 次 shell/读文件（67 KB）。
  这里 **graph 是 10–20× 的效率增益**，且不会因“读少了”漏边（A 的漏边风险
  来自人工展开预算，本 harness 给足了预算才达到 0.98）。
- **链路的量化价值**：调用图把“跨文件上下文”从 O(文件字节) 降到
  O(边/节点数)；在 xv6 上相当于 **~5–10× 字节、10–20× 工具调用**，仓库越大
  差距越大。这是默认最值得提供的能力。

### 3.2 symbol / position 值多少？——高价值但可被 grep 部分替代

- 符号/位置（函数定义、文件:行、签名）让 B 在 **0 次源码读取** 下完成
  caller 影响面；A 的 grep 也能得到同样集合（F1 1.0），但必须读文件才能
  判断 enclosing function（本 pilot A 共读 38 KB/9.6 calls）。
- 真正不可替代的是**消歧与结构**：同名/宏/函数指针、`is_definition`、
  跨文件 caller→callee 映射、间接边标记。C5 显示当前 DB 的
  `call_targets` 为空、syscall 表函数指针未建模，**sys_open/fork 的入口链在
  调用图中不可见**——这既是 symbol/position 的价值边界，也是必须修的数据缺口。
- 建议默认提供：`functions`（含 detail/签名/位置）、`callers`、`callees`、
  受控深度的 `graph`。

### 3.3 函数内 CFG/数据流对三类 agent 分别值多少？——低信息增量、中等结构化价值

| Agent 类别 | A（读函数体） | C（深析事实） | 判断 |
|---|---|---|---|
| debug | F1 0.966，12.1 KB，2.2 calls，5 ms | F1 1.000，10.3 KB，5.2 calls，207 ms | **≈ 等价**：分支/返回/锁点在源码里本就可见；C 只省掉“找文件”，不提供新事实 |
| coding | 用不到函数内 CFG/dataflow（影响面靠 call graph） | 同左 | **低价值** |
| test-gen | F1 0.991，14.4 KB，2 calls | F1 1.000，11.7 KB，4 calls | **边际增益 +0.9pt**；价值在“可直接喂给断言/覆盖工具的机器可读清单”，而非人类可读性 |

量化：debug/test-gen 上 C 相对 A 的 F1 增益只有 **+3.4pt / +0.9pt**，
字节成本基本持平（–15% / –19%），工具调用反而 +2.4 / +2.0 次，单查询延迟
高 30–40×（CLI 子进程启动开销；持久 MCP server 可摊薄）。
**用户怀疑成立**：对 xv6 这种小文件，函数内 CFG/dataflow 不值得默认深析；
它更适合“按需查询 + 结构化消费”，例如 test-gen 需要逐函数覆盖清单时一次
`branches/returns/resources` 查询，而不是默认注入 agent 上下文。
（OH 大仓可能不同：找函数本身仍靠符号索引/调用图，所以结论大概率不变。）

### 3.4 哪些深析事实默认提供 / 按需提供？

| 事实 | 建议 | 理由 |
|---|---|---|
| function list/detail、签名、位置 | **默认** | 低成本高命中，B 的 caller 查询基座 |
| 直接 callers/callees、1–2 hop graph、反向/双向 | **默认**（depth 上限 + 文件聚类） | 量化增益最大（§3.1） |
| 分支/返回/资源操作 | **按需**，以“函数摘要”（计数/错误返回数/锁配对）形式可默认 | C 相对 A 增益小；整表 JSON 冗长 |
| assignments（变量级） | **按需** | 对三类 agent 均未表现需求；上下文噪声大 |
| leaks/findings | **默认仅作线索**，必须带置信度/证据；不默认当 bug | D5 的唯一 leak finding 是**误报** |
| 完整 CFG block/edge | **不默认**，仅程序化消费/按需 | 信息可由源码/分支表推导，体积大 |

## 4. 失败案例

1. **D5 deep-analysis 误报**：`leaks` 报 `proc_mapstacks` kalloc 泄漏
   （severity=warning），但内核栈页表常驻、只初始化一次，无释放是设计意图。
   agent 若直接采信会写假阳性 bug 报告 → finding 必须标注“候选/需人工确认”。
2. **C5 syscall 间接边缺失**：`query callers sys_open` 返回空；源码
   `kernel/syscall.c:125` 是 `[SYS_open] = sys_open` 函数指针表。
   本 DB `call_targets` 为空，静态分析不建模 syscall 分发 → `open/fork/...`
   在调用图中是根节点，影响面分析漏掉 syscall 入口。**修复优先级高。**
3. **A 的“相关性过滤”未被建模**：harness 的 A 抽取器只做行级抽取，不能像
   LLM 那样删掉与问题无关的分支/调用；真实 agent 的 A 条件 F1 可能更低、
   上下文更长。故本文的 A 是**偏乐观**基线，CA 的正确性优势可能被低估，
   但成本对比不受影响。
4. **深析 JSON 冗长**：D3/T3 的 C 条件字节数（22.3 KB）高于 A 只读函数体
   （9.4 KB），因为 branches/returns/resources/calls 全量 JSON 带 id/block/col
   等字段。若按字节计，深析事实在单函数场景并不省——建议 CLI/MCP 支持
   `--fields` 投影与批量查询。
5. **B 的边界**：B 缺分支/返回/资源，debug/test-gen recall 仅 0.51/0.41；
   B 的有效场景是 coding 影响面与链路遍历的起点，不是深析替代品。

## 5. 推荐（面向 CA 产品）

1. **默认 API 面**：`functions/detail`、`callers/callees`、`graph`（depth 限制、
   反向、文件聚类）；显式标注 resolved/indirect/candidate 边。
2. **按需 deep facts**：函数级 `branches/returns/resources` 摘要（counts +
   错误返回数 + 锁 acquire/release 配对），完整清单再展开；assignments 后置。
3. **findings** 只作线索：附 evidence、confidence，默认标注 false-positive 风险。
4. **数据缺口**：填充 `call_targets`（syscall 表、函数指针、宏），否则
   sys_* 入口、间接调用链（C5）与部分跨文件影响面不可见。
5. **上下文成本**：MCP 增加字段投影/批量/紧凑输出；CLI 进程启动延迟在
   pilot 中放大了 C 的 wall time，MCP 常驻 server 下会消失。
6. **补测**：OH DB 就绪后复跑同一 harness（任务集换 OH 函数），
   重点验证“大仓下 call graph 增益是否进一步放大 + 深析是否仍低边际”。

## 6. 真实 LLM 小实验（6 runs，2026-10-18）

为验证 §2–§3 的 deterministic 结论，lead 用 subagent 独立执行了真实 LLM agent
A/C 小实验：3 类任务各取 1 个（D3/C2/T2）× 2 条件（A 源码 / C CA 基础+深度），
单次运行，答案在 `.tmp/llm_eval/`，评分脚本 `score_llm.py`，原始结果
`results/llm_pilot_xv6.json`。

### 6.1 协议与评分

- 同任务、同模型、独立 subagent；A 只读源码（grep/read），C 只用 CA CLI（禁读源码）。
- 评分：引用/断言级 GT 覆盖（复用 `tasks.json` GT；引用的 `file:line` 按文档顺序
  归属最近文件，≤12 行区间展开；精度只看任务 scope 内引用，scope 外正确引用另计），
  人工 rubric 0.3 权重评价因果推理/测试设计/缺口标注；combined = 0.7×F1 + 0.3×manual。
- 局限：每格仅 1 次、subagent 非生产 agent、xv6 小仓、单评审；不能作统计显著结论。

### 6.2 答案质量（A vs C）

| 任务 | A auto P/R/F1 | A manual | A combined | C auto P/R/F1 | C manual | C combined |
|---|---|---|---|---|---|---|
| D3 debug | .833/.758/.794 | .95 | .841 | **.963/.788/.867** | .85 | **.862** |
| C2 coding | 1.0/1.0/1.0 | **.95** | **.985** | 1.0/1.0/1.0 | .80 | .940 |
| T2 testgen | .724/.955/.824 | **.95** | .862 | **1.0/.864/.927** | .92 | **.925** |
| 均值 | .872 | **.950** | .896 | **.931** | .857 | **.909** |

- **事实枚举：C 更完整**（debug/testgen auto F1 +7.3/+10.3 pt）。C 逐条列出 CA 的
  branch/return/resource 行与调用者，D3/T2 的返回值与清理点覆盖优于 A 的叙述式答案。
- **因果/语义：A 更强**（C2 manual +0.15；D3 +0.10；T2 +0.03）。A 能解释
  “为什么锁可能缺失”（发现 `uartintr` 需新增通道锁）、“namex 尾斜杠/空路径反直觉
  语义”并给出可执行测试；C 只能基于 CA 字段推断，明确写出“CA 未覆盖”。
- **幻觉**：6 份答案的 `file:line` 引用 **0 个幻觉位置**（无不存在文件/越界行）；
  仅 1 处轻微范围误差（C2_A 把 wakeup 定义区间写成 574–595，实际 576–595，
  多含注释行）。C 条件在数据缺失处主动标注“未覆盖/未验证”，未编造事实。
- 与 deterministic pilot 一致的是**方向**：结构化事实提升枚举完备性，
  源码阅读保留语义推理优势；但两条件在 coding 任务打平，与 pilot 中 B/C 的
  大幅正确率优势不同（因为真实 agent 的 A 也能完整推断调用者）。

### 6.3 Effort（答案自报，含核验）

| 任务 | A（源码） | C（CA） |
|---|---|---|
| D3 | 13 tool calls（5 bash+8 read），6 文件，~18 KB 读取 | 28 CA 查询，~40 KB 输出（保存 JSON 20 KB） |
| C2 | 18 calls（4 grep+13 read+1 ls），10 文件，40–45 KB | 22 CA 查询，47,188 B 输出 |
| T2 | 6 文件读取，~17 KB（文件上界 33,482 B） | 21 CA 查询，137,765 B 输出（任务必需的 5 条查询 25,431 B） |
| 合计 | ~37 calls / ~75–80 KB | ~71 queries / ~225 KB |

**与 deterministic pilot 相反的发现**：真实 agent 的 C 条件上下文成本约为 A 的
**3×**、工具调用约 **2×**。原因不是事实无用，而是：(a) `query detail` 单函数输出
13–23 KB（带 id/block/col/params），agent 为交叉核对连发 20+ 条；
(b) 缺少批量/字段投影/摘要优先入口，agent 用“多查”弥补字段不可组合；
(c) A 的 agent 只读所需行区间，成本被自身策略压得很低。
→ **CA 的上下文优势不是自动的**，需要产品化：compact/投影输出、batch 查询、
“摘要→按需下钻”的推荐查询路径、MCP 常驻 server。

### 6.4 条件 C 的信息缺口（观察自 6 份答案，供 API/数据改进）

1. **声明/头文件缺失**：`functions` 只返回定义（kernel/defs.h 未被索引），
   C2_C 无法列出需同步修改的原型声明。
2. **returns 塌缩到同一 exit block**：D3_C/T2_C 无法由 block 配对“返回点↔清理路径”。
3. **锁操作与调用点的支配关系不可查**：无 CFG/支配查询，C2_C 不能证明调用时持锁。
4. **常量未入 DB**（MAXPATH/NDEV/NOFILE/NFILE）：C 无法量化错误条件。
5. **引用计数未建模**：`iget/idup/iput` 不在 resource_ops；`leaks` 无结果且
   `deep_ready=false`，C 明确拒绝据此断言“无泄漏”（这是正确的防御行为）。
6. **无 IRQ/上下文属性**、**无 `name` 输出参数赋值**（T2_C）。
7. **CLI 输出冗长**：`detail` 13–23 KB/函数，直接推高 C 的上下文成本。

### 6.5 对 deterministic 结论的验证

- ✅ 调用链/符号基础索引：两条件都能完整回答 direct caller 集合（C2 双方 12/12）；
  CA 的价值在**查询效率**，本次因 agent 过度查询而未体现（见 §6.3）。
- ✅ 函数内深析事实：C 的枚举完备性更高（§6.2），但语义解释仍靠源码阅读；
  与 §3.3“debug/test-gen 边际增益”一致，且 C 的 manual 质量未超过 A。
- ⏳ findings 误导：本轮未包含 D5；deterministic D5 + manual trace 仍是唯一直接证据。
  C 条件在缺失数据时表现出的谨慎（`leaks` 空结果不当作“无泄漏”）支持“findings 仅作线索”，
  但“误报是否诱导 agent 报假 bug”仍需 LLM 版 D5 实测。

## 7. xv6 全矩阵真实 LLM 评估（45 runs：15 任务 × A/B/C）

### 7.1 协议

- 15 个任务（debug/coding/test-gen 各 5）× 3 条件，每 cell **1 次独立 subagent 运行**
  （45 runs，无重复；lead 的 workflow fan-out）。答案 `.tmp/llm_eval/full/<ID>_<cond>.md`，
  评分脚本 `score_matrix.py`，原始结果 `results/llm_matrix_xv6.json`。
- 条件：**A** 只读源码；**B** 只用 CA 基础（`functions/callers/callees/detail` + 位置）；
  **C** CA 基础 + 深度（`branches/returns/resources/assignments/graph`）。
  所有条件禁读 `tasks.json`/GT/此前的答案。
- **重要事实修正**：`detail` 本身返回 L1 的 branch/return/resource 事实，因此 B
  **并非“没有深析事实”**；B/C 的真实差异是 C 允许显式深度查询与递归 graph。
  下文按实际条件解读，不再把 B 当作“浅层”条件。
- 评分：auto F1 = 引用/断言对 GT 的覆盖（`tasks.json`；引用按文档顺序归属文件、
  支持 `file:line`/范围/表格行号/裸行号等；scope 内精度）；rubric proxy = 每个
  task 的语义 checklist 命中率（自动，**非人类评审**，本轮饱和于 0.97–1.0，
  区分度有限）；combined = 0.7×F1 + 0.3×proxy；effort 取答案自报
  （tool calls/读取或 CA 输出字节/文件数）。**45 份答案 0 个幻觉 `file:line`**
  （无不存在文件/越界行）；未做逐条语义真伪复核，见 §9 局限。
- 统计：mean±std 跨任务；paired per-task delta + 精确 sign test（初判，n=15 总 /
  n=5 每类，单次运行，不能当作显著性结论）。

### 7.2 聚合结果（mean±std）

| 类别 | 条件 | F1 | rubric proxy | combined | 答案字符数 |
|---|---|---|---|---|---|
| ALL | A | 0.8259 ± 0.1386 | 0.970 | 0.8691 | 11,598 |
| ALL | B | **0.8598 ± 0.1596** | 0.987 | 0.8978 | **10,390** |
| ALL | C | 0.8573 ± 0.1301 | **1.000** | **0.9001** | 11,990 |
| debug | A | 0.6943 ± 0.0853 | 1.0 | 0.7860 | 7,878 |
| debug | B | 0.6680 ± 0.1096 | 1.0 | 0.7676 | 7,570 |
| debug | C | **0.7381 ± 0.1023** | 1.0 | **0.8167** | 9,380 |
| coding | A | 0.9818 ± 0.0407 | 0.95 | 0.9723 | 12,111 |
| coding | B | **1.0000 ± 0.0000** | 1.0 | **1.0000** | 9,217 |
| coding | C | **1.0000 ± 0.0000** | 1.0 | **1.0000** | 11,818 |
| test-gen | A | 0.8016 ± 0.0742 | 0.96 | 0.8491 | 14,805 |
| test-gen | B | **0.9113 ± 0.0576** | 0.96 | **0.9259** | 14,383 |
| test-gen | C | 0.8337 ± 0.0699 | 1.0 | 0.8836 | 14,772 |

### 7.3 Pairwise（paired per-task delta，正 = 后者更好）

| 范围 | F1 delta | F1 W/L/T | sign p | combined delta | combined p |
|---|---|---|---|---|---|
| ALL A→B | **+0.0339** | 8/2/5 | 0.109 | **+0.0287** | 0.065 |
| ALL A→C | **+0.0314** | 7/3/5 | 0.344 | **+0.0310** | 0.065 |
| ALL B→C | −0.0025 | 3/6/6 | 0.508 | +0.0023 | 1.000 |
| debug A→C | **+0.0439** | 3/1/1 | 0.625 | +0.0307 | 0.625 |
| debug B→C | **+0.0701** | 3/1/1 | 0.625 | +0.0491 | 0.625 |
| coding A→B/C | +0.0182 | 1/0/4 | 1.000 | +0.0277 | 0.500 |
| test-gen A→B | **+0.1097** | **5/0/0** | **0.0625** | +0.0768 | 0.0625 |
| test-gen B→C | −0.0775 | 0/5/0 | 0.0625 | −0.0423 | 0.375 |

初判：**test-gen 上 B 优于 A 有方向性意义**（5/5 胜，n=5 可达到的最小 p=0.0625）；
**debug 上 C 最优**（+4.4 pt F1，3/5 胜）；**B 与 C 无显著差异**（F1 −0.25 pt），
coding 上 B/C 达到天花板。总体 A→B/C 的 +3 pt 是“初步/可能”，不能作强结论。

### 7.4 Effort（答案自报；context bytes = A 读取源码 / B·C 的 CA 输出；KB）

| 条件 | tool calls 中位/均值 | context 中位 | context 均值 | context max | 文件数均值 | caveat 提及/答案 |
|---|---|---|---|---|---|---|
| A 源码 | 21 / 23.2 | **23.8 KB** | 31.6 KB | 100 KB | 10.4 | **0.33** |
| B CA 基础 | 20 / 21.5 | 56.3 KB | 63.7 KB | 153 KB | 0.5 | **16.53** |
| C CA 深度 | 25 / 26.4 | 31.2 KB | 843.6 KB | **5,529 KB** | 0.9 | 9.67 |

- **C/A 中位差 +31%，B/A +137%**：本轮真实 agent 没有把 CA 变成“省 context”。
  唯一例外是 **debug 类：C 中位 14.1 KB vs A 23.2 KB（−39%）**，因为 deep 查询
  可一次取到锁/分支/返回事实；而 **B 在 debug 上中位 81.9 KB**（detail 全量 dump），
  反而最贵。
- **C 的 2 个 5.4 MB 离群**（C2/T4）：agent 直接把 DB 导出成 JSONL（17,716 行）
  再本地分析——这是条件 C 的真实失败模式，也说明“原始 DB 可访问”会诱发过度取数。
- **B/C 答案的 CA-gap 提及 9.7–16.5 次/答案**（A 0.33）：头文件声明、returns
  exit-block、锁支配、常量、refcount 等缺口让 agent 大量自我限定/解释；既是
  诚实性优点，也是 token 成本。

### 7.5 失败模式与异常 cell

1. **D5（findings 误导）三条件全部判对**：A/B/C 都识别 proc_mapstacks 为
   **误报**，并给出“内核常驻/所有权转移”解释 → 在本轮 agent 能力下，findings
   线索没有诱导假 bug；仍建议 finding 默认标注“候选/需人工确认”。
2. **最低 F1 cell**：D5 三条件并列 0.571（GT 覆盖已满 R=1.0，但 P=0.4 因引用
   大量真实现码行）；D4_B 0.595（detail 无法枚举 buffer 内部路径）；D1_B 0.600
   （锁操作行缺失）；D4_A 0.638（A 散文引用多、精确行少 → 精度损失，非幻觉）。
3. **T3_C 的 0.0 异常已修复**：C 答案大量用表格裸行号（`339/347/…`）与
   `sys_open` 段内引用；评分器加入文档顺序 + section hint + 表格行号归属后
   恢复到 0.857。这提醒：**自动评分对引用风格敏感**。
4. **0 幻觉位置**：45 份答案的 `file:line` 全部可解析到真实文件/有效行；
   但“行存在”≠“语义正确”，本轮未做逐条语义核验（人工仅覆盖 §6 的 6 份）。
5. **C 条件的 graph 没有带来可测收益**：B（detail L1）在 test-gen 上甚至
   优于 C（0.911 vs 0.834）；C 的优势集中在 debug 的锁/路径题。
6. **条件 A 的语义优势未被 auto F1 捕捉**：人类评分的 6-run 子集里 A manual
   0.950 > C 0.857（§6），本轮 proxy 饱和（0.97–1.0），因此 combined 偏向
   引用完备性；结论应读作“结构事实覆盖”，不是“语义质量”。

### 7.6 结论（回答“哪些 fact 最值钱、哪些优化优先”）

- **最值钱**：符号/位置 + 直接调用者（coding 近 100% 正确）；debug 的
  分支/返回/资源/锁事实（C 最优且 context 最小）；test-gen 的 L1 分支/返回/
  资源清单（B 最优）。**graph 递归与 assignments 在本轮无正收益证据**。
- **深析 CFG/dataflow 的增量价值有限**：B 的 `detail` 已含 L1 事实；C 增加
  graph/深度查询只对 debug +4.4 pt。与 deterministic pilot 的“函数内事实
  低边际”一致，但 **debug 的锁/错误路径是例外**，值得按需保留。
- **真正瓶颈是接口形态而非分析深度**：B/C 中位 context 反而高于 A，agent
  会全量 dump `detail`/导出 DB。优化顺序见 §8 与
  [OPTIMIZATION_BACKLOG.md](OPTIMIZATION_BACKLOG.md)：① compact/字段投影；
  ② 批量查询；③ 摘要优先入口；④ 头文件声明索引；⑤ returns/支配关系修复；
  ⑥ call_targets/间接边；⑦ 常量/refcount/IRQ；⑧ findings 线索化。
- **OH 扩展**：验收 shard DB 就绪后按 [OH_EXTENSION_DESIGN.md](OH_EXTENSION_DESIGN.md)
  设计 6–9 个 C 任务（A/B/C 矩阵）并对比 xv6；预期大仓上调用链/符号索引的
  收益放大，接口成本问题更突出。

## 8. 下一步

- **补重复/方差**：对 3–5 个关键 cell 补第 2 次运行（同模型、随机顺序），
  验证本矩阵的单次结论；重点 cell：debug C、test-gen B、coding B/C、D5。
- **优化 A/B 实验**：实现 backlog #1–#3（compact/投影、批量查询、summary-first）
  后用同一 45-run 协议复测，验收指标 = context 中位 ≤ A、tool calls 下降、
  F1 不降；这是“CA 是否真的省 context”的关键实验。
- **语义质量补充**：对 9–12 份答案做人工双评（当前 auto F1 偏向引用完备性，
  proxy 饱和）；补 D5 类 findings 任务的 LLM 版本（本轮三条件均判对误报）。
- **OH 扩展**：验收 shard DB 就绪后按 [OH_EXTENSION_DESIGN.md](OH_EXTENSION_DESIGN.md)
  执行 6–9 任务 × A/B/C，对比 xv6 的 F1/context/tool calls。

## 9. 方法限制（必读）

- **主实验不是 LLM-agent 实跑**：§1–§5 的 90-run 主实验是
  **受控确定性检索流程**（每条件固定证据策略 + 同规则事实抽取器，记录真实
  命令/字节/调用数/延迟），测“事实获取成本/可得性”，不测 LLM 推理；
  人工 trace（`manual_traces.json`）抽样验证 3 个场景。
- **真实 LLM 实验规模有限**：§7 为 45 runs 但每 cell **仅 1 次**（无重复方差）、
  subagent 非生产 agent；§6 的 6-run 子集有单评审人工分。paired sign test 只能
  作“初判”，不能当显著结论。
- **auto F1 偏向引用完备性**：A 的散文答案引用精确行少、CA 答案直接复制行号，
  因此 F1/combined 对 CA 略有利；§6 人工子集里 A 的语义质量更高（manual .95 vs
  C .857）。rubric proxy 在本矩阵饱和（0.97–1.0），区分度低。
- **“0 幻觉”仅指 file:line 可解析**：45 份答案没有不存在的文件/越界行；
  未逐条复核“行存在但语义错配”的情况（人工仅覆盖 §6 的 6 份）。
- **条件 B 的实际信息面**：`detail` 已含 L1 分支/返回/资源事实，B 并非“浅层”；
  B/C 差异只是显式深度查询与 graph。评估结论应按此解读。
- **GT 与 C 同源风险**：GT 由 DB 枚举（C 的查询面）后逐条源码行校验；
  C 的 F1=1.0 接近同义反复。A/B 的对比仍有效（它们不查询同一面），但
  “C 比 A 更正确”应谨慎解读为“C 的字段完备性 = GT 定义”。
- **xv6 太小**（kernel .c ≈5.2K 行，单文件 15–50 KB）：读源码本来便宜，
  这**有利于 A**；大仓（OH）预期拉开差距，但本次未测。
- **单一 DB/语言/commit**，未做跨语料显著性检验；2 次重复只验证确定性，
  不是统计重复。延迟含 CLI 子进程启动，不代表 MCP 常驻开销。
- 本 pilot 未修改 `src/`；所有结果可由 §1 命令复现。
