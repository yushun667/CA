# AGENTS.md — parse（Clang AST 提取）

解析 TU 提取函数/类型/字段/调用与位置，产出 `ParseResult`；活动 `ParsedUnit` 留 worker 内供 analyze 复用。

## 关键文件

- `libclang_parser.py`：`parse_compilation_unit(tu, options=None)`。参数归一：`-working-directory`、源操作数/产物/链接参数过滤、交叉 target/`-mabi`/Darwin sysroot；跨编译器兼容：preflight 学习未知 vendor 参数并记 note、诊断驱动精确剥离、codegen-only 忽略、driver（含相对路径）→`-resource-dir`/libc++/`-stdlib` 注入、逐 cursor 隔离、real→relaxed→compat→host 逐级且环境错误不重复解析。后端可经 `CA_LIBCLANG_LIBRARY` 切版本（OH 用 libclang15，默认 libclang18）。`ParseOptions`：降级/host/宏展开/声明/`record_scope`(all|main_file)/`emit_header_types`/`emit_header_declarations`/`collect_include_files`/preflight；另出 virtual 元数据/include 事实。
- `cursor_utils.py`：libclang 引导与环境后端选择、C API 补充绑定（inline/static method/参数个数/宏展开/bitfield 安全宽度）、`PathMapper`（绝对/相对→仓库相对 POSIX，realpath）、USR/linkage/kind 映射（含模板/`using`）、Sloc。
- `__init__.py`：导出 `parse_compilation_unit`/`ParseOptions`/`ParsedTranslationUnit`。

## 局部规则

- 实体 ID 用 `ca.core.ids`；外部链接 USR，内部链接附加仓库相对文件；无 USR 回退位置。
- 只提取 `repo_root` 下文件；头文件定义记录，跨 TU 去重由 store 负责；TU 内按 id 去重（定义优先）。
- 调用点 `caller_id` 等于对应 `FunctionRecord.id`（遍历上下文确定）；`callee_id` 同规则。
- 解析失败不中断整批：`parse_ok=False`+`error` 保留记录；活动 TU/游标只留 `ParsedTranslationUnit`，`parse_result()` 可 pickle。
- 测试：`.venv/bin/python -m pytest tests/test_parse_*.py -q`；夹具 `tests/fixtures/ast_*`；header 声明见 `test_parse_header_decls.py`；C++/fnptr/OHOS/后端见 `test_parse_cpp_basic.py`、`test_parse_fnptr.py`、`test_parse_ohos_compat.py`、`test_parse_libclang_backend.py`。
