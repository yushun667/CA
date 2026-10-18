# DSH 插件（`@local/dsh-ca`）

CA 同时以 DeepSeek Harness bundle 形式提供：在 Harness Web UI 内嵌完整 CA 页面、给
agent 的 12 个原生 `ca_*` 工具、自安装的本地或远程 Linux `ca serve` 后端、长索引任务
与 AI 解释。CA 本体不变：插件只调用 CA 既有的
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

这里安装的是**插件**本身；CA 分析器**后端**由插件的后端安装器单独安装（见下文）。

## 配置

部署配置来自 Cordis patch 行（`dsh/cordis.patch.yml`）；在后端页面里做的修改以稀疏
用户 patch 保存在 `$DSH_HOME/plugin-data/dsh-ca/settings.json`（0600）。合并顺序：
默认值 ← patch 行 ← 用户 patch。不写 DSH profile。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `backend` | `auto` | `auto` 优先已配置的远程主机，否则本地 DB；`local`/`remote` 强制模式 |
| `autoStart` | `true` | 插件激活时自动启动后端 |
| `projects` / `activeProjectId` | `[]` / `null` | 已保存的项目（见上）；active 项目决定后端目标 |
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
| `remote.repoPath` | — | Linux 主机上已有的 CA checkout（source 模式/手工安装） |
| `remote.installDir` | — | rsync 目标与 legacy source 模式的安装目录；wheel 安装使用安装表单里的独立目录 |
| `remote.python` | 自动（`python3`） | 远端解释器 |
| `remote.db` / `remote.dbDir` | — | 远端单库或 shard 目录 |
| `remote.serveRemotePort` / `remote.localPort` | `0` / `0` | 远端 serve 端口与本地隧道端口（0 = 自动） |
| `local.env` / `remote.env` | `{}` | 子进程附加环境变量 |
| `log.*` | 有界 | 插件自身日志轮转与内存环形缓冲设置 |

后端安装器在各次 `#/backend` 请求中配置（`mode: wheel|source`、`offline`、
`wheelhousePath`、`installDir`、`python`）；最近一次成功安装记录在
`$DSH_HOME/plugin-data/dsh-ca/install-state.json`（0600），并通过
`backend.get.install` 暴露。

### 项目（源码根 + 数据库 + 后端位置）

CA 是通用分析器，因此插件围绕**项目**组织，与在 DSH 中打开项目一致。一个项目把三件
始终成对的东西绑定在一起：

| 字段 | 含义 |
|---|---|
| `name` | 唯一、用户起的名字 |
| `sourceRoot` | 建索引所用源码树（后端主机上的绝对路径） |
| `db` **或** `dbDir` | 单个 SQLite 索引，或联邦 shard 目录 |
| `backend` + `ssh` | 本地运行，或运行在远程 Linux 主机（SSH 目标） |
| `compdb`、`python`、`caRepoPath` | 可选：compile database 与 CA/venv 位置 |

项目在 `#/projects` 页面**手动**创建、编辑、删除并持久化到用户 patch；不从扫描到的
目录推断任何东西。顶栏始终显示当前项目；切换项目会调用 `projects.activate`，重新配置
后端、重启 `ca serve`（及 SSH 隧道）并重新加载页面；切换失败回滚到之前的配置。从旧的
单目标配置升级时，插件会把它迁移成一个以数据库命名的默认项目；请检查其 `sourceRoot`，
若源码树在别处就编辑它。

远端路径必须是绝对路径或以 `~/` 开头。

以下可选覆盖项也从同一 config 对象读取：`maxToolOutputChars`（或
`tools.maxOutputChars`，4000–60000，默认 20000）、`maxConcurrentTasks`（或
`tasks.maxConcurrent`，1–8，默认 2）、`maxTaskLogBytes`（或 `tasks.maxLogBytes`，
64 KiB–16 MiB，默认 1 MiB）。

## 安装 CA 后端（本地或远程）

CA 是纯 Python，**不需要编译**；运行依赖（`clang`/`libclang`、`duckdb`、`pytest`
及其传递依赖）都是预编译 wheel。插件用内置的 CA wheel
（`dsh/backend/wheels/ca_analyzer-<version>-py3-none-any.whl`，约 360 KB）为
你安装后端；离线机器再由按平台生成的 wheelhouse 提供依赖 wheel。

后端主机要求：**Python >= 3.11** 且带 `venv`/`pip`（Windows 用
`py -3.13`/`py -3.12`/`py -3.11`；macOS/Linux 自动探测
`python3.13`/`3.12`/`3.11`）；远程后端需要可用的 SSH（推荐 `scp`，legacy
source 模式还需要 `rsync`）。在线安装需要网络；离线安装需要目标平台的 wheelhouse。

### 安装 / 修复（后端页）

打开 `#/backend`；两个安装器都是幂等的——已有可用 venv 会复用，
**Install / repair** 会修复或重建损坏的 venv，步骤/日志经
`backend.installStatus` 轮询。

- **本地后端安装**（Windows/macOS/Linux）：可选安装目录（默认插件的私有
  `backends/local`）与 Python 解释器，然后选择 **Online (PyPI)** 或
  **Offline (wheelhouse)**。步骤：environment → wheel → venv →
  dependencies → verify（`ca version --json`）。
- **远程安装**（Linux over SSH）：选择安装方式 **wheel（推荐）** 或
  **source tree (legacy)**，以及 Online/Offline。wheel 步骤：probe →
  Python check → 上传 CA wheel →（离线）上传 wheelhouse → 远端 venv →
  pip install → verify。默认远端安装目录 `~/dsh-ca/backend`；上传优先用
  `scp`，否则通过 SSH 流式传输（wheelhouse 目录以 tar 流上传）。随后远端
  `ca serve` 在本地 SSH 隧道后面运行。

安装器把最近一次成功安装记录在
`$DSH_HOME/plugin-data/dsh-ca/install-state.json`（0600），并通过
`backend.get.install` 报告平台、venv Python、CA 版本与安装目录；安装完成后创建或
激活指向该安装的 project。

### 离线安装（隔离网络主机）

在有网络的机器上构建一次依赖 wheelhouse：

```sh
node dsh/scripts/build-wheelhouse.mjs --platform <id> [--tar]
node dsh/scripts/build-wheelhouse.mjs --list-platforms
```

平台 id：`linux-x64`、`linux-arm64`、`macos-arm64`、`macos-x64`、
`windows-x64`。输出到 `dsh/artifacts/wheelhouse/<platform>/`（依赖 wheel +
CA wheel 副本 + `manifest.json`）；`--tar` 还会写出 `<platform>.tar.gz`。体积：
`macos-arm64` 约 41 MB，`linux-x64` 约 46 MB。

本地离线安装：把目录复制到插件状态目录默认位置
`$DSH_HOME/plugin-data/dsh-ca/wheelhouse/<platform>/`，或在表单里指向该目录；
`.tar.gz` 必须先解压，因为 `pip --find-links` 需要目录。远程离线安装：在表单里
选择本地 wheelhouse 目录，安装器会上传到 Linux 主机。离线 pip 使用
`--no-index --find-links`，不访问网络。

实测：macOS arm64 本地离线安装约 **1.4 s**；真实 Linux 服务器离线 wheel 安装
约 **6.4 s**（包含 46 MB wheelhouse 的上传）。

### 平台支持

| 主机 | 本地后端 | 远程后端 | 说明 |
|---|---|---|---|
| macOS arm64 / x64 | 代码路径支持；arm64 已真机验证 | 目标必须是 Linux | 主要开发主机 |
| Linux x64 / aarch64 | 代码路径支持 | 已在真实 Linux 服务器验证 | 创建 venv 需要 `pythonX.Y-venv` |
| Windows x64 | 代码路径已实现、未真机验证 | 目标必须是 Linux | 用 `py -3.11+`；CA `watch` 在 Windows 不可用 |

依赖 wheel 决定平台矩阵；远程后端只支持 Linux。

### 开发者回退（source 模式）

旧流程仍可用于开发：project 指向已有的 CA checkout/venv/索引，或手工创建 venv
并 editable 安装
（`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .`；
`scripts/bootstrap.sh` 在 macOS/Linux 做同样的事）。远程安装方式选择
**source tree (legacy)** 会保留 rsync + editable 流程（两端都需要 `rsync`）。
新部署优先用 wheel 安装器；source 模式仅为兼容与调试保留。

## 本地后端

1. 如果本机还没有 CA，先在 `#/backend` 运行 **Install / repair local backend**
   （见上）；它创建私有 venv，不触碰系统 Python。
2. 在 `#/projects` 新建 project：`backend: local`、源码根与本地数据库（单库或
   shard 目录）。后端页安装卡可用 **Create a local project with this Python**
   为新 project 预填解释器。
3. 激活 project。Host 把它映射为后端配置并运行
   `ca serve --host 127.0.0.1 --port 0 --db <db>`，从 stderr 解析就绪 URL 并校验
   `GET /api/health`；`--port 0` 无法解析时用本地临时空闲端口重试一次。
4. 启动前可用 **Probe local** 查看发现的解释器、CA 版本、CA checkout 与 libclang
   状态。

进程由 Host 持有：插件释放时停止，崩溃重启有次数上限，数据库缺失会返回可操作的错误。

## 远程 Linux 后端

适用于 DSH 在 macOS/Windows、源码与索引在 Linux 服务器的场景。

1. 确认基于密钥的 SSH 可非交互执行（`ssh -i <key> user@host true`）。
2. 在 `#/backend` 填写 SSH 字段（host/user/port/key path），用
   **Install on remote host**、方式 **wheel（推荐）**，并选择 **Online (PyPI)** 或
   **Offline (wheelhouse)**。安装器探测主机、校验 Python >= 3.11、上传内置 CA
   wheel（离线时再上传 wheelhouse）、创建 `~/dsh-ca/backend/venv` 并安装 CA，
   最后用 `ca version --json` 校验。安装目录可在表单覆盖；不需要 rsync 或源码
   checkout。
3. 在 `#/projects` 新建 project：`backend: remote`、服务器上的源码根与数据库
   （或 shard 目录）路径、同一 SSH 目标。激活它：Host 配置后端并在远端 loopback
   上运行 `ca serve`，同时打开本地
   `ssh -N -L <localPort>:127.0.0.1:<remotePort>` 隧道；健康检查与所有查询都走该
   隧道。停止时通过远端 PID 文件终止进程，因为只关本地 SSH 客户端并不能可靠停掉它。
4. 安装/启动前可运行 **Probe remote**：报告 OS、Python/pip、libclang、路径可写性
   与磁盘。**source tree (legacy)** 方式保留旧的 rsync + editable 流程（两端都需要
   `rsync`）；优先用 wheel 模式。

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
- `#/projects` — 创建/编辑/删除/激活项目（名称、后端、源码根、`db`/`dbDir`、
  compile database、SSH 目标）。管理索引的主要入口。
- `#/backend` — 后端状态、启动/停止/重启、本地/远程探测、本地 **Install /
  repair** 与远程 **Install on remote host**（wheel 在线/离线）、底层可编辑配置与
  `/api/health` 检查。
- 函数页 → **Explain with AI** 面板，见下文。

## 原生工具

agent 可见 12 个工具；每个返回有界文本并响应工具调用取消信号。

| 工具 | 用途 |
|---|---|
| `ca_status` | 后端状态、健康、当前项目、数据库路径、索引计数、任务摘要 |
| `ca_project` | 项目 list/use/status；`use` 激活已存在项目（创建与编辑仍在 UI） |
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

- **后端未配置** — 先按上文安装后端，再在 `#/projects` 创建并激活 project；
  `ca_status` 会给出可操作的提示。
- **数据库/仓库未找到** — 在 `#/backend` 运行 **Probe local**，把报告中的路径填进
  project 表单或显式设置解释器；先为仓库建立索引。
- **安装失败** — 在 `backend.installStatus` 查看失败步骤。wheel 步骤为
  `probe/python/upload-wheel/upload-wheelhouse/venv/pip/verify`（本地为
  `environment/wheel/venv/dependencies/verify`）；检查 SSH/scp、Python >= 3.11 +
  venv、磁盘空间；离线 wheelhouse 必须与目标平台匹配。source 模式还需要 `rsync`。
- **`UNSUPPORTED_PYTHON`** — 安装 Python 3.11+（Debian/Ubuntu 装
  `python3.11-venv`，Windows 用 `py -3.11`），或在安装表单/`remote.python` 显式
  指定解释器；Ubuntu 22.04 默认 Python 3.10。
- **离线安装找不到 wheel** — wheelhouse 必须是目录（`.tar.gz` 先解压）、用
  `--platform` 按目标平台生成，且包含 CA wheel 与全部依赖 wheel。
- **隧道或健康检查失败** — 确认远端 `ca serve` 监听 loopback 且 SSH key 允许 `-L`
  转发；重启时残留的远端进程会按其 PID 文件清理。
- **页面或工具缺失** — 重跑核验脚本并重新启用 bundle；Host 改动后重启 Harness，
  Client 改动后重建 `client.js` 并刷新页面。
- **AI 解释不可用** — 在 Harness 中选择默认模型，或改用
  `ca_symbol`/`ca_facts`/`ca_source` 手工分析。
- **未选择项目 / 迁移项目指向 CA 检出目录** — 打开 `#/projects`，把 `sourceRoot`
  改成真实源码树并选择 `db`/`dbDir`，然后激活；在你修改前迁移项目仍按旧目标工作。

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
