# AGENTS.md — CA

## 项目原始目标

构建一个操作系统源码分析工具：读取仓库真实编译参数，解析 AST，提取函数、类型、调用和源码位置；结合 CFG 与数据流分析，提取分支条件、赋值、返回路径及资源操作；将结果保存到数据库。解析必须足够快且精度足够高，数据库查询与写入必须足够快。

原始目标是项目边界的权威来源。只有用户明确改变目标时才能修改本节，并在同一改动中记录对应决策。

用户入口：根 [README.md](README.md)（英文）/ [README.zh.md](README.zh.md)；操作指南见 [docs/usage.md](docs/usage.md) 与 [docs/usage.zh.md](docs/usage.zh.md)。

## 不可覆盖的全局规约

1. 项目必须使用 Git 管理。
2. 所有代码修改必须通过[代码规范审查](.agents/standards/code-review.md)，所有文档修改必须通过[文档规范审查](.agents/standards/document-review.md)，通过后才能提交。
3. 所有预期保留的项目修改都必须由 Git 跟踪。
4. 禁止把多个独立逻辑改动放在同一个 commit；代码、测试和配套文档只有服务于同一目的时才能同提交。
5. commit 信息必须说明修改原因，而不只是描述修改内容。

## 根文件驱动的按需导航

- 每个任务先读本文件，再按“顶层导航”选择相关分支。
- 进入或修改任一维护目录前，先读该目录的 `AGENTS.md`；需要继续深入时，按其中链接逐层读取。
- 只加载与当前任务有关的分支，禁止递归预读全部 `AGENTS.md`、整个 `.agents/` 或无关目录。
- 单次任务加载的管理文档总量不得超过 15,000 字符；具体单文件上限见 [.agents/governance.json](.agents/governance.json)。超限时先拆分和下沉，不得直接扩大预算。
- 每个由项目维护且对 Git 可见的目录都必须有 `AGENTS.md`。创建、移动或删除目录时，同一改动必须更新该目录和父目录索引。
- 每个目录的 `AGENTS.md` 只说明本目录职责、直接子目录、关键文件、读取条件和局部规则；不得复制父级规约。
- 禁止使用会遮蔽同目录 `AGENTS.md` 的 `AGENTS.override.md`。依赖、缓存、构建产物及其他被 Git 忽略的目录不受目录索引规则约束。

## 顶层导航

| 路径 | 何时读取 | 作用 |
|---|---|---|
| [src/AGENTS.md](src/AGENTS.md) | 实现或修改分析流水线代码时 | Python 包与模块边界 |
| [tests/AGENTS.md](tests/AGENTS.md) | 编写或运行测试时 | 单元、端到端与精度测试 |
| [benchmarks/AGENTS.md](benchmarks/AGENTS.md) | 运行或解读性能基准时 | 解析/写入/查询基准 |
| [scripts/AGENTS.md](scripts/AGENTS.md) | 搭建本地环境时 | 环境引导脚本 |
| [.github/AGENTS.md](.github/AGENTS.md) | 修改 CI 门禁时 | CI 工作流与精度/测试门禁 |
| [docs/AGENTS.md](docs/AGENTS.md) | 编写项目文档时 | 文档职责、写法与维护规范 |
| [.agents/AGENTS.md](.agents/AGENTS.md) | 需要决策、架构、规划、问题、项目 skill、规范或治理校验时 | AI 项目管理系统入口 |

新增顶层维护目录时，必须在此表添加其 `AGENTS.md`、用途和读取条件。

## 管理资料更新触发器

- 非平凡的行为、架构、契约、流程或测试策略改动：同一逻辑改动更新 `.agents/decisions/` 中对应决策及备选理由；机械局部修改豁免。
- 改变模块边界、依赖或数据流：更新 `.agents/architecture/`。
- 阶段、步骤或进度变化：更新 `.agents/planning/`。
- 遇到需要调查、阻塞或可复用故障知识：更新 `.agents/issues/`。
- 形成可复用项目工作流：写入 `.agents/skills/`。

提交前运行 `python3 .agents/validate.py --root . --mode precommit`，完成适用的代码/文档审查，并确认暂存区只有一个逻辑目的。
