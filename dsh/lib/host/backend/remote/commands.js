/**
 * Pure command builders for the remote Linux backend.
 *
 * Every SSH/rsync/shell string is constructed here so the remote behaviour is
 * unit-testable (assert argv, no real host) and auditable in one place:
 *   - SSH always runs in BatchMode with a bounded ConnectTimeout: missing keys
 *     or a down host fail fast with an actionable error instead of prompting.
 *   - remote paths support absolute paths and `~/...` (expanded from $HOME by
 *     the remote shell); paths are quoted, so spaces are safe.
 *   - long-running remote commands write a PID file first, which is how stop
 *     and cancel reach the remote process even after the local ssh client dies.
 *   - no secrets are stored: only paths to keys are passed; password auth is
 *     intentionally unsupported.
 */

import { BackendError, describeCause } from '../errors.js';
import { isPlainObject, nonEmptyString, tailText } from '../util.js';

/** rsync excludes for syncing this checkout to a Linux server. */
export const DEFAULT_RSYNC_EXCLUDES = [
  '.git',
  '.venv',
  '.tmp',
  '.pytest_cache',
  '.mypy_cache',
  '__pycache__',
  'node_modules',
  '*.pyc',
  '*.pyo',
  '.DS_Store',
];

/** ssh connection hardening common to every invocation. */
export const SSH_CONNECT_OPTIONS = [
  '-o',
  'BatchMode=yes',
  '-o',
  'ConnectTimeout=10',
  '-o',
  'ServerAliveInterval=15',
  '-o',
  'ServerAliveCountMax=3',
  '-o',
  'StrictHostKeyChecking=accept-new',
  '-o',
  'LogLevel=ERROR',
];

/** Extra options for the port-forwarding ssh process. */
export const SSH_TUNNEL_OPTIONS = ['-o', 'ExitOnForwardFailure=yes'];

const SAFE_SHELL_TOKEN_RE = /^[A-Za-z0-9_@%+=:,./-]+$/;

/**
 * POSIX shell single-quote for one token. Safe tokens are returned verbatim so
 * commands stay readable in logs.
 * @param {unknown} value
 * @returns {string}
 */
export function shellQuote(value) {
  const text = String(value ?? '');
  if (text.length > 0 && SAFE_SHELL_TOKEN_RE.test(text)) return text;
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

/**
 * Expression that yields a remote path when interpreted by the remote shell.
 * Absolute paths become single-quoted literals; `~/x` becomes "$HOME/x" (the
 * quotes still expand $HOME, unlike a single-quoted `~/x`).
 * @param {string} value
 * @returns {string}
 */
export function remotePathExpression(value) {
  const text = nonEmptyString(value);
  if (!text) throw new BackendError('INVALID_ARGUMENT', '远程路径不能为空');
  if (text === '~') return '"$HOME"';
  if (text.startsWith('~/')) {
    const rest = text.slice(2).replace(/([\\"$`])/g, '\\$1');
    return `"$HOME/${rest}"`;
  }
  return shellQuote(text);
}

/**
 * Quote one CLI argument for a remote command: `~/...` values are expanded,
 * everything else is a literal.
 * @param {string} value
 */
export function remoteArgExpression(value) {
  const text = String(value ?? '');
  if (text === '~' || text.startsWith('~/')) return remotePathExpression(text);
  return shellQuote(text);
}

/**
 * Normalize an SSH target from several accepted shapes:
 *   'alias', 'user@host', {host,user,port,keyPath}
 *
 * @param {string|object|null|undefined} sshInput
 * @param {object|null} [fallback] e.g. config.remote.ssh
 * @returns {{host: string, user: string|null, port: number|null, keyPath: string|null, target: string}}
 */
export function normalizeSshConfig(sshInput, fallback = null) {
  let raw = sshInput;
  if (raw === undefined || raw === null || raw === '') raw = fallback;
  if (typeof raw === 'string') raw = { host: raw };
  if (raw === undefined || raw === null) raw = {};
  if (!isPlainObject(raw)) {
    throw new BackendError(
      'INVALID_ARGUMENT',
      'remote.ssh 必须是对象 {host,user,port,keyPath} 或字符串（ssh config alias / user@host）',
    );
  }
  const explicitUser = nonEmptyString(raw.user);
  let host = nonEmptyString(raw.host) || nonEmptyString(fallback?.host);
  let user = explicitUser || nonEmptyString(fallback?.user);
  if (host && host.includes('@')) {
    const at = host.indexOf('@');
    const hostUser = host.slice(0, at);
    const hostName = host.slice(at + 1);
    if (hostUser && hostName) {
      // user@host is an explicit request: it wins over a fallback user.
      if (!explicitUser) user = hostUser;
      host = hostName;
    }
  }
  if (!host) {
    throw new BackendError(
      'INVALID_ARGUMENT',
      '远程 SSH 主机未配置：请设置 remote.ssh.host（支持 ssh config alias 或 user@host）',
      { field: 'remote.ssh.host' },
    );
  }
  let port = raw.port ?? fallback?.port ?? null;
  if (port !== null && port !== undefined && port !== '') {
    port = Number(port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new BackendError('INVALID_ARGUMENT', `SSH 端口无效: ${String(raw.port)}（应为 1-65535）`, {
        field: 'remote.ssh.port',
      });
    }
  } else {
    port = null;
  }
  const keyPath = nonEmptyString(raw.keyPath) || nonEmptyString(fallback?.keyPath) || null;
  return {
    host,
    user,
    port,
    keyPath,
    target: user ? `${user}@${host}` : host,
  };
}

/**
 * ssh option args shared by interactive exec, rsync -e and tunnels.
 * @param {ReturnType<typeof normalizeSshConfig>} ssh
 */
export function buildSshOptionArgs(ssh) {
  const args = [];
  if (ssh.port) args.push('-p', String(ssh.port));
  if (ssh.keyPath) args.push('-i', ssh.keyPath);
  args.push(...SSH_CONNECT_OPTIONS);
  return args;
}

/**
 * Full ssh argv executing `remoteCommand` on the target host.
 * @param {string|object} sshInput
 * @param {string} remoteCommand shell script interpreted by the remote shell
 * @param {{tty?: boolean, extraOptions?: string[], sshBinary?: string, fallback?: object|null}} [options]
 * @returns {{command: string, args: string[], ssh: ReturnType<typeof normalizeSshConfig>}}
 */
export function buildSshArgv(sshInput, remoteCommand, options = {}) {
  const ssh = normalizeSshConfig(sshInput, options.fallback || null);
  const args = buildSshOptionArgs(ssh);
  if (Array.isArray(options.extraOptions)) args.push(...options.extraOptions);
  if (options.tty !== true) args.push('-T');
  args.push(ssh.target);
  if (remoteCommand !== undefined && remoteCommand !== null) args.push(String(remoteCommand));
  return { command: options.sshBinary || 'ssh', args, ssh };
}

/**
 * ssh argv for `-N -L localPort:remoteHost:remotePort` port forwarding.
 * @param {string|object} sshInput
 * @param {{localPort: number, remotePort: number, remoteHost?: string, sshBinary?: string, fallback?: object|null}} options
 */
export function buildSshTunnelArgv(sshInput, options) {
  const ssh = normalizeSshConfig(sshInput, options.fallback || null);
  const localPort = Number(options.localPort);
  const remotePort = Number(options.remotePort);
  if (!Number.isInteger(localPort) || localPort < 1 || localPort > 65535) {
    throw new BackendError('INVALID_ARGUMENT', `本地隧道端口无效: ${String(options.localPort)}`);
  }
  if (!Number.isInteger(remotePort) || remotePort < 1 || remotePort > 65535) {
    throw new BackendError('INVALID_ARGUMENT', `远端服务端口无效: ${String(options.remotePort)}`);
  }
  const remoteHost = options.remoteHost || '127.0.0.1';
  const args = [
    ...buildSshOptionArgs(ssh),
    ...SSH_TUNNEL_OPTIONS,
    '-N',
    '-L',
    `${localPort}:${remoteHost}:${remotePort}`,
    ssh.target,
  ];
  return { command: options.sshBinary || 'ssh', args, ssh };
}

/**
 * rsync argv copying a local CA checkout to the remote host.
 * @param {object} options
 * @param {string|object} options.ssh
 * @param {string} options.sourceDir local CA checkout (contents are copied)
 * @param {string} options.targetDir remote directory
 * @param {boolean} [options.delete] default true
 * @param {string[]} [options.excludes]
 * @param {string} [options.rsyncBinary]
 * @param {object|null} [options.fallbackSsh]
 */
export function buildRsyncArgv(options) {
  const ssh = normalizeSshConfig(options.ssh, options.fallbackSsh || null);
  const sourceDir = nonEmptyString(options.sourceDir);
  const targetDir = nonEmptyString(options.targetDir);
  if (!sourceDir) throw new BackendError('INVALID_ARGUMENT', 'rsync 源目录不能为空');
  if (!targetDir) {
    throw new BackendError(
      'BACKEND_NOT_CONFIGURED',
      '远程安装目录未配置：请设置 remote.installDir（或 remote.repoPath 指向已有检出）',
      { field: 'remote.installDir' },
    );
  }
  const sshCommand = [options.sshBinary || 'ssh', ...buildSshOptionArgs(ssh)]
    .map((token) => shellQuote(token))
    .join(' ');
  const args = ['-az'];
  if (options.delete !== false) args.push('--delete');
  for (const exclude of options.excludes || DEFAULT_RSYNC_EXCLUDES) {
    args.push(`--exclude=${exclude}`);
  }
  args.push('-e', sshCommand);
  args.push(sourceDir.endsWith('/') ? sourceDir : `${sourceDir}/`);
  args.push(`${ssh.target}:${shellQuote(targetDir)}`);
  return { command: options.rsyncBinary || 'rsync', args, ssh };
}

/** Export lines for configured environment variables. */
export function envExportLines(env) {
  const lines = [];
  for (const [key, value] of Object.entries(env || {})) {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      throw new BackendError('INVALID_ARGUMENT', `远程环境变量名无效: ${key}`);
    }
    lines.push(`export ${key}=${shellQuote(value)}`);
  }
  return lines;
}

/** Directory (remote shell expression) where PID files/logs are kept. */
export function remoteRunDirExpression(repoPath, runDir) {
  const base = nonEmptyString(repoPath);
  if (runDir) return remotePathExpression(runDir);
  if (!base) return '"$HOME/.dsh-ca"';
  return remotePathExpression(`${base}/.dsh-ca`);
}

/**
 * Remote script that starts `ca serve` in the foreground with a PID file.
 *
 * `exec` replaces the shell, so `$$` (written to the PID file first) is the CA
 * server PID; a later `kill -TERM "$(cat pidfile)"` therefore stops the server
 * even though it lives on the far side of ssh.
 *
 * @param {object} options
 * @param {string} options.repoPath remote CA checkout (cwd)
 * @param {string} options.python remote python interpreter
 * @param {string|null} [options.db]
 * @param {string|null} [options.dbDir]
 * @param {string|null} [options.stateDb]
 * @param {string|null} [options.repo] `ca serve --repo`
 * @param {string|null} [options.compdb]
 * @param {string} [options.host] bind host, default 127.0.0.1
 * @param {number} [options.port] 0 = OS-assigned
 * @param {string|null} [options.runDir]
 * @param {string} [options.pidFile] per-instance PID file stem (default "serve")
 * @param {Record<string,string>} [options.env]
 */
export function buildRemoteServeScript(options) {
  const repoPath = nonEmptyString(options.repoPath);
  if (!repoPath) {
    throw new BackendError(
      'BACKEND_NOT_CONFIGURED',
      '远程 CA 检出路径未配置：请设置 remote.repoPath（已有检出）或 remote.installDir（安装目标）',
      { field: 'remote.repoPath' },
    );
  }
  const python = nonEmptyString(options.python) || `${repoPath}/.venv/bin/python`;
  const runDir = remoteRunDirExpression(repoPath, options.runDir);
  const pidFile = sanitizeTag(options.pidFile || 'serve');
  const lines = ['set -e', ...envExportLines(options.env)];
  lines.push(`RUN_DIR=${runDir}`);
  lines.push('mkdir -p "$RUN_DIR"');
  lines.push(`cd ${remotePathExpression(repoPath)}`);
  lines.push(`echo $$ > "$RUN_DIR/${pidFile}.pid"`);
  const parts = [
    remotePathExpression(python),
    '-m',
    'ca',
    'serve',
    '--host',
    shellQuote(options.host || '127.0.0.1'),
    '--port',
    String(Number.isInteger(Number(options.port)) ? Number(options.port) : 0),
  ];
  if (options.db) parts.push('--db', remotePathExpression(options.db));
  else if (options.dbDir) parts.push('--db-dir', remotePathExpression(options.dbDir));
  if (options.stateDb) parts.push('--state-db', remotePathExpression(options.stateDb));
  if (options.repo) parts.push('--repo', remotePathExpression(options.repo));
  if (options.compdb) parts.push('--compdb', remotePathExpression(options.compdb));
  lines.push(`exec ${parts.join(' ')}`);
  return lines.join('\n');
}

/**
 * Remote script that stops the server recorded in `<runDir>/<pidFile>`.
 * @param {{repoPath?: string|null, runDir?: string|null, pidFile?: string, signal?: string}} options
 */
export function buildRemoteStopScript(options = {}) {
  const pidFile = sanitizeTag(options.pidFile || 'serve') ;
  const runDir = remoteRunDirExpression(options.repoPath, options.runDir);
  const signal = /^[A-Z]+$/.test(String(options.signal || 'TERM')) ? String(options.signal || 'TERM') : 'TERM';
  return [
    'set +e',
    `RUN_DIR=${runDir}`,
    `PID_FILE="$RUN_DIR/${pidFile}.pid"`,
    'if [ -f "$PID_FILE" ]; then',
    '  PID=$(cat "$PID_FILE" 2>/dev/null)',
    '  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then',
    `    kill -${signal} "$PID" 2>/dev/null`,
    '    i=0',
    '    while [ $i -lt 25 ] && kill -0 "$PID" 2>/dev/null; do',
    '      sleep 0.2',
    '      i=$((i+1))',
    '    done',
    '    if kill -0 "$PID" 2>/dev/null; then kill -KILL "$PID" 2>/dev/null; fi',
    '  fi',
    '  rm -f "$PID_FILE"',
    'fi',
    'echo "CA_STOP_DONE"',
  ].join('\n');
}

/** Keep PID file names to a safe alphabet. */
export function sanitizeTag(value) {
  const text = String(value || 'task').replace(/[^A-Za-z0-9_-]/g, '-');
  return text.length > 0 ? text.slice(0, 64) : 'task';
}

/**
 * Remote script executing one CA CLI command (index/update/...), with a PID
 * file so a cancelled task can be killed remotely.
 *
 * @param {object} options
 * @param {string} options.repoPath remote cwd
 * @param {string} options.python remote interpreter
 * @param {string[]} options.args CA CLI args
 * @param {string|null} [options.runDir]
 * @param {string} [options.pidTag]
 * @param {Record<string,string>} [options.env]
 */
export function buildRemoteCaScript(options) {
  const repoPath = nonEmptyString(options.repoPath);
  if (!repoPath) {
    throw new BackendError(
      'BACKEND_NOT_CONFIGURED',
      '远程 CA 检出路径未配置：请设置 remote.repoPath 或 remote.installDir',
    );
  }
  const python = nonEmptyString(options.python) || `${repoPath}/.venv/bin/python`;
  const args = Array.isArray(options.args) ? options.args.map(String) : [];
  const lines = ['set -e', ...envExportLines(options.env)];
  const runDir = remoteRunDirExpression(repoPath, options.runDir);
  lines.push(`RUN_DIR=${runDir}`);
  lines.push('mkdir -p "$RUN_DIR"');
  lines.push(`cd ${remotePathExpression(repoPath)}`);
  const pidTag = sanitizeTag(options.pidTag || 'task');
  lines.push(`echo $$ > "$RUN_DIR/${pidTag}.pid"`);
  const parts = [remotePathExpression(python), '-m', 'ca', ...args.map(remoteArgExpression)];
  lines.push(`exec ${parts.join(' ')}`);
  return lines.join('\n');
}

/**
 * Remote probe script: prints `CA_PROBE:<key>=<value>` markers that
 * {@link parseProbeOutput} turns into a report. One ssh round trip.
 *
 * @param {object} [options]
 * @param {string|null} [options.repoPath]
 * @param {string|null} [options.installDir]
 * @param {string|null} [options.python]
 * @param {string|null} [options.db]
 * @param {string|null} [options.dbDir]
 * @param {Record<string,string>} [options.env]
 */
export function buildProbeScript(options = {}) {
  const lines = ['set +e', ...envExportLines(options.env)];
  lines.push('echo "CA_PROBE:begin=1"');
  lines.push('echo "CA_PROBE:os=$(uname -s 2>/dev/null)"');
  lines.push('echo "CA_PROBE:arch=$(uname -m 2>/dev/null)"');
  lines.push('echo "CA_PROBE:nproc=$( (nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0) )"');
  lines.push('echo "CA_PROBE:home=$HOME"');
  lines.push('echo "CA_PROBE:python3=$(command -v python3 2>/dev/null)"');
  lines.push('PYV=$(python3 -V 2>&1)');
  lines.push('echo "CA_PROBE:python3_version=$PYV"');
  lines.push('PY313=$(command -v python3.13 2>/dev/null)');
  lines.push('echo "CA_PROBE:python313=$PY313"');
  lines.push('if [ -n "$PY313" ]; then echo "CA_PROBE:python313_version=$($PY313 -V 2>&1)"; fi');
  lines.push('PY312=$(command -v python3.12 2>/dev/null)');
  lines.push('echo "CA_PROBE:python312=$PY312"');
  lines.push('if [ -n "$PY312" ]; then echo "CA_PROBE:python312_version=$($PY312 -V 2>&1)"; fi');
  lines.push('PY311=$(command -v python3.11 2>/dev/null)');
  lines.push('echo "CA_PROBE:python311=$PY311"');
  lines.push('if [ -n "$PY311" ]; then echo "CA_PROBE:python311_version=$($PY311 -V 2>&1)"; fi');
  lines.push('echo "CA_PROBE:pip=$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null)"');
  lines.push('echo "CA_PROBE:rsync=$(command -v rsync 2>/dev/null)"');
  lines.push('echo "CA_PROBE:ca_on_path=$(command -v ca 2>/dev/null)"');
  lines.push('echo "CA_PROBE:ssh_ok=1"');

  if (options.repoPath) {
    lines.push(`REPO=${remotePathExpression(options.repoPath)}`);
    lines.push('if [ -d "$REPO" ]; then echo "CA_PROBE:repo_dir=1"; else echo "CA_PROBE:repo_dir=0"; fi');
    lines.push('if [ -f "$REPO/pyproject.toml" ]; then echo "CA_PROBE:repo_manifest=1"; else echo "CA_PROBE:repo_manifest=0"; fi');
    lines.push(
      `if grep -Eq 'name[[:space:]]*=[[:space:]]*"ca-analyzer"' "$REPO/pyproject.toml" 2>/dev/null; then echo "CA_PROBE:repo_ca=1"; else echo "CA_PROBE:repo_ca=0"; fi`,
    );
    lines.push('if [ -d "$REPO" ] && [ -w "$REPO" ]; then echo "CA_PROBE:repo_writable=1"; else echo "CA_PROBE:repo_writable=0"; fi');
    lines.push('if [ -x "$REPO/.venv/bin/python" ]; then echo "CA_PROBE:venv_python=1"; else echo "CA_PROBE:venv_python=0"; fi');
    lines.push('if [ -x "$REPO/.venv/bin/ca" ]; then echo "CA_PROBE:venv_ca=1"; else echo "CA_PROBE:venv_ca=0"; fi');
  }
  if (options.python) {
    lines.push(`PYCFG=${remotePathExpression(options.python)}`);
    lines.push('if [ -x "$PYCFG" ]; then echo "CA_PROBE:python_configured=1"; else echo "CA_PROBE:python_configured=0"; fi');
  }
  // Prefer the configured interpreter (may be an out-of-repo venv), then the
  // repo venv, then the `ca` console script on PATH.
  lines.push('VP=""');
  if (options.python) {
    lines.push('if [ -x "$PYCFG" ]; then VP="$PYCFG"; fi');
  }
  if (options.repoPath) {
    const repoVenvPython = remotePathExpression(`${options.repoPath}/.venv/bin/python`);
    lines.push(`if [ -z "$VP" ] && [ -x ${repoVenvPython} ]; then VP=${repoVenvPython}; fi`);
  }
  lines.push('if [ -n "$VP" ]; then');
  lines.push('  echo "CA_PROBE:python_used=$VP"');
  lines.push('  CV=$("$VP" -m ca version --json 2>/dev/null | tr "\\n" " ")');
  lines.push('  echo "CA_PROBE:ca_version_json=$CV"');
  lines.push('  LC=$("$VP" -c \'import clang.cindex\' 2>/dev/null && echo ok)');
  lines.push('  echo "CA_PROBE:libclang=$LC"');
  lines.push('fi');
  lines.push('if [ -z "$VP" ] && command -v ca >/dev/null 2>&1; then');
  lines.push('  CV=$(ca version --json 2>/dev/null | tr "\\n" " ")');
  lines.push('  echo "CA_PROBE:ca_version_json=$CV"');
  lines.push('fi');
  if (options.installDir) {
    lines.push(`IDIR=${remotePathExpression(options.installDir)}`);
    lines.push('if [ -d "$IDIR" ]; then echo "CA_PROBE:install_dir=1"; else echo "CA_PROBE:install_dir=0"; fi');
    lines.push('IPARENT=$(dirname "$IDIR")');
    lines.push('if [ -d "$IPARENT" ] && [ -w "$IPARENT" ]; then echo "CA_PROBE:install_parent_writable=1"; else echo "CA_PROBE:install_parent_writable=0"; fi');
  }
  if (options.db) {
    lines.push(`DBF=${remotePathExpression(options.db)}`);
    lines.push('if [ -f "$DBF" ]; then echo "CA_PROBE:db_file=1"; else echo "CA_PROBE:db_file=0"; fi');
    lines.push('DBB=$(wc -c < "$DBF" 2>/dev/null | tr -d " ")');
    lines.push('echo "CA_PROBE:db_bytes=$DBB"');
  }
  if (options.dbDir) {
    lines.push(`DBD=${remotePathExpression(options.dbDir)}`);
    lines.push('if [ -d "$DBD" ]; then echo "CA_PROBE:db_dir=1"; else echo "CA_PROBE:db_dir=0"; fi');
    lines.push('DBS=$(ls -1 "$DBD"/*.db 2>/dev/null | wc -l | tr -d " ")');
    lines.push('echo "CA_PROBE:db_shards=$DBS"');
  }
  const diskProbeExpr = options.repoPath
    ? remotePathExpression(options.repoPath)
    : options.installDir
      ? remotePathExpression(options.installDir)
      : '"$HOME"';
  lines.push(
    `echo "CA_PROBE:disk_kb=$(df -Pk ${diskProbeExpr} 2>/dev/null | awk 'NR==2 {print $4}')"`,
  );
  lines.push('echo "CA_PROBE:end=1"');
  return lines.join('\n');
}

/**
 * Parse `CA_PROBE:key=value` markers.
 * @param {string} stdout
 * @returns {{markers: Record<string,string>, complete: boolean}}
 */
export function parseProbeOutput(stdout) {
  /** @type {Record<string,string>} */
  const markers = {};
  for (const rawLine of String(stdout ?? '').split(/\r?\n/)) {
    const line = rawLine.trim();
    const match = /^CA_PROBE:([^=]+)=(.*)$/.exec(line);
    if (!match) continue;
    markers[match[1].trim()] = match[2];
  }
  return { markers, complete: markers.begin === '1' && markers.end === '1' };
}

/**
 * Classify an ssh invocation failure into a stable code + actionable message.
 * @param {{code?: number|null, signal?: string|null, stderr?: string, stdout?: string,
 *          spawnError?: Error|null, timedOut?: boolean}} result
 */
export function classifySshFailure(result) {
  const stderr = String(result?.stderr || '');
  const stdout = String(result?.stdout || '');
  const detail = tailText(`${stderr}\n${stdout}`.trim(), 800);
  const tail = detail ? `；输出: ${detail}` : '';
  if (result?.spawnError) {
    return {
      code: 'SSH_UNREACHABLE',
      message: `无法启动 ssh 客户端：${describeCause(result.spawnError)}；请确认本机装有 OpenSSH`,
      hint: '安装 OpenSSH（macOS 自带）并确保 ssh 在 PATH 中',
    };
  }
  if (/Permission denied|publickey|Authentication failed|Host key verification failed/i.test(stderr)) {
    return {
      code: 'SSH_AUTH_FAILED',
      message: `SSH 认证失败${tail}；请确认已配置免密登录（BatchMode）或 remote.ssh.keyPath 指向可用私钥`,
      hint: 'ssh-copy-id <user>@<host> 或设置 remote.ssh.keyPath；不支持下密码/口令交互',
    };
  }
  if (/Could not resolve hostname|Name or service not known|nodename nor servname provided/i.test(stderr)) {
    return {
      code: 'SSH_HOST_UNKNOWN',
      message: `无法解析 SSH 主机名${tail}；请检查 remote.ssh.host 或 ~/.ssh/config 中的 alias`,
      hint: '确认主机名/alias 拼写与网络可达',
    };
  }
  if (
    result?.timedOut ||
    /Connection refused|timed out|No route to host|Network is unreachable|Connection timed out|Operation timed out/i.test(stderr)
  ) {
    return {
      code: 'SSH_UNREACHABLE',
      message: `无法连接远程主机${tail}；请确认主机在线、SSH 端口可达（remote.ssh.port）`,
      hint: '在终端手动执行 ssh <target> true 排查网络/端口/防火墙',
    };
  }
  return {
    code: 'SSH_FAILED',
    message: `ssh 执行失败（exitCode=${result?.code ?? 'null'}${result?.signal ? `, signal=${result.signal}` : ''}）${tail}`,
    hint: '在终端手动执行同一条 ssh 命令查看错误',
  };
}

/** Last `n` lines of rsync/ssh output for error details. */
export function outputTailOf(result, lines = 8) {
  const text = [result?.stderr, result?.stdout]
    .filter((part) => part && String(part).trim().length > 0)
    .map((part) => String(part).trimEnd())
    .join('\n');
  if (!text) return '';
  return tailText(text.split('\n').slice(-lines).join('\n'), 4000);
}

/**
 * Wrap shell lines into a `set -e` script with env exports.
 * @param {string[]} lines
 * @param {Record<string,string>} [env]
 */
export function buildRemoteShellScript(lines, env) {
  return ['set -e', ...envExportLines(env), ...lines].join('\n');
}

/**
 * Remote script creating the CA venv (idempotent: `python -m venv` reuses an
 * existing directory but does not reinstall packages).
 * @param {{repoPath: string, python: string, env?: Record<string,string>}} options
 */
export function buildRemoteVenvScript(options) {
  const repo = remotePathExpression(options.repoPath);
  const python = remotePathExpression(options.python);
  const venv = remotePathExpression(`${options.repoPath}/.venv`);
  return buildRemoteShellScript(
    [
      `mkdir -p ${repo}`,
      `${python} -m venv ${venv}`,
      `${remotePathExpression(`${options.repoPath}/.venv/bin/python`)} -V`,
    ],
    options.env,
  );
}

/**
 * Remote script installing pinned requirements into the CA venv. Offline mode
 * uses `--no-index --find-links <wheelDir>` (host must contain the wheels).
 *
 * @param {object} options
 * @param {string} options.repoPath
 * @param {string} [options.venvPython]
 * @param {boolean} [options.offline]
 * @param {string|null} [options.wheelDir]
 * @param {boolean} [options.upgradePip] default true unless offline
 * @param {Record<string,string>} [options.env]
 */
export function buildRemotePipScript(options) {
  const venvPython = options.venvPython || `${options.repoPath}/.venv/bin/python`;
  const requirements = remotePathExpression(`${options.repoPath}/requirements.txt`);
  const lines = [`VP=${remotePathExpression(venvPython)}`];
  const offline = options.offline === true;
  if (offline) {
    const wheelDir = nonEmptyString(options.wheelDir);
    if (!wheelDir) {
      throw new BackendError(
        'BACKEND_NOT_CONFIGURED',
        '离线安装需要 wheelDir：请把 requirements.txt 对应 wheel 传到远端并设置 installOptions.wheelDir',
        { field: 'installOptions.wheelDir' },
      );
    }
    lines.push(
      `"$VP" -m pip install --disable-pip-version-check --no-index --find-links ${remotePathExpression(wheelDir)} -r ${requirements}`,
    );
  } else {
    if (options.upgradePip !== false) {
      lines.push('"$VP" -m pip install --disable-pip-version-check --upgrade pip');
    }
    lines.push(`"$VP" -m pip install --disable-pip-version-check -r ${requirements}`);
  }
  return buildRemoteShellScript(lines, options.env);
}

/**
 * Remote script doing the editable install (`pip install -e .`). A wheel would
 * miss non-package data (store schema.sql, UI static assets), so editable is
 * the only supported mode.
 * @param {{repoPath: string, venvPython?: string, env?: Record<string,string>}} options
 */
export function buildRemoteEditableInstallScript(options) {
  const venvPython = options.venvPython || `${options.repoPath}/.venv/bin/python`;
  return buildRemoteShellScript(
    [
      `VP=${remotePathExpression(venvPython)}`,
      `"$VP" -m pip install --disable-pip-version-check -e ${remotePathExpression(options.repoPath)}`,
    ],
    options.env,
  );
}

/**
 * Remote script verifying the installed CA: prints `ca version --json`, checks
 * the console script and echoes `CA_VERIFY_DONE=1` on success.
 * @param {{repoPath: string, venvPython?: string, env?: Record<string,string>}} options
 */
export function buildRemoteVerifyScript(options) {
  const venvPython = options.venvPython || `${options.repoPath}/.venv/bin/python`;
  const venvCa = `${options.repoPath}/.venv/bin/ca`;
  return buildRemoteShellScript(
    [
      `VP=${remotePathExpression(venvPython)}`,
      'echo "CA_PY_VERSION=$("$VP" -V 2>&1)"',
      '"$VP" -m ca version --json',
      `if [ -x ${remotePathExpression(venvCa)} ]; then echo "CA_CONSOLE_OK=1"; else echo "CA_CONSOLE_OK=0"; fi`,
      'echo "CA_VERIFY_DONE=1"',
    ],
    options.env,
  );
}

/**
 * Remote script listing database candidates under one root directory.
 *
 * One ssh round trip, one directory level (plus root itself):
 *   - direct `*.db` files (skipping `-wal`/`-shm` sidecars);
 *   - subdirectories containing `*.db` files, with a `dbCount`;
 *   - the root itself as a `dbDir` candidate.
 *
 * Hidden directories and `.ca`/`.tmp` are skipped. Primary implementation is
 * python3 (JSON-safe for arbitrary paths); a POSIX shell + `stat` fallback is
 * used when python3 is unavailable. Output markers are parsed by
 * `parseRemoteDatabaseScanOutput` in ../databases.js.
 *
 * @param {{root: string, maxEntries?: number}} options
 */
export function buildListDatabasesScript(options = {}) {
  const root = nonEmptyString(options.root);
  if (!root) {
    throw new BackendError('INVALID_ARGUMENT', '数据库扫描根目录不能为空');
  }
  const rootExpr = remotePathExpression(root);
  const maxEntries = Math.max(1, Math.min(500, Math.trunc(Number(options.maxEntries) || 240)));
  const lines = [
    'set +e',
    `ROOT=${rootExpr}`,
    'echo "CA_DB_SCAN_ROOT=$ROOT"',
    'if [ ! -d "$ROOT" ]; then',
    '  echo "CA_DB_ERROR=root_missing"',
    '  exit 0',
    'fi',
    'if command -v python3 >/dev/null 2>&1; then',
    `  python3 - "$ROOT" ${maxEntries} <<'PY'`,
    'import json, os, sys',
    '',
    'root = sys.argv[1]',
    'max_entries = int(sys.argv[2])',
    'emitted = 0',
    '',
    'def emit(obj):',
    '    global emitted',
    '    if emitted >= max_entries:',
    '        return',
    '    emitted += 1',
    '    print("CA_DB_ENTRY:" + json.dumps(obj, separators=(",", ":")))',
    '',
    'def is_db_file(name):',
    '    return name.endswith(".db") and not name.endswith(("-wal", "-shm"))',
    '',
    'try:',
    '    entries = list(os.scandir(root))',
    'except OSError as exc:',
    '    print("CA_DB_ERROR=" + str(exc).replace(chr(10), " "))',
    '    sys.exit(0)',
    '',
    'root_db_count = 0',
    'for entry in entries:',
    '    try:',
    '        if entry.is_file() and is_db_file(entry.name):',
    '            root_db_count += 1',
    '            st = entry.stat()',
    '            emit({"kind": "db", "path": os.path.join(root, entry.name), "name": entry.name, "sizeBytes": st.st_size, "mtime": int(st.st_mtime * 1000), "source": "scan"})',
    '    except OSError:',
    '        pass',
    '',
    'try:',
    '    root_stat = os.stat(root)',
    '    emit({"kind": "dbDir", "path": root, "name": os.path.basename(root) or root, "sizeBytes": None, "mtime": int(root_stat.st_mtime * 1000), "dbCount": root_db_count, "source": "scan"})',
    'except OSError:',
    '    pass',
    '',
    'for entry in entries:',
    '    try:',
    '        if not entry.is_dir():',
    '            continue',
    '    except OSError:',
    '        continue',
    '    name = entry.name',
    '    if name.startswith(".") or name in (".ca", ".tmp"):',
    '        continue',
    '    path = os.path.join(root, name)',
    '    count = 0',
    '    try:',
    '        for child in os.scandir(path):',
    '            try:',
    '                if child.is_file() and is_db_file(child.name):',
    '                    count += 1',
    '            except OSError:',
    '                pass',
    '    except OSError:',
    '        continue',
    '    if count == 0:',
    '        continue',
    '    mtime = None',
    '    try:',
    '        mtime = int(entry.stat().st_mtime * 1000)',
    '    except OSError:',
    '        pass',
    '    emit({"kind": "dbDir", "path": path, "name": name, "sizeBytes": None, "mtime": mtime, "dbCount": count, "source": "scan"})',
    '',
    'print("CA_DB_DONE=1")',
    'PY',
    'else',
    '  # POSIX shell fallback (no python3): compute sizes with stat/wc.',
    '  EMITTED=0',
    '  ROOT_COUNT=0',
    '  for f in "$ROOT"/*.db; do',
    '    [ -f "$f" ] || continue',
    '    case "$f" in *-wal|*-shm) continue;; esac',
    '    ROOT_COUNT=$((ROOT_COUNT+1))',
    '    SIZE=$(wc -c < "$f" 2>/dev/null)',
    '    [ -n "$SIZE" ] || SIZE=0',
    '    MTIME=$(stat -c %Y "$f" 2>/dev/null)',
    '    [ -n "$MTIME" ] || MTIME=0',
    '    MTIME=$((MTIME*1000))',
    '    NAME=$(basename "$f")',
    `    printf 'CA_DB_ENTRY:{"kind":"db","path":"%s","name":"%s","sizeBytes":%s,"mtime":%s,"source":"scan"}\\n' "$f" "$NAME" "$SIZE" "$MTIME"`,
    '    EMITTED=$((EMITTED+1))',
    '    [ "$EMITTED" -ge ' + String(maxEntries) + ' ] && break',
    '  done',
    '  dir_mtime=$(stat -c %Y "$ROOT" 2>/dev/null)',
    '  [ -n "$dir_mtime" ] || dir_mtime=0',
    '  dir_mtime=$((dir_mtime*1000))',
    `  printf 'CA_DB_ENTRY:{"kind":"dbDir","path":"%s","name":"%s","sizeBytes":null,"mtime":%s,"dbCount":%s,"source":"scan"}\\n' "$ROOT" "$(basename "$ROOT")" "$dir_mtime" "$ROOT_COUNT"`,
    '  for d in "$ROOT"/*/; do',
    '    [ -d "$d" ] || continue',
    '    name=${d%/}',
    '    name=${name##*/}',
    '    case "$name" in .*) continue;; esac',
    '    [ "$name" = ".ca" ] && continue',
    '    [ "$name" = ".tmp" ] && continue',
    '    count=0',
    '    for f in "$d"*.db; do',
    '      [ -f "$f" ] || continue',
    '      case "$f" in *-wal|*-shm) continue;; esac',
    '      count=$((count+1))',
    '    done',
    '    [ "$count" -gt 0 ] || continue',
    '    MTIME=$(stat -c %Y "${d%/}" 2>/dev/null)',
    '    [ -n "$MTIME" ] || MTIME=0',
    '    MTIME=$((MTIME*1000))',
    "    printf 'CA_DB_ENTRY:{\"kind\":\"dbDir\",\"path\":\"%s\",\"name\":\"%s\",\"sizeBytes\":null,\"mtime\":%s,\"dbCount\":%s,\"source\":\"scan\"}\\n' \"${d%/}\" \"$name\" \"$MTIME\" \"$count\"",
    '    EMITTED=$((EMITTED+1))',
    '    [ "$EMITTED" -ge ' + String(maxEntries) + ' ] && break',
    '  done',
    '  echo "CA_DB_DONE=1"',
    'fi',
  ];
  return lines.join('\n');
}

/**
 * Remote script validating a database switch target without changing anything.
 * Prints `CA_DB_VALID=1:OK[:count[:size]]` or `CA_DB_VALID=0:<REASON>`.
 *
 * @param {{kind: 'db'|'dbDir', path: string}} options
 */
export function buildValidateDatabaseScript(options = {}) {
  const kind = options.kind;
  const target = nonEmptyString(options.path);
  if (kind !== 'db' && kind !== 'dbDir') {
    throw new BackendError('INVALID_ARGUMENT', `kind 必须是 'db' 或 'dbDir'（当前 ${JSON.stringify(kind)}）`);
  }
  if (!target) throw new BackendError('INVALID_ARGUMENT', '数据库路径不能为空');
  const targetExpr = remotePathExpression(target);
  const lines = [
    'set +e',
    `TARGET=${targetExpr}`,
    'if [ ! -e "$TARGET" ]; then',
    '  if [ ! -d "$TARGET" ]; then',
    '    echo "CA_DB_VALID=0:NOT_FOUND"',
    '    exit 0',
    '  fi',
    'fi',
  ];
  if (kind === 'dbDir') {
    lines.push(
      'if [ ! -d "$TARGET" ]; then echo "CA_DB_VALID=0:NOT_DIR"; exit 0; fi',
      'COUNT=0',
      'for f in "$TARGET"/*.db; do',
      '  [ -f "$f" ] || continue',
      '  case "$f" in *-wal|*-shm) continue;; esac',
      '  COUNT=$((COUNT+1))',
      'done',
      'if [ "$COUNT" -eq 0 ]; then echo "CA_DB_VALID=0:NO_DB"; else echo "CA_DB_VALID=1:OK:$COUNT"; fi',
    );
  } else {
    lines.push(
      'if [ ! -f "$TARGET" ]; then echo "CA_DB_VALID=0:NOT_FILE"; exit 0; fi',
      'case "$TARGET" in *.db) ;; *) echo "CA_DB_VALID=0:NOT_DB_EXTENSION"; exit 0;; esac',
      'SIZE=$(wc -c < "$TARGET" 2>/dev/null)',
      '[ -n "$SIZE" ] || SIZE=0',
      'echo "CA_DB_VALID=1:OK:1:$SIZE"',
    );
  }
  return lines.join('\n');
}
