# CA Web UI

`ca ui` 在 CA 索引之上提供一个**本地只读 Web UI**：单页浏览器界面加一组小型
JSON API。它复用 CLI 与 MCP 相同的 store 查询（单库经 `ca.store`，多 shard 经
`ca.store.federated.FederatedStore`），页面上的每个数字与对应 CLI 输出含义一致。

前端是纯 HTML/CSS/JavaScript：无 CDN、无外部字体、无 Node 构建步骤、无第三方运行时
依赖，除本服务外不发任何网络请求。HTTP 服务使用 Python 标准库（`http.server`）。

## 启动 UI

```sh
# 单个数据库（与 CLI 单库语义完全一致：没有 shard 列）
.venv/bin/ca ui --db ca.db

# 换端口或绑定地址（127.0.0.1 是默认值，也是安全的值）
.venv/bin/ca ui --db ca.db --port 9000
.venv/bin/ca ui --db ca.db --host 127.0.0.1

# 多个 shard 数据库（联邦查询，行里带 shard 列）
.venv/bin/ca ui --db-dir /home/yu/CA_OH/index
.venv/bin/ca ui --db oh_S1.db --db oh_S3.db

# console script 不在 PATH 时用模块形式
PYTHONPATH=src .venv/bin/python -m ca ui --db ca.db
```

服务在 stderr 打印实际 URL，例如
`[ui] CA read-only web UI listening on http://127.0.0.1:8766/`；在浏览器打开该地址，
按 `Ctrl-C` 停止（SIGINT/SIGTERM 优雅退出）。正常停止退出码为 0；数据库选择无效或
端口无法绑定时为 1。

| 选项 | 含义 |
|---|---|
| `--db PATH` | SQLite 数据库（可重复；多于一个即联邦模式） |
| `--db-dir DIR` | shard `*.db` 文件目录（联邦；不可读 shard 跳过） |
| `--host HOST` | 绑定地址（默认 `127.0.0.1`） |
| `--port PORT` | 绑定端口（默认 `8766`；`0` 表示由系统选空闲端口） |

## 页面

单页由搜索栏（结果浮层）和详情区组成；导航状态保存在 URL hash 中，因此可以收藏或
分享某个函数：

- `#/fn/<名字或 id>/<tab>` —— 函数页，并展开指定的手风琴分区（`overview`、
  `branches`、`returns`、`resources`、`findings`、`explain`）；省略 `/<tab>`
  表示所有分区折叠
- `#/stats` —— 数据库统计
- `#/callbacks` —— 跨 TU 回调注册事实

可以查看的内容：

- **符号搜索** —— 先精确命中 `id`/名字/qualified name，再子串匹配（store 的 FTS5
  trigram 索引；短模式回退 `LIKE`）。结果在搜索框下方的**浮层下拉**中展示（不再
  占用左侧结果栏），选中符号、清空查询、按 `Esc` 或点击外部时下拉关闭。结果展示
  定义/声明、`static`、源码位置，联邦模式下还有 shard。
- **函数页布局** —— 顶部是当前打开函数的**完整源码**（行号、只读、语法高亮、
  可折叠/滚动，带 `file:line`、shard 与函数行范围高亮），操作调用图时**固定不变**。
  下方左侧是**可折叠手风琴侧栏**，列出 `overview`、`branches`、`returns`、
  `resources`、`findings`、`explain` 并带计数徽章：点击标签会在**侧栏内
  就地展开**该分区内容（再次点击或点击其他标签收起），内容在侧栏内滚动，折叠状态记在
  `localStorage`。**调用图常驻中间列**（方向/深度/重建控件始终可用）。**右侧面板
  只显示所选调用图节点的源码**（未选中时显示提示），tab 内容与 CFG 不再显示在右侧。
- **跳转历史与返回** —— 双击调用图节点（或使用节点面板的“打开详情”）会把当前函数
  与 tab 记入会话级历史（去重、上限 50 条、持久化在 `sessionStorage`）。随后打开的
  函数页显示“← 返回上一个函数”按钮；该按钮、`Alt+Left` 与 `Alt+Backspace` 都会回到
  上一个函数并恢复其 tab，而普通搜索/首次打开不显示返回按钮。函数/统计/回调视图会
  通过 pushState 写入 hash 历史，因此浏览器原生 Back 可沿同一历史返回；hash 深链与
  `hashchange` 路由仍然有效。
- **概览** —— 签名、返回类型、file:line 范围、linkage、USR/id/TU id 与参数；用徽章
  区分 `deep facts` 与 `deep facts pending`（base/L0 数据库在运行 `ca ensure-deep`
  之前只有符号和调用事实）。
- **AI 解释** —— 对当前打开函数按需生成只读解释：用途、关键分支与错误路径、资源
  获取/释放、调用关系、findings 风险与阅读建议。打开该分区时懒加载，跟随 UI 语言，
  可刷新、可复制；provider 徽章默认显示“本地规则”，配置 LLM 后端时显示模型名
  （见 [AI 解释后端](#ai-解释后端)）。
- **调用图** —— SVG 遍历（方向 `both`/正向/反向，深度 1–6），直接边实线，候选回调
  边虚线并标注 kind 与 confidence，环为红色。**单击**节点（或按 Enter）会在
  **右侧面板**显示该函数的**完整源码**，不离开页面：行号、`file:line` 头部、
  所属 shard 徽章、独立滚动，并有“关闭/打开详情”按钮；若节点行落在顶部固定源码
  窗口内，顶部还会高亮并滚动到该行范围，否则顶部显示轻提示
  “节点位于 `<file>:<line>`”而不会错误高亮。**双击**节点（取消约 250ms 的单击
  延迟）会跳转到该函数详情页。关闭节点源码后右侧面板回到“点击节点查看源码”提示。
  窄屏时侧栏变整宽抽屉，调用图与节点源码面板上下堆叠。
- **分支** —— 展开该手风琴分区后，**在左侧侧栏内**用 `cfg_blocks`/`cfg_edges` 加
  分支条件生成 SVG 控制流图：入口/出口块高亮，true/false 边不同颜色并标注 `T`/`F`，
  回边虚线，可横向滚动与缩放（40–180%）。没有深析 CFG 的函数显示“深析未生成”
  回退提示，不再用表格。
- **返回 / 资源 / findings** —— CFG 与数据流分析提取的深析事实及位置。
- **统计** —— 表行数、数据库大小、schema/SQLite 版本、回调统计；联邦模式下还有
  每个 shard 的大小/schema/计数。
- **回调** —— 函数指针注册事实（object path、目标函数、注册类型、confidence、
  module）；可按 object path 前缀、字段或 module 过滤。
- 键盘：`/` 聚焦搜索框，`Esc` 清空。

两个代码区共用本地 C/C++ tokenizer（`highlight.js`）：关键字、类型、注释（含跨行
块注释）、字符串/字符字面量、数字与预处理行在客户端高亮，无 CDN、无外部请求。所有
内容渲染时做 HTML 转义；UI 绝不把数据库内容当 HTML 执行。

## 可调整分区与全屏铺满

页面铺满整个视口（没有固定 max-width 容器），外壳只保留 8px 间距，各内容区独立滚动。
函数页有三个可拖拽分隔条：

- **横向 splitter**（头部下方）——拖动调整顶部源码区高度（默认 `32vh`，最小
  140px）；双击恢复默认。
- **两条竖向 splitter**——调整左侧手风琴侧栏、中间调用图、右侧节点源码栏的宽度
  （默认左 `22%`、右 `26%`，中间自适应；最小宽度 220 / 420 / 280px）；双击恢复
  相邻列的默认宽度。
- 尺寸与折叠状态持久化在 `localStorage` 的 `ca.ui.layout`，刷新后保持；头部的
  **“重置布局”** 按钮恢复全部默认（并展开左右两栏）。
- 左右两栏可用头部按钮折叠，折叠后调用图中部占满；右栏折叠时选中调用图节点会自动
  展开。
- 分隔条带 `role="separator"` 与 `aria-orientation`/`aria-valuenow`，聚焦后用方向键
  调整（`↑`/`↓` 调整源码高度，`←`/`→` 调整左右栏宽）。
- 宽度小于 1100px 时禁用拖拽，三块区域改为上下堆叠（侧栏变为整宽抽屉），保证触屏
  可用。

## AI 解释后端

`GET /api/explain?symbol=<名字或 id>&lang=en|zh` 只读，响应形状固定：
`{symbol, function, provider, model, llm_configured, language, explanation,
facts_summary, cached, fallback_reason, elapsed_ms}`。

- **默认：本地规则解释。** 未配置 LLM 时不会发起任何网络请求，解释完全由索引事实
  生成，并在 UI 中标注 `provider: "heuristic"` / “本地规则”，不会伪装成 AI 生成。
- **可选 OpenAI 兼容 LLM。** 在启动 `ca ui` 前配置环境变量：

  | 变量 | 含义 |
  |---|---|
  | `CA_LLM_BASE_URL` | OpenAI 兼容 API 的 base URL，例如 `http://127.0.0.1:8000/v1`（服务会 POST 到 `<base>/chat/completions`） |
  | `CA_LLM_API_KEY` | Bearer token（本地服务可省略） |
  | `CA_LLM_MODEL` | 请求中使用的模型名 |
  | `CA_LLM_TIMEOUT` | 请求超时秒数（默认 30，上限 120） |

  提示词包含函数元信息、索引事实与最多 400 行源码，并要求模型用 UI 语言回答、不得编造
  事实。超时、HTTP 或解析错误时回退本地规则解释，仍返回 HTTP 200 并附带
  `fallback_reason`；服务启动时不会联网（配置在每个请求读取）。
- **隐私。** 配置 LLM 后，所选函数的源码与索引事实会发送到该服务；不设置 `CA_LLM_*`
  则全部保持在本地。该端点只读取索引与源码树，绝不写数据库。

## 语言

界面支持中英双语；数据本身不做翻译——函数名、文件路径、签名、代码片段与原始字段值
保持索引原样。

- 默认：跟随浏览器语言（`navigator.languages`）；中文浏览器显示中文，其余显示英文。
- 覆盖：页面 URL 加 `?lang=zh` 或 `?lang=en`。
- 切换：顶栏的`中文 / EN`控件即时更新页面（静态标签、tab、表格、图例、空态与错误
  提示），同时切换 `<html lang>`，并把选择写入 `localStorage` 的 `ca.ui.lang`，
  刷新后保持。JSON API 及其数据在两种语言下完全一致，只有浏览器界面被本地化。

## JSON API

所有端点返回统一信封，且只接受 `GET`：

```json
{"ok": true, "data": { }}
{"ok": false, "error": {"code": "NOT_FOUND", "message": "no function matching 'x'"}}
```

| 端点 | 参数 | 返回数据 |
|---|---|---|
| `GET /api/health`（别名 `/health`） | — | 状态、版本、uptime、数据库路径/shard/skipped、`db_ok`、表计数 |
| `GET /api/search` | `q`（必填）、`limit`（≤500）、`offset` | `{query, items, count, limit, offset, federated}` |
| `GET /api/function/<名字或 id>` | 路径段；或 `?symbol=` | `{function, deep_ready, shard?, branches, assignments, returns, resource_ops, findings}` |
| `GET /api/callers` | `symbol`（必填）、`limit`（≤2000）、`offset` | 合并后的直接 + 候选调用者边 |
| `GET /api/callees` | `symbol`（必填）、`limit`（≤2000）、`offset` | 合并后的直接 + 候选被调用者边 |
| `GET /api/graph` | `symbol`、`direction=both\|forward\|reverse`、`depth=0..12`、`limit` | 规范化图：`root, direction, depth, nodes[], edges[], cycles, truncated, stats, found` |
| `GET /api/stats` | — | 表计数、`db_bytes`、schema 版本、`callbacks`、联邦 `shards` |
| `GET /api/callbacks` | `path`（前缀）、`field`、`module`、`kind`、`limit`、`offset` | 注册事实（目标、evidence、confidence） |
| `GET /api/source` | `symbol`（必填）、`context`（0–10，默认 3） | 只读源码窗口：`{symbol, function{id,name,qualified_name,kind,file,line,end_line,signature}, file, language, start, end, total_lines, function_start, function_end, context, truncated, lines[{line,text}], shard, path, repo_root}` |
| `GET /api/cfg` | `symbol`（必填） | 只读控制流图：`{symbol, function{id,name,qualified_name,kind,file,line,end_line}, deep_ready, shard, federated, blocks[], edges[], branches[], truncated, stats}`；blocks 含 `index/kind/terminator/line/end_line/statement_count`、分支标注（`condition`、`branch_kind`、`true_block`、`false_block`）与 `is_entry`/`is_exit`；edges 含展示用 `label`（分支边为 `true`/`false`） |
| `GET /api/explain` | `symbol`（必填）、`lang=en\|zh` | 只读函数解释：`{symbol, function, provider, model, llm_configured, language, explanation, facts_summary, cached, fallback_reason, elapsed_ms}`（默认本地规则，可选配置 LLM） |
| `GET /api/branches`、`/api/assignments`、`/api/returns`、`/api/resources` | `symbol`（必填） | 按函数的事实行（附加的便捷端点） |

`/api/cfg` 通过常规详情查询解析函数，返回其 CFG 块/边以及解码后的分支行（联邦模式
合并所属 shard 的行并加 `shard` 字段）。该接口只读；未知符号返回 `404 NOT_FOUND`，
没有深析数据的函数返回空列表且 `deep_ready: false`。

`/api/source` 通过常规详情查询解析函数，从 `tus` 表读取所属翻译单元的
`repo_root`（联邦模式下使用包含该函数的 shard），再读取 `repo_root/file`。解析后的
真实路径必须位于该仓库根内：包含 `..`、绝对路径、指向根外的符号链接以及文件不存在
一律返回 `404 NOT_FOUND`。窗口最多 400 行，`context` 被限制在 `0–10`；该接口只读，
绝不写数据库。

示例：

```sh
curl -s 'http://127.0.0.1:8766/api/search?q=syscall&limit=5' | python3 -m json.tool
curl -s 'http://127.0.0.1:8766/api/function/syscall' | python3 -m json.tool
curl -s 'http://127.0.0.1:8766/api/graph?symbol=main&direction=both&depth=2' \
  | python3 -m json.tool
```

错误码：`INVALID_ARGUMENT`（400）、`NOT_FOUND`（404）、`READ_ONLY`（405，任何非 GET
方法）、`DB_ERROR`/`INTERNAL`（500）、`DB_UNAVAILABLE`（503，没有可读的数据库或
shard）。未知路径返回 `NOT_FOUND`。

## 只读保证与安全

- **UI 绝不写数据库。** 单库通过 `PRAGMA query_only=ON` 的连接打开，任何误写的 SQL
  都会被 SQLite 拒绝；联邦 shard 由 `FederatedStore` 以只读方式 attach。UI 有意不复用
  `ca.cli.dbsource.open_query_store` 的单库分支，因为该 helper 打开的是可写
  `SqliteStore`，其 `open()` 会执行 DDL 与 meta upsert。
- **不存在写端点。** 所有非 GET 方法返回 `405` 与
  `{"ok": false, "error": {"code": "READ_ONLY", ...}}`；HTTP 不做索引、
  `ensure-deep`、回调解析或 workspace 变更，这些请走 CLI/MCP。
- **默认只绑回环地址。** 服务绑定 `127.0.0.1` 且没有认证，不要把端口暴露到公网。
  查看远程索引时在源码机器上运行 CA 并转发端口，例如
  `ssh -N -L 8766:127.0.0.1:8766 user@source-host`，再在本机打开
  `http://127.0.0.1:8766/`。
- **静态文件是固定白名单**（`index.html`、`app.js`、`style.css`、`favicon.svg`）；
  请求路径不会直接拼到文件系统。响应带
  `Content-Security-Policy: default-src 'self'; script-src 'self'; ...`、
  `X-Content-Type-Options: nosniff` 与 `Cache-Control: no-store`。
- 每个请求打开一个短生命周期的只读连接，使多线程服务保持简单安全，同时允许其他进程
  （如 `ca watch`）刷新数据库。它是交互式排查工具，不是基准或高吞吐服务。
- 联邦 shard 可带 `-wal`/`-shm` sidecar 原地查询，也可用静态单文件副本（只读
  `immutable=1` 回退；可用但略慢）。不要把 UI 指向另一进程正在 checkpoint 的 shard。
- base/L0 索引只含符号与调用；深析列表为空且函数头部显示 `deep facts pending`。
  运行 `ca ensure-deep --db DB --repo REPO --compdb COMPDB --symbols NAME`（或用
  `--layer deep` 重新索引）补齐。

## 测试

UI 测试通过 `ca.store` 直接构造临时数据库（不需要 Clang/compile database），在线程内
以随机回环端口启动 server，断言 health/search/detail/graph/stats/callbacks/静态页、
只读保证与联邦 shard 来源：

```sh
.venv/bin/python -m pytest tests/test_ui.py -q
```

相关文档：CLI 见 [usage.zh.md](usage.zh.md)，Agent 接口见 [mcp.zh.md](mcp.zh.md)，
按需深析见 [layered-indexing.zh.md](layered-indexing.zh.md)，候选回调边见
[cross-tu-callbacks.zh.md](cross-tu-callbacks.zh.md)。
