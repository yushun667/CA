# AGENTS.md — AI 项目管理系统

本目录保存供 agent 按需读取的长期项目上下文，不应整树加载。

## 分支导航

| 路径 | 何时读取 | 内容 |
|---|---|---|
| [decisions/AGENTS.md](decisions/AGENTS.md) | 需要理解选择理由、提出新决策或检查取代关系时 | 生效与作废决策 |
| [architecture/AGENTS.md](architecture/AGENTS.md) | 修改模块边界、接口、依赖或数据流时 | 整体架构现状 |
| [planning/AGENTS.md](planning/AGENTS.md) | 规划工作、确定下一步或更新进度时 | 路线图、当前阶段和进度 |
| [issues/AGENTS.md](issues/AGENTS.md) | 遇到缺陷、阻塞、调查或复盘时 | 开放与已解决问题 |
| [skills/AGENTS.md](skills/AGENTS.md) | 创建或使用项目专属工作流时 | 项目 skills |
| [standards/AGENTS.md](standards/AGENTS.md) | 审查代码、文档或准备 Git 提交时 | 审查和提交规范 |

## 本目录文件

- [i18n.md](i18n.md)：编写或更新项目文档、决策记录的双语配对时读取。
- `governance.json`：文档预算和模板版本的机器可读权威配置；修改预算时读取。
- `validate.py`：检查 Git 根、目录索引、遮蔽文件、文档预算、链接和提交前状态；治理结构变化后运行。

只读取匹配任务的行所指向分支。新增分支时，为其创建 `AGENTS.md` 并在本表登记。
