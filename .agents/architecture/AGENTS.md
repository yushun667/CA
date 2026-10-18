# AGENTS.md — 架构

本目录记录系统结构现状，不记录决策历史或任务进度。

- [overview.md](overview.md)：涉及模块、依赖、数据流、核心接口或部署边界时读取。
- overview.zh.md：中文对照，阅读中文架构说明时使用；与 overview.md 逐节对应。
- overview.i18n.yaml：双语一致性记录（文件名与 SHA-256）；修改任一语言版本后更新。

架构变复杂时按主题拆分文件，并在本文件登记“链接｜覆盖范围｜何时读取”。理由写入 `../decisions/`。
- [agent-platform.md](agent-platform.md)：Agent 平台与服务器部署现状；涉及 MCP/图查询/离线增量/OpenHarmony 接入时读取。中文 agent-platform.zh.md，一致性记录 agent-platform.i18n.yaml。
- [unified-serve-backend.md](unified-serve-backend.md)：统一后端 `ca serve`、jobs/SSE、MCP/UI 双客户端与跨客户端闭环；改 serve/jobs 时读取。中文 unified-serve-backend.zh.md，一致性记录 unified-serve-backend.i18n.yaml。
