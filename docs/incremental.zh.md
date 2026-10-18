# 离线索引与增量更新

CA 把批处理与交互查询分离：

- **离线全量**——`ca index`（或改动后的 `ca update`）解析/分析源码并重写 SQLite
  数据库。放后台或定时跑；agent 从不触发全量解析。
- **在线查询**——MCP server（`ca mcp`）与 CLI 查询命令只读已完成的数据库，从不
  写库。

本页说明增量模型、失效边界与 watcher/daemon 设计。MCP 协议见 [mcp.zh.md](mcp.zh.md)；
基准数字见 `benchmarks/results/`。

## 两个入口

```sh
# 离线：对 compile database 中所有 TU 做全量
ca index --repo /src/proj --compdb /src/proj/compile_commands.json \
    --db /data/proj.db --jobs 24

# 增量：改动少量文件后刷新
ca update --repo /src/proj --compdb /src/proj/compile_commands.json \
    --db /data/proj.db --changed-files src/foo.c src/bar.c

# 只看受影响 TU 计划，不动数据库
ca update --repo /src/proj --compdb /src/proj/compile_commands.json \
    --db /data/proj.db --changed-files src/foo.c --dry-run
```

`ca update` 把每个变更路径映射到编译单元，然后用 per-TU 缓存重新跑流水线：

- 变更/受影响 TU 缓存未命中，重新 parse/analyze；
- 未变更 TU 保持数据库现有行（缓存命中，不解析、不回放、不重写）；
- 只对受影响 TU 原地替换行（`remove_units` 级联 + 单事务重新插入），FTS5
  增量维护；
- 存在回调注册或间接调用点时，写入后刷新跨 TU 回调候选。

命令会打印计划与缓存统计：

```text
[update] affected 2/1682 TUs (header_change=False), cache=/data/.ca-cache
[update] db_write: incremental tus=2 rows -136/+141 fts -9/+10
[update] callback_refresh: rerun units=1682 registrations=391 candidate_edges=196 links=196
[update] cache hits=1680/1682 misses=2 (unaffected TUs left untouched)
```

## 缓存语义

缓存目录默认 `<db-dir>/.ca-cache`，可用 `--cache-dir` 指定。每个 TU 一个条目，保存
独立的 `ParseResult` 与其 `AnalysisResult`。键是以下内容的 SHA-256：

1. TU **主源文件内容**；
2. 语言与编译器 driver；
3. 精确编译参数；
4. 深析范围（`main`/`all`/`none`）与记录范围（`all`/`main_file`）；
5. CA 工具版本与 schema 版本。

`analyze_scope=all` 无法 per-TU 缓存：它的定义文件 ownership 是语料级的
（phase A），因此该范围下 `ca update` 退化为全量分析，流水线也会禁用增量缓存。

缺失、损坏或截断的条目视为未命中：正常重新解析并重写条目。删除缓存目录永远安全
（`ca index --clear-cache` 会自动删除）。

### 失效边界

| 变更 | 是否检测 | 行为 |
|---|---|---|
| 主源文件内容 | 是 | 下次 `update` 重解析该 TU |
| 编译参数/include flag | 是 | 受影响 TU 重解析（缓存键变化） |
| 工具或 schema 升级 | 是 | 整个缓存一次性失效 |
| **头文件内容** | 是（有 include 事实时） | 只有传递 include 集合包含该头文件的 TU 重解析；无关 TU 保持缓存命中 |
| 头文件/构建文件但无 include 事实 | 回退 | 旧行为：清空整个缓存、警告并重解析全部 TU |
| 构建文件（`Makefile`、`CMakeLists.txt` 等） | 构建文件仍全量重解析 | 仅 header-like 变更才做依赖裁剪 |
| compile database 增删文件 | 下次 `update` 重新解析 compdb | 新 TU 解析；被删 TU 在数据库重建后消失 |

`ca index` 会记录每个 TU 的 repo 内直接 include 与 include 边（含 header→header），
store 把传递闭包持久化到 `tu_includes`（`include_path -> tu_id`），因此变更头文件
可以直接映射到依赖 TU：

```text
[update] include dependencies: 2/1682 TUs include the changed header(s)
[update] dependency-aware: invalidated 2 cache entries for 2 dependent TUs
[update] cache hits=1680/1682 misses=2 (unaffected TUs left untouched)
```

没有被任何已索引 TU include 的头文件变更是 no-op（`ca update` 退出 2，提示
"not included by any indexed TU"）。旧库（没有 include 事实）或 parser 未采集时
回退旧的全缓存清理行为并告警。`ca watch` 使用同一映射，并在每轮 JSON 中报告
`dependency_aware`、`fallback_full`、`affected_count`、`cache_hits`、`cache_misses`。

## 数据库原地增量写入

`ca update` 不再删除并重建数据库：只解析/分析受影响的 TU，并用 store 的
per-TU delete+insert 原语（`remove_units`/`replace_units`）原地替换。级联删除该
TU 的 `tus`/functions/types/fields/calls/CFG/数据流事实/call targets/callbacks/
links/deep_status/includes，然后在同一事务中写回新记录。未受影响 TU 的行 id 与
内容保持不变，FTS5 只对受影响函数做增量 delete/insert（不跑整库 `finalize()`）。

构建文件、没有 include 事实的旧库以及 `analyze_scope=all` 仍走保守的全量重建路径
（后者因为 definition ownership 是语料级的）。header 内联定义归属于首个写入它的
TU，因此 update 除依赖计划中的 TU 外，还会重索引“拥有该 header 行”的贡献 TU。

写入后，若数据库存在回调注册或间接调用点，会刷新跨 TU 回调候选：复用现有回调
pass（`finalize=False`），并在之前删除仅关联被替换注册的旧候选边，保证不残留；
没有回调数据时跳过。该 pass 为了 AST 级事实会重新解析 TU。

指标：

```json
{"db_write": {"mode": "incremental", "tu_count": 2,
              "removed_rows": 136, "added_rows": 141,
              "fts_deleted": 9, "fts_inserted": 10},
 "incremental_rows": {"removed": 136, "added": 141},
 "callback_refresh": {"mode": "rerun", "units": 1682, "registrations": 391,
                      "candidate_edges": 196, "links": 196}}
```

`db_write.mode` 为 `incremental`、`full_rebuild`、`noop` 或错误时缺省；
`incremental_rows` 是受影响 TU 的行增减；`callback_refresh.mode` 为
`rerun`/`skipped`/`error`，附带计数与耗时。增量写入失败会回滚事务、保留旧行；
回调刷新失败只告警，索引行仍有效，可下次全量 `ca index`/`ca resolve-callbacks`
修复候选集合。

## Watcher：`ca watch`

`ca watch` 在 Agent 查询期间保持数据库新鲜：优先使用原生事件后端——Linux
**inotify**（libc ctypes 递归 watch）或 macOS **FSEvents**（CoreServices、文件级
事件、~0.1s 延迟），并保留可移植的 `size+mtime` 轮询作为回退。事件按 C/C++
源/头后缀过滤、去重、debounce 后直接作为 changed paths 传给与 `ca update` 相同的
规划器/增量写路径；启动 baseline 与周期性 snapshot 对账用于兜底丢事件。
`--backend auto|native|poll` 选择模式：`auto` 优先 native、不可用/初始化失败回退
poll，`native` 不可用直接报错，`poll` 保持旧行为。

```sh
# 单次轮询（测试/脚本）；--json 输出一个 JSON 对象
ca watch --repo <repo> --db ca.db --compdb <compdb> --once --json

# 持续监听（SIGINT/SIGTERM 在当前轮询结束后退出）
ca watch --repo <repo> --db ca.db --compdb <compdb> --interval 5 --debounce 2 \
    --backend auto

# 强制原生事件（inotify/FSEvents；不可用则 exit 1）
ca watch --repo <repo> --db ca.db --compdb <compdb> --backend native

# 手动批处理（语义同 ca update，watch 指标格式）
ca watch --repo <repo> --db ca.db --compdb <compdb> --changed-files kernel/a.c
```

每轮指标（`--json` 每轮输出一行 JSON）：

```json
{"cycle": 2, "changed": ["kernel/syscall.c"], "changed_count": 1,
 "header_change": false, "affected_count": 1, "updated": true,
 "skipped": false, "skip_reason": "", "compdb_stale": false,
 "cache_hits": 45, "cache_misses": 1, "db_size": 4702208,
 "db_write": {"mode": "incremental", "tu_count": 1, "removed_rows": 63,
              "added_rows": 64, "fts_deleted": 3, "fts_inserted": 3},
 "incremental_rows": {"removed": 63, "added": 64},
 "callback_refresh": {"mode": "skipped", "reason": "no callback registrations or indirect calls"},
 "backend": "native", "fallback_reason": "",
 "deep_invalidated": true, "elapsed_seconds": 0.359, "errors": []}
```

行为与限制：

- **原生后端**：Linux inotify 递归监听仓库（跳过 `.git`、`.tmp`、`build`、`out`、
  `node_modules`、`.ca-cache` 等），处理 create/modify/close_write/delete/move/
  attrib，新目录自动加 watch、删除目录清理；文件事件按
  `.c/.cc/.cpp/.cxx/.c++/.h/.hh/.hpp/.hxx/.inc` 过滤。macOS 用 FSEvents 文件级
  事件，跑在独立 run-loop 线程。平台/框架/初始化不可用时返回 `None`，`auto`
  记录 `backend="poll"` + `fallback_reason`。native 事件直接作为 changed paths；
  每 10 个 interval 仍跑一次 snapshot 对账兜底丢事件。
- **状态**：`<db>.watch.json` 保存上次 size+mtime 快照，仅在更新成功后推进；失败
  轮询保留旧快照，下一轮重试同一变更集；native 循环也维护该快照用于对账与重启。
- **单实例**：`<db>.watch.lock`（flock）；第二个 watcher 以 code 2 退出。查询不受
  影响（SQLite WAL；watcher 是唯一写者）。
- **头文件变更**：只有传递 include 包含该头文件的 TU 重解析（依赖感知，与
  `ca update` 一致）；无关 TU 保持缓存命中。构建文件与无 include 事实的旧库仍
  回退清全缓存全量重建。
- **新增/删除文件不在 compile database 中**：compdb 无法知道它们，轮询报告
  `compdb_stale` 并跳过更新、记录错误；重新生成 compile database 后重启 watcher。
- **数据库原地写入**：每轮只替换受影响 TU 的行，未受影响行的 id/内容保持稳定。
  有回调注册/间接调用时会触发回调刷新 pass（为 AST 事实重新解析 TU），否则
  跳过；大规模变更/构建文件仍走全量重建，建议直接 `ca index`。
- **失败隔离**：每轮异常写入 `errors`，不杀死 daemon；SIGINT/SIGTERM 在当前轮询
  结束后退出并释放锁。native 运行时失败会回退到 poll 继续运行（记录
  `backend="poll"`/`fallback_reason`）；`--backend native` 启动失败退出 1，
  `--backend auto` 自动回退。
- **平台限制**：inotify 受内核 watch 上限约束（watcher 最多 5 万目录），
  进程未运行时发生的变更不会产生事件；FSEvents 有 0.1s 合并延迟，可能同时给出
  目录根路径（已按后缀过滤）。不支持的平台始终使用 poll；`--once`/
  `--changed-files` 无论 backend 都走 snapshot cycle。

最小 systemd unit（Linux 服务器）：

```ini
[Unit]
Description=CA incremental index watcher
After=network.target

[Service]
User=yu
WorkingDirectory=/home/yu/CA_OH/ca
Environment=PYTHONPATH=/home/yu/CA_OH/ca/src
Environment=CA_LIBCLANG_LIBRARY=/home/yu/CA_OH/openharmony/prebuilts/clang/ohos/linux-x86_64/llvm/lib/libclang.so.15
ExecStart=/home/yu/CA_OH/venv/bin/python -m ca watch \
    --repo /home/yu/CA_OH/openharmony \
    --db /home/yu/CA_OH/openharmony.db \
    --compdb /home/yu/CA_OH/openharmony/out/rk3588/compile_commands.json \
    --interval 10 --debounce 2
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 失败处理

- `update` 失败时缓存保留，下次重试相同受影响 TU；数据库只在流水线 `finalize()`
  成功后替换。
- 损坏缓存条目自动删除。
- 头/构建文件变更强制全量重解析，避免脏结果。
- 批量上限防止大规模变更（如 `git checkout`）退化成成千上万次单文件更新；这类
  场景直接用 `ca index`。

## 验证

`ca update` 由端到端测试覆盖：dry-run 计划、`replace_units` 原地写入、仅受影响
TU 变化且未变 TU 行/id 不变、更新后 FTS 可搜索、回调候选与全新全量索引一致、
header 依赖感知失效与全量回退。`tests/test_e2e_includes.py` 覆盖直接/传递 include、仅依赖 TU
重解析（无关 TU 缓存命中）、无关 header no-op、无 include 信息回退与
`ca watch --once` 依赖指标；`tests/test_e2e_incremental_write.py` 覆盖原地写、
FTS、回调刷新与 watch 指标。`tests/test_e2e_watch.py` 覆盖基线快照、改源码后增量
更新、头文件更新、compdb 缺新文件时 `compdb_stale` 跳过、deep 状态失效、锁冲突、
失败不推进快照。另见 `tests/test_e2e_graph_mcp.py` 与 `tests/test_e2e_pipeline.py`。
