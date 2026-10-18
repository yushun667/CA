# AGENTS.md — 插件测试基线

`node:test` 测试（`.test.mjs`），默认不启动真实 CA、不调用真实模型、不访问网络；真实本地/远程用例按环境变量显式开启。

## 文件

- `manifest.test.mjs`：bundle manifest/patch/client 产物/locale/icon 静态检查。
- `client-bundle.test.mjs`、`client-source.test.mjs`：`client.js` 语法/格式与 Client 源码约束（仅相对 import + react、无禁用语法）。
- `host-modules.test.mjs`：Host 模块可加载与纯函数行为。

## 子目录

- [backend/AGENTS.md](backend/AGENTS.md)：本地/远程后端测试与 fake 基础设施。
- [rpc/AGENTS.md](rpc/AGENTS.md)：`/dsh-ca` 通道与 HTTP 传输测试。
- [tools/AGENTS.md](tools/AGENTS.md)：tools/tasks/prompt 测试。
- [explain/AGENTS.md](explain/AGENTS.md)：AI 解释 Host/Client 测试。

## 规则

- 运行：`cd dsh && node --test`（Node 24/26 不要用 `node --test test/`，会被当模块而报错）。
- 用 fake 依赖并保持接口与 `dsh/CONTRACT.md` 一致；测试不写工作区、不联网。
