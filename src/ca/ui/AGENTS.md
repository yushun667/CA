# AGENTS.md — ui（人类只读 Web UI）

`ca ui` 的本地只读浏览器界面：标准库 `http.server` 提供 JSON API，`static/` 内为无构建步骤的 vanilla HTML/CSS/JS。数据来自 `ca.store` 查询 API（单库）与 `ca.store.federated.FederatedStore`（多 shard）；选择器展开复用 `ca.cli.dbsource.resolve_paths`。

## 关键文件

- `server.py`：`make_server()`/`serve()`/`cmd_ui()`；端点路由（含只读源码窗口 `/api/source` 与 CFG `/api/cfg`）、统一 `{ok,data,error}` 信封、静态白名单、SIGINT/SIGTERM 优雅退出。
- `static/`：单页前端（`index.html`/`i18n.js`/`highlight.js`/`app.js`/`style.css`/`favicon.svg`，中英双语）；零 CDN、零外网请求。修改规则见 [static/AGENTS.md](static/AGENTS.md)。
- `../../tests/test_ui.py`：线程内随机端口起 server + 临时 DB 的 API/静态页/只读/联邦断言。

## 只读与安全边界

- 单库用 `PRAGMA query_only=ON` 连接；联邦用现有只读 `FederatedStore`。不复用 `open_query_store`（其单库分支 `SqliteStore.open()` 会建表/写 meta）。
- 无写端点：非 GET 一律 405；默认只绑 `127.0.0.1`，无认证，不做远程部署。
- 静态文件仅按白名单读取；请求路径不直接拼文件系统。

## 局部规则

- 改 API 或页面行为同步 `docs/ui.md`/`.zh.md`/`.i18n.yaml` 与 `docs/usage.*` 入口。
- 前端不引入构建工具或外部依赖；所有数据文本必须转义后再插入 DOM。
- 测试：`.venv/bin/python -m pytest tests/test_ui.py -q`；手工验证 `ca ui --db <db>`。
