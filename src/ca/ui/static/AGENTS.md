# AGENTS.md — ui/static（前端静态资源）

单页 UI 的静态资源，由 `server.py` 按白名单提供，无构建步骤、无 CDN、无外网请求。

## 关键文件

- `index.html`：页面骨架（搜索栏、结果列表、详情/统计/回调视图、语言切换）。
- `i18n.js`：中英字典、浏览器语言探测、`中文/EN` 切换、`data-i18n` 静态翻译、`<html lang>`/localStorage 记忆。
- `highlight.js`：本地 C/C++ 轻量 tokenizer（关键字/类型/注释/字符串/数字/预处理），供顶部源码与节点源码面板共用；无外网/CDN。
- `app.js`：路由（hash）、API 调用、详情/调用图 SVG/统计/回调渲染与事件绑定；动态文案经 `t()` 取字典。
- `style.css`：亮/暗色主题、响应式布局、表格/徽章/SVG 图样式。
- `favicon.svg`：内联图标。

## 局部规则

- 不引入任何外部字体、脚本、样式或网络请求；新增静态文件须同时登记到 `server.py` 的白名单与父目录索引。
- 所有来自 API 的文本在插入 DOM 前必须经 `escapeHtml`；SVG 节点用 `svg` 字符串拼接时同样转义。
- 代码渲染必须经 `highlight.js` 的 `highlightLines()`（自动转义 HTML），不得拼接未转义源码。
- 所有用户可见文案必须经 `i18n.js` 的 `t()` 或 `data-i18n` 属性提供，禁止在 `app.js`/`index.html` 写死英文；符号名、路径、代码与原始字段值不翻译。
- 三区布局尺寸/折叠状态存 `localStorage` 的 `ca.ui.layout`；改布局、splitter 或
  快捷键时同步 `docs/ui.*` 与 `tests/test_ui.py` 的静态断言。
- 远程模式代码（`/api/ui` 模式探测、连接徽章/手动重试）不得引入外部脚本；UI 不含 AI 解释 tab/请求，前端不使用 EventSource。
- 变体选择器（`registry-bar`/`variant-select`）仅在 registry 可用时显示；切换 POST `/api/registry/active` 后刷新数据视图；URL/localStorage 与 i18n 规则见 docs/ui。
- 改样式或交互后手工用 `ca ui --db <db>` 在浏览器验证亮/暗色、窄屏与中英切换。
