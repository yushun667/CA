# 决策：OpenHarmony 优先的阶段 3 接入与执行策略

Status: active
Date: 2026-10-18

## 背景

工具使用者包括 debug、coding、测试生成三类 AI agent 以及人类程序员（UI 最后）。
源码树最终要覆盖 OpenHarmony、Yocto、Android、Ubuntu；用户决定先做 OpenHarmony
当前版本，且只做 C/C++。OpenHarmony 树在 Linux 服务器 `yu@192.168.0.100`
（`~/OH_6.1/src`，500 个 repo project，产品 opi5plus/板 rk3588），原树必须保持
只读。用户要求离线全量 + 实时增量（不做实时全量解析），CA 跑在源码机上，通过
MCP 查询。

## 决策

- 在 `/home/yu/CA_OH/openharmony` 使用镜像工作副本（rsync 原树源码，排除构建
  输出与 repo object store），绝不修改 `~/OH_6.1/src`。
- 从 OpenHarmony GN 构建图（`out/rk3588/build.ninja`）离线生成真实编译参数：
  `ninja -t compdb` / GN 导出；生成语料不要求全量编译。宿主环境无法 GN 生成时
  回退现有 Docker builder 镜像。
- 阶段 3 只索引 C/C++；其他语言与其他 OS 树放到后续阶段。
- 两种模式分离：离线批处理构建全量基线快照（开发用仓库子集验证，验收用全量
  C/C++ 语料）；实时增量路径只重解析变更 TU。
- CA 运行在源码机用户级 Python 3.11 环境；源码与索引库留在服务器，通过内网/SSH
  的 MCP 暴露查询。人类 UI 放到后续阶段。

## 原因

内核规模阶段已证明单 TU 解析/分析是成本主导，全树实时解析不可行；离线快照 +
按 TU 增量可在不重解析全树的前提下提供交互式查询。源码机具备构建环境，避免把
100 GB 级源码树跨网络搬运。GN→compdb 无需长时间全量编译即可得到真实 flags。

## 考虑过的替代方案

- **在 macOS 工作站建索引**：需要复制/流式传输源码且缺少 OpenHarmony 构建工具
  链，否决。
- **实时全量解析**：用户明确否决，且规模不可行。
- **为拿 flags 构建完整 OpenHarmony 镜像**：没有必要；GN 图生成即可得到相同命令。
- **先做其他 OS 树或语言**：按用户优先级延后。

## 后果

- 语料生成依赖工作副本中的 GN（宿主或 Docker）；失败时如实报告，不合成 flags。
- 全量验收索引是服务器上的长时离线任务；开发子集保持快速迭代。
- MCP 仅在用户内网/SSH 路径暴露；认证是后续工作。
- 回调/函数指针边是带置信度的候选边，不是可靠的指向分析结果。

## 验证与风险

验收：当前 OpenHarmony 快照的真实 C/C++ compdb；抽样解析成功率 ≥95%；开发子集
离线索引可通过 MCP 查询；全量 C/C++ 验收批处理完成；调用图与候选边在标注夹具上
验证。风险：GN 环境损坏、全量语料的磁盘/时间增长、MCP 网络暴露。

## 关联与取代

- 相关架构：[overview.zh.md](../../architecture/overview.zh.md)
- 相关决策：[编译参数来源策略](2026-10-17-compile-command-policy.zh.md)、
  [增量索引](2026-10-17-incremental-indexing.zh.md)、
  [FTS5 搜索](2026-10-17-fts5-symbol-search.zh.md)
- 取代的决策：无
- 被以下决策取代：无
