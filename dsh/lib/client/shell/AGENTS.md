# AGENTS.md — 整页外壳

`createShell(React, {ui, api, i18n, layout}) -> {App, registerPage, navigate, getRoute}`：注册进 DSH `main` slot 的整页应用。

## 关键文件

- `index.js`：顶部栏（品牌/导航/variant/连接与后端状态/语言切换）、hash 路由（`#/search`、`#/fn/<symbol>/<tab>`、`#/graph`、`#/stats`、`#/callbacks`、`#/tasks`、`#/backend`）、页面注册表、初次加载与未配置/不可达状态引导（指向 `#/backend`）、状态轮询。

## 局部规则

- 页面通过 `registerPage(name, component)` 注册，路由名与 `KNOWN_PAGES` 保持一致；组件收 `{route, navigate}`。
- 统一经 `bag.api` 访问 Host；错误统一渲染，不吞异常。
- 用主题 token 与 `ca-` 类名；不操作宿主 DOM。
