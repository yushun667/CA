# 问题：xv6 真实编译数据库解析出 0 个函数

Status: resolved
Opened: 2026-10-17
Resolved: 2026-10-17

## 现象与影响

用俘获的 xv6 RISC-V 真实 `compile_commands.json` 建索引时，摘要显示「46/46 TU 已解析」，但产出 0 函数、0 类型、0 调用；直接用 libclang 重放参数会抛 `TranslationUnitLoadError`。修复前旗舰级真实语料路径不可用，而且空结果看起来像成功。

## 复现或触发条件

```sh
PYTHONPATH=src .venv/bin/python -m ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db /tmp/xv6.db --jobs 8
# 观察到：indexed 46/46，records: 0 functions, 0 calls
```

用 compdb 参数直接解析单个 TU 失败，而手写 RISC-V 参数可解析 `kernel/bio.c` 并得到 7 个函数定义。

## 调查

- 仓库内 libclang 18.1.1 支持 RISC-V target；"缺后端"假设被排除。
- GCC 风格编译数据库会把输入源文件作为操作数追加在末尾（`... -c -o kernel/bio.o kernel/bio.c`）。libclang 以单独参数接收源路径，重复出现即加载失败；旧过滤逻辑用绝对路径比较，未识别相对操作数。
- 未显式指定 target 时 libclang 使用宿主 triple，拒绝 `-mcmodel=medany`；缺少交叉 target/ABI 推断。
- xPack 交叉 GCC 默认 rv32 ABI，俘获包装器会注入 `-mabi=lp64d`；早期 compdb 未记录它，导致无法自包含重放。
- `console.c`/`printk.c`/`printf` 需要交叉 GCC 内置头目录才能找到 `stdarg.h`。
- `-no-pie` 是链接期参数，在 RISC-V target 下被 libclang 报告为 unused；叠加 `-Werror` 变成 error 诊断。
- `--repo` 与 compdb 源路径不匹配时，抽取过滤器会丢弃全部实体，却仍报告成功。

## 根因

面向 GCC/kbuild 风格数据库的编译参数归一不完整：未剔除源操作数与链接期参数，未推断交叉 target/ABI 与内置头目录；且全量过滤可能静默产出零记录。

## 解决与验证

`ca.parse` 现在：按 realpath 过滤与 `tu.file` 相同的源操作数，丢弃 `-o/-MD/-MF` 值及链接期参数，从 driver/`-march` 推断 `--target=riscv64/32-unknown-elf` 与 `-mabi=lp64d`，追加 `-isystem <driver -print-file-name=include>`，且不把 `unused-command-line-argument` 判为失败。`ca.cli` 在没有 TU 文件位于 `--repo` 下时以退出码 1 报错，不再写空库。

验证：xv6 46/46 TU 零 error 诊断；TU 主文件内 354 个函数定义；端到端得到 394 个唯一函数、2829 次调用、1423 个分支、575 个资源操作，墙钟 0.79s。回归测试覆盖相对源操作数、`-no-pie` + `-Werror`、RISC-V target 推断与 unused 参数容忍。

## 关联

- 决策或架构：[overview.zh.md](../../architecture/overview.zh.md)、[分析技术栈](../../decisions/active/2026-10-17-analysis-stack.zh.md)、[编译参数来源策略](../../decisions/active/2026-10-17-compile-command-policy.zh.md)
- 相关进度：[progress.zh.md](../../planning/progress.zh.md)
