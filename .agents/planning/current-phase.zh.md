# 当前阶段拆分

Current phase: 3. OpenHarmony 优先的 Agent 平台

Goal: 让 CA 能面向真实 OpenHarmony C/C++ 源码服务 AI agent 与人：离线全量索引 +
实时增量更新、回调/函数指针解析与调用图查询、多仓 workspace/variant 数据模型、
以及运行在源码机上的 MCP 查询服务。人类 UI 明确放在最后。

## 完成条件

- **OpenHarmony 语料**：在固定当前快照（产品 opi5plus / 板 rk3588）上，从工作副本
  `/home/yu/CA_OH/openharmony` 生成真实 C/C++ `compile_commands.json`；原树
  `~/OH_6.1/src` 保持只读。开发阶段用部分仓库作验证库，验收使用全量 C/C++ 语料。
- **离线全量 + 实时增量**：离线批处理建立全量基线；实时更新路径只重解析变更 TU
  并更新快照。不做实时全量解析。
- **调用智能**：间接调用得到回调/函数指针候选目标与置信度；`ca graph` 支持正向/
  反向/路径/深度查询，输出 table/JSON/DOT，并合并 direct 与候选边。
- **Agent 接口**：MCP server（`ca mcp`）运行在源码机，向 debug/coding/test-gen
  agent 暴露符号/引用/调用图/上下文查询；有最小 MCP 客户端测试与双语文档。
- **数据模型**：持久化 `CallTargetRecord`；存储 workspace/project/variant 元数据
  （OpenHarmony 500 个 project、product/board/version），并记录全量规模的分片/
  联合查询路径。
- **质量门禁**：全部测试通过、qizai 校验通过、README/文档记录阶段 3 数字与限制。

## 步骤

| 步骤 | 产出 | 状态 | 阻塞 |
|---|---|---|---|
| 1 | 服务器侦察、工作副本、CA venv、`CallTargetRecord` 契约（Lead） | 已完成 | 无 |
| 2 | OpenHarmony 真实 C/C++ compdb 与语料（task-11） | 已完成 | 无 |
| 3 | MCP server、`ca graph`、离线/增量交互（task-12） | 已完成 | 无 |
| 4 | 回调/函数指针候选解析（task-13） | 已完成 | 无 |
| 5 | `call_targets` 持久化、图查询、workspace/variant 模型（task-14） | 已完成 | 无 |
| 6 | OpenHarmony C/C++ 解析兼容（task-15） | 已完成 | 无 |
| 7 | 集成、分片验收批处理、治理（Lead） | 进行中 | 无 |

阶段 4+（人类 UI 最后、其他 OS 树与语言）在 [roadmap.zh.md](roadmap.zh.md) 中提出，
需要单独确认范围。
