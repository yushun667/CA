# 整体架构

Status: 阶段 2 于 2026-10-17 交付；模块已在 xv6、FreeRTOS 与 Linux 6.12 内核子集上验证（见 ../planning/progress.zh.md）。

选择理由见 `../decisions/active/`；执行状态见 `../planning/`。

## 系统边界

- 项目目标：见根 `AGENTS.md` 的「项目原始目标」。
- 用户：分析操作系统源码仓库的工程师与 AI agent（优先 C/C++）。
- 输入：源码仓库 + 真实编译数据库，或俘获到的构建。
- 输出：一个 SQLite 数据库与基于已提取事实的 CLI 查询。
- 本阶段不做：运行时跟踪、二进制分析、行为仿真、全程序指针/别名分析。

## 组成与职责

| 组件 | 职责 | 契约 |
|---|---|---|
| `ca.build` | 获取真实编译参数：compile_commands、构建俘获、Linux 内核 `.cmd` 重建、显式标注的合成降级 | `load_compilation_database`、`capture_build`、`reconstruct_kernel_commands`、`synthesize_for_files` |
| `ca.parse` | 用 libclang 解析每个 TU，提取函数、类型、字段、调用与源码位置 | `parse_compilation_unit -> ParsedUnit` |
| `ca.analyze` | 函数级 CFG、分支条件、赋值、返回路径、资源操作、函数内数据流结论 | `analyze_function -> AnalysisResult` |
| `ca.store` | 批量写入、去重、索引与查询 API | `SqliteStore` 实现 `RecordStore` |
| `ca.pipeline` | 多进程编排、指标统计、单 TU 错误隔离 | `run_index -> IndexSummary` |
| `ca.cli` | `index`、`query`、`stats`、`export` 子命令 | `main(argv) -> int` |
| `ca.desktop` | 启动回环地址统一后端并打开浏览器；初始化默认用户数据 | 显式 `ca desktop`，或冻结可执行文件无参数启动 |

`ca.core` 由 Lead 维护，保存记录契约、稳定 ID 与模块协议；其他模块不把 Clang 类型带入 `core`。

## 关键数据流

```text
源码仓库 + 真实构建
  -> CompilationUnit（绝对 file/directory、原始 argv、参数来源）
  -> worker 进程：libclang AST -> ParseResult + 每个函数的 AnalysisResult
  -> 主进程：分批 SQLite 事务 -> 建索引的数据库
  -> CLI 查询 / JSONL 导出
```

只有纯 dataclass 跨进程与数据库边界；活动 libclang 对象只存在于 worker 内。调用的 `callee_id` 可能指向仓库外声明，store 会写入占位行，保证 caller/callee 连接完整。

## 接口与依赖边界

- 导入方向：`core <- {build, parse, analyze, store} <- pipeline <- cli`。
- 实体 ID 由 `ca.core.ids` 生成：外部链接用 Clang USR，内部链接附加文件，无 USR 时回退到 name+位置。
- `SCHEMA_VERSION` 版本化记录/文件格式；字段或 ID 规则变化属于契约变更，必须记录决策。
- 头文件中的定义会经多个 TU 重复出现；store 按稳定 ID 去重，分析结果按 worker 内去重 + 数据库主键去重。

## 阶段 2 增补

- `ca.build.kernel_corpus`：通过 Apple container Linux 环境复现 Linux 内核语料
  （固定内核与镜像、LLVM arm64 构建、内核 `gen_compile_commands.py`、容器→宿主
  路径重写）；容器运行时不可用时显式报错。
- `ca.store`：`functions_fts` 是 `finalize()` 中建立的 FTS5 trigram
  external-content 索引，`meta.fts_ready` 记录可用性；符号子串查询优先 FTS，
  回退 `LIKE`；精确匹配保留原索引路径。
- `ca.pipeline`/CLI：`.ca-cache/` 下的增量 TU 缓存按源文件哈希 + 编译参数 +
  schema/工具版本失效，未变 TU 跳过 parse/analyze；`ca` 可作为控制台命令使用。
- `.github/workflows/ci.yml`：CI 在 Python 3.11 上运行全量测试与精度门禁
  （macro F1 >= 0.9）。

## 运行与部署

- Python 3.11 CLI，带可选 MCP/UI/统一后端；原生 Windows/Linux/macOS 包提供相同命令。
- 依赖见 `requirements.txt`；目录包包含 Python、匹配的 libclang、DuckDB、schema
  与 UI。冻结 worker 分派先于 CLI；仅冻结的无参数入口默认进入桌面模式。
- 桌面绑定回环地址；用户数据保存后端任务状态和默认空索引。安装器不拥有用户索引或
  源码树。安装器与便携包共用 `dist/ca`，原生 CI 验证构建。契约见
  [二进制指南](../../docs/binaries.zh.md)。
- 并行：每个 worker 一个进程（`ProcessPoolExecutor`）；SQLite WAL 允许单写者（主进程）与并发读。
- 语料、俘获构建与基准产物放在 `.tmp/`（已忽略）。
