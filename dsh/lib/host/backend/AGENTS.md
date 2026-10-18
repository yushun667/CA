# AGENTS.md — CA 后端生命周期

`createBackendManager(ctx, config)` 统一管理本地或远程 `ca serve` 后端：配置、状态、HTTP 请求、CLI 执行、日志。上层（RPC/tools/tasks/explain）只依赖它，不直接起进程。

## 关键文件

- `manager.js`：单一入口；解析设置、路由 local/remote、项目存储与安装状态、统一 status/log 事件流。
- `projects.js`：项目定义校验/迁移/映射为后端配置（产品主模型，契约见 `../../CONTRACT.md`）。
- `local.js`：本地 `ca serve` 启动/就绪/健康检查/崩溃重启/停止。
- `localbrowse.js`：本地目录浏览（项目页“选择目录”弹窗，只返回子目录）。
- `discover.js`：CA 仓库、Python 解释器、DB 发现与探测报告。
- `http.js`：CA `/api/*` 客户端（超时/取消）；`process.js`：子进程与行解码；`log.js`：环形+文件日志。
- `ca-protocol.js`：就绪行正则与版本解析（local/remote 共用）；`errors.js`：稳定错误码 `BackendError`；`util.js`：纯函数工具；`index.js`：汇总导出。

## 子目录

- [installer/AGENTS.md](installer/AGENTS.md)：本地/远程 Linux 的 CA wheel 安装器与离线 wheelhouse。
- [remote/AGENTS.md](remote/AGENTS.md)：远程 Linux 后端（SSH 认证/密码 askpass/目录浏览/probe/serve+隧 道/安装执行）；一键项目所需的 `remote.connect`/`remote.browse` 由 manager 暴露。

## 局部规则

- CA 后端只监听 loopback 且无鉴权：不得绑非回环地址，不得在日志/错误里泄露凭据；远程只走 SSH 隧道。
- 错误跨层一律归一为 `BackendError{code,message,details}`；应用级错误保留 CA envelope。
- 设置只写 `$DSH_HOME/plugin-data/dsh-ca/settings.json`（0600，稀疏 patch），不写 DSH profile。
- 测试：`dsh/test/backend/` 用 fake 进程/HTTP；真实启动仅在显式测试中。
