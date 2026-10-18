# AGENTS.md — 调用图视图

`createCallGraph(React, bag) -> Component`，props `{symbol, direction, depth, limit, onSelectNode, onOpenDetail}`；从 `/api/graph` 取数，按左到右分列渲染 SVG。

## 关键文件

- `index.js`：布局/缩放/平移/控件、节点选择回调、truncated 提示。

## 局部规则

- 边语义固定：实线=直接调用、紫色虚线=回调候选、青色虚线=虚派发候选、红色=环；候选是目标集合，不显示评分。
- 单击节点回调右侧源码面板，双击导航函数详情；组件自持 direction/depth/limit 状态（传入值仅初值）。
- 只读 `bag.api` 数据；不写宿主 DOM；类名 `ca-` 前缀。
