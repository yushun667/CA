# AGENTS.md — tools/tasks/prompt 测试

覆盖 `lib/host/tools/`、`lib/host/tasks/`、`lib/host/prompt.js`：fake backend/tasks 断言工具 schema、参数校验、输出摘要、任务状态机与提示词注册。

## 文件

- `tools.test.mjs`：11 个工具的 schema/参数校验/输出与错误码。
- `tasks.test.mjs`：任务启动/状态机/日志分页/取消/并发上限。
- `prompt.test.mjs`：系统提示词 section 注册与可选服务缺失时的 no-op。
- `helpers.mjs`：fake backend/tasks/ctx。

## 规则

- 不执行真实 `ca`；工具名与 `dsh/CONTRACT.md` 的 11 个名字保持一致。
- 输出断言关注有界性与可操作性，不断言整段长文本。
