# 决策：按语料匹配 libclang 后端

Status: active
Date: 2026-10-18

## 背景

OpenHarmony 用 clang 15 与 libc++ 15 头文件构建，而默认 Python bindings 使用
libclang 18。用 libclang 18 解析真实 OH TU 会出现语义不匹配诊断（如
`nullptr_t`/`__promote`），20 个样本只有 3 个零错误；根因不是 flags。改用与 OH
匹配的 `libclang.so.15` 后，100 TU 零错误率提升到 97%，parse 约 0.8–1.5 秒/TU。
其他语料（xv6、FreeRTOS、本地测试）继续用内置 libclang 18，不能回归。

## 决策

- 解析后端按语料选择：默认仍是内置 libclang 18；OpenHarmony 验收通过
  `CA_LIBCLANG_LIBRARY`（配合匹配 bindings 的 `PYTHONPATH`）使用 OH clang 15
  后端，不改变包依赖。
- 两个后端统一 argv 归一：去掉源文件操作数（libclang 15 会拒绝）、输出/依赖
  参数与 vendor `-Xclang` 透传；captured 命令缺少时注入 OH resource dir、
  `libcxx-ohos` include 与 `-stdlib=libc++`，并记录审计 note。
- 版本相关测试条件化：仅在配置匹配后端时运行，否则 skip。
- 验收环境与启动命令写入 build metadata 和 usage 文档。

## 备选

- 只用 libclang 18：主语料解析质量低，放弃。
- 全局升级到 libclang 15：可能破坏 xv6/FreeRTOS 夹具并锁死旧 API，放弃。
- 用 OH driver 的 `clang -ast-dump=json`：消除绑定不匹配，但 JSON I/O 大且抽取更慢，
  深析路径不采用。

## 后果

- 验收有明确的匹配后端环境；不同后端的数字不可直接混用。
- parser 增加条件兼容代码（源操作数处理、bitfield 回退）与少量版本化测试。
- 报告中的 OH 解析质量/耗时必须标注后端版本。

## 验证

OH libclang 15 lean：sample_20 19/20（A 口径 20/20）、sample_100 97/100、
100 TU 验证 100/100；libclang 18 在 xv6/FreeRTOS 与本地测试保持绿。
