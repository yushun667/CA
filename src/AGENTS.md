# AGENTS.md — 源代码

本目录保存可导入的 Python 包；实现按模块边界拆分，测试位于 ../tests/。

## 子目录导航

| 路径 | 何时读取 | 内容 |
|---|---|---|
| [ca/AGENTS.md](ca/AGENTS.md) | 修改包布局、模块依赖或跨模块契约时 | 实现包入口与模块索引 |

## 局部规则

- 依赖只用 `../requirements.txt` 固定的版本；新增依赖须记录决策并更新清单与 `.venv`。
- 在仓库根运行 `.venv/bin/python -m pytest -q`。
- 模块只能通过 `ca.core` 的契约类型通信；禁止反向依赖（如 store 导入 parse）。
