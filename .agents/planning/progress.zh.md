# 当前阶段进度

Overall: 阶段 3 验收进行中（OpenHarmony 优先）。阶段 1/2 已交付并归档；本地测试
288 passed / 3 skipped，验收期间服务器测试保持绿。

## 阶段 3 已完成

- 服务器侦察：OpenHarmony 6.1 树（`~/OH_6.1/src`，500 个 repo project，产品
  opi5plus/板 rk3588，105 GB 源码，原树只读）、Ubuntu 22.04 主机 24 核 / 62 GB
  内存 / 1.2 TB 可用、Docker builder 镜像与 prebuilt gn/ninja/clang 可用。
- 工作副本已启动：`/home/yu/CA_OH/openharmony`（从原树 rsync，排除构建输出与
  repo object store）；原树未被修改。
- CA 已部署到 `/home/yu/CA_OH/ca`；服务器 venv `/home/yu/CA_OH/venv`（Python 3.11）
  libclang 可用、`ca` 控制台入口已安装。
- 契约扩展：新增 `CallTargetRecord`、`AnalysisResult.call_targets` 与
  `call_target_id`，用于回调/函数指针候选边。
- 阶段 3 任务板建立（task-11~15），5 个 agent 已按互斥作用域恢复。
- 回调/函数指针候选解析已交付：注册赋值、数组/designated initializer、参数传递、
  typedef 两跳、返回指针；夹具 precision = recall = 1.0；xv6 从 syscall 表得到
  22 条候选边（confidence 0.65，may-call 语义）。
- 调用图 CLI 已交付：正向/反向/双向、深度/路径/环查询、table/JSON/DOT；xv6 `main`
  depth1 = 54 nodes/178 edges，depth2 = 94/375（1 个环）；direct 与候选边合并并
  携带 kind/confidence/source。
- `call_targets`、`workspaces/projects/variants`、图查询辅助与
  `store_schema_version=2` 已交付，旧库幂等升级；10 万节点/30 万边图查询 depth1
  亚毫秒，双向 depth2 约 0.33ms。
- MCP server 已交付并在源码服务器验证：stdio + HTTP JSON-RPC、12 个只读工具、
  稳定信封/分页/错误码；`scripts/server_bootstrap.sh` 输出 SERVER_OK。
- 离线 + 增量交互已交付：`ca update --changed-files` 只重解析变更 TU、未变 TU
  命中缓存、头文件变化清缓存；watcher/daemon 设计已文档化。
- Linux 可移植性修复：测试不再硬编码 macOS `.dylib`；服务器 pytest 242 passed /
  1 个预期 relocation 失败 / 3 skipped。
- 本地 246 项测试通过；服务器 bootstrap 与 MCP smoke 通过。

## 正在进行

- 阶段 3 验收：对唯一源码语料（65,023 个 source TU / 70,346 唯一文件；
  204,422 条 source 命令）做分片离线索引，匹配 libclang 15、`--record-scope
  main_file --analyze-scope main`、2 shard × jobs6、wave 级重建池；ETA 约 3.6h。
  每 shard 以 `.done + exit0 + stats` 才算完成。
- 失败归因：S4 存在稳定的 ~27–29% 失败/degraded 带（缺生成头，系统性）；S3 的
  比例也在归因。最终报告按目标/环境分类，不静默丢弃。
- 已完成能力：真实 compdb（214,543 命令；3.4GB）、匹配解析后端、调用图/回调、
  schema v3 + deep_status、MCP 15 工具、L0/L1/L2 分层、45-run Agent 矩阵（0 个
  编造位置）、决策与 OOM/回收事故复盘。
- 剩余：8-shard stats/graph/MCP 汇总、验收报告、commit，然后按 Agent 评估做 P0
  优化并扩展 OH 评估。

## 下一步

- 验收成功后：commit 阶段 3 基线（代码/测试，再文档/验收结果），实现 P0（compact/
  字段投影、批量查询、`ca brief`、头文件声明、returns/支配/常量），并用同一 45-run
  矩阵复测，门槛 context 中位 ≤23.8KB 且 F1 不回归。
- 之后确认阶段 4 范围（其他 OS/语言；人类 UI 最后；可选 PCH/原生索引器）。

## 阻塞与风险

- 全量深析所有 TU 不可行；分层 lazy 路径就是架构，深析覆盖与完整 L0 分开报告。
- 真实 Agent 矩阵显示 CA 提升结构化事实覆盖，但不稳定省 context：`detail` 冗长、
  缺头文件声明、returns 块塌缩、缺锁支配/常量/引用计数/IRQ 是已知缺口。
- 回调候选只覆盖同 TU 注册；跨 TU 表与 C++ virtual 未建模。
- MCP 仅内网/SSH、无认证；人类 UI 留到后续阶段。
