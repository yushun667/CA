# DSH 插件（`@local/dsh-ca`）

CA 同时以 [DeepSeek Harness](https://github.com/deepseek-ai) bundle 形式提供：在
Harness Web UI 内嵌完整 CA 页面、给 agent 的 11 个原生 `ca_*` 工具、本地或远程
Linux `ca serve` 后端、长索引任务与 AI 解释。CA 本体不变：插件只调用 CA 既有的
只读 HTTP API 与 CLI，也不改变 CA 的测试基线（写作时为 497 passed / 4 skipped）。

## 安装（本地目录 bundle）

bundle 位于仓库根目录 `dsh/`，不发布到任何 registry。

1. 修改 `dsh/lib/client/` 后先重建 Harness 加载的客户端产物：
   `node dsh/scripts/build-client.mjs`（不要手改 `dsh/client.js`）。
2. 在 Harness plugin manager 选择 **install bundle from local directory**，指向
   `<repo>/dsh`。这是本地链接安装（不是打包副本），后续改动按下面的重载规则生效。
3. 只读核对安装：`node dsh/scripts/verify/verify-install.mjs`（`--json` 输出机器
   可读结果）。脚本检查 manifest/patch/client 产物/locale/icon 与 profile 登记；
   退出码 0 表示无 FAIL。
4. 在目标 profile 中启用该 bundle。**“已安装但未激活”不算成功**：只有插件处于
   激活状态，页面、工具与提示词才会出现。

重载规则：Host 改动（`dsh/lib/host/**`）需要重启 Harness（或禁用后重新启用插件），
因为入口模块会被缓存；Client 改动需要重建 `client.js` 并刷新页面（旧页面仍持有旧
bundle）；改 `dsh/lib/host/bootstrap.js` 必须完整重启 Harness。

## 配置

部署配置来自 Cordis patch 行（`dsh/cordis.patch.yml`）；在后端页面里做的修改以稀疏
用户 patch 保存在 `$DSH_HOME/plugin-data/dsh-ca/settings.json`（0600）。合并顺序：
默认值 ← patch 行 ← 用户 patch。不写 DSH profile。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `backend` | `auto` | `auto` 优先已配置的远程主机，否则本地 DB；`local`/`remote` 强制模式 |
| `autoStart` | `true` | 插件激活时自动启动后端 |
| `requestTimeoutMs` | `30000` | CA HTTP 请求超时 |
| `readyTimeoutMs` | `20000` | 等待 `ca serve` 就绪上限 |
| `local.repoRoot` | 自动 | CA 检出根目录（未设置时从 cwd 向上发现） |
| `local.python` | 自动 | 解释器；回退 `<repo>/.venv/bin/python`，再回退 `python3` |
| `local.db` / `local.dbDir` | — | 单 SQLite 索引 **或** 联邦 shard 目录（互斥） |
| `local.stateDb` | CA 默认 | `ca serve` 的可写 job 状态库 |
| `local.repo` / `local.compdb` | — | 深析工具的仓库根 / `compile_commands.json` |
| `local.host` / `local.port` | `127.0.0.1` / `0` | 绑定地址与端口；`0` = 系统分配 |
| `local.maxRestarts` | `3` | 崩溃重启次数上限 |
| `remote.ssh.host` / `user` / `port` / `keyPath` | — | SSH 目标（alias 或 `user@host`）、端口、本地私钥路径；不支持密码 |
| `remote.repoPath` | — | Linux 主机上已有的 CA checkout（优先） |
| `remote.installDir` | — | 没有 `repoPath` 时的 rsync 目标目录 |
| `remote.python` | 自动（`python3`） | 远端解释器 |
| `remote.db` / `remote.dbDir` | — | 远端单库或 shard 目录 |
| `remote.serveRemotePort` / `remote.localPort` | `0` / `0` | 远端 serve 端口与本地隧道端口（0 = 自动） |
| `local.env` / `remote.env` | `{}` | 子进程附加环境变量 |
| `log.*` | 有界 | 插件自身日志轮转与内存环形缓冲设置 |

远端路径必须是绝对路径或以 `~/` 开头。

以下可选覆盖项也从同一 config 对象读取：`maxToolOutputChars`（或
`tools.maxOutputChars`，4000–60000，默认 20000）、`maxConcurrentTasks`（或
`tasks.maxConcurrent`，1–8，默认 2）、`maxTaskLogBytes`（或 `tasks.maxLogBytes`，
64 KiB–16 MiB，默认 1 MiB）。

## 本地后端

1. 设置 `local.db`（联邦 shard 用 `local.dbDir`）；当 CA checkout 与 venv 可被发现
   时 `local.repoRoot`/`local.python` 可留空。
2. 打开 `#/backend` 启动后端。Host 运行
   `ca serve --host 127.0.0.1 --port 0 --db <db>`，从 stderr 解析就绪 URL 并校验
   `GET /api/health`；`--port 0` 无法解析时用本地临时空闲端口重试一次。
3. 启动前可用 **Probe local** 查看发现的解释器、CA 版本、候选数据库与 libclang 状态。

进程由 Host 持有：插件释放时停止，崩溃重启有次数上限，数据库缺失会返回可操作的错误。

## 远程 Linux 后端

适用于 DSH 在 macOS、源码与索引在 Linux 服务器的场景。

1. 确认基于密钥的 SSH 可非交互执行（`ssh -i <key> user@host true`）。
2. 在 `#/backend` 填写 SSH 字段，然后设置 `remote.repoPath`（服务器上已有 CA
   checkout）或 `remote.installDir`（插件把本仓库 rsync 到该目录，排除 `.git`、
   `.venv`、`.tmp`）。
3. 运行 **Probe remote**：一次 SSH 往返报告 OS、Python/pip、rsync、libclang、路径
   可写性与磁盘空间。
4. 运行 **Install**：`probe → source → rsync → venv → pip install -r requirements.txt
   → pip install -e . → ca version --json`。editable 安装是强制的：项目没有
   package-data，wheel 会丢 `schema.sql` 与 UI 静态资源。安装是幂等的，步骤/日志经
   `backend.installStatus` 轮询。
5. 启动：Host 在远端 loopback 上运行 `ca serve`，并打开本地
   `ssh -N -L <localPort>:127.0.0.1:<remotePort>` 隧道；健康检查与所有查询都走该
   隧道。停止时通过远端 PID 文件终止进程，因为只关本地 SSH 客户端并不能可靠停掉它。

远程长索引通过 `#/tasks` 或 `ca_task` 执行，它们经 SSH 在远端运行 `ca`。

## UI 页面

插件在 Harness `main` slot 注册整页（key/locale `ca`），并在侧栏
`sidebar.panellist` 注册入口。路由：

- `#/search` — 符号搜索 + 结果下拉（`Enter` 打开、`Esc` 关闭、`/` 聚焦）。默认落地页。
- `#/fn/<symbol>/<tab>` — 函数详情：带行号与语法高亮的完整源码；左侧 accordion
  含 overview、branches（内嵌 CFG）、returns、resources、findings；可拖拽三栏布局；
  调用图与选中节点源码。`Alt+Left` 在函数历史中返回。
- `#/graph` — 独立调用图。实线=直接调用、紫色虚线=回调候选、青色虚线=虚派发候选、
  红色=环；方向 forward/reverse/both，深度 0–12，节点上限。
- `#/stats` — 表行数、数据库大小、schema/SQLite 版本、回调统计与联邦 shard 信息。
- `#/callbacks` — 跨 TU 回调注册，支持 path/field/module/kind 过滤与分页。
- `#/tasks` — 长任务列表、启动、增量日志与取消。
- `#/backend` — 后端状态、启动/停止/重启、本地/远程探测、远程安装、可编辑配置与
  `/api/health` 检查。
- 函数页 → **Explain with AI** 面板，见下文。

## 原生工具

agent 可见 11 个工具；每个返回有界文本并响应工具调用取消信号。

| 工具 | 用途 |
|---|---|
| `ca_status` | 后端状态、健康、数据库路径、索引计数、任务摘要 |
| `ca_search` | 解析函数名/id/限定名/子串（分页） |
| `ca_symbol` | 单个函数/类型记录与深析事实计数、关键条件 |
| `ca_graph` | 符号的调用者/被调用者/双向图，候选边有标记 |
| `ca_facts` | 单一事实类型：branches、assignments、returns、resources 或 findings |
| `ca_source` | 已记录的源码窗口（函数 ± 上下文，CA 上限 400 行） |
| `ca_stats` | 索引级统计与联邦 shard |
| `ca_callbacks` | 跨 TU 回调注册（带过滤） |
| `ca_registry` | 已注册 workspace/project/variant；内存中切换 active variant |
| `ca_explain` | 基于有界 CA 证据的 AI 解释（DSH 模型，见下文） |
| `ca_task` | 启动/列表/状态/日志/取消 CA CLI 长任务 |

`ca_task` 启动后立即返回；用返回的 `nextOffset` 轮询 `action=logs`，不要阻塞等待。
系统提示词会告诉模型这个顺序（status → search → detail/facts/source）。

## 长任务

任务类型：`index`、`update`、`ensure_deep`、`prewarm`、`resolve_callbacks`、
`watch`。任务以 CA CLI 子进程执行（本地或经 SSH），最多同时两个；状态机为
`queued → running → succeeded|failed|cancelled`，每个任务保留有界环形日志（约
1 MiB）并支持 offset 分页。用 `#/tasks` 或 `ca_task` 操作；取消会终止 CLI 进程。
全量索引可能耗时数分钟到数小时。

## AI 解释

函数页的 **Explain with AI** 按钮与 `ca_explain` 工具使用 DSH 会话的默认模型
（`agentDefaultModel` 选择 + `llm` 服务）——插件不保存任何 LLM 配置。调用模型前，
插件先为单个符号收集有界只读 CA 证据（签名、源码窗口、callers/callees、branches、
resources、findings），并要求模型只依据这些证据、按请求语言（en/zh）、以 Markdown
作答。结果展示模型与证据数量；渲染器转义全部模型输出，从不注入原始 HTML。会话没有
默认模型时该功能报 `EXPLAIN_UNAVAILABLE`，插件其余部分照常工作。

## 故障排查

- **后端未配置** — 设置 `local.db`/`local.dbDir`（或远程 SSH + 数据库字段）后启动；
  `ca_status` 会给出可操作的提示。
- **数据库/仓库未找到** — 运行 **Probe local** 使用报告中的路径，或显式设置
  `local.repoRoot`/`local.python`；先为仓库建立索引。
- **远程安装失败** — 查看失败步骤
  （`probe/source/rsync/venv/dependencies/editable/verify`）与日志；检查 rsync、
  Python/venv 与磁盘。不要用 wheel 替换 editable 安装。
- **隧道或健康检查失败** — 确认远端 `ca serve` 监听 loopback 且 SSH key 允许 `-L`
  转发；重启时残留的远端进程会按其 PID 文件清理。
- **页面或工具缺失** — 重跑核验脚本并重新启用 bundle；Host 改动后重启 Harness，
  Client 改动后重建 `client.js` 并刷新页面。
- **AI 解释不可用** — 在 Harness 中选择默认模型，或改用
  `ca_symbol`/`ca_facts`/`ca_source` 手工分析。

## 安全边界

- CA 后端只绑定 loopback 且无认证（与 [ca ui](ui.zh.md)、[ca mcp](mcp.zh.md) 相同
  信任模型）。不要把它的端口暴露到公网。
- DSH 页面与 `/dsh-ca` 路由由 Harness 页面会话鉴权；路由还会调用
  `connection.requestRejection` 并拒绝外部来源（401/403）。
- 远程访问只经 SSH 隧道。仓库与设置中不保存任何凭据；`remote.ssh.keyPath` 是本地
  私钥路径，且有意不支持密码。
- 插件只读取 CA 数据，只写自己的设置、日志与任务状态；不写 DSH profile，也不写
  CA 源码/索引。

## 非目标

- 不替代 `ca` CLI、[ca ui](ui.zh.md)、[ca mcp](mcp.zh.md) 或 MCP；它们保持原样，
  可与插件并行使用。
- 不编辑源码、不编译、不写索引：CA 描述既有索引。
- 除上述路由外不提供 reports 或文件浏览器页面。
- 不引入 CA 侧 LLM 配置，也不新增 CA explain 端点。
- 不发布为软件包；安装方式是本地目录链接。
