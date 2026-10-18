# DSH 插件（`dsh/`，bundle `@local/dsh-ca`）

Status: 已交付。CA 核心（`src/`、`tests/`、`docs/`）不变：这是一个增量的 DeepSeek
Harness bundle，复用 CA 只读 API 与 CLI。理由见[决策](../decisions/active/2026-10-18-dsh-plugin.md)。

## 包与激活

| 组成 | 职责 |
|---|---|
| `dsh/package.json`、`dsh/cordis.patch.yml` | 私有 bundle 清单：Host 入口、patch、`dsh.client`、Client 导出、locale |
| `dsh/lib/host/bootstrap.js` | Host 入口，`inject=['tools','connection','webServer']`；用带时间戳的 query 动态 import `impl.js`，重激活插件即重载 Host 代码（改 bootstrap 需重启 DSH） |
| `dsh/lib/host/impl.js` | 组装：BackendManager → TaskManager → ExplainService，再在 `ctx.effect` 中注册 RPC/tools/prompt |
| `dsh/lib/client/index.js` → `dsh/client.js` | Client 入口经 `dsh/scripts/build-client.mjs` 打包为 `window.__ModuleLoader__.load(...)`；挂载 `main` 整页（key/locale `ca`）与 `sidebar.panellist` 侧栏项 |

## Host 服务

- **BackendManager**（`dsh/lib/host/backend/`）：统一 status/request/exec。
  本地：`ca serve --db|--db-dir --port 0`，就绪 = stderr URL 行 + `GET /api/health`，
  有界崩溃重启、优雅停止。远程：ssh 探测 → 复用已有 checkout 或 rsync → venv →
  `pip install -e .`（必须 editable：`pyproject.toml` 无 package-data，wheel 会丢
  `schema.sql` 与 UI 静态资源）→ 远端 `ca serve` + 本地 `ssh -N -L` 隧道 → 经
  loopback 健康检查；停止用远端 PID 文件。设置：默认值 ← patch 配置 ← 用户在
  `$DSH_HOME/plugin-data/dsh-ca/settings.json` 的稀疏 patch（0600）；不写 DSH profile。
- **TaskManager**（`dsh/lib/host/tasks/`）：经 `execCa` 执行 CA CLI 长任务（`index`、
  `update`、`ensure_deep`、`prewarm`、`resolve_callbacks`、`watch`）；并发 2、每任务
  环形日志、`queued → running → succeeded|failed|cancelled`。
- **ExplainService**（`dsh/lib/host/explain/`）：用 DSH 原生模型
  `ctx.agentDefaultModel.currentSelection()` 与 `ctx.llm.stream`；先收集有界只读 CA
  证据（function、source、callers/callees、branches、resources、findings）；90s 超时；
  CA 不持有 LLM 配置。

## Client ↔ Host 通道

`ctx.webServer.register({kind:'prefix', path:'/dsh-ca'})`，不是
`connection.rpc.handle`（后者在 plain-JS 插件中激活失败：
`cannot get property "webServer" without inject`）。

```text
浏览器（DSH 已鉴权页面）               harness host                     CA 后端
  fetch POST /dsh-ca/<endpoint>  ─────> connection.requestRejection
    JSON envelope                      └─> dispatcher ── ca.request ──> GET/POST /api/*
  {ok:true,value} | {ok:false,error} <── BackendManager/TaskManager/Explain
```

端点：`ping`、`status`、`ca.request`、
`backend.get|configure|start|stop|restart|probe|install|installStatus`、
`tasks.start|list|get|logs|cancel`、`explain`。状态码：业务 200、参数错误 400、body
过大 413、未知端点 404、非 POST 405、信任拒绝 401/403；payload 在到达服务前校验并
限制大小。

## 原生 tools 与提示词

11 个 plain JSON Schema 工具经 `ctx.tools.register` 注册：`ca_status`、`ca_search`、
`ca_symbol`、`ca_graph`、`ca_facts`、`ca_source`、`ca_stats`、`ca_callbacks`、
`ca_registry`、`ca_explain`、`ca_task`。每个返回有界文本（约 20k 字符）并响应
`exec.signal`；`ca_task` 驱动 TaskManager，`ca_explain` 复用 ExplainService。
`prompt.js` 增加一段系统提示词，说明使用顺序（status → search → detail/facts/source）。

## Client 页面

`main` 整页 hash 路由：`#/search`、`#/fn/<symbol>/<tab>`、`#/graph`、`#/stats`、
`#/callbacks`、`#/tasks`、`#/backend`。视图：函数详情（源码、
overview/branches/returns/resources/findings accordion、三栏布局、调用图、CFG、AI
面板）、调用图（直接/回调/虚派发/环边）、CFG（entry/exit、T/F 标签、回边、
`deep_ready` 回退）。数据一律走 `/dsh-ca`；只用 `--dsw-*` 主题 token 与 `ca-` 类名
前缀；不改宿主 DOM。

## 零破坏边界

- CA `src/`、CLI、网页端与 MCP 均未改动；插件只调用既有只读 `/api/*` 并运行
  `ca serve`/`ca` CLI。不新增 CA 端点、不引入 CA 侧 LLM 配置、只写自己的状态目录。
- `ca ui`/`ca mcp` 可与插件并行使用；每个插件实例一个 CA 后端进程（单 `--db` 或
  联邦 `--db-dir`）。
- `dsh/client.js` 是构建产物；改 `dsh/lib/client/` 后用
  `node dsh/scripts/build-client.mjs` 重新生成。

## 限制

- CA 后端仅 loopback 且无鉴权；远程访问只经 SSH 隧道，页面本身由 DSH 鉴权。
- DSH 内部服务（`webServer.register`、`connection.requestRejection`、`slots`、
  `llm`、`agentDefaultModel`）是集成契约；宿主版本漂移是主要兼容风险。
- 除上述路由外无 reports/文件浏览器页面，也不写源码、不编译：CA 描述既有索引。
