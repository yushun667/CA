# AGENTS.md — 后端测试替身

`fakes.mjs`：backend 测试共享的 fake（spawn/fetch/时钟/临时目录等），让测试不接触真实进程与网络。

## 规则

- 只做测试替身，不放产品逻辑；接口形状必须与 `dsh/CONTRACT.md` 的 BackendManager/HTTP 契约一致。
- 被 `dsh/test/backend/*.test.mjs` 引用；修改后运行 `cd dsh && node --test`。
