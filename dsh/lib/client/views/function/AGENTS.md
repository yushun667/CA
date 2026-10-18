# AGENTS.md — 函数详情页

`createFunctionPage(React, bag)`：路由 `#/fn/<symbol>/<tab>` 的整页视图，移植 CA Web UI 的函数页体验。数据来自已鉴权的 `/api/*` 透传。

## 关键文件

- `index.js`：页面组合（源码顶栏 + 左 accordion + 中调用图 + 右节点源码，可拖拽三栏）。
- `source.js`、`highlight.js`：源码视图与本地 C/C++ tokenizer；只产出 `{text, cls}` token，React 转义渲染，禁止 HTML 注入。
- `accordion.js`、`data.js`、`i18n-keys.js`：overview/branches/returns/resources/findings 分区、数据助手与默认英文 key。
- `layout.js`、`splitter.js`、`history.js`：布局持久化（`ca.ui.layout`）、可访问分隔条、会话跳转历史（`ca.ui.fnHistory`）。
- `styles.js`、`ui-kit.js`：`ca-fn-` 样式与 `bag.ui` 原语回退。

## 局部规则

- 组合 `views/graph`、`views/cfg`、`views/explain` 组件，不重复实现它们。
- 窄屏堆叠；Alt+Left / Alt+Backspace 返回上一函数。
- 新增 i18n key 同步 `i18n-keys.js` 默认值与 ui-shell 字典。
