# CA 用户指南

CA 从操作系统源码的**真实构建**出发：读取 `compile_commands.json`（或俘获构建）
中的精确编译参数，用 Clang 解析每个翻译单元，提取函数、类型、调用与源码位置，
再做 CFG/数据流分析得到分支条件、赋值、返回路径与资源操作，最终全部写入 SQLite
供快速查询。

流水线：**build → parse → analyze → store → CLI**。

## 安装

要求：Python 3.11、带（最好有）`compile_commands.json` 的 C/C++ 项目、
macOS 或 Linux。无需 sudo，Python 依赖均固定在 `requirements.txt`。

```sh
# 在仓库根执行
scripts/bootstrap.sh     # 创建 .venv、装固定依赖、`pip install -e .`、校验 libclang
.venv/bin/ca version     # editable 安装提供的控制台命令
```

`bootstrap.sh` 会以 editable 模式安装包，因此 `ca` 控制台命令位于
`.venv/bin/ca`。也可以用模块方式运行（适合不依赖控制台命令的脚本）：

```sh
PYTHONPATH=src .venv/bin/python -m ca version
```

## 索引仓库

```sh
# 用 xv6 俘获的 compile database 建索引（最稳妥是显式指定路径）
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --db ca.db \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json

# 指定 compile database 并限制工作量（快速检查）
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --db ca.db \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --jobs 8 --max-tus 20
```

`index` 选项：

| 选项 | 含义 |
|---|---|
| `--repo DIR` | 仓库根目录；提取出的路径都相对它 |
| `--db PATH` | 输出 SQLite 数据库（已存在则重建） |
| `--compdb FILE\|DIR` | `compile_commands.json` 或其所在目录 |
| `--jobs N` | worker 进程数（默认 8）；解析/分析并行，父进程是唯一写库者 |
| `--max-tus N` | 最多索引 N 个翻译单元 |
| `--source auto\|compdb\|capture\|synthesized` | 编译参数来源 |
| `--build-cmd CMD` | 仅 `capture` 模式：要执行的构建命令，如 `make -j8` |
| `--incremental` | 复用未变化 TU 的解析/分析缓存（缓存在 DB 旁 `<db-dir>/.ca-cache`） |
| `--cache-dir DIR` | 显式指定增量缓存目录（隐含 `--incremental`） |
| `--clear-cache` | 建索引前删除增量缓存（隐含 `--incremental`） |
| `--analyze-scope main\|all\|none` | 深析范围（默认 `main`，见下） |
| `--record-scope auto\|all\|main_file` | AST 记录范围：`auto` 在超过 200 TU 时用 `main_file`（默认），小仓保留 header 符号 |
| `--transport-filter none\|declarations` | 从 worker 传输负载中丢弃声明记录（有损、可选） |
| `--measure-transport` | 在 stderr 报告每 TU 的 ParseResult pickle 字节数 |

编译参数来源：

- `auto`（默认）：在仓库根或常见构建目录找到 `compile_commands.json` 就用；
  找不到则退化为合成参数，并在 stderr 打印醒目的 `WARNING: ... SYNTHESIZED`
  警告。`auto` 不会到 `--repo` 之外去找，compile database 在其他位置时必须显式
  传 `--compdb`。
- compile database 必须描述 `--repo` 之下的源码（`file` 与 `directory` 都要在
  其中，符号链接会归一化）；否则 `index` 会以退出码 `1` 明确报错，而不是静默
  写出空数据库。
- `compdb`：必须有 compile database，否则报错。
- `capture`：用编译器包装器执行 `--build-cmd`，记录真实命令；此时 `--compdb`
  指定输出目录。
- `synthesized`：不使用真实构建数据，按仓库布局猜测参数，且
  `CompilationUnit.source` 标为 `synthesized`。请把提取质量视为有所下降。

### 深析范围（大语料）

在 OpenHarmony 这类语料上，每个 TU 会展开数千个头文件内联函数，而 TU 主文件里
只有几十个定义。若每个包含头文件的 TU 都重复深析这些 header 定义，索引时间会被
分析主导，因此 `ca index` 默认：

| 范围 | 深析内容 | 适用场景 |
|---|---|---|
| `main`（默认） | 只深析 `sloc.file` 等于 TU 主源文件的定义 | 大语料；随 `--jobs` 扩展 |
| `all` | 全部定义；phase A 为每个定义文件确定唯一 owner TU，phase B 重解析并每个定义只分析一次（多一次 parse） | 需要全语料完整函数级事实 |
| `none` | 不做 CFG/数据流分析 | 只要符号/调用/调用图，最快 |

无论哪种 `--analyze-scope`，`query functions`、`callers/callees`、`graph` 与
MCP 查询都基于已提取记录工作；只有分支/赋值/返回/资源操作/findings 受深析范围
影响（header 函数没有深析事实）。

`--record-scope` 控制把多少 AST 变成记录：`all` 保留所有 header 与主文件定义/调用；
`main_file` 只记录 TU 主文件中的定义，以及 caller 属于已记录函数的调用点（类型、
字段仍全量保留，结构体布局/偏移分析可用），header 内实体在库中没有符号行。
`auto`（默认）在超过 200 TU 时用 `main_file`，否则用 `all`，因此小/玩具仓库保留
完整符号图，大语料不再为每个 TU 重复记录同一批 header 定义。在 OpenHarmony 上该
模式使跨进程负载下降约 85%（≈9.2MB → ≈1.4MB pickle/TU）、parse CPU 下降约 39%。
若大语料仍需要一份 header 符号，可用 `--record-scope all` 单独跑代表 TU（或后续
符号采集），两种范围可以跨次运行混用。

stderr 摘要会报告拆分，例如 `[index] scope=main record_scope=main_file: analyzed
347/354 definitions (7 skipped)`；加 `--measure-transport` 还会打印声明过滤前后的
pickle MB/TU。`ca update` 使用同样的默认值；增量缓存键同时包含两个范围，
`analyze-scope all` 因 ownership 是语料级而禁用缓存。

### 增量重建

`--incremental` 会为每个翻译单元缓存一份 `ParseResult` 及其分析结果，放在
`<db-dir>/.ca-cache`（或用 `--cache-dir DIR` 指定）。缓存键由 TU 主源文件内容、
语言/driver、精确编译参数以及工具/schema 版本共同决定。下次运行时未变化的 TU
会跳过 parse+analyze，直接回放缓存记录入库，stderr 摘要会报告命中率：

```sh
# 第一次：冷缓存（hits=0/N）
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db ca.db --incremental

# 第二次：未变化 TU 全部命中
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db ca.db --incremental
```

失效规则：

- 仅当 **TU 主源文件内容**、语言、driver、编译参数、工具版本与 schema 版本都
  未变化时才复用缓存；
- **头文件内容不参与缓存键**，但 `ca index` 会记录 include 依赖
  （`tu_includes`）：改头文件后 `ca update --changed-files` 只失效包含它的 TU
  （传递闭包）；若旧库/parser 没有 include 数据则回退清全缓存并警告；无关头文件
  是 no-op；
- 缓存是尽力而为：损坏/截断的条目会被丢弃并正常重新解析；`--clear-cache`
  （或删除 `.ca-cache`）永远安全。

进度与指标输出到 **stderr**，stdout 保持为空，便于脚本调用。部分翻译单元失败
不会中断整体运行：失败会列在摘要里，只要至少一个 TU 解析成功就返回 `0`；返回
`1` 表示参数/运行错误，或没有任何 TU 解析成功。

## 查询

所有查询都接受 `--db`、可选 `--limit N` 和 `--json`：

```sh
Q="PYTHONPATH=src .venv/bin/python -m ca query"
$Q functions kalloc --db ca.db
$Q callers kfree --db ca.db
$Q callees syscall --db ca.db
$Q branches syscall --db ca.db
$Q assignments fork --db ca.db
$Q returns wait --db ca.db
$Q resources kalloc --db ca.db
$Q leaks memory --db ca.db
$Q detail kalloc --db ca.db
```

- 符号参数是函数名或唯一子串（先精确匹配，再子串匹配）；`leaks` 例外，它把
  符号当作资源类型过滤。因此 `resources` 列出的是某函数**内部**的 acquire/release 操作。
- 默认在 stdout 输出人类可读表格（按事实类型选择列）。
- `--json` 输出原始查询行；键名稳定，即 `ca.core.schema` 的字段名，源码位置
  平铺为 `file, line, col, end_line, end_col`；`detail` 输出单个 JSON 对象。
- 退出码 `2` 表示查询成功但无结果；`1` 表示用法/运行错误。

只有 `leaks` 可省略符号，此时列出全部泄漏结论；其符号参数按资源类型过滤
（`memory`、`file`、`lock` 等）。其他查询类型都必须提供符号。

## 统计与导出

```sh
PYTHONPATH=src .venv/bin/python -m ca stats --db ca.db          # 行数 + 库大小
PYTHONPATH=src .venv/bin/python -m ca stats --db ca.db --json
PYTHONPATH=src .venv/bin/python -m ca export --db ca.db --out ca.jsonl
```

`export` 每行一个 JSON 对象，均带 `kind` 与 `schema`。kind 取值：
`function, type, field, call, cfg_block, cfg_edge, branch, assignment, return,
resource_op, finding`（若持久化还有 `compilation_unit`）。精度基准即使用该格式。

## 输出说明

`index` 向 stderr 输出摘要：

- 解析成功/失败的翻译单元数与数据库路径；
- 记录计数（函数、类型、调用、文件数、LOC）；计数为去重后实际写入的行数，
  头文件定义被多个 TU 解析时只计一次（`stats` 表中的计数与此一致）；
- `records produced before dedupe`（函数/类型/字段/调用的逐 TU 原始产出总量）：
  produced 计数包含每个 TU 重复解析到的头文件声明，仅作诊断，summary/DB 仍为
  去重唯一计数；
- 分析计数（已分析函数、分支、赋值、返回、资源操作、findings）；
- 耗时拆分：解析 CPU 秒（多 worker 求和）、分析 CPU 秒、写库秒、墙钟时间；
- 数据库大小与最多十行错误信息；
- 增量模式的 `[index] cache: hits=.. / misses=..` 行与缓存目录。

SQLite 表：`meta`、`tus`、`functions`、`params`、`types`、`fields`、`calls`、
`cfg_blocks`、`cfg_edges`、`branches`、`assignments`、`returns`、`resource_ops`、
`findings`。二级索引与 `ANALYZE` 在 `finalize()` 中完成；在 finalize 前中断的
数据库不会被查询优化。

## 分层索引（先基础、后按需深析）

超大源码树建议先建廉价基础层、再按需深析：

```sh
.venv/bin/ca index --repo <repo> --compdb <compdb> --db ca.db --jobs 20 --layer base
.venv/bin/ca ensure-deep --db ca.db --repo <repo> --compdb <compdb> \
    --symbols func_a,func_b --jobs 2
.venv/bin/ca deep-status --db ca.db
.venv/bin/ca prewarm --db ca.db --repo <repo> --compdb <compdb> --limit 20
```

`ca index`/`ca update` 是显式批处理命令；`ca watch` 自动保持数据库新鲜：轮询
源码/头文件 mtime、防抖合并并复用同一增量更新路径，逐轮输出指标（`--once` 单次
轮询）。详见 [incremental.zh.md](incremental.zh.md)。

`--layer base` 对每个唯一源文件只解析一次并跳过深析；符号/类型/调用/调用图立即可用，
`deep_ready=false`。`ensure-deep` 只解析目标函数所属 TU，写入 CFG/分支/赋值/返回/
资源/候选边并标记 `deep_ready=true`，重复调用命中深析状态（毫秒级）。MCP 通过
`ensure_deep`、`deep_status`、`prewarm` 工具提供同样能力，并在 detail/graph/事实
查询中增加 `deep_ready`/`pending` 字段。延迟预算、失效与回退见
[layered-indexing.zh.md](layered-indexing.zh.md)。

## 跨分片联邦查询

大语料可按 shard 建多个数据库（OH 验收为 `oh_S1.db` … `oh_S8.db`）。`query`、
`graph`、`stats` 支持目录或重复 `--db`，只读合并各 shard：

```sh
# 目录下所有 *.db（按路径排序）；不可读文件跳过
.venv/bin/ca query functions MySymbol --db-dir /home/yu/CA_OH/index --json
.venv/bin/ca query detail MySymbol --db-dir /home/yu/CA_OH/index
.venv/bin/ca query callers MySymbol --db-dir /home/yu/CA_OH/index --limit 20
.venv/bin/ca graph --from MySymbol --depth 2 --db-dir /home/yu/CA_OH/index
.venv/bin/ca stats --db-dir /home/yu/CA_OH/index

# 显式 shard 列表代替目录
.venv/bin/ca query branches MySymbol --db oh_S3.db --db oh_S5.db
```

- 合并后的每行 JSON/表格增加 `shard` 字段（DB 文件名 stem）；按稳定 id 去重
  （按路径排序，首个 shard 胜出）、确定性排序，`--offset`/`--limit` 在合并去重后
  精确应用；内部按 `offset+limit` 下推到各 shard 限制扫描量。
- `stats --db-dir` 汇总各表计数/字节数，并增加 `shards`（每个 shard 的
  `db_bytes`/`schema_version`/tables）与 `skipped`。
- 不可读/缺失的 shard 文件跳过并在 stderr 警告；没有任何可读 shard 时命令 exit 1。
- 联邦模式只读：索引、深析、回调后处理等写命令不适用于 shard 集合；`ensure_deep`/
  `prewarm` 需单个 `--db`。单 `--db` 行为不变（无 `shard` 字段）。
- shard 可原地查询（带 `-wal`/`-shm` sidecar），也可用单文件 `.db` 副本；副本回退
  到只读 `immutable=1`（可用但略慢），查询期间 shard 必须静止（正在写入/checkpoint
  的库不是合法联邦源）。
- 本地基准（macOS arm64，5 个 shard = 真实 OH S7 副本 + 4 个合成，只读；单文件
  副本）：`functions` p50 7.6ms / p95 9.1ms、`detail` 7.0/7.3ms、`callers`
  11.3/16.1ms、`graph depth=1` 6.9/7.4ms、`stats` 12.0/12.3ms；单 S7 基线
  p50 3.7–10.0ms（见 `benchmarks/results/federated_bench.json`）。带 sidecar
  原地查询比单文件 immutable 副本更快。

## 统一后端服务（`ca serve`）

`ca serve --db DB`（或 `--db-dir DIR`）为两个客户端提供同一常驻后端：浏览器用
`/api/*` + `/events`（SSE），agent 用无状态 `/mcp` 或
`ca mcp --serve-url http://127.0.0.1:8765`。服务还持有持久通用 job 队列
（创建/领取/完成），写在独立状态库中，索引 shard 保持只读。不内置 AI 解释功能。
端点、通用 job/SSE 语义、安全与限制见 [server.zh.md](server.zh.md)。

## Agent 接入：MCP、调用图、增量更新

CA 设计为跑在源码所在机器上，AI agent 不复制仓库而是直接查询。三个接口支撑该流程：

```sh
# 只读 MCP server（默认 stdio；HTTP 为 POST /mcp + GET /health）
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db
.venv/bin/ca mcp --db /home/yu/CA_OH/openharmony.db --transport http --port 8765

# 调用图：forward/reverse/both，限深度，支持 table/JSON/DOT
.venv/bin/ca graph --from syscall --depth 2 --db ca.db
.venv/bin/ca graph --from kfree --depth 1 --reverse --db ca.db --format dot

# 少量文件变更后的增量刷新（离线批处理仍用 index）
.venv/bin/ca update --repo .tmp/corpus/xv6-riscv --db ca.db \
    --changed-files kernel/syscall.c
```

MCP 暴露 `find_symbol`/`functions`、`callers`、`callees`、`graph`、`branches`、
`assignments`、`returns`、`resources`、`leaks`、`detail`、`stats`，输出稳定 JSON
信封、limit/分页与稳定错误码。工具 schema、SSH/隧道与服务器验证见
[mcp.zh.md](mcp.zh.md)；离线/在线模型、缓存失效（头文件按 include 依赖裁剪）与
watcher/daemon 设计见 [incremental.zh.md](incremental.zh.md)。

### 跨 TU 回调（后处理 pass）

索引内的回调解析只在单个 TU 内可见。跨 TU 的全局注册表（ops 结构体、数组表、
designated initializer、`AddCallback(&g_ops, fn)` 之类注册调用、参数传递的回调、
getter 返回的函数指针）由独立 pass 在**已有 DB**上后处理：

```sh
# 对已有 DB 后处理（幂等，可重复运行）
.venv/bin/ca resolve-callbacks --db ca.db --repo .tmp/corpus/xv6-riscv --jobs 2

# 或索引后自动执行
.venv/bin/ca index --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --db ca.db \
    --resolve-callbacks

# 查看注册事实与候选边
.venv/bin/ca stats --db ca.db --json          # .callbacks：注册数/对象路径/字段/模块/已连边
.venv/bin/ca graph --from filewrite --depth 1 --db ca.db --format json
```

该 pass 会重新解析 DB 中的 TU（需要 AST），输出解析成功/失败 TU 数、按 kind 的注册
事实、间接调用点、按 kind 的候选边、links 与耗时；`--max-tus` 可限制试跑规模。
只记录写入**全局存储**的注册，避免同名局部测试结构体造成误报。候选边就是普通
`call_targets` 行（`kind="indirect_candidate"`、`source="call_target"`、
`candidate=true`、`reason`），因此 `ca graph`、`ca query callees` 与 MCP `graph`
无需其他改动即可展示；MCP 另增 `callbacks` 工具
与 `stats.callbacks` 字段。未变更的 DB 重复运行不会写入任何行（自然键 upsert + 稳定
id）。同一逻辑边（同 caller/callee/line）的 header-inline 重复由 resolver 的
dedupe 规则折叠；完整注册/限定符/去重契约见
[cross-tu-callbacks.zh.md](cross-tu-callbacks.zh.md)。OpenHarmony 验收 shard
（libclang15，parse-only）上该 pass 新增 **264 条逻辑候选边**（S5 256、去重后
S7 8），candidate callers 从 2.18% 提升到 2.32%，生产路径抽样 precision 10/10。
增量重建/`ca update` 与 watcher 见 [incremental.zh.md](incremental.zh.md)。

## Web UI（只读浏览器）

`ca ui` 在同一个索引上提供本地只读 Web UI：符号搜索、函数详情（位置/签名/分支/
返回/资源/调用）、合并后的调用者/被调用者、SVG 调用图（候选虚线边表示可能目标
集合）、回调注册事实与数据库统计。联邦 shard 查询的每一行显示 `shard` 徽章。
服务默认只绑 `127.0.0.1`，提供标准库 JSON API（`/api/health`、`/api/search`、
`/api/function/...`、`/api/callers`、`/api/callees`、`/api/graph`、`/api/stats`、
`/api/callbacks`），且没有写端点：

```sh
.venv/bin/ca ui --db ca.db                 # http://127.0.0.1:8766/
.venv/bin/ca ui --db-dir shards/ --port 9000
```

前端位于 `src/ca/ui/static`，无 CDN、无构建步骤。页面、JSON 信封/错误码、只读
`PRAGMA query_only` 保证与 SSH 隧道说明见 [ui.zh.md](ui.zh.md)。

## 精度与性能

可复现脚本在 `benchmarks/`（结果写入 `benchmarks/results/`，已被 git 忽略）：

```sh
.venv/bin/python benchmarks/run_accuracy.py     # golden 夹具的 precision/recall/F1
.venv/bin/python benchmarks/run_parse_bench.py  # xv6 上的 TU/s、LOC/s、峰值内存
.venv/bin/python benchmarks/run_db_bench.py     # 写入 rows/s、查询 p50/p95

# 真实语料：始终显式传 compile database
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8
# 冷/暖增量重建：命中率与加速比
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8 \
    --incremental --out benchmarks/results/parse_bench_incremental.json
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/FreeRTOS-Kernel \
    --compdb .tmp/corpus/freertos/compile_commands.json --jobs 8 \
    --out benchmarks/results/parse_bench_freertos.json
```

### 内核规模

Linux 内核语料远大于玩具语料：容器化 Linux 构建产出 1,682 个真实 C TU
（kernel/ mm/ fs/ drivers/net/，arm64 defconfig）。内核单个 TU 会展开大量头文件，
parse 约 2–5s/TU CPU，之后还有分析；建议分层从少量 TU 开始：

```sh
# 前 100 个 TU（实测：100/100 解析、0 失败、107 MiB DB）
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/linux/linux-6.12.113 \
    --compdb .tmp/corpus/linux/compile_commands.json \
    --jobs 8 --max-tus 100 --out benchmarks/results/parse_bench_linux_100.json

# 同一层级的端到端索引 + 查询
.venv/bin/ca index --repo .tmp/linux/linux-6.12.113 \
    --compdb .tmp/corpus/linux/compile_commands.json \
    --db .tmp/linux-kernel.db --jobs 8 --max-tus 100
.venv/bin/ca query callers kfree --db .tmp/linux-kernel.db
.venv/bin/ca query resources skb_share_check --db .tmp/linux-kernel.db
```

18 核主机高负载下实测：墙钟 183s（parse CPU 447s、analyze CPU 432s、SQLite 写入
124s），DB 107 MiB，含头文件源码 67.4 万行；库内 26,570 个函数、49,321 次调用、
19,060 个分支、28,037 条赋值、14,286 条返回、1,457 个资源操作、18 条 findings。
每个内核 TU 会展开大量头文件，100 TU 即触发约 14 万次函数分析；该规模下分析、
写入与内存（父进程峰值约 6 GB）成为主要成本。全量 1,682 TU 属于数十分钟级批处理，
适合夜间 CI 而非交互使用。增量重建对未变化代码树的重复跑有效，但修改头文件后仍需
全量重建。

### OpenHarmony 规模验证

在 24 核/62GB Linux 服务器上用同一流水线实测 300 TU OpenHarmony 子集（rk3588 产品，
270 个唯一源文件）。使用大语料默认 `--record-scope main_file --analyze-scope main`，
环境生成头仍部分缺失（280/300 TU 解析成功）：

| 配置 | wall | CPU/TU | parse CPU | analyze CPU | write | pickle/TU | DB | 7 万文件 ETA |
|---|---|---|---|---|---|---|---|---|
| jobs=8, scope=main | 75.2s | 1.81s | 484.9s | 15.4s | 6.5s | 1.13MB | 89.2MB | 5.2h |
| jobs=20, scope=main | 54.2s | 2.80s | 745.9s | 25.1s | 12.7s | 1.15MB | 89.7MB | **3.75h** |
| jobs=8, scope=all | 139.7s | 3.43s | 939.7s | 14.7s | 6.9s | 1.17MB | 89.6MB | 9.7h |
| jobs=20, scope=all | 101.8s | 5.45s | 1487.0s | 23.4s | 15.4s | 1.17MB | 89.6MB | 7.1h |

同一子集在优化前记录范围（`--record-scope all`，即 "before" 基线）下实测：
jobs=8 时 191.8s wall / 3.39 CPU/TU / 7.53MB pickle/TU / 358.9MB DB；jobs=20 时
173.8s / 4.52 CPU/TU / 外推 12.07h。因此 `record_scope=main_file` 使 DB 下降约
75%、跨进程 pickle 负载下降约 85%、wall 提升 2.6–3.2×，全量 ETA 从 13.3h/12.1h
（jobs=8/20，略高于目标）降到 5.2h/**3.75h**。

目标（`scope=main`、`jobs=20`：CPU/TU ≤5s、全量 ETA ≤12h）达成：2.80s CPU/TU、
外推 3.75h。`analyze_scope=all` + main-file 记录也在 7.1h 内。以上为按相同 jobs
从 280 个已解析 TU 外推的 ETA；环境头补齐后 parse_ok 与 wall 预计会更好。

每个结果 JSON 都记录机器、语料版本、compile database 哈希、语料元数据（内核版本/
架构/配置）与命令行，数字可复现、可纵向对比。

## 测试

```sh
.venv/bin/python -m pytest -q                  # 全部测试
.venv/bin/python -m pytest tests/test_e2e_*.py -q
```

端到端测试会在临时目录构造自包含仓库；只有
`.tmp/corpus/xv6-riscv/compile_commands.json` 存在时才运行 xv6 测试。

## 面向 Agent 的查询辅助

- `ca brief <symbol> --db ... [--json]` 返回紧凑摘要：签名、位置、调用者/被调者
  计数、分支/返回/资源计数与首条关键条件，便于 Agent 先判断是否需要详情。
- `ca query detail <symbol> --fields function,deep_ready --json` 只保留指定的
  顶层字段；`--compact` 返回内置紧凑形态，避免 13–23KB 的 detail 载荷。
- `ca query <kind> --symbols FILE|- --json` 一次批量查询多个符号（逐符号结果、
  稳定顺序），同样支持 `--db-dir` 联邦索引。

## 已知限制

- **真实构建数据优于合成参数。** 没有真正的 `compile_commands.json` 时，
  flag、include 路径与宏都是猜测，头文件解析和类型查询精度下降。严肃分析请
  俘获真实构建。
- **预处理配置只有一个。** 只分析俘获命令中的宏/include，不探索其他配置的
  `#ifdef` 分支。
- **函数指针与间接调用**记录为未解析被调方；调用图仅对直接调用完整。
- **宏**在展开后分析；分支/赋值文本来自展开结果，可能与原始宏写法不同。
- **不建模跨函数数据流**：泄漏结论描述单函数内的资源路径，不代表函数之间
  资源所有权的转移。
- **分析失败按函数隔离、解析失败按 TU 隔离。** 部分结果请查看摘要中的错误行。
- **增量缓存只跟踪 TU 主源文件与编译参数。** 它复用未变化 TU 的解析/分析结果，
  但不检测头文件变化：改过头文件后请不加 `--incremental` 全量重建。数据库本身
  每次 `index` 仍会重建，只是跳过昂贵的 parse/analyze 工作。
- **单写者。** 并行 worker 只做解析/分析，SQLite 写入在父进程完成。
- 索引只覆盖 `--repo` 下的文件，且只覆盖 compile database 列出的翻译单元
  （合成模式下为发现的 `*.c`）。
