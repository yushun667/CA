# Native binary packages

Each native package contains the CA CLI, MCP server, browser UI, Python,
libclang, DuckDB and the SQLite schema. Installation and startup need no Python,
LLVM, pip or network access. Analyzing your sources still requires their real
compile arguments and referenced headers/toolchain files; CA does not supply a
project's build environment.

## Targets and downloads

| Target | Build baseline | Installer | Portable archive |
|---|---|---|---|
| Windows x64 | Windows 10/11 x64 | `-setup.exe` | `-portable.zip` |
| Linux x64 | Ubuntu 22.04, glibc 2.35+ | `.deb` | `-portable.tar.gz` |
| macOS x64 | macOS 15 | `.pkg` | `-portable.tar.gz` |
| macOS arm64 | macOS 15 | `.pkg` | `-portable.tar.gz` |

Older macOS versions, Linux musl distributions and Windows/Linux ARM64 are not
validated targets. Each OS/architecture is built on a native runner, not by
cross-compiling a Windows build. A configured target is considered verified
only after its workflow job passes the binary smoke checks.

Open a successful [Binaries workflow](https://github.com/yushun667/CA/actions/workflows/binaries.yml)
run and download **Artifacts → `ca-<os>-<arch>`**. Extract this outer workflow
archive to find the installer, portable archive and `SHA256SUMS`. Filenames begin
with `ca-<version>-<os>-<arch>`, for example
`ca-0.1.0-windows-x64-setup.exe`. Compare the file's SHA-256 against
`SHA256SUMS` (`Get-FileHash` on Windows, `sha256sum` on Linux,
`shasum -a 256` on macOS). The workflow does not create a GitHub Release.

Packages have no publisher certificate or Apple notarization; operating-system
prompts therefore show an unsigned/unverified publisher. Verify the source and
checksum before approving the package through the system's normal UI.

## Install and launch

- **Windows:** run `-setup.exe`. The per-user default is
  `%LOCALAPPDATA%\Programs\CA`; it creates a Start menu entry, offers a desktop
  shortcut and adds CA to the user's PATH. Open a new terminal for `ca`.
- **Linux:** install with `sudo apt install ./ca-<version>-linux-x64.deb`.
  Files live in `/opt/ca`, the CLI is `/usr/bin/ca`, and the application menu
  entry opens the browser. A graphical session and browser are needed for
  double-click launch; CLI/MCP also work on a headless host.
- **macOS:** open the `.pkg`. It installs `/Applications/CA.app` and
  `/usr/local/bin/ca`; open CA in Applications or run `ca` in a terminal.
- **Portable:** on Windows/Linux, keep the entire `ca/` directory, including
  `_internal/`; run `ca/ca` (`ca\ca.exe` on Windows). On macOS, keep `CA.app`
  and the accompanying `ca` link together; double-click `CA.app` or run `./ca`.
  Add the executable's directory to PATH yourself if desired.

Double-click CA, or run `ca desktop`. The frozen executable also opens desktop
mode when invoked without arguments. Source installations keep the usual CLI
help on an invocation without arguments; `ca desktop` is explicit on both.
The launcher opens a browser and serves UI, `/api` and HTTP `/mcp` on
`127.0.0.1:8766`. If that default port is busy it selects a free one and prints
the URL. Explicit `--port N` fails if occupied; `--port 0` selects a free port.

```sh
ca desktop --db /absolute/path/index.db --repo /absolute/path/source
ca desktop --db /absolute/path/index.db --port 9000 --no-browser
```

Closing the browser leaves CA running. For a terminal launch, stop it with
Ctrl+C or close the launch console; for an application-menu launch, stop the CA
process through the system's process manager. Desktop mode always binds loopback. For a dedicated
server, see [`ca serve`](server.md); for a read-only UI see [`ca ui`](ui.md).

## Index and use all three interfaces

```sh
ca version
ca index --repo /absolute/path/source --compdb /absolute/path/compile_commands.json --db /absolute/path/index.db --jobs 4
ca query functions main --db /absolute/path/index.db
ca desktop --db /absolute/path/index.db --repo /absolute/path/source
ca mcp --db /absolute/path/index.db
```

In Windows PowerShell, quote paths and use `&` for an executable path:

```powershell
& "$env:LOCALAPPDATA\Programs\CA\ca.exe" index --repo "C:\src\project" --compdb "C:\src\project\build\compile_commands.json" --db "C:\data\index.db" --jobs 4
```

For a stdio MCP client, use the installed executable's absolute path:

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

Replace both paths. On Linux use `/usr/bin/ca`; on macOS use
`/usr/local/bin/ca`, with an absolute database path. MCP starts directly without
a browser or startup text on stdout. To query a running desktop backend instead,
use `args: ["mcp", "--serve-url", "http://127.0.0.1:8766"]`, replacing the port
with the displayed one. Protocol and HTTP-client details are in [mcp.md](mcp.md).

## Data, upgrades and limits

The first default desktop launch creates an **empty** `index.db`:

| OS | Default user data directory |
|---|---|
| Windows | `%LOCALAPPDATA%\CA` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/ca` |
| macOS | `~/Library/Application Support/CA` |

`CA_DATA_DIR` overrides this directory. Desktop backend state is stored in the
user data directory as a separate `desktop-*.serve-state.db` for each index.
Explicit `--db` requires an existing
index and does not create a misspelled path. Indexing commands always use their
explicit `--db`; they do not implicitly choose the desktop database.

Stop CA before upgrading; install the new package or replace the complete
portable directory. Installers leave user indexes, caches and sources in place.
Uninstall through Windows Installed apps, `sudo apt remove ca-analyzer` on Linux,
or remove `/Applications/CA.app` and its `/usr/local/bin/ca` link on macOS. Remove
user data separately only when you no longer need it.

Windows indexing/query/UI/MCP are supported; `ca watch` still relies on Unix
locking and is not supported there. An existing Windows header-update bug can
also miss dependent translation units when stored include paths and changed
paths use different separators. After changing a header, rebuild with
`ca index --repo ... --compdb ... --db ...` **without `--incremental`**; do not
rely on `ca update` for that change. See the
[tracked issue](../.agents/issues/open/2026-10-19-windows-header-update-paths.md).
Windows does not report Unix peak-RSS metrics. Other analysis limitations are
in [usage.md](usage.md).

## Build from source

On each target OS, use Python 3.11 and install `requirements.txt` plus
`requirements-build.txt`, then install CA with `python -m pip install -e .`:

```sh
python scripts/build_binary.py
python scripts/build_installer.py --bundle dist/ca
```

The runtime is `dist/ca/ca` (`ca.exe` on Windows), with supporting files in
`dist/ca/`. Packages and checksums go to `dist/installers/`. Native installer
tools are Inno Setup on Windows, `dpkg-deb` on Linux and `pkgbuild` on macOS;
`--portable-only` skips the installer. Build details and smoke checks are
executable in [binaries.yml](../.github/workflows/binaries.yml).
