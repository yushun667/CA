# AGENTS.md — DSH 插件

本目录是 CA 的 DeepSeek Harness 插件包（bundle `@local/dsh-ca`）：Host 原生 tools + Client 整页 UI + 本地/远程后端生命周期 + 长任务 + AI 解释。对 CA 既有 CLI、网页端、MCP 只做**新增**，不得改变其行为。

## 结构

- `package.json`、`cordis.patch.yml`：bundle 清单与 Host 行；`locale/`、`icon.svg`：展示元数据。
- `lib/host/`：Host 半（backend 生命周期、RPC、tools、tasks、explain、prompt）。
- `lib/client/`：Client 半 ESM 源；`client.js` 由 `scripts/build-client.mjs` 从 `lib/client/index.js` 生成，禁止手改。
- `test/`：node:test；`scripts/verify/`：安装核验；`scripts/build-client.mjs`：无依赖打包器。
- `CONTRACT.md`：模块边界、接口与文件所有权，改动接口先改它并通知 Lead。

## 子目录

- [lib/AGENTS.md](lib/AGENTS.md)：Host/Client 源码，模块接口见 [CONTRACT.md](CONTRACT.md)。
- [backend/AGENTS.md](backend/AGENTS.md)：内置 CA wheel（安装产物）。
- [locale/AGENTS.md](locale/AGENTS.md)：bundle 展示元数据（en/zh）。
- [scripts/AGENTS.md](scripts/AGENTS.md)：client 打包器与安装核验。
- [test/AGENTS.md](test/AGENTS.md)：node:test 测试基线。

## 局部规则

- 只新增本目录与治理文档；不改 CA `src/`、`tests/`、`docs/` 的行为。
- Host JS 只用 Node 内置模块；Client 源码只 import 相对路径与 `react`；禁止 npm 依赖、CSS import、动态 import。
- 修改后运行 `node --check`、`cd dsh && node --test`（无参数时 Node 自动递归发现 `test/*.test.mjs`；**不要**用 `node --test test/`，本机 Node 24/26 会把它当模块而报错）、`node scripts/build-client.mjs`，并提交生成的 `client.js`。
- 安装插件、修改 profile、Git 提交都只由 Lead 执行；成员不要运行 `plugin_manager`。
- 真实验证入口：`dsh/scripts/verify/`、CA 根 `.venv/bin/python -m pytest -q`、`python3 .agents/validate.py --root . --mode precommit`。
