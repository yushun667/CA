# AGENTS.md — 远程后端测试

覆盖 `lib/host/backend/remote/`：所有 SSH/rsync 命令构造用 fake executor 断言 argv 与解析，不连真实主机。

## 文件

- `commands.test.mjs`：命令构造/引号/ssh 参数归一化。
- `probe.test.mjs`、`install.test.mjs`、`serve.test.mjs`、`remote-backend.test.mjs`：探测、幂等安装步骤、远端 serve+隧道状态机、组合接口。
- `real-remote.test.mjs`：真实主机用例，按环境变量跳过。

## 规则

- 测试里不出现真实凭据；断言错误信息已脱敏。
- 不绕过 `commands.js` 直接拼 shell 字符串。
