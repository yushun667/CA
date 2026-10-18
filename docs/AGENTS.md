# AGENTS.md — 项目文档

本目录保存面向维护者和使用者的项目文档；进入子目录前按本索引读取。

## 信息归属

| 位置 | 职责 |
|---|---|
| 根 AGENTS.md | 全局约束、项目目标与顶层导航 |
| 子目录 AGENTS.md | 本目录职责、子项与读取条件 |
| .agents/architecture/ | 架构现状、模块关系和扩展边界 |
| 决策档案 | 选择理由与取代关系；见 [.agents/AGENTS.md](../.agents/AGENTS.md) |
| .agents/planning/ | 路线图、阶段与进度 |
| .agents/issues/ | 故障调查与复盘 |
| 模块 README | 配置、行为契约、失败与扩展点 |
| .agents/skills/ | 可复用工作流与规程 |

## 本目录文件

- [usage.md](usage.md)：安装、索引、查询、输出与限制。
- [mcp.md](mcp.md)：MCP 查询接口（stdio/HTTP、工具）；改行为时同步。
- [incremental.md](incremental.md)：离线全量+增量、缓存失效、watcher。
- [layered-indexing.md](layered-indexing.md)：L0/L1/L2 分层与 deep_ready 语义。
- [cross-tu-callbacks.md](cross-tu-callbacks.md)：跨 TU 回调注册与去重口径。
- [cross-tu-virtual-dispatch.md](cross-tu-virtual-dispatch.md)：C++ 虚派发元数据、候选边与限制。
- [ui.md](ui.md)：只读 Web UI（启动、页面）；改 `ca ui` 时同步。
- [server.md](server.md)：统一后端 `ca serve`（/api、jobs/SSE、/mcp）；改 serve 时同步。
- [dsh-plugin.md](dsh-plugin.md)：DSH 插件（安装/配置、本地与远程 Linux 后端、UI 页面、11 个工具、长任务、AI 解释、安全边界）；维护 `dsh/` 或排查插件问题时同步。
- [functional-verification.md](functional-verification.md)：阶段 3 dsoftbus/camera 功能验证（virtual/回调/header 增量/MCP）命令、数字与限制；改验收范围时同步。
- functional-verification.zh.md / functional-verification.i18n.yaml：中文对照与一致性记录。
- 每篇 `.md` 均有同名 `.zh.md` 中文对照与 `.i18n.yaml` 一致性记录；改语言后同步三者。

## 写作规则

- 一个事实一个家，其他位置用相对链接；不复制父级规约。
- 现状不写演变故事；理由放决策，事故历史放复盘。
- 细讲本层主题，子主题只概述并链接；新增子目录同步索引。
- 注释写行为、时序、所有权、失败后果，不写推理过程。
- 按 [.agents/i18n.md](../.agents/i18n.md) 维护双语；审查见[文档规范审查](../.agents/standards/document-review.md)。

## 子目录导航

新增文档子目录时登记链接与读取条件。

- [reference/AGENTS.md](reference/AGENTS.md)｜参考文档｜需要评估结论时。
