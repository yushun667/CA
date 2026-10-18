# AGENTS.md — Client↔Host 通道（`/dsh-ca`）

`registerRpc(ctx, {backend,tasks,explain,config})` 用 `ctx.webServer.register({kind:'prefix',path:'/dsh-ca',handler})` 注册同源 HTTP 路由；**不要**用 `ctx.connection.rpc.handle`（plain-JS 插件激活报 `cannot get property "webServer" without inject`，见决策 `.agents/decisions/active/2026-10-18-dsh-plugin.md`）。

## 关键文件

- `index.js`：端点清单 `RPC_ENDPOINTS`（`ping`、`status`、`ca.request`、`backend.*`、`tasks.*`、`explain`）、payload 校验/大小上限、envelope、鉴权与状态码。

## 局部规则

- 鉴权：处理前调用 `ctx.connection.requestRejection(req)`，非空回 401/403；非 POST 405；未知端点 404；body 过大 413。
- 响应统一 `{ok:true,value}` / `{ok:false,error:{code,message,details}}`（业务错误 HTTP 200）；handler 不得抛未捕获异常。
- `ca.request` 只允许以 `/api/` 开头的路径，转给 BackendManager；限制 query/body 大小。
- 端点名与 Client（`lib/client/api.js`）和 [../../../CONTRACT.md](../../../CONTRACT.md) 保持同步。
- 测试：`dsh/test/rpc/`（fake services + HTTP 传输）。
