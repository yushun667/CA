/**
 * Simple SSH authentication for the "one directory on a server" UX.
 *
 * Users should only need `user@host` + optional port + a private key (or
 * ssh-agent), or a password. This module owns:
 *   - parsing/normalizing the `{host,user,port,keyPath,password}` shape;
 *   - building ssh argv for passwordless vs password auth (BatchMode only when
 *     no password is needed);
 *   - the temporary SSH_ASKPASS helper used for password auth on POSIX hosts
 *     (macOS OpenSSH 10.2 `SSH_ASKPASS_REQUIRE=force` verified). The helper
 *     reads the password from an environment variable, so the secret never
 *     appears in argv, files, logs or error messages;
 *   - one-shot `testRemoteConnection()` and `listRemoteDirs()` helpers that
 *     reuse the connection for the UI's remote directory picker.
 *
 * Windows password auth is not supported yet (OpenSSH for Windows does not
 * support SSH_ASKPASS in this setup): callers get
 * PASSWORD_AUTH_UNSUPPORTED_ON_WINDOWS and should use a key/ssh-agent.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn as nodeSpawn } from 'node:child_process';

import { buildChildEnv, runProcess as runProcessOneShot } from '../process.js';
import { BackendError, describeCause, normalizeError } from '../errors.js';
import { nonEmptyString } from '../util.js';
import {
  buildSshOptionArgs,
  buildSshArgv as buildBaseSshArgv,
  classifySshFailure,
  normalizeSshConfig,
  remotePathExpression,
} from './commands.js';

/** Environment variable used to hand the password to the askpass helper. */
export const ASKPASS_PASSWORD_ENV = 'DSH_CA_SSH_ASKPASS_PASSWORD';

/** Max directory entries returned by {@link listRemoteDirs}. */
export const MAX_REMOTE_DIR_ENTRIES = 500;

/**
 * Normalize any accepted SSH auth input:
 *   'alias' | 'user@host' | {host,user,port,keyPath,password,agent}
 *
 * The returned object keeps `password` in memory only; call
 * {@link publicSshConfig} before persisting/logging anything.
 *
 * @param {unknown} input
 * @param {object|null} [fallback]
 * @returns {{host: string, user: string|null, port: number|null, keyPath: string|null,
 *            target: string, password: string|null, agent: boolean}}
 */
export function normalizeSshAuth(input, fallback = null) {
  const raw = input && typeof input === 'object' ? input : {};
  const password = nonEmptyString(raw.password) || null;
  const ssh = normalizeSshConfig(
    typeof input === 'string' ? input : raw,
    fallback || null,
  );
  const agent = raw.agent === true;
  return { ...ssh, password, agent };
}

/** Persistable/loggable SSH config: strips the password. */
export function publicSshConfig(sshInput) {
  const normalized = normalizeSshAuth(sshInput);
  return {
    host: normalized.host,
    user: normalized.user,
    port: normalized.port,
    keyPath: normalized.keyPath,
  };
}

/** Stable map key for an ssh target (`user@host:port`). */
export function sshTargetKey(sshInput) {
  const ssh = normalizeSshAuth(sshInput);
  return `${ssh.user || ''}@${ssh.host}:${ssh.port || 22}`;
}

/** The askpass helper: reads the password from env, never contains it. */
export const ASKPASS_HELPER_CONTENT = [
  '#!/bin/sh',
  `printf '%s\\n' "$${ASKPASS_PASSWORD_ENV}"`,
  '',
].join('\n');

function platformOf(deps = {}) {
  return deps.platform || process.platform;
}

/**
 * Create a temporary 0600 SSH_ASKPASS helper.
 * @param {{deps?: object, fsImpl?: typeof import('node:fs')}} [options]
 * @returns {{path: string, dir: string, cleanup: () => void}}
 */
export function createAskpassHelper(options = {}) {
  const fsImpl = options.fsImpl || fs;
  const base = options.tmpDir || os.tmpdir();
  const dir = fsImpl.mkdtempSync(path.join(base, 'dsh-ca-askpass-'));
  const helperPath = path.join(dir, 'askpass.sh');
  try {
    fsImpl.writeFileSync(helperPath, ASKPASS_HELPER_CONTENT, { encoding: 'utf8', mode: 0o600 });
    try {
      fsImpl.chmodSync(helperPath, 0o600);
    } catch {
      // best effort (Windows)
    }
  } catch (error) {
    try {
      fsImpl.rmSync(dir, { recursive: true, force: true });
    } catch {
      // ignore
    }
    throw new BackendError('ASKPASS_SETUP_FAILED', `无法创建 SSH_ASKPASS helper：${describeCause(error)}`);
  }
  let cleaned = false;
  return {
    path: helperPath,
    dir,
    cleanup() {
      if (cleaned) return;
      cleaned = true;
      try {
        fsImpl.rmSync(dir, { recursive: true, force: true });
      } catch {
        // helper is in the OS temp dir; failing to delete is not fatal
      }
    },
  };
}

/**
 * Build a reusable ssh auth session.
 *
 * Passwordless: BatchMode=yes (existing behaviour, agent/key/ssh config).
 * Password: BatchMode off + keyboard-interactive/password prompts limited to
 * one, with SSH_ASKPASS pointing at a throwaway 0600 helper. The caller MUST
 * call `cleanup()` when the ssh command/process finishes.
 *
 * @param {object} options
 * @param {unknown} options.ssh
 * @param {object} [options.deps] injectables: platform, display, fsImpl, tmpDir
 * @param {any} [options.logger]
 * @returns {{ssh: object, password: string|null, method: 'agent'|'key'|'password',
 *            batchMode: boolean, extraOptions: string[], env: Record<string,string>,
 *            askpass: {path: string, dir: string, cleanup: () => void}|null,
 *            cleanup: () => void}}
 */
export function createSshAuthSession(options) {
  const deps = options.deps || {};
  const ssh = normalizeSshAuth(options.ssh);
  const hasPassword = Boolean(ssh.password);
  const method = hasPassword ? 'password' : ssh.keyPath ? 'key' : 'agent';
  const password = ssh.password;
  delete ssh.password;
  if (hasPassword && platformOf(deps) === 'win32') {
    throw new BackendError(
      'PASSWORD_AUTH_UNSUPPORTED_ON_WINDOWS',
      'Windows 暂不支持密码认证：请改用私钥或 ssh-agent（免密）登录远程服务器。',
      { platform: 'win32', auth: 'password' },
    );
  }
  if (!hasPassword) {
    return {
      ssh,
      password: null,
      method,
      batchMode: true,
      extraOptions: [],
      env: {},
      askpass: null,
      cleanup() {},
    };
  }
  const askpass = createAskpassHelper({ fsImpl: deps.fsImpl, tmpDir: deps.tmpDir });
  const display = nonEmptyString(deps.display) || nonEmptyString(process.env.DISPLAY) || ':0';
  return {
    ssh,
    password,
    method,
    batchMode: false,
    extraOptions: [
      '-o',
      'PubkeyAuthentication=yes',
      '-o',
      'PreferredAuthentications=publickey,password,keyboard-interactive',
      '-o',
      'NumberOfPasswordPrompts=1',
    ],
    env: {
      SSH_ASKPASS: askpass.path,
      SSH_ASKPASS_REQUIRE: 'force',
      DISPLAY: display,
      [ASKPASS_PASSWORD_ENV]: password,
    },
    askpass,
    cleanup() {
      askpass.cleanup();
    },
  };
}

/**
 * Build ssh argv for a remote command, honouring password auth when a session
 * is supplied.
 *
 * @param {unknown} sshInput
 * @param {string} remoteCommand
 * @param {object} [options]
 * @param {ReturnType<typeof createSshAuthSession>|null} [options.session]
 * @param {string} [options.sshBinary]
 * @param {boolean} [options.tty]
 * @returns {{command: string, args: string[], ssh: object}}
 */
export function buildSshArgv(sshInput, remoteCommand, options = {}) {
  const session = options.session || null;
  const ssh = session ? session.ssh : normalizeSshAuth(sshInput);
  const extraOptions = [...(session?.extraOptions || []), ...(options.extraOptions || [])];
  const args = buildSshOptionArgs(ssh, { batchMode: session ? session.batchMode : true });
  if (extraOptions.length > 0) args.push(...extraOptions);
  if (options.tty !== true) args.push('-T');
  args.push(ssh.target);
  if (remoteCommand !== undefined && remoteCommand !== null) args.push(String(remoteCommand));
  return { command: options.sshBinary || 'ssh', args, ssh };
}

function defaultRunProcess(deps) {
  if (typeof deps.runProcess === 'function') return deps.runProcess;
  const spawnFn = typeof deps.spawn === 'function' ? deps.spawn : nodeSpawn;
  return (runOptions) => runProcessOneShot({ spawn: spawnFn, ...runOptions });
}

/**
 * Execute one remote script with auth handling (askpass env, cleanup).
 *
 * @param {object} options
 * @param {unknown} options.ssh
 * @param {string} options.script
 * @param {object} [options.deps]
 * @param {any} [options.logger]
 * @param {number} [options.timeoutMs]
 * @param {AbortSignal} [options.signal]
 * @param {string} [options.label]
 * @returns {Promise<{code: number|null, stdout: string, stderr: string, ssh: object, authMethod: string}>}
 */
export async function runSshWithAuth(options) {
  const deps = options.deps || {};
  const logger = options.logger || null;
  const session = createSshAuthSession({ ssh: options.ssh, deps, logger });
  try {
    const built = buildSshArgv(session.ssh, options.script, {
      session,
      sshBinary: deps.sshBinary,
    });
    logger?.info?.(`[ssh] ${options.label || 'run'}: ${session.ssh.target}（${session.method}）`);
    const result = await defaultRunProcess(deps)({
      command: built.command,
      args: built.args,
      env: buildChildEnv(session.env, deps.env || process.env),
      timeoutMs: options.timeoutMs || 30000,
      signal: options.signal,
      logger,
      maxOutputBytes: options.maxOutputBytes || 256 * 1024,
      onStdoutLine: options.onStdoutLine,
      onStderrLine: options.onStderrLine,
    });
    return { ...result, ssh: session.ssh, authMethod: session.method };
  } finally {
    session.cleanup();
  }
}

/** Classify a failed ssh result into an actionable BackendError (no secrets). */
export function sshFailureToBackendError(result) {
  const failure = classifySshFailure(result);
  return new BackendError(failure.code, failure.message, { hint: failure.hint });
}

function assertSshOk(result) {
  if (result?.cancelled) {
    throw new BackendError('CANCELLED', 'SSH 操作已取消');
  }
  if (result?.timedOut) {
    throw new BackendError('SSH_TIMEOUT', 'SSH 操作超时；请检查网络或主机地址');
  }
  if (result?.spawnError || result?.code !== 0) {
    throw sshFailureToBackendError(result);
  }
}

// ------------------------------------------------------------------------ //
// connection test
// ------------------------------------------------------------------------ //

/** Remote script for {@link testRemoteConnection}. */
export function buildConnectionTestScript() {
  return [
    'set +e',
    'echo "CA_CONN_HOME=$HOME"',
    'echo "CA_CONN_OS=$(uname -s 2>/dev/null)"',
    'echo "CA_CONN_ARCH=$(uname -m 2>/dev/null)"',
    // System Python: prefer an explicit 3.13/3.12/3.11 binary over python3.
    'PY=""',
    'for CAND in python3.13 python3.12 python3.11 python3; do',
    '  FOUND=$(command -v "$CAND" 2>/dev/null)',
    '  if [ -n "$FOUND" ]; then PY="$FOUND"; break; fi',
    'done',
    'echo "CA_CONN_PYTHON=$PY"',
    'if [ -n "$PY" ]; then echo "CA_CONN_PY_VERSION=$("$PY" -V 2>&1)"; fi',
    // Existing CA-era venvs on a server (avoid forcing the user to install Python again).
    'for VP in "$HOME/CA_OH/venv/bin/python" "$HOME/CA/venv/bin/python" "$HOME/ca/.venv/bin/python" "$HOME/.venv/bin/python" "$HOME/venv/bin/python" "$HOME/.dsh-ca/backend/venv/bin/python"; do',
    '  if [ -x "$VP" ]; then echo "CA_CONN_PY_CANDIDATE=$VP|$("$VP" -V 2>&1)"; fi',
    'done',
    'CA_BIN=$(command -v ca 2>/dev/null)',
    'echo "CA_CONN_CA=$CA_BIN"',
    'if [ -n "$CA_BIN" ]; then echo "CA_CONN_CA_VERSION=$("$CA_BIN" version --json 2>/dev/null | tr "\\n" " ")"; fi',
    'echo "CA_CONN_DONE=1"',
  ].join('\n');
}

function parseKeyValueMarkers(stdout, prefix = 'CA_CONN_') {
  /** @type {Record<string,string>} */
  const markers = {};
  for (const line of String(stdout || '').split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith(prefix)) continue;
    const body = trimmed.slice(prefix.length);
    const eq = body.indexOf('=');
    if (eq < 0) continue;
    markers[body.slice(0, eq)] = body.slice(eq + 1);
  }
  return markers;
}

/**
 * One SSH round trip: home, OS, python and CA availability.
 * `ok` means "SSH works"; a missing CA is reported via `ca.installed=false`.
 *
 * @param {unknown} ssh
 * @param {{deps?: object, logger?: any, timeoutMs?: number, signal?: AbortSignal}} [options]
 */
export async function testRemoteConnection(ssh, options = {}) {
  const normalized = normalizeSshAuth(ssh);
  const result = await runSshWithAuth({
    ssh: normalized,
    script: buildConnectionTestScript(),
    deps: options.deps,
    logger: options.logger,
    timeoutMs: Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : 30000,
    signal: options.signal,
    label: 'test connection',
  });
  assertSshOk(result);
  const markers = parseKeyValueMarkers(result.stdout);
  if (markers.DONE !== '1') {
    throw new BackendError(
      'PROBE_FAILED',
      `SSH 已连接但未能读取环境信息（${String(result.stdout || result.stderr || '').slice(-300) || '无输出'}）`,
    );
  }
  const osName = nonEmptyString(markers.OS) || 'unknown';
  const pythonCandidates = [];
  for (const line of String(result.stdout || '').split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('CA_CONN_PY_CANDIDATE=')) continue;
    const payload = trimmed.slice('CA_CONN_PY_CANDIDATE='.length);
    const separator = payload.indexOf('|');
    if (separator <= 0) continue;
    const candidatePath = payload.slice(0, separator);
    const candidateVersionRaw = payload.slice(separator + 1);
    const candidateVersion = /(\d+)\.(\d+)(?:\.(\d+))?/.exec(candidateVersionRaw);
    pythonCandidates.push({
      path: candidatePath,
      version: candidateVersion ? `${candidateVersion[1]}.${candidateVersion[2]}.${candidateVersion[3] || 0}` : null,
      versionText: candidateVersionRaw,
      supported: candidateVersion
        ? Number(candidateVersion[1]) > 3 ||
          (Number(candidateVersion[1]) === 3 && Number(candidateVersion[2]) >= 11)
        : false,
    });
  }
  const systemPythonPath = nonEmptyString(markers.PYTHON);
  const systemVersionRaw = nonEmptyString(markers.PY_VERSION);
  const systemVersion = systemVersionRaw ? /(\d+)\.(\d+)(?:\.(\d+))?/.exec(systemVersionRaw) : null;
  const systemSupported = systemVersion
    ? Number(systemVersion[1]) > 3 ||
      (Number(systemVersion[1]) === 3 && Number(systemVersion[2]) >= 11)
    : false;
  const selectedCandidate = systemSupported
    ? null
    : pythonCandidates.find((candidate) => candidate.supported) || null;
  const pythonPath = selectedCandidate ? selectedCandidate.path : systemPythonPath;
  const pythonVersionRaw = selectedCandidate ? selectedCandidate.versionText : systemVersionRaw;
  const pythonVersion = selectedCandidate
    ? /(\d+)\.(\d+)(?:\.(\d+))?/.exec(selectedCandidate.versionText)
    : systemVersion;
  const pythonSupported = selectedCandidate ? true : systemSupported;
  let caVersion = null;
  const caVersionRaw = nonEmptyString(markers.CA_VERSION);
  if (caVersionRaw) {
    const start = caVersionRaw.indexOf('{');
    try {
      const parsed = start >= 0 ? JSON.parse(caVersionRaw.slice(start)) : null;
      caVersion = parsed?.version || null;
    } catch {
      caVersion = null;
    }
  }
  const caBin = nonEmptyString(markers.CA);
  return {
    ok: true,
    authMethod: result.authMethod,
    target: normalized.target,
    home: nonEmptyString(markers.HOME) || null,
    platform: {
      os: osName,
      arch: nonEmptyString(markers.ARCH) || null,
      label: `${osName} ${nonEmptyString(markers.ARCH) || ''}`.trim(),
    },
    python: {
      path: pythonPath,
      version: pythonVersion ? `${pythonVersion[1]}.${pythonVersion[2]}.${pythonVersion[3] || 0}` : null,
      versionText: pythonVersionRaw,
      supported: pythonSupported,
      system: systemPythonPath,
      candidates: pythonCandidates,
    },
    ca: {
      installed: Boolean(caBin) || Boolean(caVersion),
      version: caVersion,
      path: caBin,
    },
  };
}

// ------------------------------------------------------------------------ //
// remote directory browsing
// ------------------------------------------------------------------------ //

/**
 * POSIX sh script listing sub-directories of one remote path.
 * @param {{path?: string|null}} [options]
 */
export function buildListDirsScript(options = {}) {
  const target = nonEmptyString(options.path);
  const targetExpr = target ? remotePathExpression(target) : '"$HOME"';
  return [
    'set +e',
    `TARGET=${targetExpr}`,
    'if [ -z "$TARGET" ]; then TARGET="$HOME"; fi',
    'echo "CA_DIR_HOME=$HOME"',
    'if [ ! -e "$TARGET" ]; then echo "CA_DIR_ERROR=NOT_FOUND"; exit 0; fi',
    'if [ ! -d "$TARGET" ]; then echo "CA_DIR_ERROR=NOT_DIRECTORY"; exit 0; fi',
    'if [ ! -r "$TARGET" ]; then echo "CA_DIR_ERROR=NOT_READABLE"; exit 0; fi',
    'REAL=$(cd "$TARGET" 2>/dev/null && pwd -P) || { echo "CA_DIR_ERROR=ACCESS_DENIED"; exit 0; }',
    'echo "CA_DIR_PATH=$REAL"',
    'echo "CA_DIR_PARENT=$(dirname "$REAL")"',
    'if [ "$REAL" = "$HOME" ]; then echo "CA_DIR_IS_HOME=1"; else echo "CA_DIR_IS_HOME=0"; fi',
    `COUNT=0`,
    'for d in "$REAL"/*/; do',
    '  [ -d "$d" ] || continue',
    '  NAME=$(basename "$d")',
    '  case "$NAME" in .*) continue;; esac',
    '  printf "CA_DIR_ENTRY=%s\\n" "$NAME"',
    '  COUNT=$((COUNT+1))',
    `  [ "$COUNT" -ge ${MAX_REMOTE_DIR_ENTRIES} ] && break`,
    'done',
    'echo "CA_DIR_DONE=1"',
  ].join('\n');
}

const REMOTE_DIR_ERRORS = {
  NOT_FOUND: { code: 'DIR_NOT_FOUND', message: '远程目录不存在' },
  NOT_DIRECTORY: { code: 'DIR_NOT_A_DIRECTORY', message: '该路径不是目录' },
  NOT_READABLE: { code: 'DIR_NOT_READABLE', message: '没有权限读取该目录' },
  ACCESS_DENIED: { code: 'DIR_NOT_READABLE', message: '没有权限读取该目录' },
};

/**
 * List immediate sub-directories of a remote path (one SSH round trip).
 * Empty `path` starts at the remote home directory.
 *
 * @param {unknown} ssh
 * @param {{path?: string|null, home?: string|null}} [selection]
 * @param {{deps?: object, logger?: any, timeoutMs?: number, signal?: AbortSignal}} [options]
 * @returns {Promise<{path: string, parent: string|null, home: string|null, isHome: boolean,
 *                    entries: Array<{name: string, path: string}>, authMethod: string}>}
 */
export async function listRemoteDirs(ssh, selection = {}, options = {}) {
  const normalized = normalizeSshAuth(ssh);
  let targetPath = nonEmptyString(selection.path);
  const knownHome = nonEmptyString(selection.home);
  if (!targetPath && knownHome) targetPath = knownHome;
  const result = await runSshWithAuth({
    ssh: normalized,
    script: buildListDirsScript({ path: targetPath }),
    deps: options.deps,
    logger: options.logger,
    timeoutMs: Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : 30000,
    signal: options.signal,
    label: 'browse remote directories',
  });
  assertSshOk(result);
  /** @type {Record<string,string>} */
  const markers = parseKeyValueMarkers(result.stdout, 'CA_DIR_');
  if (markers.ERROR) {
    const definition = REMOTE_DIR_ERRORS[markers.ERROR] || {
      code: 'REMOTE_PATH_ERROR',
      message: `远程目录访问失败（${markers.ERROR}）`,
    };
    throw new BackendError(definition.code, `${definition.message}: ${targetPath || markers.HOME || '(home)'}`, {
      reason: markers.ERROR,
      path: targetPath || null,
    });
  }
  if (markers.DONE !== '1') {
    throw new BackendError(
      'REMOTE_PATH_ERROR',
      `无法列出远程目录（${String(result.stdout || result.stderr || '').slice(-300) || '无输出'}）`,
    );
  }
  const resolvedPath = nonEmptyString(markers.PATH) || targetPath || markers.HOME || '/';
  const entries = [];
  for (const line of String(result.stdout || '').split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('CA_DIR_ENTRY=')) continue;
    const name = trimmed.slice('CA_DIR_ENTRY='.length);
    if (!nonEmptyString(name)) continue;
    entries.push({
      name,
      path: path.posix.join(resolvedPath, name),
    });
  }
  const home = nonEmptyString(markers.HOME) || knownHome || null;
  return {
    path: resolvedPath,
    parent: nonEmptyString(markers.PARENT),
    home,
    isHome: markers.IS_HOME === '1',
    entries,
    authMethod: result.authMethod,
  };
}

/** Format an SSE/ssh error for user facing text without leaking credentials. */
export function safeSshErrorText(error) {
  return normalizeError(error, 'SSH_FAILED', 'SSH 连接失败').message;
}
