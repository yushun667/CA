# 问题：Windows 头文件增量更新可能漏掉依赖翻译单元

Status: open
Opened: 2026-10-19

## 现象与影响

Windows 上修改头文件后，即使索引已经保存该头文件的 `tu_includes` 依赖，
`ca update` 仍可能保留过期分析。全量索引、查询、MCP 与网页启动不属于此故障。

## 复现或触发条件

建立包含 TU 对 `include/os.h` 依赖的索引，修改头文件，在 Windows 上增量更新。
若更新计划将变更头文件标为 `include\os.h`，依赖 TU 查询可能没有匹配结果，
从而跳过应有的重新解析。

复现测试：`python -m pytest tests/test_e2e_graph_mcp.py::test_update_header_change_clears_cache`。
已有索引包含两个依赖 TU，但 update 退出码为 2，并报告
`changed header(s) are not included by any indexed TU`。

## 调查

Windows 扩展测试在原生打包改动版本和导出的打包前 `da9d1ba` 基线中都复现了该失败。
这是分析器既有缺陷，不是冻结或安装目录导致。另三个基线失败涉及 Node 编码和
子进程测试环境缺少 Windows `SystemRoot`，属于测试环境调查结果，不另列为产品限制。

## 根因

`tu_includes` 保存 POSIX 风格 include 路径，而 `ca.cli.watcher.plan_changes`
可能产生 Windows 反斜杠标签。`ca.cli.commands` 将其交给按字面路径比较的依赖查询，
分隔符不一致导致无法命中。

## 解决与验证

未解决。下一步在共享边界将 include 键与变更文件键规范为同一种形式，再同时验证
Windows/POSIX 上依赖头文件会重解析对应 TU，而无关头文件保持 no-op。此修复不在
原生安装包交付范围内。修复前修改头文件应使用不带 `--incremental` 的完整
`ca index` 重建，不要依赖 `ca update` 处理。Windows `ca watch` 另因 Unix 文件锁
暂不支持，与此缺陷独立。

## 关联

- 用户侧替代方法：[二进制指南](../../../docs/binaries.zh.md)。
- 架构：[整体架构](../../architecture/overview.zh.md)。
- 相关提交：尚无修复提交。
