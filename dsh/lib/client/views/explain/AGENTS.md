# AGENTS.md — Client AI 解释面板

`createExplainPanel(React, {ui, api, i18n})` 返回供函数页挂载的解释面板，props 为
`{symbol, language?, onNavigate?}`。解释由 Host `explain` 服务生成，经已鉴权 RPC
`/dsh-ca` 的 `explain` 端点到达；CA 后端在这里只读。

## 关键文件

- `index.js`：面板组件（按钮/问题输入/加载/错误重试/复制/依据数量）、RPC 解包与错误归一化、React 元素渲染 markdown。
- `markdown.js`：有界纯解析器（标题/列表/代码块/引用/粗体/`[E1]`/`file:line`），只产出纯数据。

## 局部规则

- 只 import 相对路径；React 由工厂参数传入；只用 `bag.ui` 原语与 `--dsw-alias-*` 主题 token，不注入全局 CSS、不操作宿主 DOM。
- 模型输出一律作为文本交给 React 转义渲染，禁止原始 HTML 注入。
- 缺 i18n key 时使用内置 en/zh fallback；新增 key 同步给 ui-shell。
- 测试：`dsh/test/explain/client-explain.test.mjs`（fake React）；`cd dsh && node --test`。
