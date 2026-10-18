# 当前阶段进度

Overall: 阶段 3 功能收口进行中（OpenHarmony 优先）。阶段 1/2 已交付并归档；本地测试
421 passed / 3 skipped。按用户要求，全量验收推迟到全部功能完成之后；OH 是代表性
功能验证语料，不是阶段 3 的验收门槛。

## 已完成

- 服务器与只读 OH 工作副本 `/home/yu/CA_OH/openharmony`；真实 compdb
  （214,543 条命令 / 3.4 GB）与匹配 libclang 15 后端。
- 离线索引管线在 5/8 shard 上跑通，仅作为**功能证据**（41,897 TU、82 失败、
  11.23 GB），不算验收。
- AST 提取函数/类型/字段/调用/源码位置；CFG、分支、赋值、返回、资源操作落库
  （schema v5）。
- 跨 TU 回调候选与 direct/候选边调用图查询；workspace/project/variant 模型与
  federated 分片读取。
- L0/L1/L2 分层深析（`ensure_deep`/`prewarm`/`deep_status`）；轮询式 `ca watch`
  与 `ca update --changed-files`（主 TU 增量缓存）。
- MCP server：18 个只读工具，stdio/HTTP 与 `--serve-url` 代理。
- 统一 `ca serve` 后端（查询 API、通用 jobs/SSE、`/mcp`）；只读 Web UI：查询浏览、
  调用图、源码/CFG、远程模式。
- P0 查询体验（`brief`、compact/fields、batch、头文件声明）；xv6 45-run 真实 Agent
  矩阵，0 个编造位置。
- AI 解释功能曾加入后已端到端移除；不再有 explain/sampling 端点或工具。

## 阶段 3 剩余核心功能

- C++ 虚函数/动态派发与注册式回调之外的通用函数指针候选。
- header 依赖感知的增量索引：当前缓存只跟踪主 TU 源文件，头文件变更会漏掉依赖 TU。
- 可选的 workspace/project/variant CLI/MCP 选择入口；数据模型已存在但无用户界面。
- 把最新代码部署到源码机，并在 OH 的一个 shard 子集上做功能检查（不是全量验收）。

## 下一步

实现上述两个必需缺口，在代表性 OH 语料上做功能验证，更新 README/文档数字，然后确认
阶段 4 范围（其他 OS 树与语言）。全量 8-shard 验收推迟到全部功能完成之后。

## 风险与阻塞

- C++ 虚函数派发需要类层次/override 数据并新增候选生成；大型 C++ 代码可能产生噪声。
- header 依赖跟踪需要 include 图存储与缓存失效；漏判就是增量正确性 bug。
- OH 巨型 C++/IDL TU 慢且吃内存（S2 已有 partial 归档）。
- 深析覆盖按设计是部分的；findings 是函数内启发式。
- 回调候选仍是注册式；MCP/UI 无认证；SQLite 单写者。
