# 分层索引：先基础符号，按需深析

CA 把"谁在哪里"的廉价层与 CFG/数据流昂贵层分开：新检出后快速得到可用数据库，
只有真正被查看的函数才付出深析成本。

```text
L0 基础索引      符号/类型/字段/直接调用/位置
（ca index --layer base）                 deep_ready = 0
      │
      ▼
L1 按需深析      CFG/分支/赋值/返回/资源/候选边
（ca ensure-deep）  仅对请求函数         deep_ready = 1
      │
      ▼
L2 预热队列      按 Agent 查询频次后台预处理
（ca prewarm / MCP prewarm）
```

## L0 — 基础索引

```sh
ca index --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/compile_commands.unique.json \
    --db /home/yu/CA_OH/openharmony.db --jobs 20 --layer base
```

`--layer base` 对每个**唯一源文件只解析一次**（同一文件的重复 TU 被合并），强制
`record_scope=all` + `analyze_scope=none`，写入符号、类型、字段、直接调用与位置。
分析表为空，因此所有函数 `deep_ready=false`；只依赖基础层的查询（find_symbol/
functions、callers/callees、graph、detail 概要、stats、export）保持毫秒级。
`ca update --layer base` 可在重建时保持 lazy 模式并重置深析状态。

## L1 — 按需深析

```sh
ca ensure-deep --db /home/yu/CA_OH/openharmony.db \
    --repo /home/yu/CA_OH/openharmony \
    --compdb /home/yu/CA_OH/corpus/openharmony/compile_commands.unique.json \
    --symbols OHOS_MyFunc,MyClass::Method --jobs 2
```

对每个请求符号：解析函数行与其 TU；若 `deep_status` 已 ready 立即返回（命中）；
否则用 `record_scope=main_file` 解析该 TU（header 内联函数按需回退全记录解析），
只分析请求函数，经 `store.add_analysis_result` 写入事实并标记 ready。无关 TU
永不被解析；失败按函数隔离（`status="error"`，库不受影响）。

结果键（稳定）：`requested, results[{symbol,function_id,status,deep_ready,
latency_ms,tu,facts,error}], metrics{hits,computed,errors,hit_rate,parse_ms,
analyze_ms,write_ms,first_latency_ms,hit_latency_ms}`。

xv6（46 TU）实测：L0 基础索引 0.31s 墙钟 / 1.6MB DB / deep_ready=0；
`ensure_deep` 20 个函数：首次约 10ms/符号（其 TU 解析共 208ms），第二次约
5ms/符号（状态命中），0 错误。

OpenHarmony **unique-100** 抽样（libclang15 匹配后端，20 jobs）实测：

| 层 | 结果 |
|---|---|
| L0 基础 | 100/100 TU（已去重；21 个 TU 因缺生成头 degraded），206,017 函数 / 37,260 类型 / 192,340 调用 / 5,446 文件 / 143 万 LOC；parse CPU 711s（全记录提取约 7.1s/TU）、写库 69s、墙钟 87.5s、DB 359MB；深析覆盖率 0 |
| L1 ensure_deep（20 符号） | 首次 41.5s 总计（按需 TU 解析，约 2.06s/符号），20/20 计算、0 错误；第二次 20.4ms 总计（约 1ms/符号命中）；覆盖率 20/206,017 |
| MCP | `deep_status`/`detail` 返回深析标记；`ensure_deep` 命中约 1.6ms |

按同口径外推 L0 到 6.5 万唯一文件、20 jobs：约 **15.8h** 墙钟（瓶颈是全记录
提取 7.1s CPU/TU）；"数小时"目标需要提高并发（24 核约 13h）或等 parser 侧
header 记录剪枝优化。抽样语料仍缺生成头，补齐后 parse_ok/覆盖率会改善。
`ensure_deep` 首次延迟主要由所属 TU 的解析决定，同 TU 多符号批处理可摊薄。

## L2 — 预热队列

`ca prewarm --db ... --limit 20 --jobs 2` 分析 Agent 查询频次最高的符号（也可
显式 `--symbols`）。MCP server 自动记录查询频次；`prewarm` 支持同步或
`background=true` 守护线程。队列持久化在数据库旁，与离线索引完全解耦——慢预热
不会阻塞交互查询。

## deep_ready 可见性（CLI/MCP）

- `ca deep-status --db ... [--symbols ...] [--json]`：`{mode,total_functions,
  deep_ready,pending,failed,coverage}`。
- `ca query detail` 与 MCP `detail` 增加 `deep_ready`。
- MCP `graph` 增加 `deep_ready`（根节点；节点级由 store 可用时补充）。
- MCP `branches/assignments/returns/resources` 增加 `deep_ready` 与 `pending`；
  未深析时列表为空且 `pending=true`，调用方决定是否 `ensure_deep`。
- `leaks` 的 `deep_ready` 为 `null`（findings 跨函数）。
- 新增 MCP 工具：`ensure_deep`（symbols/jobs/wait）、`deep_status`、`prewarm`
  （limit/jobs/background）；其余工具 schema 不变，只增字段。

MCP server 需要 `--repo/--compdb` 才能做 lazy deep / prewarm；缺省时这些工具返回
`DEEP_UNAVAILABLE`，查询仍可返回基础事实。

## 状态持久化与失效

深析状态由 store（task-19）的 `deep_status` 表持久化：`mark_deep_ready/failed/
pending`、`get_deep_status`、`list_deep_pending/ready`、`count_deep_status`、
`deep_stats`、`invalidate_deep`；`query_function_detail`/`query_call_graph` 合并
`deep_ready`（只增键）。旧 store 无该 API 时，CLI/MCP 回退到数据库旁
`<db>.deep.json` 侧车，契约不变。`--layer deep` 索引会把已分析函数标记 ready
（`--analyze-scope all` 时为 full，mixed 运行按既有事实推断）。

文件变更时 `ca update` 重建数据库并重置深析状态（`--layer base` 保持 lazy）；
per-TU 深析失效等 store 的 per-TU replace 支持后通过 `invalidate_deep(tu_ids=…)`
落地。

## 延迟预算与回退

- 基础查询（符号/调用者/被调/graph/detail 概要）：毫秒级。
- `ensure_deep` 首次：一次 TU 解析（xv6 ~0.2s；大型 OH TU ~1–4s）分摊到该 TU 的
  多个符号 + 每函数毫秒级分析；命中：个位数毫秒。
- 解析/分析不可用（缺环境头、编译器 flags 不支持）时按符号返回
  `status="error"`，基础事实保留，查询继续以 `deep_ready=false` 工作。

## 复现

```sh
.venv/bin/python benchmarks/run_deep_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --symbols 20 --jobs 4
```

JSON 结果记录基础索引成本、首次/命中延迟、事实产出与覆盖率
（`benchmarks/results/deep_bench*.json`）。
