# 原生二进制安装包

每个原生包包含 CA 命令行、MCP 服务、网页、Python、libclang、DuckDB 与 SQLite
schema。安装和启动不需要 Python、LLVM、pip 或网络。分析自己的源码仍需真实编译参数
及其引用的头文件、工具链文件；CA 不提供待分析项目的构建环境。

## 构建目标与下载

| 目标 | 系统基线 | 安装器 | 便携包 | 原生构建/安装后冒烟 |
|---|---|---|---|---|
| Windows x64 | Windows 10/11 x64 | `-setup.exe` | `-portable.zip` | [通过](https://github.com/yushun667/CA/actions/runs/39253506257/job/118062887696) |
| Linux x64 | Ubuntu 22.04，glibc 2.35+ | `.deb` | `-portable.tar.gz` | [通过](https://github.com/yushun667/CA/actions/runs/39253506257/job/118062888040) |
| macOS x64 | macOS 15 | `.pkg` | `-portable.tar.gz` | [通过](https://github.com/yushun667/CA/actions/runs/39253506257/job/118062887804) |
| macOS arm64 | macOS 15 | `.pkg` | `-portable.tar.gz` | [通过](https://github.com/yushun667/CA/actions/runs/39253506257/job/118062887785) |

四个目标均在 [运行 39253506257](https://github.com/yushun667/CA/actions/runs/39253506257)
通过原生构建、安装器安装，以及命令行/索引、MCP 与网页冒烟检查，源码为
[d7d0f8](https://github.com/yushun667/CA/commit/d7d0f8244816bc1b6ee11cc23e5884c47bb5eec3)。
CI runner 为 Windows Server 2022、Ubuntu 22.04 与 macOS 15。全仓测试基线仍有失败，
包括下文的 Windows 头文件增量问题。

较旧 macOS、使用 musl 的 Linux 发行版、Windows/Linux ARM64 不属于已验证目标。
各系统与架构在原生 runner 上构建，不从 Windows 交叉编译。

打开已验证的 [Binaries 构建](https://github.com/yushun667/CA/actions/runs/39253506257)，
下载 **Artifacts → `ca-<os>-<arch>`**。解压外层工作流压缩包后可见安装器、
便携包与 `SHA256SUMS`。文件名以 `ca-<version>-<os>-<arch>` 开头，例如
`ca-0.1.0-windows-x64-setup.exe`。将文件 SHA-256 与 `SHA256SUMS` 比较：Windows
用 `Get-FileHash`，Linux 用 `sha256sum`，macOS 用 `shasum -a 256`。
该工作流不创建 GitHub Release。
本地汇总产物时，各目标分别放在 `dist/installers/ca-<os>-<arch>/`，避免四份
`SHA256SUMS` 相互覆盖。

安装包没有发布者证书或 Apple 公证，因此系统提示会显示未签名或未验证发布者。
确认来源与校验和后，通过系统正常界面批准安装。

## 安装与启动

- **Windows：**运行 `-setup.exe`。默认按用户安装到
  `%LOCALAPPDATA%\Programs\CA`，创建开始菜单入口，可选桌面快捷方式，并加入用户
  PATH。安装后新开终端即可使用 `ca`。
- **Linux：**用 `sudo apt install ./ca-<version>-linux-x64.deb` 安装。程序在
  `/opt/ca`，命令行入口为 `/usr/bin/ca`，应用菜单入口会打开浏览器。双击启动需要
  图形会话与浏览器；命令行和 MCP 也可用于无图形界面的主机。
- **macOS：**打开 `.pkg`，安装到 `/Applications/CA.app`，并创建
  `/usr/local/bin/ca`。在应用程序中打开 CA，或在终端运行 `ca`。
- **便携包：**Windows/Linux 保留完整 `ca/` 目录，包括 `_internal/`，运行
  `ca/ca`（Windows 为 `ca\ca.exe`）。macOS 保留 `CA.app` 及随附的 `ca` 链接，
  双击 `CA.app` 或运行 `./ca`。需要时自行将执行文件目录加入 PATH。

双击 CA，或运行 `ca desktop`。冻结后的可执行文件无参数启动时也进入桌面模式。
源码安装无参数时保留原来的命令行帮助；两种安装方式均可显式使用 `ca desktop`。
启动器打开浏览器，在 `127.0.0.1:8766` 同时提供网页、`/api` 与 HTTP `/mcp`。
默认端口被占用时会选择空闲端口并打印地址；显式指定 `--port N` 时若被占用则报错，
`--port 0` 则选择空闲端口。

```sh
ca desktop --db /absolute/path/index.db --repo /absolute/path/source
ca desktop --db /absolute/path/index.db --port 9000 --no-browser
```

关闭浏览器后 CA 继续运行；从终端启动时按 Ctrl+C 或关闭启动控制台可停止，
从应用菜单启动时可在系统进程管理器中停止 CA 进程。
桌面模式始终绑定本机回环地址。专用后端见 [`ca serve`](server.zh.md)，
只读网页见 [`ca ui`](ui.zh.md)。

## 索引与三个使用入口

```sh
ca version
ca index --repo /absolute/path/source --compdb /absolute/path/compile_commands.json --db /absolute/path/index.db --jobs 4
ca query functions main --db /absolute/path/index.db
ca desktop --db /absolute/path/index.db --repo /absolute/path/source
ca mcp --db /absolute/path/index.db
```

Windows PowerShell 中给路径加引号，并用 `&` 调用可执行文件路径：

```powershell
& "$env:LOCALAPPDATA\Programs\CA\ca.exe" index --repo "C:\src\project" --compdb "C:\src\project\build\compile_commands.json" --db "C:\data\index.db" --jobs 4
```

stdio MCP 客户端需填写安装后可执行文件的绝对路径：

```json
{
  "mcpServers": {
    "ca": {
      "command": "C:\\Users\\YOUR_NAME\\AppData\\Local\\Programs\\CA\\ca.exe",
      "args": ["mcp", "--db", "C:\\data\\index.db"]
    }
  }
}
```

请替换两个路径。Linux 命令路径为 `/usr/bin/ca`，macOS 为 `/usr/local/bin/ca`，
数据库也填写绝对路径。MCP 直接启动，不打开浏览器，也不向 stdout 写启动提示。
若需查询正在运行的桌面后端，可用
`args: ["mcp", "--serve-url", "http://127.0.0.1:8766"]`，将端口替换为实际显示值。
协议与 HTTP 客户端说明见 [mcp.zh.md](mcp.zh.md)。

## 数据、升级与限制

首次默认桌面启动创建的是**空** `index.db`：

| 系统 | 默认用户数据目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\CA` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/ca` |
| macOS | `~/Library/Application Support/CA` |

`CA_DATA_DIR` 可覆盖此目录。桌面后端状态保存在用户数据目录，按索引分开写入
`desktop-*.serve-state.db`。显式 `--db` 必须指向已有索引，不会因路径拼错而新建库。
索引命令始终使用显式 `--db`，不会隐式选用桌面默认数据库。

升级前停止 CA，再安装新包或替换完整便携目录。安装器保留用户索引、缓存与源码。
Windows 从“已安装的应用”卸载；Linux 用 `sudo apt remove ca-analyzer`；macOS
删除 `/Applications/CA.app` 及其 `/usr/local/bin/ca` 链接。确认不再需要后再单独
删除用户数据。

Windows 支持索引、查询、网页与 MCP；`ca watch` 仍依赖 Unix 文件锁，在 Windows
上暂不支持。既有 Windows 头文件增量问题还可能因库内 include 路径与变更路径的
分隔符不同而漏掉依赖翻译单元。修改头文件后，请用
`ca index --repo ... --compdb ... --db ...` **不带 `--incremental` 全量重建**，
不要依赖 `ca update` 处理该变更。见
[问题记录](../.agents/issues/open/2026-10-19-windows-header-update-paths.zh.md)。
Windows 不报告 Unix 峰值 RSS 内存指标。其他分析限制见 [usage.zh.md](usage.zh.md)。

## 从源码构建

在各目标系统上使用 Python 3.11，安装 `requirements.txt` 与
`requirements-build.txt`，再用 `python -m pip install -e .` 安装 CA：

```sh
python scripts/build_binary.py
python scripts/build_installer.py --bundle dist/ca
```

运行入口为 `dist/ca/ca`（Windows 为 `ca.exe`），随附文件也在 `dist/ca/`。
安装包与校验和写入 `dist/installers/`。原生安装器工具分别为 Windows Inno Setup、
Linux `dpkg-deb` 与 macOS `pkgbuild`；`--portable-only` 可跳过安装器。
可执行的构建步骤和冒烟检查见 [binaries.yml](../.github/workflows/binaries.yml)。
