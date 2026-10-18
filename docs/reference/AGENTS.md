# AGENTS.md — 参考文档

面向使用者/维护者的项目参考文档（评估、接口与可复现结论）；按主题分文件维护，新增子目录或文档时同步本索引。

## 文件

- [agent-eval.md](agent-eval.md)：Agent 效用评估（源码 vs CA 基础/深度事实）的 xv6 pilot 参考结论、方法、失败案例与复现入口；对应 [agent-eval.zh.md](agent-eval.zh.md)，一致性记录 [agent-eval.i18n.yaml](agent-eval.i18n.yaml)。
- 完整逐任务报告与原始数据在 [benchmarks/agent_eval/](../../benchmarks/agent_eval/README.md)，本目录只保留结论级事实。

## 局部规则

- 按 [.agents/i18n.md](../../.agents/i18n.md) 维护双语三件套；修改一边同步另一边并更新哈希。
- 一个事实一个家：细节留在 benchmarks/agent_eval/REPORT.md，这里只概述并链接。
