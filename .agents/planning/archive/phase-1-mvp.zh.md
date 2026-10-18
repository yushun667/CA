# 阶段 1 归档 — MVP 流水线

完成日期：2026-10-17
目标：把项目原始目标转化为可在真实操作系统源码语料上度量的端到端分析器。

## 交付内容

- 契约：记录、稳定 ID、模块接口、包骨架。
- `ca.build`：compile_commands 加载、编译器包装器俘获、Linux kbuild `.cmd`
  重建、显式合成降级；已俘获 xv6 RISC-V 真实编译数据库（46 TU，可重放）。
- `ca.parse`：libclang 提取函数/类型/字段/调用/位置、跨 TU 稳定 ID、
  RISC-V/ARM target 推断、GCC sysroot；xv6 46/46 TU 零 error 诊断。
- `ca.analyze`：CFG、分支条件、赋值、返回路径、资源操作与函数内泄漏/重复释放
  结论；守卫配对与循环终止性调优后，xv6 findings 163→2（均为已知跨函数误报），
  FreeRTOS 16→0。
- `ca.store`：13 表 SQLite schema、WAL 批量写入、17 个索引 + ANALYZE、
  完整查询 API 与 JSONL 导出。
- `ca.pipeline` + CLI（`index/query/stats/export/version`）、端到端测试、
  精度工具、解析/DB 基准、双语用户指南。

## 已测基线（macOS arm64，18 核，Python 3.11，libclang 18.1.1）

- xv6（46 TU，真实 compdb）：墙钟 0.79s；唯一 394 函数 / 2829 调用 / 1423
  分支 / 1679 赋值 / 339 返回 / 575 资源操作 / 2 findings；DB 4.2 MiB；
  基准 66.2 TU/s、32.3k LOC/s。
- FreeRTOS-Kernel（10 TU，真实 CMake compdb）：201 函数 / 368 调用 / 554
  分支 / 25 资源操作 / 0 findings；DB 2.0 MiB。
- 精度：标注夹具 7/7 类 precision = recall = F1 = 1.0。
- 数据库：15.7 万合成行写入 528,933 rows/s；686 万行 158,153 rows/s
  （1.7 GiB），索引点查亚毫秒；子串搜索 60–75ms（阶段 2 用 FTS5 修复）。
- 测试：126 passed；`pytest tests/test_e2e_*.py -q` 14 passed；qizai 校验通过。

## 产物与引用

- 架构：[../architecture/overview.zh.md](../../architecture/overview.zh.md)
- 决策：[../decisions/active/AGENTS.md](../../decisions/active/AGENTS.md)
- 已解决问题：
  [xv6 compdb 解析失败](../../issues/resolved/2026-10-17-xv6-compdb-parse-failure.zh.md)
- 用户指南：[docs/usage.zh.md](../../../docs/usage.zh.md)
