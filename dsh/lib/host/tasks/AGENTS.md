# AGENTS.md — 长任务管理

`createTaskManager(ctx, {backend,config})` 执行 CA CLI 长任务：`index|update|ensure_deep|prewarm|resolve_callbacks|watch`。用后端 `execCa` 在本地或经 SSH 远程运行，不经过 CA job 队列。

## 关键文件

- `index.js`：参数校验与 CLI flag 映射（对应 `src/ca/cli/main.py`）、有界并发（默认 2）、状态机 `queued→running→succeeded|failed|cancelled`、每任务环形日志（约 1 MiB，带 offset 分页）、取消与变更通知。

## 局部规则

- 不 import DSH/Cordis、不直接起进程或访问网络；backend 依赖注入，便于 fake 测试。
- 取消通过 abort signal 终止 CLI 进程；任务状态在进程退出后收敛。
- 与 RPC `tasks.*` 及工具 `ca_task` 的返回结构保持一致；改字段同步更新契约。
- 测试：`dsh/test/tools/tasks.test.mjs`。
