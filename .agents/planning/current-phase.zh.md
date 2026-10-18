# 当前阶段拆分

Current phase: 3. OpenHarmony 优先的 Agent 平台

Goal: 让 CA 能面向真实 OpenHarmony C/C++ 源码服务 AI agent 与人：离线 + 增量索引、
回调/函数指针解析与调用图查询、多仓 workspace/variant 数据模型、以及运行在源码机上
的 MCP 查询服务。阶段 3 在核心功能实现并**在 OpenHarmony dsoftbus 与 camera 仓库上完成功能验证**后收口；
该功能验证现已完成。
按用户要求，全量验收推迟到全部功能完成之后；这些仓库是功能验证语料，不是阶段 3 的验收门槛。

## 完成条件

- **OpenHarmony 语料**：从工作副本 `/home/yu/CA_OH/openharmony` 生成真实 C/C++
  `compile_commands.json`；原树 `~/OH_6.1/src` 保持只读。
- **离线 + 实时增量**：离线批处理建立快照；实时更新只重解析受影响 TU。header 依赖
  必须使依赖 TU 失效，而不只是主 TU 源文件。
- **调用智能**：间接调用得到回调/函数指针候选目标与置信度；`ca graph` 合并 direct
  与候选边，支持正向/反向/路径/深度查询与 table/JSON/DOT。除注册式回调外，还必须
  覆盖 C++ 虚函数/动态派发。
- **Agent 接口**：`ca mcp` 向 debug/coding/test-gen agent 暴露符号/引用/调用图/
  上下文查询（18 个只读工具）；stdio、HTTP 与 `--serve-url` 代理已验证。
- **数据模型**：持久化 `CallTargetRecord`；存储 workspace/project/variant 元数据并
  记录分片/联邦路径。用户可用的 workspace/variant 选择入口为可选增强。
- **质量门禁**：测试与 qizai 校验通过；README/文档记录当前功能数字与限制。功能验证
  只用 OpenHarmony dsoftbus 与 camera 仓库；全量验收是后续独立里程碑。

## 步骤

| 步骤 | 产出 | 状态 | 阻塞 |
|---|---|---|---|
| 1 | 服务器侦察、工作副本、CA venv、`CallTargetRecord` 契约 | 已完成 | 无 |
| 2 | OpenHarmony 真实 C/C++ compdb 与语料 | 已完成 | 无 |
| 3 | MCP server、`ca graph`、离线/增量交互 | 已完成 | 无 |
| 4 | 回调/函数指针候选解析（注册模式） | 已完成 | 无 |
| 5 | `call_targets`、图查询、workspace/variant 模型 | 已完成 | 无 |
| 6 | OpenHarmony C/C++ 解析兼容（匹配 libclang 后端） | 已完成 | 无 |
| 7 | 集成、统一后端、只读 UI、P0 查询体验、Agent 评估 | 已完成 | 无 |
| 8 | C++ 虚函数/动态派发 + 通用间接调用候选 | 待做 | 无 |
| 9 | header 依赖感知的增量索引/watch | 待做 | 无 |
| 10 | workspace/project/variant CLI/MCP 入口（可选） | 待做 | 无 |
| 11 | OH dsoftbus + camera 仓库功能验证 + 文档数字 | 待做 | 步骤 8-9 |

## 剩余缺口的实施方案

- **C++ 虚函数/动态派发**：类型提取增加基类与虚函数/override 关系并持久化；调用目标为
  虚方法时，向可静态解析的 override（同 TU/跨 TU）生成候选边，置信度低于 direct；
  合并进 `call_targets` 与图查询。多继承与无法解析的动态类型仍是有界启发式。
- **header 依赖增量**：解析时记录每个 TU 的直接 include，持久化 TU→header 依赖图；
  `update`/`watch` 把变更文件映射到依赖 TU，只失效并重解析这些 TU。功能测试：改 header
  会重解析依赖者，改无关 header 不重解析。
- **workspace/variant 入口（可选）**：CLI/MCP 增加 project/variant 过滤；无需改存储。
- **功能验证**：只用 OH dsoftbus 与 camera 仓库，不跑全量 8-shard 验收；在 README/文档
  记录命令、数字与限制。

全量分片验收、S2/S6/S8 补完与最终验收报告**推迟**，不再作为阶段 3 功能门槛。
阶段 4+（其他 OS 树/语言、DWARF、更多 UI）需要单独确认范围。
