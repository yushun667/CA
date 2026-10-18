# dsh/ 工程契约（Lead 维护）

DSH 插件包 `@local/dsh-ca` 的模块边界与接口。改接口先改本文件并通知 Lead。

## 包与安装

- 包根 `dsh/`：Host 入口 `lib/host/bootstrap.js`（稳定的小入口，静态 `inject`），实现体 `lib/host/impl.js` 经 cache-busted 动态 import 加载（编辑 impl 或其后模块后重激活即可生效；改 bootstrap/inject 需重启宿主）；Client 产物 `client.js` 由 `scripts/build-client.mjs` 从 `lib/client/index.js` 生成（勿手改）。
- Host 行在 `cordis.patch.yml`；Client 由 `dsh.client` 字段 + `exports["./client"]` 自动挂载。
- 安装只由 Lead 执行：`plugin_manager install_bundle`，target=`/Users/yu/Documents/hobby/CA/dsh`。成员不得安装/改 profile/Git 提交。
- Host 代码只用 Node 内置模块；Client 源码只 import 相对路径与 `react`。禁止 npm 依赖、CSS import、动态 import、顶层 await。

## Host 组装（Lead 维护 lib/host/bootstrap.js → impl.js）

```js
const backend = createBackendManager(ctx, config)
const tasks = createTaskManager(ctx, { backend, config })
const explain = createExplainService(ctx, { backend, config })
registerRpc(ctx, { backend, tasks, explain, config })
registerTools(ctx, { backend, tasks, explain, config })
registerPrompt(ctx, config)
```

`inject` 至少 `['tools','connection','webServer']`（`webServer` 用于挂 `/dsh-ca` 路由，`connection` 用于 `requestRejection` 同源鉴权；`ctx.connection.rpc.handle` 不可用）；`llm`/`agentDefaultModel`/`systemPrompt` 可选（`ctx.get`/`ctx.inject`）。所有注册包 `ctx.effect(...)` 并返回 disposer；异步初始化不得阻塞 `apply`。

## BackendManager（backend-ops）

`createBackendManager(ctx, config, deps?) -> manager`

- `getStatus() -> {mode:'unconfigured'|'local'|'remote', state:'stopped'|'starting'|'running'|'error', baseUrl, db, error, since, version?, features?}`
- `start(opts?)/stop()/restart()`；`request(path,{method='GET',query,body,timeoutMs=30000}) -> CA envelope {ok,data|error}`（传输错误抛 `BackendError`）；`execCa(spec,{onStdout,onStderr,signal}) -> {exitCode,signal}`
- `probeLocal()`、`onChange(fn)`、`dispose()`；`config` 解析后的设置。`BackendError{code,message,details}`。
- `listDatabases(options?) / useDatabase({kind,path})`：低层数据库扫描/切换（内部辅助，不是产品主流程）。
- 本地：`ca serve --db/--db-dir --port 0 ...`，就绪判定 = stderr URL 行 + `/api/health`。远程：probe → rsync 源码 + venv + `pip install -e .` → 远端 `ca serve` + SSH 隧道；`execCa` 经 ssh。设置持久化 `$DSH_HOME/plugin-data/dsh-ca/settings.json`。

## Project model（backend-ops；产品主模型）

一个**项目** = 一份源码 + 一个数据库（单库或分片目录）+ 一个后端位置，由用户手动创建/编辑，通用工具不绑定任何特定库。

`project = { id, name, backend:'local'|'remote', sourceRoot, db:string|null, dbDir:string|null, compdb:string|null, ssh:{host,user?,port?,keyPath?}|null, python?:string|null, caRepoPath?:string|null }`；`db`/`dbDir` 二选一，`sourceRoot` 必填，name 唯一（忽略大小写）。

Manager 方法：`listProjects()`、`saveProject(input)`、`deleteProject(id)`、`activateProject(id)`（映射为 backend 配置 → configure → restart → status，失败回滚）、`getActiveProject()`。持久化在 settings.json 的 `projects[]` + `activeProjectId`；旧配置首次升级时迁移为默认项目。`--db-dir` 只用于该项目的分片目录；不要从扫描结果推断项目。

## TaskManager（host-tools）

`createTaskManager(ctx,{backend,config}) -> {start(kind,params),list(),get(id),logs(id,{offset,limit}),cancel(id),onChange,dispose}`

- `kind ∈ index|update|ensure_deep|prewarm|resolve_callbacks`；经 `backend.execCa` 执行 CA CLI；stdout/stderr 环形日志（每任务约 1MB）；状态 `queued|running|succeeded|failed|cancelled`；并发 ≤2。
- `Task: {id,kind,status,params,createdAt,startedAt,finishedAt,exitCode,error,logBytes}`。

## ExplainService（ai-explain）

`createExplainService(ctx,{backend,config}) -> {async explain({symbol,language='en',question,signal}) -> {symbol,text,model,evidenceCount,usage?}}`

- `ctx.agentDefaultModel.currentSelection()` 取 provider/model，`ctx.llm.stream(...)` 聚合 text-delta；先经 `backend.request` 收 detail/source/callers/callees/graph(±cfg) 且有界；错误归一化。

## Client↔Host 通道（host-tools；**HTTP web 路由**，非 connection.rpc）

- 结论：`ctx.connection.rpc.handle` 在 plain-JS 插件中会激活失败（`cannot get property "webServer" without inject`），**不要使用**。改用 DSH 原生 `ctx.webServer.register` 注册同源 HTTP 路由。
- Host：`registerRpc(ctx,{backend,tasks,explain,config})` 注册 `ctx.webServer.register({kind:'prefix',path:'/dsh-ca',handler})`。
  - 请求：`POST /dsh-ca/<endpoint>`，body 为 JSON payload（≤1 MiB）。
  - 鉴权：先 `const rejection = ctx.connection.requestRejection(req)`，非空则回 401/403；非 POST 回 405；路径不匹配 404。
  - 响应：`content-type: application/json`，`{ok:true,value}` / `{ok:false,error:{code,message,details}}`；业务错误 HTTP 200，参数错误 400，body 过大 413。
- Client：`fetch('/dsh-ca/<endpoint>', { method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(payload||{}), signal, credentials:'same-origin' })`，解包同一 envelope。
- 端点：`ping`；`status`（含 `project` 字段）；`ca.request{method,path,query,body}`（仅 `/api/` 前缀）；`backend.get|configure|start|stop|restart|probe|install`；`projects.list|save|remove|activate`；`tasks.start|list|get|logs|cancel`；`explain`。
  - `projects.save` 仅创建/更新项目定义；`projects.activate` 切换活动项目并重启后端（失败回滚）。
  - `backend.databases|useDatabase` 保留为低层辅助，不作为产品主流程。
- Handler 不得抛未捕获异常；参数与大小校验。

## Tools（host-tools；plain JSON Schema，不 import defineTool/schemastery）

`registerTools(ctx,{backend,tasks,explain,config})`；固定工具名：
`ca_status ca_project ca_search ca_symbol ca_graph ca_facts ca_source ca_stats ca_callbacks ca_registry ca_explain ca_task`

- 每个工具返回 `{text:string}`，`output.render` → `[{type:'text',text}]`；参数自行校验；长输出有界（约 20k 字符）并提示分页。
- `ca_project` action=list|use|status：项目=源码+数据库+后端位置；`use` 只能激活已存在项目（创建/编辑是 UI 用户权限）。
- `ca_graph` 覆盖 callers/callees（direction/depth/limit）；`ca_facts` 用 kind 选 branches/assignments/returns/resources/findings；`ca_task` action=start|list|status|logs|cancel；其余映射 CA `/api/*`。

## Client（Lead 维护 lib/client/index.js 与打包器）

- `window.__ModuleLoader__.load({ id:'@local/dsh-ca', factory(require){ const React=require('react'); return {inject:[...], apply(ctx){...}} } })`。
- 整页：`ctx.slots.register({name:'main', key:'ca', locale:'ca'}, App)`；侧栏：`{name:'sidebar.panellist', id:'ca', order:20, label:()=>t('panel'), locale:'ca'}`；点击切换由宿主完成。
- 模块工厂签名：
  - `createUI(React, ctx) -> {Button, IconButton, Input, Select, TextArea, Card, Table, Tabs, Badge, Spinner, ErrorBox, EmptyState, CodeView, Splitter, ...}`（样式在 ctx.effect 内注入并清理）
  - `createApi(ctx) -> {rpc(endpoint,payload,signal), caRequest({method,path,query,body,signal}), ...}`
  - `createI18n(ctx) -> {t(key,vars), lang, subscribe(fn), setLang(lang)}`；locale NS=`ca`，语言 en/zh
  - `createShell(React,{ui,api,i18n,layout}) -> {App, registerPage(name,component), navigate(route), getRoute()}`
  - 页面/视图工厂：`createXPage(React,{ui,api,i18n}) -> Component`；组件 props `{route, navigate}`。
- 视图组合（Lead 入口 `lib/client/index.js` 组装）：
  - `views/function/index.js` 的 `createFunctionPage` 直接相对 import `../graph/index.js`、`../cfg/index.js`、`../explain/index.js`；打包时这些文件必须存在。
  - CallGraph props：`{symbol, direction, depth, limit, onSelectNode, onOpenDetail}`；CfgGraph props：`{symbol}`；ExplainPanel props：`{symbol, language?, onNavigate?}`。
  - `pages/graph.js|stats.js|callbacks.js` 的 `createGraphPage/createStatsPage/createCallbacksPage` 组件 props `{route, navigate}`。
- 样式只用 `--dsw-alias-*`/`--dsw-radius-*` 等主题 token；类名 `ca-` 前缀；不 import Harness 包；不操作 body/宿主 DOM。

## 文件所有权

| 路径 | 所有 |
|---|---|
| package.json, cordis.patch.yml, lib/host/index.js, lib/client/index.js, client.js, scripts/build-client.mjs, locale/, icon.svg, AGENTS.md, CONTRACT.md | Lead |
| lib/host/config.js, lib/host/backend/**, test/backend/** | backend-ops |
| lib/host/rpc/**, lib/host/tools/**, lib/host/tasks/**, lib/host/prompt.js, test/rpc/**, test/tools/** | host-tools |
| lib/host/explain/**, lib/client/views/explain/**, test/explain/** | ai-explain |
| lib/client/ui/**, shell/**, api.js, i18n/**, pages/{search,backend,tasks}.js | ui-shell |
| lib/client/views/function/** | ui-detail |
| lib/client/views/{graph,cfg}/**, pages/{graph,stats,callbacks}.js | ui-graph |
| test/, scripts/verify/ | qa-verify |
