# AGENTS.md — AI 解释测试

覆盖 `lib/host/explain/` 与 `lib/client/views/explain/`。测试只读：不启动真实 CA
进程、不调用真实模型、不访问网络。

## 文件

- `host-explain.test.mjs`：fake BackendManager 与 `ctx.llm`，断言事实端点/参数、证据编号与预算、text-delta 聚合、finish error/aborted、abort/超时、并发上限、错误码。
- `client-explain.test.mjs`：纯 markdown 解析器 + fake React（hook stand-in），断言 RPC payload、安全渲染、错误重试、复制反馈、无原始 HTML 注入。

## 规则

- 只 mock、不写工作区；fake 对象要与 `dsh/CONTRACT.md` 的接口形状一致。
- 运行：`cd dsh && node --test`（Node 24/26 不要用 `node --test test/`）。
