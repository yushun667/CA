# AGENTS.md — RPC 通道测试

覆盖 `lib/host/rpc/index.js`：用 fake backend/tasks/explain 验证端点路由、参数校验、错误 envelope 与 HTTP 状态码。

## 文件

- `rpc.test.mjs`：端点分发、参数/大小校验、CA 路径白名单、错误码。
- `transport-http.test.mjs`：`webServer.register` 路由、`requestRejection` 鉴权、POST/405/404/413/401 状态、body 限额。
- `helpers.mjs`：fake ctx/services 与请求构造。

## 规则

- 不连真实 CA/DSH；断言 channel `/dsh-ca` 与 `RPC_ENDPOINTS` 同步。
- 改动端点或状态码时同步 `dsh/CONTRACT.md` 与 `lib/client/api.js`。
