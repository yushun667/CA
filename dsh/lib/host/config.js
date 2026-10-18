/**
 * Settings for the CA backend: defaults, merge order, validation, persistence.
 *
 * Merge order (later wins):
 *   1. {@link DEFAULT_CONFIG}
 *   2. the `config` passed to `createBackendManager(ctx, config)` by the host
 *      wiring (deployment defaults from the Cordis patch)
 *   3. user settings saved through the UI (`backend.configure`), stored in
 *      `$DSH_HOME/plugin-data/dsh-ca/settings.json`
 *
 * Only user edits are persisted (as a sparse patch), never the full resolved
 * object, so defaults can evolve without stale files pinning old values.
 * Nothing is written to the DSH profile (no package.json/cordis.patch.yml).
 *
 * The config contains no credentials. `remote.ssh.keyPath` is a local path to
 * a private key, not key material; SSH passwords are not supported on purpose
 * (use keys or an ssh-agent) and are never stored.
 */

import os from 'node:os';
import path from 'node:path';

import { BackendError } from './backend/errors.js';
import {
  clampInt,
  deepMerge,
  expandPath,
  isPlainObject,
  nonEmptyString,
  atomicWriteJson,
  readJsonFile,
} from './backend/util.js';

export const CONFIG_VERSION = 1;

/** Behaviour when `backend` is left at `'auto'`. */
export const BACKEND_MODES = Object.freeze(['auto', 'local', 'remote']);

/** All defaults; keep this aligned with CONTRACT.md. */
export const DEFAULT_CONFIG = Object.freeze({
  /** 'auto' | 'local' | 'remote' */
  backend: 'auto',
  /** Start the backend automatically when the DSH plugin applies. */
  autoStart: true,
  /** Timeout for CA HTTP requests (`request`/`requestStream` connect). */
  requestTimeoutMs: 30000,
  /** Max wait for `ca serve` to report ready (stderr URL line + /api/health). */
  readyTimeoutMs: 20000,
  /** Health poll interval while starting. */
  healthIntervalMs: 250,
  log: {
    maxBytes: 524288,
    keepFiles: 3,
    ringSize: 500,
  },
  local: {
    /** CA repository root; discovery walks up from cwd when null. */
    repoRoot: null,
    /** Python interpreter; falls back to <repo>/.venv/bin/python then python3. */
    python: null,
    /** SQLite index database (mutually exclusive with dbDir). */
    db: null,
    /** Federated shard directory (mutually exclusive with db). */
    dbDir: null,
    /** Writable job state DB; CA default is <db>.serve-state.db / <dbDir>/.ca/jobs.db. */
    stateDb: null,
    /** Repo root for deep tools (`ca serve --repo`). */
    repo: null,
    /** compile_commands.json for deep tools (`ca serve --compdb`). */
    compdb: null,
    host: '127.0.0.1',
    /** 0 = let CA bind an OS-assigned port and parse the URL from stderr. */
    port: 0,
    maxRestarts: 3,
    restartBackoffMs: 500,
    /** Extra environment variables for child processes. */
    env: {},
  },
  remote: {
    ssh: {
      /** Host, ssh config alias, or user@host. */
      host: null,
      /** SSH user; null means the ssh config/current user decides. */
      user: null,
      port: null,
      keyPath: null,
    },
    /** Existing CA checkout on the remote host (preferred over installDir). */
    repoPath: null,
    /** Directory to rsync this checkout into when repoPath is absent. */
    installDir: null,
    /** Remote Python interpreter (default python3). */
    python: null,
    /** Remote single DB (mutually exclusive with dbDir). */
    db: null,
    /** Remote federated shard directory (mutually exclusive with db). */
    dbDir: null,
    /** Writable remote job state DB (CA default applies when null). */
    stateDb: null,
    repo: null,
    compdb: null,
    /** Remote `ca serve --port`; 0 = OS-assigned, parsed from the ready line. */
    serveRemotePort: 0,
    /** Local tunnel port; 0 = pick a free local port. */
    localPort: 0,
    readyTimeoutMs: 20000,
    maxRestarts: 2,
    restartBackoffMs: 1000,
    env: {},
  },
});

/**
 * Resolve DSH state locations.
 *
 * `$DSH_HOME` defaults to `~/.dsh`; `$DSH_CA_STATE_DIR` is an explicit test /
 * power-user override and takes precedence over the derived path.
 *
 * @param {{env?: Record<string,string|undefined>, homedir?: string}} [options]
 */
export function resolveConfigPaths(options = {}) {
  const env = options.env || process.env;
  const homedir = options.homedir || os.homedir();
  const dshHome = nonEmptyString(env.DSH_HOME) || path.join(homedir, '.dsh');
  const stateDir =
    nonEmptyString(env.DSH_CA_STATE_DIR) || path.join(dshHome, 'plugin-data', 'dsh-ca');
  return {
    dshHome,
    stateDir,
    logDir: path.join(stateDir, 'logs'),
    settingsPath: path.join(stateDir, 'settings.json'),
  };
}

function configError(message, details) {
  return new BackendError('CONFIG_INVALID', message, details);
}

/** Require a string field or null. Returns the trimmed value (or null). */
function checkString(value, dotted, { allowEmpty = false, required = false } = {}) {
  if (value === undefined || value === null) {
    if (required) throw configError(`缺少配置项 ${dotted}（必须是非空字符串）`, { field: dotted });
    return null;
  }
  if (typeof value !== 'string') {
    throw configError(`配置项 ${dotted} 必须是字符串（当前 ${typeof value}）`, { field: dotted });
  }
  const trimmed = value.trim();
  if (!allowEmpty && trimmed.length === 0) return null;
  return trimmed;
}

function checkPath(value, dotted, { baseDir, homedir, required = false, remote = false } = {}) {
  const text = checkString(value, dotted, { required });
  if (text === null) return null;
  if (remote) {
    if (text.startsWith('~/') || text === '~' || path.posix.isAbsolute(text)) return text;
    throw configError(
      `配置项 ${dotted} 必须是远程绝对路径或 ~/ 开头（当前 "${text}"）；相对路径在远端无法可靠解析`,
      { field: dotted },
    );
  }
  return expandPath(text, baseDir);
}

function checkBoolean(value, dotted) {
  if (typeof value !== 'boolean') {
    throw configError(`配置项 ${dotted} 必须是 true 或 false（当前 ${JSON.stringify(value)}）`, {
      field: dotted,
    });
  }
  return value;
}

function checkPort(value, dotted, fallback) {
  const port = clampInt(value, 0, 65535, fallback);
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0 || value > 65535) {
    // clampInt silently clamps; configuration should fail loudly instead.
    throw configError(`配置项 ${dotted} 必须是 0-65535 的整数（0 = 自动选端口）`, { field: dotted });
  }
  return port;
}

function checkInteger(value, dotted, min, max, fallback) {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < min || value > max) {
    throw configError(`配置项 ${dotted} 必须是 ${min}-${max} 的整数（当前 ${JSON.stringify(value)}）`, {
      field: dotted,
    });
  }
  return value;
}

function checkEnv(value, dotted) {
  if (value === undefined || value === null) return {};
  if (!isPlainObject(value)) {
    throw configError(`配置项 ${dotted} 必须是对象（环境变量名 -> 字符串）`, { field: dotted });
  }
  /** @type {Record<string,string>} */
  const env = {};
  for (const [key, raw] of Object.entries(value)) {
    if (typeof raw === 'number' || typeof raw === 'boolean') env[key] = String(raw);
    else if (typeof raw === 'string') env[key] = raw;
    else throw configError(`配置项 ${dotted}.${key} 必须是字符串、数字或布尔值`, { field: `${dotted}.${key}` });
  }
  return env;
}

/**
 * Validate + normalize an already-merged config object. Throws BackendError
 * with an actionable message and the offending dotted path in `details`.
 *
 * @param {Record<string, any>} input
 * @param {{cwd?: string, homedir?: string}} [options]
 * @returns {Record<string, any>} normalized copy
 */
export function validateConfig(input, options = {}) {
  if (!isPlainObject(input)) throw configError('配置必须是对象', { field: '(root)' });
  const cwd = options.cwd || process.cwd();
  const config = deepMerge(DEFAULT_CONFIG, input);

  if (!BACKEND_MODES.includes(config.backend)) {
    throw configError(
      `配置项 backend 必须是 ${BACKEND_MODES.join('|')} 之一（当前 ${JSON.stringify(config.backend)}）`,
      { field: 'backend' },
    );
  }
  config.autoStart = checkBoolean(config.autoStart, 'autoStart');
  config.requestTimeoutMs = checkInteger(
    config.requestTimeoutMs,
    'requestTimeoutMs',
    1000,
    300000,
    DEFAULT_CONFIG.requestTimeoutMs,
  );
  config.readyTimeoutMs = checkInteger(
    config.readyTimeoutMs,
    'readyTimeoutMs',
    1000,
    120000,
    DEFAULT_CONFIG.readyTimeoutMs,
  );
  config.healthIntervalMs = checkInteger(
    config.healthIntervalMs,
    'healthIntervalMs',
    50,
    10000,
    DEFAULT_CONFIG.healthIntervalMs,
  );

  config.log = deepMerge(DEFAULT_CONFIG.log, config.log);
  config.log.maxBytes = checkInteger(config.log.maxBytes, 'log.maxBytes', 4096, 64 * 1024 * 1024, 524288);
  config.log.keepFiles = checkInteger(config.log.keepFiles, 'log.keepFiles', 1, 20, 3);
  config.log.ringSize = checkInteger(config.log.ringSize, 'log.ringSize', 16, 20000, 500);

  // -- local -------------------------------------------------------------- //
  config.local = deepMerge(DEFAULT_CONFIG.local, config.local);
  config.local.repoRoot = checkPath(config.local.repoRoot, 'local.repoRoot', { baseDir: cwd });
  config.local.python = checkPath(config.local.python, 'local.python', { baseDir: cwd });
  config.local.db = checkPath(config.local.db, 'local.db', { baseDir: cwd });
  config.local.dbDir = checkPath(config.local.dbDir, 'local.dbDir', { baseDir: cwd });
  if (config.local.db && config.local.dbDir) {
    throw configError(
      '配置项 local.db 与 local.dbDir 互斥；单库用 db，分片联邦用 dbDir。',
      { field: 'local.db' },
    );
  }
  config.local.stateDb = checkPath(config.local.stateDb, 'local.stateDb', { baseDir: cwd });
  config.local.repo = checkPath(config.local.repo, 'local.repo', { baseDir: cwd });
  config.local.compdb = checkPath(config.local.compdb, 'local.compdb', { baseDir: cwd });
  config.local.host = nonEmptyString(config.local.host) || '127.0.0.1';
  config.local.port = checkPort(config.local.port, 'local.port', 0);
  config.local.maxRestarts = checkInteger(config.local.maxRestarts, 'local.maxRestarts', 0, 20, 3);
  config.local.restartBackoffMs = checkInteger(
    config.local.restartBackoffMs,
    'local.restartBackoffMs',
    0,
    60000,
    500,
  );
  config.local.env = checkEnv(config.local.env, 'local.env');

  // -- remote ------------------------------------------------------------- //
  config.remote = deepMerge(DEFAULT_CONFIG.remote, config.remote);
  config.remote.ssh = deepMerge(DEFAULT_CONFIG.remote.ssh, config.remote.ssh);
  config.remote.ssh.host = checkString(config.remote.ssh.host, 'remote.ssh.host');
  config.remote.ssh.user = checkString(config.remote.ssh.user, 'remote.ssh.user');
  config.remote.ssh.keyPath = checkPath(config.remote.ssh.keyPath, 'remote.ssh.keyPath', { baseDir: cwd });
  if (config.remote.ssh.port !== null && config.remote.ssh.port !== undefined) {
    config.remote.ssh.port = checkInteger(config.remote.ssh.port, 'remote.ssh.port', 1, 65535, 22);
  } else {
    config.remote.ssh.port = null;
  }
  config.remote.repoPath = checkPath(config.remote.repoPath, 'remote.repoPath', { remote: true });
  config.remote.installDir = checkPath(config.remote.installDir, 'remote.installDir', { remote: true });
  config.remote.python = checkString(config.remote.python, 'remote.python');
  config.remote.db = checkPath(config.remote.db, 'remote.db', { remote: true });
  config.remote.dbDir = checkPath(config.remote.dbDir, 'remote.dbDir', { remote: true });
  if (config.remote.db && config.remote.dbDir) {
    throw configError(
      '配置项 remote.db 与 remote.dbDir 互斥；单库用 db，分片联邦用 dbDir。',
      { field: 'remote.db' },
    );
  }
  config.remote.stateDb = checkPath(config.remote.stateDb, 'remote.stateDb', { remote: true });
  config.remote.repo = checkPath(config.remote.repo, 'remote.repo', { remote: true });
  config.remote.compdb = checkPath(config.remote.compdb, 'remote.compdb', { remote: true });
  config.remote.serveRemotePort = checkPort(config.remote.serveRemotePort, 'remote.serveRemotePort', 0);
  config.remote.localPort = checkPort(config.remote.localPort, 'remote.localPort', 0);
  config.remote.readyTimeoutMs = checkInteger(
    config.remote.readyTimeoutMs,
    'remote.readyTimeoutMs',
    1000,
    120000,
    DEFAULT_CONFIG.remote.readyTimeoutMs,
  );
  config.remote.maxRestarts = checkInteger(config.remote.maxRestarts, 'remote.maxRestarts', 0, 20, 2);
  config.remote.restartBackoffMs = checkInteger(
    config.remote.restartBackoffMs,
    'remote.restartBackoffMs',
    0,
    60000,
    1000,
  );
  config.remote.env = checkEnv(config.remote.env, 'remote.env');

  return config;
}

/**
 * Resolve the effective settings from deployment config + persisted user patch.
 *
 * @param {object} [options]
 * @param {Record<string, any>} [options.explicit] config from the host wiring
 * @param {Record<string, any>} [options.stored] sparse user patch from disk
 * @param {Record<string,string|undefined>} [options.env]
 * @param {string} [options.homedir]
 * @param {string} [options.cwd]
 */
export function resolveConfig(options = {}) {
  const explicit = isPlainObject(options.explicit) ? options.explicit : {};
  const stored = isPlainObject(options.stored) ? options.stored : {};
  const merged = deepMerge(deepMerge(DEFAULT_CONFIG, explicit), stored);
  return validateConfig(merged, {
    cwd: options.cwd || process.cwd(),
    homedir: options.homedir || os.homedir(),
  });
}

/**
 * Load the persisted sparse patch. A missing file is normal; a corrupt file is
 * reported so the manager can log it and fall back to defaults.
 *
 * @param {{settingsPath: string}} paths
 * @returns {{patch: Record<string, any>, exists: boolean, error: Error|null}}
 */
export function loadStoredSettings(paths) {
  const result = readJsonFile(paths.settingsPath);
  if (!result.exists) return { patch: {}, exists: false, error: null };
  if (result.error) return { patch: {}, exists: true, error: result.error };
  const payload = result.value;
  if (!isPlainObject(payload)) {
    return {
      patch: {},
      exists: true,
      error: new Error('settings.json 顶层必须是对象'),
    };
  }
  if (payload.version !== undefined && payload.version !== CONFIG_VERSION) {
    return {
      patch: {},
      exists: true,
      error: new Error(`settings.json 版本不受支持: ${String(payload.version)}`),
    };
  }
  const patch = isPlainObject(payload.config) ? payload.config : {};
  return { patch, exists: true, error: null };
}

/**
 * Persist the sparse user patch atomically (`settings.json` is 0600).
 * @param {{settingsPath: string}} paths
 * @param {Record<string, any>} patch
 */
export function saveStoredSettings(paths, patch) {
  try {
    atomicWriteJson(paths.settingsPath, { version: CONFIG_VERSION, config: patch ?? {} }, { mode: 0o600 });
  } catch (error) {
    throw new BackendError(
      'CONFIG_WRITE_FAILED',
      `无法写入 CA 后端设置 ${paths.settingsPath}：${error?.message || String(error)}`,
      { path: paths.settingsPath, cause: String(error?.message || error) },
    );
  }
}

/**
 * Choose the backend target synchronously.
 *
 * `auto` prefers an explicitly configured remote host (the common deployment:
 * DSH on macOS, CA/indices on a Linux server), otherwise a locally configured
 * DB/dbDir. Local repo/python discovery happens during `start()`/probe and does
 * not make a DB-less environment "configured".
 *
 * @param {Record<string, any>} config
 * @returns {'local'|'remote'|'unconfigured'}
 */
export function resolveConfiguredMode(config) {
  if (!config || typeof config !== 'object') return 'unconfigured';
  if (config.backend === 'local') return 'local';
  if (config.backend === 'remote') return 'remote';
  if (nonEmptyString(config.remote?.ssh?.host)) return 'remote';
  if (config.local?.db || config.local?.dbDir) return 'local';
  return 'unconfigured';
}

/**
 * Human hint for an unconfigured backend; surfaced in status.error/logs.
 * @param {Record<string, any>} config
 */
export function describeConfigurationHint(config) {
  if (resolveConfiguredMode(config) !== 'unconfigured') return null;
  return (
    'CA 后端尚未配置：请在插件设置中填写本地 DB（local.db 或 local.dbDir）' +
    '，或配置远程 Linux 服务器（remote.ssh.host + remote.db/dbDir），然后调用 backend.start。'
  );
}

/** Public snapshot for UI/status: includes resolved state paths. */
export function publicConfig(config, paths) {
  return {
    ...deepMerge(DEFAULT_CONFIG, config),
    paths: paths ? { ...paths } : undefined,
  };
}

/**
 * Apply a user patch on top of the current stored patch and resolve.
 * Validation happens before anything is written.
 *
 * @param {object} options
 * @param {Record<string, any>} options.currentPatch stored user patch
 * @param {Record<string, any>} options.patch new patch from the UI
 * @param {Record<string, any>} [options.explicit]
 * @param {Record<string,string|undefined>} [options.env]
 * @param {string} [options.homedir]
 * @param {string} [options.cwd]
 */
export function applyConfigPatch(options) {
  const nextPatch = deepMerge(isPlainObject(options.currentPatch) ? options.currentPatch : {}, options.patch);
  const config = resolveConfig({
    explicit: options.explicit,
    stored: nextPatch,
    env: options.env,
    homedir: options.homedir,
    cwd: options.cwd,
  });
  return { patch: nextPatch, config };
}
