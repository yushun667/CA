# AGENTS.md — 决策

本目录记录重要选择的理由和取代关系，不保存普通实现过程。

- [proposed/AGENTS.md](proposed/AGENTS.md)：读取待评估的决策及验收标准。
- [rejected/AGENTS.md](rejected/AGENTS.md)：读取被否决方案及仍适用的理由。
- [active/AGENTS.md](active/AGENTS.md)：读取仍然有效的决策；新决策在此创建。
- [superseded/AGENTS.md](superseded/AGENTS.md)：仅在追溯历史或防止旧方案回流时读取。
- [TEMPLATE.md](TEMPLATE.md)：写决策前读取并复制结构。

行为、架构、契约、流程或测试策略的非平凡改动必须在同一逻辑改动记录理由和实际备选；机械局部修改豁免。每篇新决策先检查取代关系；完全取代建立双向链接，部分取代保留两篇并说明边界。同一决定更新事实；改变决定写新记录。

遵守[双语契约](../i18n.md)。

文件名使用 `yyyy-mm-dd-short-title.md`。一个决定一个文件；被取代时移动旧文件并建立双向链接，不删除历史。
