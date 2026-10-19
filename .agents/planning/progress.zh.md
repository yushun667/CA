# 当前阶段进度

Overall: 阶段 3 功能目标已完成并在 OpenHarmony dsoftbus + camera 上完成功能验证
（OpenHarmony 优先）；下一步确认阶段 4 范围。阶段 1/2 已交付并归档；本地测试
497 passed / 4 skipped。按用户要求，全量验收推迟到全部功能完成之后；功能验证只用 OH dsoftbus 与 camera
仓库，不是阶段 3 的验收门槛。

## 进行中

- 原生 Windows/Linux/macOS 包：CLI/MCP/浏览器、安装器与便携包。
  仍需原生工作流结果与可下载产物，见
  [二进制指南](../../docs/binaries.zh.md)。

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
- confidence 已端到端删除：候选只保留 kind/reason/evidence；schema v8 删除数据库字段。
- C++ 虚函数/动态派发：元数据持久化（store schema v7）、同 TU 候选 + 跨 TU
  查询时合并；camera `Command::Do` 返回 29 条候选（无 override 对照为 0）；
  combined 语料 3,784 条候选边覆盖 1,531 个 call（20%）。
- header 依赖感知增量：`tu_includes` 传递闭包，`ca update`/`ca watch` 只失效
  依赖 TU；实测每个 header 6 个依赖 TU、无关 header no-op，MCP/CLI/graph 验证。
- 数据库原地增量写入：`ca update`/`ca watch` 用 `remove_units`/`replace_units`
  只替换受影响 TU（未变行 id 保持），FTS5 增量维护；有回调注册/间接调用时刷新
  候选；cycle 指标 `db_write`/`incremental_rows`/`callback_refresh`；构建文件/
  旧库回退全量重建。
- workspace/project/variant registry：CLI CRUD + index 注册、query/graph
  `--variant` 路由到已注册只读库、MCP `workspaces`/`projects`/`variants`
  （21 工具）、serve `GET /api/registry` + `POST /api/registry/active` 原子内存
  切换；UI selector 由 task-47 接。
- OH dsoftbus（1,287 TU）、camera（475 TU）与 combined（1,762 TU）功能验证：
  stats/search/detail/graph、MCP 18 工具、virtual/indirect 候选与 header 增量
  update/watch 全部通过；见 docs/functional-verification.zh.md。
- DSH 插件（`@local/dsh-ca`）：CA 页面/项目、12 个工具、本地/远程后端、任务与解释；
  wheel/离线安装与自动准备。Linux 离线安装已验证；测试独立。见
  [指南](../../docs/dsh-plugin.zh.md)。

## 延后 / 可选

- C++ 虚函数/动态派发与注册式回调之外的通用函数指针候选。
- header 依赖感知的增量索引：当前缓存只跟踪主 TU 源文件，头文件变更会漏掉依赖 TU。
- 可选的 workspace/project/variant CLI/MCP 选择入口；数据模型已存在但无用户界面。
- 把最新代码部署到源码机，并在 OH dsoftbus 与 camera 仓库上做功能检查（不是全量验收）。

## 下一步

实现上述两个必需缺口，在 OH dsoftbus 与 camera 仓库上做功能验证，更新 README/文档
数字，然后确认阶段 4 范围（其他 OS 树与语言）。全量 8-shard 验收推迟到全部功能完成之后。

## 风险与阻塞

- C++ 虚函数派发需要类层次/override 数据并新增候选生成；大型 C++ 代码可能产生噪声。
- header 依赖跟踪需要 include 图存储与缓存失效；漏判就是增量正确性 bug。
- OH 巨型 C++/IDL TU 慢且吃内存（S2 已有 partial 归档）。
- 深析覆盖按设计是部分的；findings 是函数内启发式。
- 回调候选仍是注册式；MCP/UI 无认证；SQLite 单写者。
- DSH 插件依赖 Harness 内部服务（`webServer`/`slots`/`llm`）；Harness 升级后需重新
  验证，远程 Linux 路径需要服务器上的 SSH/rsync/venv。
