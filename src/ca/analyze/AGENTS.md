# AGENTS.md — analyze（CFG 与数据流）

对 parse 的活动 AST 做函数级分析：CFG、分支/赋值/返回路径、资源操作、回调候选与泄漏结论。

## 关键文件（实现后登记）

- `function_analyzer.py`：`analyze_function(...) -> AnalysisResult`；只依赖 `ParsedUnit` 协议。
- `cfg.py`：基本块与边（if/循环/switch/goto/return/jump），`&&`/`||`/`?:` 拆决策块；`BlockMap` 映射事实→块。
- `facts.py`：赋值、返回、资源调用抽取并挂块。
- `callbacks.py`：同 TU 间接调用候选（注册/数组/参数/typedef/返回指针），按签名过滤。
- `virtual_dispatch.py`：C++ 虚调用 → `virtual_candidate`（override 链，同 TU；跨 TU 由 store 查询时合并）。
- `registrations.py`：跨 TU 全局注册事实（global_assign/struct_field/array_element/designated_initializer/argument_pass/return_pointer）+ qualifier-aware 解析 pass，产出 `indirect_candidate`/links。
- `resources.py`：注解表（libc/kernel/xv6/FreeRTOS）；支持 JSON/`entries`/`extend`。
- `dataflow.py`：有界前向 worklist，产出 `leak`/`double_release`/`unreleased`。
- `astutil.py`：extent 缓存、repo_root 路径归一、宏展开、运算符/变量名。

## 局部规则

- 只消费 `ParsedUnit`/`FunctionRecord`，不依赖 build/store；不自行解析源码。
- 输出确定：按 (line, col, 序号) 排序，ID 用 `ca.core.ids`；`elapsed_ms` 非确定。
- 结论需可审计（`matcher`/`reason`）；`analyze_function` 边界：同 TU 函数指针、跨函数所有权、异常路径不建模（跨 TU 由 `registrations.py` pass 处理）；struct-array 复杂链、C++ 虚派发仅候选边（动态类型未知、模板/多继承保守）；`while(1)` 不推出口；宏控制流不透明、文本截断；守卫配对/所有权转移启发式降误报。
- 测试：`.venv/bin/python -m pytest tests/test_analyze_*.py -q`；夹具 `tests/fixtures/df_*`。
