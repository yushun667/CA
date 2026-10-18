# AGENTS.md — 可复用视图

函数详情页与可复用图/解释组件：`createXView(React, bag) -> Component`（bag = `{ui, api, i18n}`），props 由所用页面传入。

## 子目录

- [function/AGENTS.md](function/AGENTS.md)：函数详情页（源码 + accordion + 三栏布局 + 图/解释组合）。
- [graph/AGENTS.md](graph/AGENTS.md)：SVG 调用图组件。
- [cfg/AGENTS.md](cfg/AGENTS.md)：SVG 控制流图组件。
- [explain/AGENTS.md](explain/AGENTS.md)：AI 解释面板。

## 局部规则

- 数据只经 `bag.api.caRequest('/api/...')` 或 `bag.api.rpc(...)` 获取；组件自己处理 loading/empty/error。
- React 由工厂参数传入，不直接 import `react`；只用 `bag.ui` 原语与主题 token；`ca-` 类名 + 局部样式。
- 不操作 `document.body`/宿主 DOM，不注入全局 CSS；卸载时清理监听与样式。
