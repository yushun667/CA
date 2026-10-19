# 问题：Unix 内存指标阻止 Windows 原生索引

Status: resolved
Opened: 2026-10-19

## 现象与影响

Windows 原生 Python 无法导入 `ca.pipeline`，即使已安装解析和存储依赖，
`ca index` 仍无法运行。

## 复现或触发条件

修复前在 Windows 导入 `ca.pipeline`。模块顶层的 `import resource` 抛出
`ModuleNotFoundError`；Python 仅在 Unix 提供 `resource`。

## 调查

`resource` 仅供诊断辅助函数 `_peak_rss_mb` 使用，该函数已有失败返回
`0.0` 的退化行为。另一个 Unix 专用导入是 `ca.cli.watch` 的 `fcntl`，
由独立的 watch 命令延迟加载，不阻止普通索引。

## 根因

可选的工作进程内存指标被作为必需依赖提前导入。

## 解决与验证

将 `resource` 导入移入辅助函数已有的异常处理范围。不提供该模块的平台
保留返回零的退化行为，Unix 单位换算不变。警告测试使用确定的模拟指标，
另增新进程回归测试，验证缺少 `resource` 时仍能导入流水线。

使用原生 `.venv/Scripts/python.exe -m pytest -q tests/test_e2e_pipeline.py -k
"pipeline_import_without_resource or worker_rss_warning_logged or pool_recycle_setting
or new_pool_is_plain_process_pool or worker_recycling_keeps_all_units"` 通过
5 项测试，其中包含两个工作进程的索引运行。`git diff --check` 也通过。
Windows 原生工作进程的 RSS 指标仍不可用，显示为零。
本次修改未实现独立 watch 命令的 Windows 支持。

## 关联

- 决策或架构：无；保留既有可选指标退化行为。
- 相关提交：参见本问题记录和 `src/ca/pipeline.py` 的 Git 历史。
