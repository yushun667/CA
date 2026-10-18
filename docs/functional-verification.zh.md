# 阶段 3 功能验证：OpenHarmony dsoftbus + camera

Status: phase-3 功能验证（2026-10-18）；范围按用户要求仅限 OpenHarmony
**dsoftbus** 与 **camera** 仓库。全量 8-shard 验收与 S2/S6/S8 分片完成明确延后；
本文档不是整棵 OpenHarmony 树的验收报告。

## 范围与环境

- 代码基线：`28e1fdc`（core schema v2、store schema v7，包含 virtual 查询时合并
  修复）。
- 源码机：`/home/yu/CA_OH/openharmony`（只读工作副本），Python venv
  `/home/yu/CA_OH/venv`，匹配 OH **libclang15**
  （`CA_LIBCLANG_LIBRARY=.../llvm/lib/libclang.so.15`），`PYTHONPATH` 含
  `/home/yu/CA_OH/pylib15:/home/yu/CA_OH/ca/src`。
- 索引参数：`ca index --repo /home/yu/CA_OH/openharmony --compdb
  <subset>.json --db <db> --jobs 6 --record-scope main_file --analyze-scope
  main`；进程池设置 `CA_POOL_RECYCLE_CHUNKS=2`、`MALLOC_ARENA_MAX=2`、
  `PYTHONMALLOC=malloc`。
- 语料：`/home/yu/CA_OH/corpus/openharmony/functional/{dsoftbus,camera,
  dsoftbus_camera}.json`；基线 DB 在 `/home/yu/CA_OH/index/functional/`；
  增量副本 `dsoftbus_incr.db`/`camera_incr.db`；证据
  `index/functional_dsoftbus_camera.md`、`metadata.json`、`validation.json`。

## 索引与查询基线

| 语料 | TU | functions | calls | types | call_targets | tu_includes | parse 失败 | DB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dsoftbus | 1,287 | 91,858 | 414,309 | 22,218 | 389（209 indirect + 180 virtual） | 109,228 | 1 | 577 MB |
| camera | 475 | 27,261 | 119,764 | 12,050 | 8 | 69,021 | 0 | 188 MB |
| combined | 1,762 | 115,085 | 526,914 | 27,626 | 397 | 178,249 | 1 | 752 MB |

`stats` 同时报告 CFG/分支/资源/deep 覆盖（dsoftbus：41,558 branches、194,268
CFG blocks、41,115 deep_ready；camera：8,357 branches、51,102 CFG blocks、
10,342 deep_ready；combined deep_ready 51,457 = 44.7%）。缺失生成头/vendor SDK
的 TU 记为 degraded，不会中断索引；三个语料 parse 失败分别为 1/0/1。

三个库都用 CLI 验证过查询：

```sh
ca stats --db .../dsoftbus.db --json
ca query functions NetBuilderMessageHandler --db .../dsoftbus.db --json
ca query detail NetBuilderMessageHandler --db .../dsoftbus.db --json
ca graph --from NetBuilderMessageHandler --db .../dsoftbus.db --depth 1 --format json
```

`search` 返回预期函数/file/line；`detail` 返回 function/branches/assignments/
returns/resources/findings，且符号 `deep_ready=true`；`graph` 返回有界
nodes/edges。MCP 在三个库上用 stdio 与 HTTP 验证
（`scripts/verify_shard_mcp.py`）：18 个工具，`detail`/`graph`/`deep_status`/
`ensure_deep`/`prewarm` 正常，stdio 单次会话约 0.15s
（`verify_mcp_{dsoftbus,camera,dsoftbus_camera}.json`）。

## C++ virtual 候选

camera 中有一条真实跨 TU 虚派发链作为领域样例：
`OHOS::CameraStandard::DeferredProcessing::Command::Do` 调用
`Command::Executing`（10 个文件内共 27 个 override）；在 virtual 查询修复
（`store/virtual_dispatch.py`：LIMIT 绑定 + 递归方向）后：

- `ca query callees fn:6e2268c6d1c202d014c3f31e --db camera.db --json`：
  37 行 = 8 条 direct 边（含 direct virtual `Executing`，`source="call"`）+
  **29 条 `virtual_candidate`**（27 条 depth-1、2 条 depth-2），分布在 10 个
  不同 TU 文件（`video_process_command.cpp`、`photo_command.cpp`、
  `notify_video_job_command.cpp`、`service_died_command.cpp` 等）。
- 反向：`ca query callers <override-id> --db camera.db` 中 override
  `Executing`（video_process_command.cpp:45）报告 1 条 `virtual_candidate`
  调用者：command.cpp:37 的 `Command::Do`。
- graph：`ca graph --from <Command::Do id> --depth 1 --format json` 返回
  37 nodes / 37 edges，其中 29 条候选边；MCP `graph` 返回同样数据（18 工具）。
- 无 override 对照：`IsBframeSupported`→`GetCapability`
  （`dynamic_libs/av_codec/src/av_codec_adapter.cpp:38`）没有 override 元数据，
  查询只返回 direct virtual 边，**0 条 virtual_candidate**（不伪造边）。

combined 语料查询时统计（同修复代码，只读）：7,672 个 virtual call site，
**3,784 条候选边**、覆盖 1,531 个 call（20%），depth1 3,739 / depth2 45；
仅 201 条（5.3%）与 test/gtest 相关。dsoftbus 单库物化 `call_targets` 以 gtest
为主（`TestBody`→`what`），领域 virtual 少；查询时合并后领域候选也可用。
override 深度 ≤3 的全部已知目标都进入可能目标集合；候选带
`reason`/`source="virtual_candidate"`，以 override 深度/关系作为证据，direct 边
不会被替换；候选不排序、不评分。

## 注册式间接调用

dsoftbus `NetBuilderMessageHandler`
（`core/bus_center/lnn/net_builder/src/lnn_net_builder_process.c:893`）在第 906 行
通过全局表 `g_messageProcessor[msg->what]` 派发。graph/query 返回 **16 条
`indirect_candidate` 边**，带注册 reason（例如
`ProcessDeviceVerifyPass`、`ProcessMasterElect`、`ProcessSetReSyncDeviceName`
等）；直接调用边（`HiLogPrint`/`__builtin_strrchr`）保留。该结果验证注册表
路径可用；指针运算/动态索引仍只是启发式候选。

## Header 依赖增量 update/watch

增量副本先用 `ca index --incremental` 预热一次（cache 在
`index/functional/.ca-cache`）；真实 header 采用“备份→追加声明→验证→恢复”的
方式，基线 DB 未被写入。

- camera `common/utils/camera_simple_timer.h`（6 个依赖 TU）：
  `ca update --changed-files` → `include dependencies: 6/475`、
  `dependency-aware: invalidated 6 cache entries`、
  `cache hits=426/475 misses=49`（6 个依赖 + 43 个无可复用缓存的 TU），
  新声明可在 `camera_incr.db` 查询到。
- dsoftbus `core/common/include/softbus_bitmap.h`（6 个依赖 TU）：
  dry-run `dependency_aware=true, fallback_full=false, affected 6`；实际 update
  → `include dependencies: 6/1287`、invalidated 6、`cache hits=1252/1287
  misses=35`（依赖 + 失败/degraded TU 无缓存），新声明可查询；命中率主要由
  1,281 个未变 TU 贡献。
- 无关 header（新增、无 TU include）：`ca update --changed-files` 退出 **2**，
  提示 "changed header(s) are not included by any indexed TU"，不重解析。
  无 include 事实的旧库回退清全缓存并警告（有测试覆盖）。
- camera `ca watch --once --json`：baseline 正常；改 header 后报告
  `header_change=true`、`dependency_aware=true`、`fallback_full=false`、
  `affected_count=6`、`cache_hits=426`、`cache_misses=49`、`updated=true`、
  `deep_invalidated=true`，elapsed 181.7s（整库重写；parse/analyze 增量，写入
  尚未 per-TU）。
- Store 覆盖：`tu_includes` 69,021 camera / 109,228 dsoftbus / 178,249
  combined；include 依赖映射随 schema 升级（v7）保留，旧库安全回退。

## MCP / UI

- MCP stdio/HTTP 在三个库验证：18 工具；`detail`、`graph`、`deep_status`、
  `ensure_deep`、`prewarm` 返回稳定信封；camera MCP `graph` 与 CLI 一样返回
  29 条 virtual 候选。已无 AI 解释端点/工具（功能已移除）。
- UI（`ca ui`）本次服务器验证未实跑；同一只读 API/MCP 后端由本地 UI 测试覆盖。
  需要时可 `ca ui --db ...`（内嵌）或 `ca ui --serve-url ...` 打开。

## 质量门禁

- 本地全仓：`pytest -q` → **455 passed / 3 skipped**（含 parser/include/
  virtual/store/e2e）。
- `scripts/check_artifacts.py` → i18n pairs=70、benchmark results=17、errors=0；
  `.agents/validate.py --mode structure` 通过；precommit 用临时 staged index
  （未对真实工作树 git add）验证通过 + 预期 review/gate 提示。
- 未 git add/commit；未对基线 DB（`dsoftbus.db`、`camera.db`、
  `dsoftbus_camera.db`）跑 index/update/watch——仅做过查询（SQLite 打开可能更新
  WAL/meta mtime）；header 实验只用 `*_incr.db` 副本并已恢复每个被改 header。

## 限制

- 仅验证 dsoftbus/camera（及其并集）；不做 8-shard 全量 OH 验收，S2/S6/S8
  继续延后。
- virtual 候选只是可能目标（动态类型未知）；多继承/模板/无 override 元数据会
  保守或无候选；未跟踪 virtual destructor。dsoftbus 单库物化候选以测试为主，
  领域验证请用查询时结果或 combined 语料。
- 每个语料有 1 个 parse 失败 + 若干 degraded TU（缺生成头/vendor SDK），已报告
  但不在本次范围。
- header 增量仍整库重写；`ca watch` 的 header 变更轮虽只重解析依赖 TU，但比
  源码缓存命中轮明显慢。
- MCP/server 仅 loopback/无认证；无跨 shard 事务。

## 复现

```sh
# 索引（已执行；需要 OH libclang15 环境）
JOBS=6 ./functional_subset_index.sh
# 查询
ca stats --db index/functional/dsoftbus.db --json
ca query functions NetBuilderMessageHandler --db index/functional/dsoftbus.db --json
ca graph --from fn:6e2268c6d1c202d014c3f31e --db index/functional/camera.db --depth 1 --format json
# header 增量（先备份 header；使用 *_incr.db，不要写基线 DB）
ca update --repo $OH --db index/functional/camera_incr.db \
  --compdb corpus/openharmony/functional/camera.json --jobs 6 \
  --changed-files foundation/multimedia/camera_framework/common/utils/camera_simple_timer.h
ca watch --repo $OH --db index/functional/camera_incr.db \
  --compdb corpus/openharmony/functional/camera.json --json --once
```
