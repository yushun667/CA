# 决策：把 CA 作为 DSH 插件交付（`@local/dsh-ca`）

Status: active
Date: 2026-10-18

## 背景

CA 现有 CLI、只读 Web UI 与无状态 MCP 端点；DSH 用户希望在 Harness GUI 内直接使用 CA
的索引、事实、图与索引任务，并获得 AI 解释。两个既有约束决定设计：已移除的 AI 解释
功能确立 CA 不得持有 LLM 凭据/provider 配置，并说明未来解释应放 agent 侧；CA 没有
插件/扩展点，把 agent 功能加进 `src/ca/` 会让分析器绑定单一宿主。

## 决策

把 CA 能力作为独立的 DSH bundle `dsh/`（`@local/dsh-ca`）交付，位于 Python 包之外：

- Host 半：BackendManager（项目存储 + 本地/远程 `ca serve` 生命周期）、`/dsh-ca`
  同源 HTTP 通道、12 个原生 `ca_*` 工具、系统提示词 section、CA CLI 长任务管理器与
  explain 服务。
- **项目模型替代扫描主流程**：用户在专门页面创建具名项目（一份源码 + 一个数据库/
  分片目录 + 一个后端位置）并激活其一；激活会配置并重启后端。目录扫描
  （`backend.databases`）只保留为低层辅助。理由：CA 是通用分析器，只有用户知道数据库
  属于哪份源码，扫描无法推断这种绑定，而且显式项目与 DSH 自身的项目使用方式一致。
- Client 半：DSH `main` slot 的整页 CA 页面 + `sidebar.panellist` 侧栏项，含
  search/projects/function/graph/stats/callbacks/tasks/backend 页面与 AI 面板；顶栏
  是项目选择器。
- AI 解释由 DSH 会话原生模型（`agentDefaultModel` + `ctx.llm`）基于有界只读 CA 证据
  生成；CA 不持有 LLM 配置，也没有 explain 端点/工具。
- Client↔Host 用 DSH 同源 HTTP 路由（`ctx.webServer.register` 前缀 `/dsh-ca`），由
  `ctx.connection.requestRejection` 鉴权；因为 `ctx.connection.rpc.handle` 在
  plain-JS 插件中激活失败（`cannot get property "webServer" without inject`）。
- 远程 Linux 后端：probe → checkout/rsync → venv → `pip install -e .`（必须 editable：
  wheel 会丢 `schema.sql`/UI 资源）→ 远端 `ca serve` + 本地 SSH 隧道。
- 不修改 CA `src/`、CLI、网页端与 MCP。

## 原因

- CA 保持与宿主无关：复用 `ca serve` `/api/*` 与 `ca` CLI，并按此前决策保持 CA 无 LLM。
- DSH 已提供页面、React slots、原生 tools、模型服务与提示词；在 CA 里重造会把 agent
  关注点塞进分析器。
- 独立 bundle 可按 profile 安装/移除、独立版本化。
- 本地/远程属于 GUI 部署问题，不是 `ca serve` 的职责。
- 显式项目让查询可复现：每个项目固定一份源码/数据库/后端三元组，查询不会悄悄命中
  错误的索引。

## 考虑过的替代方案

- **扩展 `src/ca/` 增加插件/UI 宿主与 explain**：否——没有扩展点，CA 必须保持无 LLM，
  且会把分析器耦合到 DSH。
- **在 CA 内配置 LLM 做 explain**：已在
  [remove-ai-explain](2026-10-18-remove-ai-explain.zh.md) 否决（凭据/provider 归属）。
- **用 `ctx.connection.rpc.handle` 做通道**：实测否决——激活失败
  （`cannot get property "webServer" without inject`）。
- **iframe 嵌入现有 `ca ui` 页面**：否——跨源、隧道与主题/鉴权集成都要重复实现，且仍
  缺原生 tools/slots。
- **保留扫描式数据库发现作为主流程**：否——扫描只能列出数据库，无法知道其源码树与
  后端；用户要求显式、可编辑的项目。
- **只用 MCP 集成**：否——没有 DSH 页面、模型与生命周期。

## 后果

- 一个 DSH profile 获得项目 + 页面 + 工具 + AI，CA 零改动；`ca ui`/`ca mcp`/CLI 继续
  可用，CA 测试基线不受影响。
- 插件依赖 DSH 内部服务（`webServer.register`、`connection.requestRejection`、
  `slots`、`llm`、`agentDefaultModel`）；宿主漂移需重新验证。
- 远程使用需要 Linux + SSH/rsync/venv 与 editable CA checkout；CA 后端仍仅 loopback
  且无鉴权，SSH 隧道是安全边界。
- `dsh/client.js` 是生成产物：Client 改动需重建，Host 改动需重启 Harness 或重激活插件。
- 旧 settings 会迁移为一个默认项目，用户应复核。
- 部分取代 `remove-ai-explain.md` 中“未来需要新设计”的风险说明。

## 验证与风险

治理校验、`dsh/` node:test 套件与 CA Python 套件（基线 497 passed / 4 skipped）均
通过；真实冒烟步骤见 [docs/dsh-plugin](../../../docs/dsh-plugin.zh.md)。风险：
DSH API 漂移、远程 SSH 环境差异、无浏览器自动化下的 UI 回归、loopback 信任模型、
项目路径失效（激活时会重新校验）。

## 关联与取代

- 相关架构：[dsh-plugin](../../architecture/dsh-plugin.zh.md)。
- 部分取代：[remove-ai-explain](2026-10-18-remove-ai-explain.zh.md)（仅其未来设计
  说明；该决策仍有效，因为 CA 仍无 explain）。
- 被以下取代：无。
