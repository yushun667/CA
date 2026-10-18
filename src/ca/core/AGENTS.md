# AGENTS.md — core（共享数据契约，Lead 所有）

`core` 定义所有阶段交换的记录、稳定 ID 与模块接口；不依赖第三方库，也不导入其他 ca 模块。

## 关键文件

- `schema.py`：记录 dataclass、`SCHEMA_VERSION`、JSONL 展开；未知值用 `""`/`-1`。
- `ids.py`：函数/类型/调用/CFG/资源的稳定 ID 规则；所有生产方必须使用。
- `interfaces.py`：跨模块入口、`ParsedUnit`/`RecordStore` 契约与导入方向。
- `jsonl.py`：测试夹具与导出的 JSONL 读写。

## 局部规则

- 修改字段、ID 规则或接口属于契约变更：先记录决策，再同步全部模块与测试，必要时升级 `SCHEMA_VERSION`。
- 记录必须可 pickle、纯数据；不得持有 Clang 对象（活动对象只存在于阶段内部）。
- 契约测试在仓库根运行：`.venv/bin/python -m pytest tests/test_core_schema.py -q`。
