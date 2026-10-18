# AGENTS.md — 内置 CA wheel

`ca_analyzer-<version>-py3-none-any.whl`：从本仓库源码构建的可安装发行包，包含 `ca/store/schema.sql` 与 `ca/ui/static/*`（pyproject 的 package-data）。

## 局部规则

- 构建：在仓库根运行 `.venv/bin/python -m pip wheel . --no-deps -w dsh/backend/wheels/`，随后删除旧版本 wheel。
- 校验：`unzip -l` 确认包含 schema.sql 与 static 资源；`node dsh/test/wheelhouse.test.mjs` 断言 wheel 存在。
- 不做手改；二进制文件随 Git 跟踪（约 360 KB）。
