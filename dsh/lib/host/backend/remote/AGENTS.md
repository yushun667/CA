# AGENTS.md — 远程 Linux 后端

DSH 在 macOS、CA 与索引在 Linux 服务器时的远程后端：探测、安装、远端 `ca serve` + 本地 SSH 隧道、远程 CLI 执行。

## 关键文件

- `index.js`：组合 serve/probe/install，暴露与本地后端相同的 manager 接口与 `getInstallStatus()`。
- `install.js`：幂等安装 probe→source→rsync→venv→dependencies→editable→verify。
- `serve.js`：远端启动/停止（PID 文件）、`ssh -N -L` 隧道、健康检查与有界重启。
- `probe.js`：一次 ssh 往返的环境报告；`exec.js`：远端命令执行；`commands.js`：所有 ssh/rsync/shell 命令构造（纯函数，可单测）。

## 局部规则

- 必须 `pip install -e .`（editable）：`pyproject.toml` 无 package-data，wheel 会丢 `schema.sql` 与 UI 静态资源。
- 不把密码/私钥写入仓库或设置；`remote.ssh.keyPath` 只是本地私钥路径，SSH 参数在错误中脱敏。
- 远程路径必须是绝对路径或 `~/` 开头；`repoPath`（已有 checkout）优先于 `installDir`（rsync 目标）。
- 测试：`dsh/test/backend/remote/` 用 fake executor；真实主机测试按环境变量跳过。
