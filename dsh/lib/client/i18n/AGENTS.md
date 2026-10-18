# AGENTS.md — Client i18n

`createI18n(ctx)` 注册 `ca` 命名空间并绑定 DSH locale 服务（语言选择由 Host 持久化）；`t(key, vars?, fallback?)` 依次回退 active → en → fallback → key。

## 关键文件

- `index.js`：facade、语言订阅/切换、`{name}` 插值与缺失 key 回退。
- `en.js`、`zh.js`：命名空间字典（键必须成对维护）。

## 局部规则

- 新增 key 同时补 en 与 zh；术语与 CA CLI/MCP 输出一致。
- 视图的默认英文 key 由各视图自带（如 `views/function/i18n-keys.js`），shell 字典是该契约的实现方。
- 不直接读写 localStorage 语言；语言状态属于 DSH locale 服务。
