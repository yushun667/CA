# AGENTS.md — 后端安装产物

插件安装 CA 后端所需的随包产物（与 `lib/host/backend/installer/` 配合）。

## 子目录

- [wheels/AGENTS.md](wheels/AGENTS.md)：内置的 CA wheel，供本地/远程安装直接 pip 安装。

## 局部规则

- wheel 由仓库源码构建（`pip wheel . --no-deps`），改了 Python 包行为或 `schema.sql`/UI 静态资源后必须重新构建并替换，保持版本号与 `pyproject.toml` 一致。
- 不在此目录放大体积 wheelhouse；依赖 wheelhouse 由 `dsh/scripts/build-wheelhouse.mjs` 生成到 `artifacts/`（被 Git 忽略）。
