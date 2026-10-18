# AGENTS.md — 远程 Linux 后端

远程主机上的 CA 安装与 `ca serve` 生命周期：SSH 认证、探测、安装/执行、本地隧道。

## 文件

- `auth.js`：SSH 认证解析（user@host/端口/私钥/agent/密码）与执行环境；密码只经临时 0600 `SSH_ASKPASS` helper，不落 argv/日志/错误/settings；Windows 密码模式拒绝。
- `commands.js`：probe/venv/pip/verify/rsync/scp/tar/serve/隧道脚本构造（纯函数）。
- `exec.js`：ssh/scp/本地进程执行与错误分类；`probe.js`：一次 ssh 探测报告。
- `serve.js`：远端 `ca serve` + 本地 SSH 隧道生命周期；`install.js`：远程 wheel/source 安装；`databases.js`：远端 DB 候选；`index.js`：远程后端门面。

## 局部规则

- 目标平台只支持 Linux；远端路径必须绝对或以 `~/` 开头；迭代目录时跳过隐藏项、限制条目数。
- 认证/网络错误归一为稳定 `BackendError` code；密码类凭据只存在于进程内存，响应与日志必须脱敏。
