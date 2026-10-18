# AGENTS.md — Client 半

DSH Web 页面的 CA 插件：ESM 源码在 `lib/client/`，由 [../../scripts/build-client.mjs](../../scripts/build-client.mjs) 打成 `client.js`（`window.__ModuleLoader__` 格式），**禁止手改产物**。

## 关键文件

- `index.js`：Lead 维护的 Client 入口，inject `['slots','locale']`，注册 `main` 整页与 `sidebar.panellist` 侧栏项。
- `selftest.js`：页面内真实 DOM 自测（由 Host 的 `selftest-pending.json` 触发，结果回报 `selftest-report.json`）。
- `api.js`：所有 Host 调用的同源 `POST /dsh-ca/<endpoint>` 封装（`credentials:'same-origin'`）。

## 子目录

- [ui/AGENTS.md](ui/AGENTS.md)：主题 token 化的 React 原语与样式注入。
- [shell/AGENTS.md](shell/AGENTS.md)：整页外壳、hash 路由与页面注册表。
- [i18n/AGENTS.md](i18n/AGENTS.md)：`ca` 命名空间 en/zh 字典与 `t()`。
- [pages/AGENTS.md](pages/AGENTS.md)：独立路由页面。
- [views/AGENTS.md](views/AGENTS.md)：函数详情、调用图、CFG 与 AI 解释视图。

## 局部规则

- 只 import 相对路径与 `react`；无 JSX、无 CSS import、无动态 import、无顶层 await。
- 只用 `--dsw-*` 主题 token 与 `ca-` 类名前缀，不操作宿主 DOM/body。
- 改源码后运行 `node scripts/build-client.mjs` 重建 `client.js`；浏览器需刷新页面才会加载新产物。
