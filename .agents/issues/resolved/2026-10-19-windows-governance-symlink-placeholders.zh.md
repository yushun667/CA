# 问题：Windows 治理检查拒绝 Git 符号链接占位文件

Status: resolved
Opened: 2026-10-19
Resolved: 2026-10-19

## 现象与影响

原生 Windows 提交前检查在 `core.symlinks=false` 的检出中报告 14 个未登记
的管理文件，阻止了原本有效的提交。

## 复现

在上述检出中暂存修改后，运行
`.venv/Scripts/python.exe .agents/validate.py --root . --mode precommit`。
受影响文件是 Git 符号链接目标为 `AGENTS.md` 的 `CLAUDE.md` 别名。

## 调查与根因

校验器会豁免文件系统上指向 `AGENTS.md` 的真实符号链接。禁用符号链接时，
Git 会将链接目标写成普通文件，因此校验器错误地要求目录索引重复登记这些别名。

## 解决与验证

只有 Git 索引第零阶段记录模式 `120000`、目标恰为 `AGENTS.md`，并且工作区
文件仍精确包含目标字节时，才识别为占位文件。一次读取索引，再使用 Git
对象哈希计算以适配仓库对象格式。真实符号链接保持原有行为；普通文件、被修改的
占位文件和其他链接目标仍须通过目录索引检查。

五个聚焦回归用例使用临时 Git 仓库并明确禁用符号链接检出，不需要文件系统
符号链接权限。覆盖占位文件通过，以及普通文件、修改内容、尾部换行和不同
索引目标被拒绝的情况。暂存本修复后，原生 Windows 提交前检查也通过。

## 关联

- 校验器：[.agents/validate.py](../../validate.py)
- 回归测试：[tests/test_governance_validation.py](../../../tests/test_governance_validation.py)
- 决策或架构：无；本修改恢复既有别名豁免行为。
- 相关提交：本修复。
