# AGENTS.md — 安装核验

`verify-install.mjs` 只读检查 DSH profile 中 bundle 是否登记、`node_modules` 链接是否指向本仓库 `dsh/`、manifest/Host 入口/client 产物/patch/locale/icon 是否完整。

## 文件

- `verify-install.mjs`：CLI（`--profile-dir`、`--json`）；退出码 0=无 FAIL、1=FAIL、2=用法错误；运行期可见性（页面、工具注入）输出人工检查清单。
- [lib/AGENTS.md](lib/AGENTS.md)：纯函数检查库（供脚本与 `dsh/test/` 共用）。

## 局部规则

- 严格只读：不写文件、不启停进程、不调用 `plugin_manager`、不访问网络。
- 只做结构/资源检查；激活与工具可见性无法脚本化时必须列为人工步骤。
