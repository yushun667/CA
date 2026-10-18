# 决策：通过 Apple container 构建 Linux 内核语料

Status: active
Date: 2026-10-17

## 背景

阶段 2 需要真实 Linux 内核编译数据库，以在内核规模上验证解析与分析。开发主机是 macOS arm64 且无 sudo；kbuild 无法在 macOS 上原生构建 Linux 内核（宿主工具与 ELF 链接器假设不满足）。Apple `container` CLI 已安装并验证：Ubuntu 24.04 guest、Linux 6.18 aarch64、virtiofs bind mount、CPU/内存限制可用，且无需宿主 sudo。本仓库 CI 尚未配置。

## 决策

在固定版本的 Ubuntu 容器镜像中通过 Apple container 构建固定版本 Linux 内核，原生 arm64 + `LLVM=1`（clang/lld）；用内核自带的 `scripts/clang-tools/gen_compile_commands.py` 从构建树生成编译数据库；语料放在 `.tmp/corpus/linux/`（不提交），并在 metadata 中记录内核 tag/commit、镜像 digest、配置与构建命令。可复现编排实现在 `ca.build.kernel_corpus`；容器运行时不可用时显式报错，绝不把合成内核参数当成真实数据库。

## 原因

这是本机获得真实内核编译数据库（真实生成头文件、配置与宏）的唯一途径，而这些输入决定解析精度。容器已安装，对项目而言无需 root；virtiofs bind mount 让宿主分析器可直接读取构建树。固定内核与镜像保证语料可复现。

## 考虑过的替代方案

- **Linux CI 构建**：长期正确的可复现路径，但需要仓库/CI 配置且迭代慢；保留为阶段 3 选项。
- **在 macOS 上交叉构建**：kbuild 宿主假设与 ELF 工具链缺失，否决。
- **合成内核参数**：生成头文件/配置使内核精度不可用，否决且明确禁止作为替代。
- **QEMU/Lima/UTM**：需要额外安装，而 Apple container 已可用，否决。

## 后果

- 语料仅 arm64；x86_64 内核需要另一套镜像/配置。
- 内核构建消耗 `.tmp` 下的时间与磁盘；记录镜像/内核固定点以便复现。
- 内核编译数据库向解析器与分析器暴露真实的宏/配置复杂度，这正是目的；失败按 TU 记录。

## 验证与风险

验收：≥300 个带真实内核 flags 的 TU、抽样子集解析成功率 ≥95%、内核子集索引有实测数字。风险：容器/镜像可用性、apt/内核源码网络、构建时间长；均已记录，可在其他主机重建语料。

## 关联与取代

- 相关架构：[overview.md](../../architecture/overview.zh.md)
- 相关决策：[分析技术栈](2026-10-17-analysis-stack.zh.md)、[编译参数来源策略](2026-10-17-compile-command-policy.zh.md)
- 取代的决策：无
- 被以下决策取代：无
