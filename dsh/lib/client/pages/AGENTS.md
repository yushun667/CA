# AGENTS.md — 路由页面

独立路由页面工厂 `createXPage(React, {ui, api, i18n}) -> Component`，组件 props `{route, navigate}`。

## 文件

- `search.js`：`#/search` 符号搜索，下拉结果 + 键盘导航，选中打开函数页。
- `projects.js`：`#/projects` 极简建项目：本机选目录 / 远程登录后浏览目录；自动 db 路径、自动安装后端与启动（createSimple+prepare），旧字段收在「高级」。
- `backend.js`：`#/backend` 后端状态/安装修复（本地/远程、在线/离线）；原始配置与探测收进「高级配置」折叠。
- `tasks.js`：`#/tasks` 长任务列表/启动/日志增量轮询/取消。
- `graph.js`：`#/graph` 独立调用图页。
- `stats.js`：`#/stats` 表行数/DB 大小/schema/回调统计/联邦 shard。
- `callbacks.js`：`#/callbacks` 回调注册过滤与分页。

## 局部规则

- 页面只组合 `bag.ui` 与 `bag.api`，取数与错误归一在 `api.js`；安装与长任务不得阻塞等待，改用轮询。
- 新增页面同步 shell 的 `registerPage`/`KNOWN_PAGES`（Lead 入口）与 `lib/client/api.js` 端点。
- 只读展示；唯一写入口是后端配置、任务启动/取消。
