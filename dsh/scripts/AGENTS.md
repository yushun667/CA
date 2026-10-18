# AGENTS.md — 构建与安装核验脚本

本目录是插件自带的开发/验证脚本（仅 Node 内置模块，无 npm 依赖）。

## 文件

- `build-wheelhouse.mjs`：为指定平台构建离线 wheelhouse（CA wheel + 依赖 wheel），支持 linux-x64/linux-arm64/macos-*/windows-x64 与 `--tar`。
- `build-client.mjs`：无依赖 ESM 打包器，读 `lib/client/index.js` 生成 `client.js`（`window.__ModuleLoader__` 格式）；不支持 JSX/CSS import/动态 import，遇到不支持语法时报错并给出文件与语句。
- [verify/AGENTS.md](verify/AGENTS.md)：安装核验脚本（只读）。

## 局部规则

- `client.js` 是产物，必须由本脚本生成并提交；不要手改。
- 修改打包器支持面需同步 `dsh/test/client-bundle.test.mjs` 与 `dsh/test/client-source.test.mjs`。
- 脚本不写 DSH profile、不调用 `plugin_manager`。
