# OH（OpenHarmony）扩展评估设计草案

状态：**草案，等待验收 shard DB 就绪**（Lead 提供 DB/路径后确认）。目标：把
xv6 45-run 矩阵结论在真实大仓上复验，回答“调用链/符号索引增益是否随仓库规模
放大、函数内深析是否仍低边际、接口成本问题是否更严重”。

## 1. 前置条件（DB 就绪检查）

1. shard DB 可用：`ca query functions <sym> --db <db>`、`callers/callees/detail`
   正常；记录 DB 大小、functions/calls/branches/returns/resource_ops 行数、
   `deep_ready` 覆盖；确认 `call_targets` 是否非空（若仍为空，C5 类任务需备注盲区）。
2. 语料源码可读（GT 人工校验需要）；记录源码版本/编译参数来源。
3. 选 2–3 个 shard 的高价值子系统（S3/S4/S5/S7 对应），每个子系统选 2–3 个函数。

## 2. 任务集（6–9 个，3 类 × 2–3）

选择标准：跨文件调用者 ≥3；含锁/资源 acquire-release 或错误返回路径；分支 ≥4；
若做回调/间接边任务需有函数指针/注册表；GT 可人工校验。

| ID | 类 | 任务模板 | 目标事实（GT 来源） |
|---|---|---|---|
| OH-D1 | debug | 某锁/资源错误路径：定位 acquire 后哪些失败分支未 release，跨文件链路 | resource_ops + branches + returns + 调用链（源码人工校验） |
| OH-D2 | debug | 某错误返回/状态不一致：列出函数错误返回点与清理动作 | returns + branches + callees（源码校验） |
| OH-D3 | debug | 回调/候选边：谁注册了该回调、可能在哪些路径被调用（若 call_targets 可用） | call_targets/候选边 + callers（人工确认置信） |
| OH-C1 | coding | 修改函数 A 的签名/语义：直接+2-hop 影响面（函数/文件） | callers + graph reverse depth≤2（源码校验） |
| OH-C2 | coding | 修改公共 API：需同步的头文件声明/实现/调用点 | functions+声明（需 header 索引；若缺则记录 CA 缺口）+ callers |
| OH-C3 | coding | 模块边界/依赖：某模块被哪些子系统调用（跨文件聚合） | callers/graph + 文件聚类（人工抽检） |
| OH-T1 | test-gen | 为含多错误分支的函数设计覆盖清单（分支/返回/资源） | branches + returns + resource_ops（源码校验） |
| OH-T2 | test-gen | 锁/资源错误注入测试要点（失败路径清理） | resource_ops + branches + returns |
| OH-T3 | test-gen | 回调/异常路径覆盖（若候选边就绪） | call_targets + branches（人工确认） |

每个任务写：`id/class/prompt/scope(文件:行)/targets(rubric 要点)/gt[]`，
GT 事实结构复用 `tasks.json`（`kind=file:line` 或 `caller`），但需**逐条源码人工
确认**（OH 源码大，必须抽样 + 双人复核关键项；GT 校验记录写入 JSON）。

## 3. 运行协议（与 xv6 矩阵一致以便对比）

- 条件 A（只读源码）/ B（CA 基础：functions/callers/callees/detail/位置）/
  C（CA 基础+深度：branches/returns/resources/assignments/graph）。
- 6–9 任务 × 3 条件，每 cell 1 次；其中 3 个关键任务补第 2 次重复（估计方差）。
- 同模型、随机顺序、禁读 GT/tasks.json/历史答案；答案写入
  `.tmp/llm_eval/oh/<ID>_<cond>[_rN].md`，effort 段格式与 xv6 一致
  （工具调用次数/读取或 CA 输出字节/文件数）。
- 记录：答案、tool calls、context bytes、wall time、幻觉位置、CA 覆盖缺口。

## 4. 评分与对比

- 复用 `score_matrix.py`（已支持 `--tasks <oh_tasks.json>`；checklist 为空时
  combined 退化为 F1，需在 OH 任务里补 rubric checklist 或人工评分）。
- 每类/条件输出 mean±std、paired per-task delta + sign test（初判）。
- **与 xv6 对比表**：F1（A/B/C）、context 中位、tool calls、debug/test-gen 的
  C/B 增益、coding 的效率比（A context / C context）。
- 关键假设（待检验）：
  1. 大仓上 A 的 context/时间显著上升 → CA 的调用链/符号索引增益放大；
  2. debug 的 C 优势保持或扩大（锁/错误路径跨文件）；
  3. test-gen 的 B 优势保持（L1 分支/返回/资源清单）；
  4. 头文件声明/常量/refcount 缺口在 OH 更明显 → backlog #4/#8/#9 收益更高；
  5. 若不先做 compact/批量优化，CA 的 context 成本问题在 OH 上更严重。

## 5. 交付与依赖

- 产物：`benchmarks/agent_eval/tasks_oh.json`、`results/llm_matrix_oh.json`、
  REPORT.md 新增“OH 矩阵”章节、`docs/reference/agent-eval*` 同步。
- 依赖：验收 shard DB 路径、源码快照、可用模型/subagent 配额；**不阻塞**全量验收。
- 时间：任务设计+GT 校验 ~2–3 h；运行 6–9×3×~1 次 ≈ 1–2 h；评分/报告 ~1 h。
