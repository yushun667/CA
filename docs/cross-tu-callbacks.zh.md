# 跨 TU 回调与函数指针

同 TU 回调解析（`ca.analyze.callbacks`）只能看到单个编译单元内的注册与调用，
因此“一个文件注册分发表、另一个文件发起调用”的模式无法解析（xv6 `devsw`、
OpenHarmony listener/ops 表、`file_operations` 类表）。本文描述跨 TU 注册事实
模型、解析 pass、逻辑去重口径与当前限制。正确性断言见
[tests/test_analyze_crosstu.py](../tests/test_analyze_crosstu.py) 与
`tests/fixtures/ast_crosstu_*.c` 夹具。

## 数据流

1. **逐 TU 提取**（`ca.analyze.registrations.extract_registrations`）：每个 TU
   产出注册事实。事实记录 `object_path`（如 `g_connManagerCb.OnDataReceived`、
   `devsw[CONSOLE].read`）、目标函数 id/name/USR、注册点
   `(tu_id, file, line, col)`、evidence 文本与 `registration_kind`：
   `global_assign`/`struct_field`/`array_element`/`designated_initializer`/
   `registration_call`/`argument_pass`/`return_pointer`。**只记录全局存储**的
   注册；写入局部测试结构体的赋值被有意忽略，从而消除“同名字段不同实例”的误报
   族。同一 pass 还产出中立的间接调用点（callee 路径、函数指针签名、调用者身份）。
2. **持久化**（store schema v4）：事实写入 `callback_registrations`；候选边是普通
   `call_targets` 行（`kind="indirect_candidate"`），并通过
   `callback_target_links(registration_id, call_target_id)` 回链到产生该候选的
   注册证据。
3. **解析**（`resolve_cross_tu_targets`）：用所有 TU 的事实匹配间接调用点；缺失
   目标函数签名时保守丢弃候选，而不是输出未校验的边。

## 匹配规则

匹配表示调用点**可能目标集合**的成员，而不是分数：resolver 保留所有签名兼容
的候选，并把证据写进 `reason`。候选按调用点（`max_candidates_per_call`）与函数
（`max_targets`）有界；没有置信度阈值或按置信度排序。

| 证据 | 写入 `reason` 的内容 |
|---|---|
| 全局路径精确匹配（无动态 qualifier） | `g_ops_a.fn = impl_a at file:line` |
| 同 base + 完整字段链，常量 qualifier 相同 | 注册路径 + qualifier 类型 |
| 调用侧动态下标 vs 注册侧常量下标（表分发） | 注册路径 + qualifier=`dynamic` |
| 跨 TU 参数传递回调 | 参数下标 + `file:line` |
| getter 返回的函数指针 | getter 名 + `file:line` |
| 同 base+字段链且 wildcard qualifier 未定 | 注册路径 + qualifier 类型 |

base 符号与完整字段链必须一致；不产生跨 base 的“字段名后缀”匹配。这是
qualifier-aware 规则：`g_ops_a.fn` 不会匹配 `g_ops_b.fn` 下的注册。

## 逻辑去重

header 内联函数会在多个 TU 中实例化，同一逻辑边可能按 TU 重复报告。因此
resolver 默认按 `(caller_USR 或 caller_id, callee_USR 或 callee_id, 调用点 file,
line)` 去重（`dedupe=True`；传 `dedupe=False` 可查看原始 per-call-site 边）。
保留一条确定性代表（按 natural key 顺序，不按分数排序）；重复 reason 合并进其
`reason`（`; also ...`），所有匹配的 registration id 都回链到保留的
`call_target_id`。验收同时报告原始 per-call-site 数量与去重后逻辑数量；覆盖率
口径使用去重后的逻辑边数。

## 验证

- 夹具：10 条期望跨 TU 边，precision/recall = 1.0，覆盖全部注册 kind、
  qualifier 负例与签名过滤。
- xv6-riscv 全语料：新增 `fileread -> consoleread`、`filewrite ->
  consolewrite`（`devsw[CONSOLE]` 在 `console.c` 注册、`file.c` 调用）；候选边
  22 -> 24，无回归（`findings=2`、FreeRTOS `findings=0`）。
- OpenHarmony S5/S7 shard 验证（libclang15 匹配工具链、parse-only pass）：S7
  新增 10 条原始 -> **8 条逻辑**边（2 条 header-inline 重复），候选调用者
  0.64% -> 0.82%；S5 新增 256 条原始 = 256 条逻辑边（该 shard 未出现
  header-inline 重复），候选调用者 5.11% -> 5.48%。合计新增 264 条逻辑边，
  候选边 1,941 -> **2,205**（+13.6%）；候选调用者 498 -> 529 / 22,800
  间接调用者 = 2.18% -> **2.32%**。
- 生产路径抽样 precision：10/10 对源码核验（mbedtls 全局 hook、alsa PCM ops、
  curl/libexif/libnl 参数回调、HDF list/foreach comparator），样本中 0 误报。

## 限制

- 多字段嵌套的 `struct`-array 表（如每驱动 `ops[a].field[b]`）目前按
  base+字段+qualifier 数量匹配；部分字段链与嵌套函数指针结构体尚未建模。
- C++ virtual 分发（`kind="virtual"`）不在本 pass 范围，只处理函数指针注册。
- 通过不透明 setter API 注册（库内部把回调复制进内部链表）只记录为
  `registration_call` 事实供审计，尚不能与后续分发点关联。
- 事实只覆盖 TU 内全局存储；全局符号之外的跨函数所有权、实例别名不追踪。
- 解析依赖目标函数签名；目标定义不在本批（parse degraded TU、生成头缺失）时
  保守丢弃候选。
- 退化语料的覆盖率跟随解析健康度：缺生成头/non-stdinc TU 多的 shard 贡献的
  facts 与候选更少。
- `analyze_function` 本身仍是同 TU；跨 TU pass 是独立批处理/后处理阶段。

## 入口

- `extract_registrations(unit) -> (facts, call_sites)`
- `resolve_cross_tu_targets(call_sites, facts, functions, signatures=...,
  existing_edges=..., dedupe=True) -> (call_targets, links)`
- `extract_target_signatures(unit, ids)`（DB 后处理 worker 用）
- `analyze_cross_tu(units)`（单进程测试/小批）
- CLI `ca resolve-callbacks`、MCP/graph 候选展示消费同一批记录（命令语法见
  [usage.md](usage.md)）。
