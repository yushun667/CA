# AGENTS.md — 跨平台 CA 安装器

在本地主机（Windows/macOS/Linux）或远程 Linux 上安装 CA 后端：解析内置 CA wheel 与可选离线 wheelhouse，创建 venv、`pip install`、`ca version` 校验。幂等，不编译、不写 DSH profile。

## 关键文件

- `platform.js`：平台 id（`HOST_PLATFORM_IDS`，与 `dsh/scripts/build-wheelhouse.mjs` 对齐）、Python >= 3.11 候选探测、venv 布局（Windows `Scripts/`、POSIX `bin/`）、内置 wheel（`BUNDLED_WHEEL_DIR`）与 wheelhouse 解析、平台修复提示。
- `local.js`：本地安装（步骤 `environment|wheel|venv|dependencies|verify`）；在线 PyPI、离线 `--no-index --find-links`；已有可用 venv 复用（`skipped`），损坏则修复/重建。
- `remote-linux.js`：远程 Linux wheel 模式（步骤 `probe|python|upload-wheel|upload-wheelhouse|venv|pip|verify`）；上传优先 `scp`，回退 `ssh stdin`/tar 流；`mode:'source'` 转交 `../remote/install.js`（legacy rsync + editable）。

## 局部规则

- 只依赖 Node 内置模块；子进程一律经注入的 `runProcess`/transfer，测试不启动真实 Python/pip/ssh。
- 默认安装目录：本地 `<stateDir>/backends/local`，远程 `~/dsh-ca/backend`；wheelhouse 默认 `<stateDir>/wheelhouse/<platform>`。
- 错误归一为 `BackendError`（`UNSUPPORTED_PYTHON`、`INSTALL_STEP_FAILED` 等）；不得保存或回显凭据。
- 改安装/上传行为同步 `dsh/test/backend/installer*.test.mjs` 与 `dsh/CONTRACT.md` 的 Installer 段。
