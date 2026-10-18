# DSH 插件（`dsh/`，bundle `@local/dsh-ca`）

Status: 已交付。在 CA 只读 API/CLI 之上的增量 DSH bundle；CA 核心（`src/`、`tests/`、
`docs/`）不变。理由见[决策](../decisions/active/2026-10-18-dsh-plugin.md)。

## 包与激活

| 组成 | 职责 |
|---|---|
| `package.json`、`cordis.patch.yml` | 私有 bundle 清单：Host 入口、patch、`dsh.client`、Client 导出、locale |
| `lib/host/bootstrap.js` | Host 入口，`inject=['tools','connection','webServer']`；对 `impl.js` 做 cache-busting import，重激活插件即重载 Host 代码（改 bootstrap 需重启 DSH） |
| `lib/host/impl.js` | 组装：backend/tasks/explain → RPC、tools、prompt，全部在 `ctx.effect` 内 |
| `lib/client/index.js` → `client.js` | 由 `scripts/build-client.mjs` 打包为 `window.__ModuleLoader__.load(...)`；挂载 `main` 整页（key/locale `ca`）与 `sidebar.panellist` 侧栏项 |
| `backend/wheels/` | 内置 CA wheel；离线依赖 wheelhouse 由 `scripts/build-wheelhouse.mjs` 生成 |

## 项目模型

**项目** = 源码树 `sourceRoot` + 唯一数据库（`db` 单库或 `dbDir` 分片目录）+ 后端位置
（`local`，或带 SSH 字段的 `remote`），另有可选 `compdb`/`python`/`caRepoPath`（CA
检出目录）。用户在 `#/projects` 手动创建/编辑/删除具名项目（名称唯一、忽略大小写），
持久化在 `$DSH_HOME/plugin-data/dsh-ca/settings.json` 的 `projects[]` +
`activeProjectId`（0600）。激活会把项目映射为后端配置、重启 `ca serve`，失败则回滚；
旧版 settings 会迁移为一个默认项目。保存/激活时校验路径（本地必须存在，远程只做格式
校验）。目录扫描（`backend.databases`/`useDatabase`）仅作为低层辅助保留。

## Host 服务

- **BackendManager**（`lib/host/backend/`）：status/request/exec + 项目存储；本地
  `ca serve --db|--db-dir --port 0`（就绪 = stderr URL + `/api/health`）。
- **Installer**（`lib/host/backend/installer/`）：在本地（Win/mac/Linux）或远程
  Linux 上用内置 CA wheel（`backend/wheels/`）安装 CA，离线可加 wheelhouse；
  Python >= 3.11 自动探测。远程上传优先 scp，否则走 ssh；legacy `source` 模式保留
  rsync + editable。安装摘要持久化在 `install-state.json`；远程 `ca serve` 经
  `ssh -N -L` 隧道运行、按 PID 文件停止。
- **TaskManager**（`lib/host/tasks/`）：经 `execCa` 执行 CA CLI 任务（`index`、
  `update`、`ensure_deep`、`prewarm`、`resolve_callbacks`、`watch`）；并发 2、环形
  日志、`queued → running → succeeded|failed|cancelled`。
- **ExplainService**（`lib/host/explain/`）：DSH 模型（`agentDefaultModel` +
  `ctx.llm.stream`）基于有界 CA 证据；90s 超时；CA 不持有 LLM 配置。

## Client ↔ Host 通道

`ctx.webServer.register({kind:'prefix', path:'/dsh-ca'})`，不是 `connection.rpc.handle`
（后者在 plain-JS 插件中激活失败：`cannot get property "webServer" without inject`）。

```text
浏览器（DSH 已鉴权页面）               harness host                     CA 后端
  fetch POST /dsh-ca/<endpoint>  ─────> connection.requestRejection
    JSON envelope                      └─> dispatcher ── ca.request ──> GET/POST /api/*
  {ok:true,value} | {ok:false,error} <── BackendManager/TaskManager/Explain
```

端点：`ping`、`status`（含活动 `project`）、`ca.request`、
`backend.get|configure|start|stop|restart|probe|install|installLocal|installStatus`、
`projects.list|save|remove|activate`、`tasks.start|list|get|logs|cancel`、`explain`。
状态码：业务 200、参数 400、过大 413、未知 404、非 POST 405、信任拒绝 401/403。

## 原生 tools 与提示词

12 个 plain JSON Schema 工具经 `ctx.tools.register` 注册：`ca_status`、`ca_project`、
`ca_search`、`ca_symbol`、`ca_graph`、`ca_facts`、`ca_source`、`ca_stats`、
`ca_callbacks`、`ca_registry`、`ca_explain`、`ca_task`；输出有界文本（约 20k 字符）、
响应 `exec.signal` 取消。`ca_status` 报告活动项目；`ca_project` 只列出/激活已有项目
（创建/编辑/删除在 UI 中完成）。`prompt.js` 增加一段系统提示词
（status → search → detail/facts/source）。

## Client 页面

顶栏：项目选择器（激活 = 重配、重启、重新加载）+ 导航。Hash 路由：`#/search`、
`#/projects`（项目管理）、`#/fn/<symbol>/<tab>`、`#/graph`、`#/stats`、`#/callbacks`、
`#/tasks`、`#/backend`（安装/修复、探测与低层状态/配置）。视图：函数详情（源码、accordion、三栏、
图、CFG、AI 面板）、调用图、CFG。数据经 `/dsh-ca`；只用 `--dsw-*` token 与 `ca-`
前缀；不改宿主 DOM。

## 零破坏边界

- CA `src/`、CLI、网页端、MCP 均未改动；插件只调用既有只读 `/api/*` 并运行
  `ca serve`/`ca` CLI；不新增 CA 端点或 LLM 配置；只写自己的状态目录。
- `ca ui`/`ca mcp` 继续可用；每个插件实例一个 CA 后端进程（单 `--db` 或联邦
  `--db-dir`）。
- `client.js` 是构建产物：`node dsh/scripts/build-client.mjs`。

## 限制

- CA 后端仅 loopback 且无鉴权；远程只经 SSH 隧道；页面由 DSH 鉴权。
- DSH 内部服务（`webServer.register`、`requestRejection`、`slots`、`llm`、
  `agentDefaultModel`）是集成契约；宿主漂移是主要风险。
- 远程后端只支持 Linux；Windows 本地安装路径未真机验证，CA `watch` 在 Windows 不可用。
- 除上述路由外无 reports/文件浏览器页面；不写源码、不编译。
