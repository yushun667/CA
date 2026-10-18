# AGENTS.md — 插件源码

本目录是 `@local/dsh-ca` 的源码根：`host/` 运行在 Harness 服务端，`client/` 运行在浏览器。跨端接口与文件所有权见 [../CONTRACT.md](../CONTRACT.md)。

## 子目录

- [host/AGENTS.md](host/AGENTS.md)：Host 半（backend、RPC、tools、tasks、explain、prompt）。
- [client/AGENTS.md](client/AGENTS.md)：Client 半 ESM 源，经打包器生成 `client.js`。

## 局部规则

- Host 代码只用 Node 内置模块；Client 源码只 import 相对路径与 `react`；禁止 npm 依赖、CSS import、动态 import、顶层 await。
- 接口变化先改 [../CONTRACT.md](../CONTRACT.md) 并通知 Lead；模块边界按目录所有权划分。
- 纯 Node 模块应能在 `node --test` 下用 fake 依赖加载，不依赖 DSH/Cordis 运行时。
