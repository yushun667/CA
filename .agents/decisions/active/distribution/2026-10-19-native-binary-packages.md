# Decision: native packages preserve CLI, MCP and browser access

Status: active
Date: 2026-10-19

## Context

The user requires directly runnable native packages on Linux, Windows and
macOS, with CLI, MCP and browser access after installation. Double-click launch
must open the local browser. Existing Python/wheel and DSH deployments remain
valid installation paths.

## Decision

- Build a PyInstaller directory bundle separately on each supported native OS
  and architecture. Bundle Python, matched Clang bindings/library, DuckDB,
  schema and web assets; do not require a user-side Python environment.
- The frozen entry point defaults to `desktop` only when no arguments are
  supplied. Explicit commands retain the same CLI contracts, especially MCP
  stdio with diagnostics confined to stderr. Initialize multiprocessing before
  dispatch so frozen worker processes do not start another desktop server.
- Desktop starts the unified backend on loopback, opens the browser, and keeps
  its data in an OS user-data directory or `CA_DATA_DIR`. Default startup
  creates an empty index; an explicit database must already exist.
  Windows listeners use exclusive binding so port selection cannot attach the
  browser to an older service sharing that port.
- Distribute Windows Inno Setup, Linux Debian and macOS pkg installers plus a
  complete portable directory archive and SHA-256 checksums. Native GitHub
  Actions jobs smoke-test artifacts; this workflow does not publish Releases.
- Initial targets are Windows x64, Linux x64 and macOS x64/arm64. Build
  baselines and user-facing contracts live in [the binary guide](../../../../docs/binaries.md).

## Reasons

Native bundles fulfill installation without Python and preserve one executable
for humans and agent clients. Directory bundles keep native dependencies and
data files together and make missing-resource failures inspectable. Separate
native builds expose platform failures without assuming cross-compilation.
Keeping user data outside the installation allows replacement and uninstall
without destroying indexes.

## Alternatives considered

- **Only wheels plus a private venv:** retained for source/DSH users, but does
  not fulfill the requested directly runnable binary installation.
- **One executable for every OS:** incompatible native runtimes require
  separate builds, so each target gets its own artifact.
- **Single-file self-extracting bundles:** a directory bundle makes dependency
  layout and worker startup easier to inspect; portable archives already offer
  a single downloadable file.
- **Auto-publish a Release:** artifact production and verification come first;
  a formal release is a separate publishing action.

## Consequences

Packages are larger because each carries its runtime. Native installer tools
are needed only on build machines. All supporting files must accompany a
portable executable. Packages have no publisher signing certificate or Apple
notarization. Windows `watch` remains unsupported because its locking is
Unix-specific; Windows peak-RSS metrics remain unavailable.

## Validation and risks

Each build must validate version/help, index a C fixture with multiple workers,
query results, run MCP initialize/tools over stdio, and request desktop UI/API
and HTTP MCP. Successful job artifacts and checksums are the build evidence;
declaring a target alone is not verification. Earlier OS versions and additional
architectures need their own runtime checks. Native packages do not replace a
source project's compile database, headers or compiler sysroot.

## Relations and supersession

- Related: [architecture](../../../architecture/overview.md),
  [wheel installation](2026-10-19-wheel-pkg.md).
- Supersedes: none; the wheel decision still governs DSH/backend installation.
- Superseded by: none.
