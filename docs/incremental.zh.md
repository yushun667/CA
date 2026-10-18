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

- 变更 TU 缓存未命中，重新 parse/analyze；
- 未变更 TU 命中缓存，直接回放记录；
- 数据库用“缓存 + 新记录”一次性重建。

命令会打印计划与缓存统计：

```text
[update] affected 2/1682 TUs (header_change=False), cache=/data/.ca-cache
[update] cache hits=1680/1682 misses=2; database rebuilt from cache
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
[update] cache hits=1680/1682 misses=2; database rebuilt from cache
```

没有被任何已索引 TU include 的头文件变更是 no-op（`ca update` 退出 2，提示
"not included by any indexed TU"）。旧库（没有 include 事实）或 parser 未采集时
回退旧的全缓存清理行为并告警。`ca watch` 使用同一映射，并在每轮 JSON 中报告
`dependency_aware`、`fallback_full`、`affected_count`、`cache_hits`、`cache_misses`。

## 当前限制：数据库行仍是全量重写

store 尚未提供 per-TU delete/replace，所以每次 `update` 都会用“缓存 + 新记录”重建
整个数据库文件（`ca update` 先删除旧库）。增量只发生在 parse/analyze，数据库写入
不是增量。计划中的 store API 是 `remove_units(tu_ids)`：级联删除
tus/functions/types/fields/calls、函数级事实（CFG、分支、赋值、返回、资源操作、
call_targets/findings），最后 finalize 重建 FTS 索引。就绪后 `ca update` 将切换为
“受影响 TU 先删后插”，不再重写未变更行。

## Watcher：`ca watch`

`ca watch` 在 Agent 查询期间保持数据库新鲜：轮询源码/头文件的 `size+mtime`
（可移植；原生 inotify/FSEvents 后端留待后续），静默去抖后合并为一批变更，复用
`ca update` 的 TU 规划器映射受影响 TU，并在进程内运行增量更新路径。

```sh
# 单次轮询（测试/脚本）；--json 输出一个 JSON 对象
ca watch --repo <repo> --db ca.db --compdb <compdb> --once --json

# 持续监听（SIGINT/SIGTERM 在当前轮询结束后退出）
ca watch --repo <repo> --db ca.db --compdb <compdb> --interval 5 --debounce 2

# 手动批处理（语义同 ca update，watch 指标格式）
ca watch --repo <repo> --db ca.db --compdb <compdb> --changed-files kernel/a.c
```

每轮指标（`--json` 每轮输出一行 JSON）：

```json
{"cycle": 2, "changed": ["kernel/syscall.c"], "changed_count": 1,
 "header_change": false, "affected_count": 1, "updated": true,
 "skipped": false, "skip_reason": "", "compdb_stale": false,
 "cache_hits": 45, "cache_misses": 1, "db_size": 4702208,
 "deep_invalidated": true, "elapsed_seconds": 0.359, "errors": []}
```

行为与限制：

- **状态**：`<db>.watch.json` 保存上次 size+mtime 快照，仅在更新成功后推进；失败
  轮询保留旧快照，下一轮重试同一变更集。
- **单实例**：`<db>.watch.lock`（flock）；第二个 watcher 以 code 2 退出。查询不受
  影响（SQLite WAL；watcher 是唯一写者）。
- **头文件变更**：只有传递 include 包含该头文件的 TU 重解析（依赖感知，与
  `ca update` 一致）；无关 TU 保持缓存命中。构建文件与无 include 事实的旧库仍
  回退清全缓存全量重建。
- **新增/删除文件不在 compile database 中**：compdb 无法知道它们，轮询报告
  `compdb_stale` 并跳过更新、记录错误；重新生成 compile database 后重启 watcher。
- **数据库整库重写**：store 的 per-TU replace 尚未实现，所以每轮会从缓存记录重写
  整个数据库文件（parse/analyze 增量，写入不是）。超大数据库上请把
  `--interval`/`--debounce` 设得合理；大规模变更直接用 `ca index`。
- **失败隔离**：每轮异常写入 `errors`，不杀死 daemon；SIGINT/SIGTERM 在当前轮询
  结束后退出并释放锁。

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

`ca update` 由端到端测试覆盖：dry-run 计划、变更 TU 重解析而其余走缓存、头文件
依赖感知失效。`tests/test_e2e_includes.py` 覆盖直接/传递 include、仅依赖 TU
重解析（无关 TU 缓存命中）、无关 header no-op、无 include 信息回退与
`ca watch --once` 依赖指标。`tests/test_e2e_watch.py` 覆盖基线快照、改源码后增量
更新、头文件更新、compdb 缺新文件时 `compdb_stale` 跳过、deep 状态失效、锁冲突、
失败不推进快照。另见 `tests/test_e2e_graph_mcp.py` 与 `tests/test_e2e_pipeline.py`。
