# AGENTS.md — 原生 tools

`registerTools(ctx, {backend,tasks,explain,config})` 用 `ctx.tools.register` 注册 12 个模型可见工具，参数用 plain JSON Schema 自行校验；输出为有界文本 `{text}`。

## 工具与文件

- 工具名固定：`ca_status`、`ca_project`、`ca_search`、`ca_symbol`、`ca_graph`、`ca_facts`、`ca_source`、`ca_stats`、`ca_callbacks`、`ca_registry`、`ca_explain`、`ca_task`。
- `projects.js`：项目列表/按 name-or-id 解析等共享助手；`ca_project` 只激活已存在项目（创建/编辑属于 UI 用户权限）。
- `index.js`：schema、参数校验、execute（`exec.signal` 可取消）、经 `/api/*` 或 tasks/explain 取数。
- `backend.js`：`caGet`/`caPost`/`ensureBackend`/`ToolError` 等公共助手；`format.js`：结构化输出格式化。

## 局部规则

- 不 import `defineTool`/`schemastery`/npm 包；描述要让模型知道何时用、参数含义与限制。
- 输出约 20k 字符上限并提示分页；长任务禁止阻塞等待（改用 `ca_task` 轮询）。
- 工具错误归一为稳定 `code`（如 `INVALID_ARGUMENT`、`EXPLAIN_UNAVAILABLE`），不暴露堆栈。
- 测试：`dsh/test/tools/` 用 fake backend/tasks；工具清单同步 `dsh/test/manifest.test.mjs`。
