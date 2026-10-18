# AGENTS.md — CFG 视图

`createCfgGraph(React, bag) -> Component`，props `{symbol, zoom?, onLoaded?}`；从 `/api/cfg` 取数，按深度分层渲染 SVG 控制流图。

## 关键文件

- `index.js`：块/边布局、entry/exit 高亮、T/F 边标签、回边虚线、缩放 40–180% 与拖拽平移、`deep_ready=false` 回退提示。

## 局部规则

- loading/empty/error 状态由组件负责；深度事实未生成时提示先跑 `ensure_deep`，不要伪造图。
- 只读主题 token 与 `bag.ui` 原语；不操作宿主 DOM。
