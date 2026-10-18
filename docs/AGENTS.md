# AGENTS.md — 项目文档

本目录保存面向维护者和使用者的项目文档；进入子目录前按本索引读取。

## 信息归属

| 位置 | 职责 |
|---|---|
| 根 AGENTS.md | 全局约束、项目目标与顶层导航 |
| 子目录 AGENTS.md | 本目录职责、直属子项、读取条件和局部规则 |
| .agents/architecture/ | 架构现状、模块关系和扩展边界 |
| 决策档案 | 选择理由、备选和取代关系；入口见 [.agents/AGENTS.md](../.agents/AGENTS.md) |
| .agents/planning/ | 路线图、阶段与进度 |
| .agents/issues/ | 故障调查与复盘 |
| 模块 README | 本单元配置、行为契约、失败方式与扩展点 |
| .agents/skills/ | 可复用工作流与判断规程 |

## 本目录文件

- [usage.md](usage.md)：安装、索引、查询、输出与限制。
- usage.zh.md：中文对照，逐节对应。
- usage.i18n.yaml：双语一致性记录（SHA-256）；改语言后更新。
- [mcp.md](mcp.md)：MCP 查询接口（stdio/HTTP、工具、错误码、SSH）；改行为时同步。
- mcp.zh.md / mcp.i18n.yaml：中文对照与一致性记录。
- [incremental.md](incremental.md)：离线全量 + 增量更新、缓存失效边界、watcher 设计。
- incremental.zh.md / incremental.i18n.yaml：中文对照与一致性记录。
- [layered-indexing.md](layered-indexing.md)：L0/L1/L2 分层与 deep_ready 语义。
- layered-indexing.zh.md / layered-indexing.i18n.yaml：中文对照与一致性记录。
- [cross-tu-callbacks.md](cross-tu-callbacks.md)：跨 TU 回调注册、限定符解析与去重口径。
- cross-tu-callbacks.zh.md / cross-tu-callbacks.i18n.yaml：中文对照与一致性记录。
- [ui.md](ui.md)：只读 Web UI（启动、页面、JSON API、只读/安全边界）；改 `ca ui` 时同步。
- ui.zh.md / ui.i18n.yaml：中文对照与一致性记录。

## 写作规则

- 一个事实一个家，其他位置用相对链接；不复制父级规约。
- 现状文档不写演变故事；理由放决策，事故历史放复盘。
- 细讲本层主题，直属子主题只概述职责并链接；新增维护子目录同步建立索引。
- 注释写行为、时序、所有权、失败和后果，不写推理过程。
- 按 [.agents/i18n.md](../.agents/i18n.md) 维护双语；审查见[文档规范审查](../.agents/standards/document-review.md)。

## 子目录导航

新增文档子目录时在此登记 AGENTS.md 链接与读取条件。

- [reference/AGENTS.md](reference/AGENTS.md)｜参考文档（评估结论与复现入口）｜需要评估结论或数据时。
