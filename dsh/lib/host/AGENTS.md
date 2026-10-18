# AGENTS.md — Host 半

Cordis 插件服务端：组装 backend/tasks/explain 服务，向 DSH 注册 `/dsh-ca` 通道、11 个原生 tools 与系统提示词；不修改 CA。

## 关键文件

- `bootstrap.js`：Host 入口（`inject=['tools','connection','webServer']`），用带时间戳的动态 import 加载 `impl.js`；重激活插件即可热重载实现，改本文件或 `inject` 需重启 DSH。
- `impl.js`：Lead 维护的组装入口（服务创建与注册顺序），接口见 [../../CONTRACT.md](../../CONTRACT.md)。
- `config.js`：默认值、校验、`$DSH_HOME/plugin-data/dsh-ca/settings.json` 稀疏持久化。
- `prompt.js`：系统提示词 section（何时用 `ca_*`，可选服务缺失时 no-op）。

## 子目录

- [backend/AGENTS.md](backend/AGENTS.md)：本地/远程 CA 后端生命周期与 HTTP 客户端。
- [rpc/AGENTS.md](rpc/AGENTS.md)：`/dsh-ca` 同源 HTTP 通道与端点分发。
- [tools/AGENTS.md](tools/AGENTS.md)：11 个模型可见工具与输出格式化。
- [tasks/AGENTS.md](tasks/AGENTS.md)：CA CLI 长任务队列。
- [explain/AGENTS.md](explain/AGENTS.md)：DSH 原生模型驱动的 AI 解释。

## 读取条件与规则

- 改插件行为、注册顺序或 DSH 服务集成时读本目录及对应子目录；先读 `CONTRACT.md`。
- 所有注册包在 `ctx.effect(...)` 内并返回 disposer；可选服务用 `ctx.get(...)`，缺失时降级而不是让插件激活失败。
