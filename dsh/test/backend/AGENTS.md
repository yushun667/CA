# AGENTS.md — 后端测试

覆盖 `lib/host/backend/` 的本地生命周期、发现、HTTP、日志与配置；用 fake 进程/HTTP 与临时目录。

## 文件

- `manager.test.mjs`、`local.test.mjs`、`discover.test.mjs`、`http.test.mjs`、`log.test.mjs`、`process.test.mjs`、`config.test.mjs`、`util.test.mjs`：各模块单元测试。
- `real-local.test.mjs`：真实本地 CA 启动，默认按环境变量跳过。

## 子目录

- [helpers/AGENTS.md](helpers/AGENTS.md)：fake spawn/fetch/时钟等共享测试替身。
- [remote/AGENTS.md](remote/AGENTS.md)：远程 Linux 后端测试。

## 规则

- 不执行真实 `ca`、不写用户 profile；临时状态用 `DSH_CA_STATE_DIR` 指向临时目录。
