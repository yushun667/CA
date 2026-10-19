# CA — 操作系统源码分析器

CA 读取仓库的**真实编译参数**，用 Clang 解析每个翻译单元，提取函数、类型、调用与源码位置，构建函数级 CFG 与数据流事实（分支条件、赋值、返回路径、资源操作与泄漏证据），并全部存入 SQLite 以支持快速查询。

Status: **阶段 2 工程化已交付（2026-10-17）**。阶段 1 流水线已在 xv6-riscv（真实 RISC-V 交叉编译数据库）与 FreeRTOS-Kernel（真实 CMake 编译数据库）上完成端到端验证；阶段 2 增加增量重建（`--incremental`）、可安装的 `ca` 控制台命令、CI 门禁（pytest + 精度 F1 ≥ 0.9）与内核规模语料基准。精度与性能基线记录在 `benchmarks/results/`。

## 安装

CA 提供 Windows、Linux 与 macOS 原生二进制包、Python 源码安装，以及 DeepSeek
Harness 插件。原生包自带 Python、libclang、DuckDB、数据库 schema 与网页资源，
用户无需另装 Python 或 LLVM。源码安装需要 **Python >= 3.11**。

### 原生应用（命令行、MCP 与网页）

**安装包由 GitHub Actions 构建生成。** Windows、Linux 与两种 macOS 架构分别使用
原生构建环境；四个目标均已在
[这次 Binaries 构建](https://github.com/yushun667/CA/actions/runs/39253506257)
通过独立运行及安装后的命令行、MCP 和网页检查。

1. 登录 GitHub，打开上述已验证构建，或从
   [Actions → Binaries](https://github.com/yushun667/CA/actions/workflows/binaries.yml)
   选择一次成功运行。
2. 在页面的 **Artifacts** 区域下载对应系统的压缩包：

   | 系统 | 制品名称 | 包内安装器 |
   |---|---|---|
   | Windows x64 | `ca-windows-x64` | `ca-<version>-windows-x64-setup.exe` |
   | Linux x64 | `ca-linux-x64` | `ca-<version>-linux-x64.deb` |
   | macOS Intel | `ca-macos-x64` | `ca-<version>-macos-x64.pkg` |
   | macOS Apple 芯片 | `ca-macos-arm64` | `ca-<version>-macos-arm64.pkg` |

3. 解压下载的压缩包，按 `SHA256SUMS` 校验安装器后运行。每组制品还包含便携包。

制品保留 **30 天**。下载需要登录 GitHub，并拥有仓库读取权限，参见
[GitHub 制品下载说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)。
当前流程将安装包上传到 **Actions → Artifacts**，不会发布 GitHub Release。
需要重新构建时，打开 **Actions → Binaries → Run workflow**。推送到
`build/binaries` 分支或推送匹配 `v*` 的标签也会触发打包；普通 `main` 推送不会触发。
系统要求、安装路径、未签名提示及校验命令见[二进制安装指南](docs/binaries.zh.md)。

安装后双击 **CA** 即打开本机浏览器界面。同一个可执行文件同时提供全部命令行功能与 MCP：

```sh
ca version
ca index --repo /path/to/source --compdb /path/to/compile_commands.json --db /path/to/index.db
ca desktop --db /path/to/index.db
ca mcp --db /path/to/index.db
```

首次启动会在用户数据目录创建空索引；查询自己的代码前需要先索引源码。便携包需保留
可执行文件及随附文件的完整目录。Windows 命令、MCP 客户端配置、用户数据与构建方法
见[二进制安装指南](docs/binaries.zh.md)。

### 作为 DeepSeek Harness 插件安装

1. 安装 DeepSeek Harness（桌面版或 `dsh web`），打开插件管理器。
2. 从本仓库的 `dsh/` 目录安装 bundle（Plugins → 从本地目录安装 →
   `<repo>/dsh`），然后启用 `@local/dsh-ca`。
3. 从侧栏打开 CA 面板并添加项目：
   - **本机**：点 **选择目录…**，选一个源码目录。插件会安装私有 CA venv，
     在源码目录的固定隐藏目录 `<源码目录>/.ca/index.db` 建索引库、启动
     `ca serve` 并激活项目。
   - **远程 Linux**：选 **远程服务器**，填用户名、主机/IP、端口（22），选择密码
     或密钥/免密，点 **连接**，浏览服务器并选择源码目录。插件会在服务器
     `~/dsh-ca/backend` 安装后端、在服务器源码目录的 `<源码目录>/.ca/index.db` 建库、
     通过 SSH 隧道启动 `ca serve` 并激活项目。
4. 在任务页运行 **index** 任务（agent 工具：`ca_task`）。新项目初始是空索引，
   索引完成前查询没有结果。
5. 默认在线安装（PyPI）。离线主机用
   `node dsh/scripts/build-wheelhouse.mjs --platform <linux-x64|linux-arm64|macos-arm64|macos-x64|windows-x64> [--tar]`
   生成对应平台的 wheelhouse，放到
   `$DSH_HOME/plugin-data/dsh-ca/wheelhouse/<platform>/`（或把路径传给安装器），
   插件会自动使用。产品 UI 没有后端配置页——后端生命周期全自动。密码只在临时
   askpass helper 中使用，不持久化，DSH 重启后需重输；Windows 宿主请用密钥/agent。

完整插件说明见 [docs/dsh-plugin.zh.md](docs/dsh-plugin.zh.md)。

### CLI / 开发安装

```sh
scripts/bootstrap.sh        # .venv + 固定依赖 + editable CA + libclang 检查
.venv/bin/ca version
```

`scripts/bootstrap.sh` 需要 Python 3.11+ 与网络（或已就绪的 pip 缓存）。索引与查询
命令见 [docs/usage.zh.md](docs/usage.zh.md)。

## 流水线

```text
真实构建 ──> ca.build ──> ca.parse ──> ca.analyze ──> ca.store ──> ca.cli
             编译参数      Clang AST    CFG + 数据流    SQLite      索引/查询/
                          事实          事实            + 索引      统计/导出
```

| 阶段 | 职责 |
|---|---|
| `ca.build` | 读取 `compile_commands.json`、通过 `CC`/`CXX` 包装器俘获真实构建、重建 Linux kbuild `.cmd`，或显式标注来源的合成参数 |
| `ca.parse` | libclang AST 提取：函数、类型、字段、调用、源码范围、稳定 ID；处理 RISC-V/ARM 交叉 target 与 GCC sysroot |
| `ca.analyze` | 函数级 CFG、分支条件、赋值、返回路径、资源操作（内存/锁/文件/引用计数等）、函数内泄漏与重复释放结论 |
| `ca.store` | 13 表 SQLite schema、WAL 批量写入、`finalize()` 建索引、查询 API 与 JSONL 导出 |
| `ca.pipeline` | 多进程编排、单 TU 失败隔离、去重、`IndexSummary` 指标 |
| `ca.cli` | `index`、`query`、`stats`、`export`、`version`；数据走 stdout，进度/指标走 stderr，退出码 0/1/2 |

共享契约位于 `src/ca/core/`（记录、稳定 ID、模块协议）；导入方向为 `core <- {build, parse, analyze, store} <- pipeline <- cli`。

## 快速开始

```sh
scripts/bootstrap.sh     # 建 .venv、装固定依赖、`pip install -e .`、检查 libclang
.venv/bin/ca version     # 控制台命令（模块方式：PYTHONPATH=src .venv/bin/python -m ca）

# 用真实编译数据库索引仓库（.tmp 下语料不提交进 Git）
.venv/bin/ca index \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json \
    --db ca.db --jobs 8

# 查询
.venv/bin/ca query functions kalloc --db ca.db
.venv/bin/ca query callers kfree --db ca.db
.venv/bin/ca query branches syscall --db ca.db
.venv/bin/ca query resources kalloc --db ca.db
.venv/bin/ca query leaks --db ca.db
.venv/bin/ca stats --db ca.db
.venv/bin/ca export --db ca.db --out ca.jsonl
```

`--repo` 必须包含编译数据库中每个 `file` 的路径，否则 `index` 以退出码 1 失败，而不是静默生成空库。没有编译数据库时 `auto` 会降级为合成参数并打印醒目警告，此类抽取精度较低。完整用法与构建俘获方式见 [docs/usage.zh.md](docs/usage.zh.md)。

## 增量重建

`ca index --incremental` 会为每个翻译单元缓存一份解析结果与其分析结果，放在
`<db-dir>/.ca-cache`（或用 `--cache-dir DIR`），缓存键由 TU 主源文件内容、
语言/driver、精确编译参数与工具/schema 版本共同决定。未变化的 TU 跳过
parse+analyze 并回放缓存记录，stderr 摘要报告命中率：

```sh
.venv/bin/ca index --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --db ca.db --incremental
# [index] cache: hits=46/46 (100.0%), misses=0, dir=.../.ca-cache
```

头文件内容不参与缓存键：修改头文件后请不加 `--incremental` 重建（或删除缓存）。
损坏的缓存条目会被丢弃并正常重新解析；缓存目录对 Git 自忽略，可安全删除。

## Agent 接入（MCP）与调用图

CA 设计为跑在源码服务器上、由 AI agent 查询。`ca mcp` 通过 JSON-RPC 2.0 提供只读
MCP 接口（默认 stdio，可按需 HTTP `POST /mcp` + `GET /health`），包含符号、调用者、
被调用者、调用图、C/C++ 事实、findings、detail、stats 等工具。调用图也可直接用
CLI：

```sh
.venv/bin/ca graph --from main --depth 2 --db ca.db
.venv/bin/ca graph --from kfree --depth 2 --reverse --db ca.db --format dot
.venv/bin/ca mcp --db ca.db          # stdio；agent 本地或经 SSH 连接
```

增量刷新用 `ca update --changed-files ... --repo ... --db ...`（通过 per-TU 缓存
只重解析受影响 TU；头文件变更退化为全量重建）。工具 schema、SSH/隧道配方与服务器
验证见 [docs/mcp.zh.md](docs/mcp.zh.md)；离线/在线模型与 watcher 设计见
[docs/incremental.zh.md](docs/incremental.zh.md)。

## 人类可用的 Web UI（只读）

需要交互式查看时，`ca ui` 在同一个索引上提供本地只读浏览器界面与标准库 JSON API
（符号搜索、带分支/返回/资源的函数详情、合并后的调用者/被调用者、含候选回调边的
SVG 调用图、回调注册事实与统计）。默认绑定 `127.0.0.1:8766`，没有写端点；前端是
纯 HTML/CSS/JS，无 CDN、无构建步骤：

```sh
.venv/bin/ca ui --db ca.db                 # 浏览器打开 http://127.0.0.1:8766/
.venv/bin/ca ui --db-dir shards/ --port 9000
```

页面、API 端点、只读保证与 SSH 隧道说明见 [docs/ui.zh.md](docs/ui.zh.md)。

## DSH 插件（DeepSeek Harness）

`dsh/` 把 CA 打包为 DeepSeek Harness bundle（`@local/dsh-ca`）：在 Harness Web UI
内提供完整 CA 页面、给 agent 的 12 个原生 `ca_*` 工具、本地或远程 Linux `ca serve`
后端、长索引任务，以及由会话模型驱动的 AI 解释（CA 本身保持无 LLM）。项目把一份源码根、一个数据库（或 shard 目录）与一个后端位置绑定；用户只需在本机
选目录，或登录服务器后选一个目录，后端安装与准备全自动。插件是纯增量：
CLI、`ca ui`、`ca mcp` 与 MCP 协议均不变，插件代码也不改变 CA 的测试基线。通过
Harness plugin manager 从本地 `dsh/` 目录安装；安装、配置、远程后端、UI 页面、工具、
故障排查与安全边界见 [docs/dsh-plugin.zh.md](docs/dsh-plugin.zh.md)。

## 持续集成

`.github/workflows/ci.yml` 在 Python 3.11 上安装固定依赖与 editable 包，运行
全量测试，然后以精度门禁收尾（`benchmarks/run_accuracy.py --fail-under 0.9`）。
语料位于被忽略的 `.tmp/`，因此真实语料端到端测试在 CI 中自动 skip，而合成数据与
golden 夹具测试在各环境都运行。

## 已验证结果

主机：macOS arm64，18 核，64 GB 内存，Python 3.11，libclang 18.1.1。

| 语料 | TU | 唯一函数 | 调用 | 分支 | 赋值 | 返回 | 资源操作 | findings | DB | 端到端 |
|---|---|---|---|---|---|---|---|---|---|---|
| xv6-riscv（真实 RISC-V compdb） | 46 | 394 | 2,829 | 1,423 | 1,679 | 339 | 575 | 2（已知跨函数 FP） | 4.2 MiB | 墙钟 0.79s |
| FreeRTOS-Kernel（真实 CMake compdb） | 10 | 201 | 368 | 554 | — | — | 25 | 0 | 2.0 MiB | 墙钟 2.1s |
| Linux 6.12.113 内核 arm64 defconfig 子集（真实内核 compdb） | 100 / 1,682 | 26,570 | 49,321 | 19,060 | 28,037 | 14,286 | 1,457 | 18 | 107 MiB | 墙钟 183s |

- **抽取精度**：标注夹具 `golden_os_mini` 上 7/7 类事实（函数、类型、调用、分支、赋值、返回、资源）precision = recall = F1 = 1.0000（目标 0.9，PASS）。
- **解析/索引吞吐**：18 核高负载主机、8 worker 下 xv6 50–66 TU/s、25–32k LOC/s（parse CPU 0.7–0.9s、analyze CPU 1.4–2.2s、写库 0.1s）；FreeRTOS 16–23 TU/s、85–120k LOC/s。
- **增量重建**：xv6 46 TU 冷启动 1.0–1.25s → 热缓存 0.29–0.30s（3.4–4.2×，命中率 100%，计数一致）；缓存只跳过 parse+analyze，数据库仍会重写。
- **内核规模**：真实 Linux 6.12.113 内核子集（kernel/ mm/ fs/ drivers/net/，arm64 defconfig，1,682 个真实 TU）前 100 个 TU 在 18 核高负载主机上：墙钟 183s（parse CPU 447s、analyze CPU 432s、写库 124s），DB 107 MiB。内核每个 TU 展开大量头文件：100 TU 产生 14 万次函数分析（1.4k/TU）、67.4 万行含头文件源码；瓶颈是头文件内联函数分析，写入与内存成本也显著（父进程峰值内存约 6 GB）。全量 1,682 TU 属于数十分钟级批处理，留给 CI/夜间任务而非交互使用。
- **数据库**：合成 1 万函数/15.7 万行写入 41–46.5 万 rows/s（40.7 MiB DB，finalize 含 FTS5 trigram 索引）；点查 p95 ≤ 3.6ms、callers p95 2.9ms；内核 100 TU 库（107 MiB、2.66 万函数、4.93 万调用）符号/调用者/分支/资源查询均为个位数毫秒。
- **测试**：`497 passed / 4 skipped`（契约、parse、analyze、store、FTS、端到端、增量）；xv6 端到端断言非空洞计数与真实编译参数来源。

## 复现

```sh
.venv/bin/python -m pytest -q
.venv/bin/python benchmarks/run_accuracy.py
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/corpus/xv6-riscv \
    --compdb .tmp/corpus/xv6-riscv/compile_commands.json --jobs 8 --incremental
.venv/bin/python benchmarks/run_db_bench.py --functions 10000 --queries 300

# 内核规模（1,682 个真实 TU 的前 100 个；全量留给夜间任务）
.venv/bin/python benchmarks/run_parse_bench.py \
    --repo .tmp/linux/linux-6.12.113 \
    --compdb .tmp/corpus/linux/compile_commands.json --jobs 8 --max-tus 100
```

每个基准结果 JSON 都记录机器、语料 revision、编译数据库哈希与命令行。

## 仓库结构

```text
src/ca/core/       共享记录、稳定 ID、模块协议（Lead 维护）
src/ca/build/      编译参数获取与语料工具
src/ca/parse/      libclang AST 提取
src/ca/analyze/    CFG、数据流、资源分析
src/ca/store/      SQLite schema、写入、查询
src/ca/pipeline.py 多进程索引编排
src/ca/cli/        命令行入口
tests/             契约、单元、端到端测试与夹具（含标注 golden 数据）
benchmarks/        精度、解析/索引（含增量）、数据库基准
docs/              双语用户指南
scripts/           环境引导（venv + editable 安装）
dsh/               DeepSeek Harness 插件 bundle（CA 页面 + 原生工具）
.github/           CI 工作流与精度门禁
.agents/           治理：架构、决策、规划、规范
```

## 精度与已知限制

- **真实构建数据优于合成**：没有真实编译数据库时参数是推测的，认真分析请使用俘获路径。
- **只分析一种预处理配置**（俘获命令的宏/包含）；其他 `#ifdef` 配置不展开。
- **函数指针/间接调用**记录为未解析被调用者；直接调用的调用图完整。
- **宏**在展开后分析；宏生成的控制流按不透明处理，文本字段有长度上限。
- **不建模跨函数数据流**：泄漏结论描述函数内资源路径，所有权转移是启发式。xv6 剩余 2 条均为已知 FP（内核常驻分配、循环携带别名）。
- **增量缓存只以 TU 主源文件与编译参数为键**：不检测头文件变化，改过头文件后
  需不加 `--incremental` 重建。数据库本身每次仍会重建，只是跳过昂贵的
  parse/analyze 工作。
- **内核规模分析昂贵**：每个 TU 会展开数千个头文件内联函数，瓶颈在 analyze 而非 parse；全量 1,682 TU 是数十分钟级批处理。头文件派生的分析记录目前可能对直接 TU 源之外的文件暴露绝对路径（待修问题）。
- **Linux 内核语料需要 Linux 构建环境**：真实内核 `compile_commands.json` 由容器化 Ubuntu 构建产生（`ca.build.kernel_corpus`，Apple `container`/Docker）；compdb 与源码树只放在被忽略的 `.tmp/`。
- **单写者**：worker 只解析/分析，写库统一由主进程完成。

## 由 agent 团队交付

项目由 Lead 与 5 个并行 agent 交付，写作用域互斥、共享任务板：`parser-engine`（AST 提取）、`build-capture`（真实编译参数与语料）、`ir-analyst`（CFG/数据流/资源）、`storage-engine`（SQLite 写入/查询）、`integration-qa`（流水线、CLI、端到端、精度/性能）。非平凡选择记录于 [.agents/decisions/active/](.agents/decisions/active/AGENTS.md)，架构与进度见 [.agents/architecture/overview.zh.md](.agents/architecture/overview.zh.md) 与 [.agents/planning/progress.zh.md](.agents/planning/progress.zh.md)。

## 下一步

阶段 2 已交付：增量重建、打包/CI 门禁与内核规模语料。下一步：FTS5 符号搜索、C++ 覆盖、跨函数/别名感知的数据流。
