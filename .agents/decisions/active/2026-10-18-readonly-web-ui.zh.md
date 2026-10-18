# 决策：本地只读 Web UI

Status: active
Date: 2026-10-18

## 背景

工具用户包括人类程序员；约定人类 UI 排在 Agent 接口之后。CA 已有只读 store、
分片联邦查询、CLI 与 MCP，运行在源码机上。UI 必须不引入构建工具链、服务器依赖
或写入路径，且不能把源码暴露到本机/局域网之外。

## 决策

- 提供 `ca ui --db DB` / `ca ui --db-dir DIR [--host 127.0.0.1 --port 8766]`，
  基于 Python 标准库 `ThreadingHTTPServer` 的本地只读 Web UI。
- 前端为 `src/ca/ui/static` 下 vanilla HTML/CSS/JS 单页；无 CDN/字体/外网请求，
  无构建步骤。提供符号搜索、函数详情（overview/callers/callees/branches/
  returns/resources/assignments/findings）、SVG 调用图（候选边虚线+confidence）、
  回调、stats 与 shard 徽章。
- 服务端暴露稳定 JSON API（health/search/function/callers/callees/graph/stats/
  callbacks/事实列表），统一 `{ok,data}` / `{ok:false,error}` 信封；非 GET 返回
  405；静态文件白名单 + self-only CSP。单库使用 query-only 连接；联邦复用
  `FederatedStore`。
- 默认仅绑定回环；远程查看用 SSH 隧道。不实现认证或写操作。

## 备选

- 桌面/Electron 或 Sourcetrail 式原生 UI：依赖与构建链重，放弃。
- React/Vite SPA：为只读查看引入 Node 工具链与打包，放弃。
- 带认证的远程 Web 服务：对回环工具无必要且扩大安全面，放弃。
- 仅 Notebook/HTML 报告：导航与交互图弱，放弃。

## 后果

- 零新增 Python 依赖；UI 随包发布（wheel 需为 `ca.ui/static` 增加 package-data）。
- 每请求打开只读 SQLite 连接；这是交互式检查工具，不是高吞吐服务。
- 无认证：不得绑到回环之外；远程用隧道。
- UI 改动同样需要测试/审查门禁；Agent 沙箱无法截图，因此以 HTTP/静态测试为门禁。

## 验证

`tests/test_ui.py` 覆盖 health/search/detail/graph/stats/callbacks/static/只读/
联邦/CLI wiring（20 项）；全仓 357 passed / 3 skipped；手工 xv6 单库与双 shard
curl 冒烟、POST 405、SIGTERM exit 0、端口冲突 exit 1。
