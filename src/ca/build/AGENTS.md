# AGENTS.md — build（真实编译参数）

为每个 TU 产生真实编译命令：读取 compile_commands.json、CMake/Meson 导出、CC/CXX 包装器俘获构建、Linux 内核 `.cmd` 重建；无构建信息时提供显式标记为 `synthesized` 的降级参数。

## 关键文件（已实现）

- `compdb.py`：加载/规范化/校验 compile_commands，entry → `CompilationUnit`，并回写标准 cdbb。
- `capture.py`：`CaptureSession`/`capture_build` 安装 cc/c++/gcc/clang 包装脚本，独立 JSON 记录并发安全、幂等；失败保留部分 compdb 并报退出码；`extra_args` 注入项写入生效 argv。
- `kernel_cmd.py`：解析 Kbuild `.cmd` 重建真实 argv，剔除 `-o`/依赖噪声，保留 include/define/`-include`。
- `synth.py`：无构建信息时显式降级，`source="synthesized"`。
- `corpus.py`：xv6 固定 revision 俘获构建；xpack 工具链经包装器注入并记录 `-mabi=lp64d`、`ld -m elf64lriscv`；Linux 限制与 CI 命令见 `LINUX_KERNEL_COMPDB_NOTES`。
- `kernel_corpus.py`：Apple `container` 内构建 Linux 内核真 compdb（固定 tarball 校验、defconfig+子集、内核 `gen_compile_commands.py`、容器→宿主路径重写、写 `.tmp/corpus/linux/`）；容器不可用抛 `ContainerUnavailableError`，绝不 synthesized。
- `openharmony.py`：OpenHarmony/`repo` 语料纯逻辑：manifest 解析、GN/Ninja 与 Docker 回退命令构造、`ninja -t compdb` 过滤为 C/C++、唯一源文件代表命令选择（arm64/非 test 优先）、目录分布与 metadata 写入；只操作工作副本，原树只读。
- `__init__.py`：四个契约入口重导出。

## 局部规则

- 严禁把猜测参数伪装成真实构建参数：`CompilationUnit.source` 必须如实填写（`compile_commands`/`capture`/`kernel_cmd`/`synthesized`）。
- 本机 RISC-V 交叉编译器在 `.tmp/toolchain/`；大语料不进 Git（`.tmp/`）。
- 测试：`.venv/bin/python -m pytest tests/test_build_*.py -q`。
