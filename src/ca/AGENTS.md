# AGENTS.md — ca 实现包

流水线：build（真实编译参数）→ parse（Clang AST）→ analyze（CFG/数据流）→ store（数据库）→ cli（入口）；`pipeline.py` 负责编排与并行。

## 子目录导航

| 路径 | 何时读取 | 职责 |
|---|---|---|
| [core/AGENTS.md](core/AGENTS.md) | 修改记录格式、稳定 ID 或模块契约时 | 共享数据契约（Lead 所有） |
| [build/AGENTS.md](build/AGENTS.md) | 获取或修复编译参数时 | compile_commands 与构建俘获 |
| [parse/AGENTS.md](parse/AGENTS.md) | 修改 AST 提取时 | libclang 解析与实体提取 |
| [analyze/AGENTS.md](analyze/AGENTS.md) | 修改 CFG、数据流或资源分析时 | 函数级 CFG/数据流事实 |
| [store/AGENTS.md](store/AGENTS.md) | 修改库表、写入或查询时 | SQLite 存储与查询 API |
| [cli/AGENTS.md](cli/AGENTS.md) | 修改命令行或索引编排时 | CLI 与端到端行为 |
| [ui/AGENTS.md](ui/AGENTS.md) | 修改人类 Web UI、HTTP API 或前端静态页时 | 只读本地浏览器界面与 JSON API |
| [server/AGENTS.md](server/AGENTS.md) | 修改统一后端 `ca serve`、jobs/SSE 或 /mcp 端点时 | 单进程后端 + 任务队列/事件 + MCP/UI 双客户端 |

## 关键文件

- `pipeline.py`：多进程索引编排，产出 `IndexSummary`；修改前读 `core/interfaces.py` 与 `.agents/architecture/`。
- `desktop.py`：`ca desktop` 启动本机浏览器、统一查询/MCP 服务与用户目录空库；冻结版无参数入口复用此命令。
