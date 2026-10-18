# 决策：分析技术栈与解析后端

Status: active
Date: 2026-10-17

## 背景

工具需要在大规模 OS C/C++ 仓库上高速产出高精度语义事实（函数/类型身份、调用目标、源码位置）。开发主机为 macOS arm64，只有 Apple clang 21 且没有 LLVM 开发头文件，无法安装系统包；可用 Python 3.11 与固定版本 pip 依赖。

## 决策

使用 Python 3.11 + 随依赖分发的 libclang 18.1.1（cindex）作为语义解析器，每个 worker 一个进程，并在 Clang 游标之上自建 CFG/数据流分析。记录与模块接口保持与 Clang 无关，便于后续替换更快的后端。

## 原因

libclang 无需宿主 LLVM SDK 就能提供语义 AST（USR、类型、链接性、被引用声明、源码范围），且动态库可作为固定 pip 依赖安装。进程级并行符合 libclang 的每 TU 模型并规避 GIL。本阶段实现与调试成本远低于 C++ LibTooling。

## 考虑过的替代方案

- **C++ LibTooling/Clang 工具**：最快且可直接用 `clang::CFG`；本阶段否决，因为缺少 LLVM 开发头文件、安装需系统权限、构建与集成成本高。阶段 2 若吞吐不达标再评估。
- **仅 tree-sitter**：语法树很快但没有语义解析（typedef、重载、隐式声明），调用/类型精度无法达标。
- **`clang -ast-dump=json`**：无需库即可获得完整语义，但大 TU 的序列化体积与 JSON 后处理开销过大。
- **clangd 索引**：符号索引好，但没有可供抽取的稳定 AST/CFG/数据流 API。

## 后果

- 吞吐受 libclang 解析时间与 Python 逐游标开销限制；通过进程并行、解析/分析共享同一 AST、worker 内重复函数抑制来缓解。
- 宏展开、函数指针与系统头声明存在已知精度边界，必须测量并如实记录。
- 解析器被隔离在 `ca.parse` 之后，可在不影响存储与 CLI 契约的前提下加入 C++/Rust 后端。

## 验证与风险

xv6 端到端索引、TU/s 与 LOC/s 实测、夹具上的分类 precision/recall。主要风险是内核级 TU 上的 Python 开销；阶段 2 用性能剖析与可选原生后端缓解。

## 关联与取代

- 相关架构：[overview.md](../../architecture/overview.zh.md)
- 取代的决策：无
- 被以下决策取代：无
