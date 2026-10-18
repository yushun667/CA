# Agent 平台与服务器部署

Status: 阶段 3 验收进行中（OpenHarmony 优先）。

本文件描述 CA 在源码机上如何部署、如何被 AI agent 查询；理由见
`../decisions/active/`。

## 目的与用户

工具服务三类 AI agent 与人类程序员（UI 最后）：debug agent（符号/调用图上下文）、
coding agent（引用/影响/调用关系）、测试生成 agent（分支条件/资源语义/调用结构）。
阶段 3 先做 OpenHarmony C/C++。

## 组成

| 组件 | 职责 |
|---|---|
| `ca mcp`（`src/ca/cli/mcp.py`） | MCP JSON-RPC server（stdio 便于 SSH，或 HTTP）；15 个工具：基础事实（符号/函数、callers、callees、graph、detail、stats）+ `ensure_deep`、`deep_status`、`prewarm`；稳定信封/错误码与 `deep_ready` 标记 |
| `ca.cli.deep` + `ca.store.deep` | 分层检索：L0 基础事实；L1 `ensure_deep` 按请求函数物化 CFG/分支/赋值/返回/资源/call_targets；L2 优先级预热；`deep_status` 持久化 pending/ready/failed，L0 查询不依赖深析覆盖 |
| `ca.pipeline` 韧性 | 离线索引 chunk≤16 TU、broken-pool 重试（减半 workers→逐 TU）、wave 级重建池（`CA_POOL_RECYCLE_CHUNKS`）与稳定 id 幂等；TU 缓存与 scope key |
| Parser 后端选择 | 默认内置 libclang 18；OpenHarmony 通过 `CA_LIBCLANG_LIBRARY` 使用匹配的 OH libclang 15（见决策 `2026-10-18-matched-libclang-backend.md`） |
| `ca graph`（`src/ca/cli/graph.py`） | 正向/反向/双向遍历，深度与硬上限，路径/环查询，table/JSON/DOT；合并 direct 与候选边 |
| `ca.analyze.callbacks` | 间接调用候选解析：注册赋值、数组、参数传递、typedef、返回指针；按函数指针签名过滤，产出 `CallTargetRecord(kind=indirect_candidate, confidence, reason)` |
| `ca.store.graph` / `ca.store.workspace` / `ca.store.deep` | `call_targets` 持久化、图查询、workspace/project/variant 元数据与 `deep_status`；`store_schema_version=3`；L0 查询在零深析行时也可用 |
| 离线/增量模式 | `ca index` 建立离线全量快照；`ca update --changed-files ...` 只重解析变更 TU；daemon 设计见 [docs/incremental.zh.md](../../docs/incremental.zh.md) |
| `ca ui`（`src/ca/ui/server.py`） | 本地只读 Web UI（`--db`/`--db-dir`），vanilla 单页、JSON API、SVG 调用图；仅回环、无认证/写操作；20 项 UI 测试 |
| `ca.build.openharmony` | OpenHarmony 语料逻辑：repo manifest/project、GN/ninja 命令构造、`ninja -t compdb` 归一化、metadata；只操作工作副本 |

## 数据流与查询路径

```text
OpenHarmony 工作副本
  -> GN 生成（out/<product>/build.ninja）
  -> ninja -t compdb（真实 C/C++ flags，无需全量编译）
  -> 源码机上的离线批处理索引（L0 基础 + 主文件深析）
  -> 分片 SQLite 快照（每 shard/workspace 一个 DB）
  -> ca mcp（SSH 上的 stdio 或 LAN HTTP）
  -> AI agent 工具
       \-> ensure_deep / prewarm 按需物化 L1 事实并缓存
```

L0 基础查询立即返回并带 `deep_ready=0`；深析事实按请求函数/模块物化并缓存在
`deep_status`。增量更新只重解析变更 TU 并失效其 base/deep 行，绝不实时全量解析。

## 服务器布局（不进 Git）

- `/home/yu/CA_OH/openharmony`：镜像的 OpenHarmony 源码树（原树
  `~/OH_6.1/src` 保持只读）。
- `/home/yu/CA_OH/ca`：部署的 CA checkout（editable install）。
- `/home/yu/CA_OH/venv`：Python 3.11 环境与固定依赖。
- `/home/yu/CA_OH/pylib15`：隔离的 clang-15 bindings；配合 `run_ca_oh15.sh`
  用于 OpenHarmony 解析验收。
- `/home/yu/CA_OH/corpus/openharmony/`：`compile_commands*.json`、shard compdb
  与 `metadata.json`（product、board、manifest revision、工具版本、生成头目标、
  稳定性 profile）。
- `/home/yu/CA_OH/index/`：每 shard 验收 DB（`oh_sN.db`）、日志、
  `acceptance_summary.json`、失败/partial 归档与 EVIDENCE。

## 接口与依赖边界

- MCP 工具返回 `{ok, data, error}` 信封与稳定键；基础查询增加 `deep_ready`，
  深析列表带 `pending`；图边携带 `kind`、`confidence`、`source`、`candidate`。
- 候选边是启发式 may-call 集合，绝不与 direct 边混同。
- workspace 元数据使用 store 本地 ID（`ws:`/`prj:`/`var:`），不改变 core 契约。
- 服务器访问仅限内网/SSH，尚无认证层。

## 限制

- 跨 TU 回调候选需要全程序符号表（尚未产出）；struct-array 回调、整数地址函数
  指针、C++ 成员指针未建模。
- 候选目标按调用点/函数有界、带置信度；跨 TU 注册/全局回调表仍是已知覆盖缺口。
- 深析覆盖按设计是部分的（L1/L2）；验收分别报告深析覆盖与完整的 L0 基础覆盖。
- 全量验收索引是长时间离线分片批处理；实时更新只处理变更 TU。人类 UI 留到后续阶段。
