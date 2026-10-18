# AGENTS.md — 测试夹具

保存手工编写的小型 C/C++ 程序、期望 JSON（golden）与可审计标注；不放生成的大文件。

## 命名与所有权

- `ast_*`：parse 所有者维护，验证实体、类型、调用与位置。
- `df_*`：analyze 所有者维护，验证分支、赋值、返回路径与资源操作。
- `golden_*`：e2e/精度所有者维护，端到端期望结果。
- `build_*`：build 所有者维护，编译参数样例。

## 子目录导航

- [ast_include/AGENTS.md](ast_include/AGENTS.md)：parse：include 解析夹具；写入该组前先读。
- [ast_static_a/AGENTS.md](ast_static_a/AGENTS.md)：parse：同名 static 函数 ID（A）；写入该组前先读。
- [ast_static_b/AGENTS.md](ast_static_b/AGENTS.md)：parse：同名 static 函数 ID（B）；写入该组前先读。
- [build_kernel/AGENTS.md](build_kernel/AGENTS.md)：build：内核 `.cmd` 重建夹具；写入该组前先读。
- [build_make_proj/AGENTS.md](build_make_proj/AGENTS.md)：build：make 包装器俘获夹具；写入该组前先读。

## 局部规则

- 夹具要能独立说明预期：文件名、注释或配套说明写清构造意图与已知边界。
- 期望 JSON 使用 `ca.core.schema` 的 kind 与字段；契约变更时同步。
- 夹具保持 KB 级；需要真实大仓时用 `.tmp/corpus`，基准脚本记录来源与版本。
