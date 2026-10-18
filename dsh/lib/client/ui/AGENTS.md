# AGENTS.md — UI 原语

`createUI(React, ctx?)` 返回 `Button/IconButton/Input/Select/TextArea/Card/Table/Tabs/Badge/Spinner/ErrorBox/EmptyState/CodeView/Splitter` 等原语与样式桥。

## 关键文件

- `index.js`：组件实现、样式注入（`<style data-ca-ui>` 经 `ctx.effect` 注入并随插件卸载移除）、`useStyles/injectCss`（引用计数清理）。

## 局部规则

- 不 import Harness React 组件或 npm 包；颜色/间距/圆角只用 `--dsw-*` 主题 token，类名 `ca-` 前缀。
- 页面/视图新增局部样式必须走 `useStyles`，不直接写 `document.body` 或全局选择器。
- 原语只做展示与交互，不含 CA 业务逻辑或取数。
