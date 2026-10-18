/**
 * Project model: a CA project binds one source tree to one database (single
 * `.db` file or shard directory) and one backend location (this machine or a
 * remote Linux host). Projects are user-created, named, persisted in
 * settings.json and activated explicitly — they are never inferred from a
 * directory scan.
 *
 * This module is deliberately free of manager state:
 *   - `normalizeProject` validates and normalizes one definition (local paths
 *     must exist; remote paths are only format-checked because they live on the
 *     far side of SSH);
 *   - `buildProjectFromLegacyConfig` maps the pre-project settings to a default
 *     project so upgrades keep working;
 *   - `projectToBackendPatch` maps a project onto the backend config shape
 *     consumed by `configure()`.
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { validateLocalDatabaseTarget } from './databases.js';
import { publicSshConfig, runSshWithAuth, sshTargetKey } from './remote/auth.js';
import { remotePathExpression } from './remote/commands.js';
import { BackendError } from './errors.js';
import { isDirectory, isExecutableFile, isFile, isPlainObject, nonEmptyString } from './util.js';

/** Backend locations a project can use. */
export const PROJECT_BACKENDS = Object.freeze(['local', 'remote']);
export const PROJECT_NAME_MAX_LENGTH = 120;
export const PROJECT_ID_MAX_LENGTH = 72;

/**
 * Generate a stable-looking project id (`proj-<8 hex>`).
 * @param {{randomUUID?: () => string, randomBytes?: (size: number) => Buffer}} [deps]
 */
export function createProjectId(deps = {}) {
  if (typeof deps.randomBytes === 'function') {
    return `proj-${deps.randomBytes(4).toString('hex')}`;
  }
  if (typeof deps.randomUUID === 'function') {
    return `proj-${String(deps.randomUUID()).replace(/-/g, '').slice(0, 8)}`;
  }
  return `proj-${crypto.randomBytes(4).toString('hex')}`;
}

/**
 * Trim + validate a display name.
 * @param {unknown} value
 * @returns {string}
 */
export function sanitizeProjectName(value) {
  const text = typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f]/g, '').trim() : '';
  if (!text) {
    throw new BackendError('INVALID_ARGUMENT', '项目名 name 不能为空', { field: 'name' });
  }
  if (text.length > PROJECT_NAME_MAX_LENGTH) {
    throw new BackendError(
      'INVALID_ARGUMENT',
      `项目名不能超过 ${PROJECT_NAME_MAX_LENGTH} 个字符（当前 ${text.length}）`,
      { field: 'name' },
    );
  }
  return text;
}

/** Case-insensitive comparison key for project names. */
export function projectNameKey(name) {
  return String(name ?? '').trim().toLowerCase();
}

/** Find a project by id. */
export function findProjectById(projects, id) {
  const wanted = nonEmptyString(id);
  if (!wanted || !Array.isArray(projects)) return null;
  return projects.find((project) => project?.id === wanted) || null;
}

/** Find a project by name (case-insensitive). */
export function findProjectByName(projects, name) {
  const wanted = projectNameKey(name);
  if (!wanted || !Array.isArray(projects)) return null;
  return projects.find((project) => projectNameKey(project?.name) === wanted) || null;
}

/**
 * Return `desired` or `desired (2)`, `desired (3)`, ... so names stay unique
 * case-insensitively (used by migration where the user cannot pick a name yet).
 */
export function uniqueProjectName(projects, desired, excludeId = null) {
  const base = sanitizeProjectName(desired);
  const taken = new Set(
    (Array.isArray(projects) ? projects : [])
      .filter((project) => project && project.id !== excludeId)
      .map((project) => projectNameKey(project.name)),
  );
  if (!taken.has(projectNameKey(base))) return base;
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${base} (${index})`;
    if (!taken.has(projectNameKey(candidate))) return candidate;
  }
  throw new BackendError('PROJECT_NAME_CONFLICT', `无法为 "${base}" 生成唯一项目名`);
}

function expandLocalPath(value, field) {
  const text = nonEmptyString(value);
  if (!text) throw new BackendError('INVALID_ARGUMENT', `${field} 不能为空`, { field });
  let expanded = text;
  if (expanded === '~') expanded = os.homedir();
  else if (expanded.startsWith('~/')) expanded = path.join(os.homedir(), expanded.slice(2));
  if (!path.isAbsolute(expanded)) {
    throw new BackendError('INVALID_ARGUMENT', `${field} 必须是绝对路径或 ~/ 开头（当前 ${text}）`, {
      field,
    });
  }
  return path.normalize(expanded);
}

function isRemoteAbsolute(text) {
  return text === '~' || text.startsWith('~/') || path.posix.isAbsolute(text);
}

function normalizeRemotePath(value, field, { required = false } = {}) {
  const text = nonEmptyString(value);
  if (!text) {
    if (required) {
      throw new BackendError('INVALID_ARGUMENT', `${field} 不能为空`, { field });
    }
    return null;
  }
  if (!isRemoteAbsolute(text)) {
    throw new BackendError(
      'INVALID_ARGUMENT',
      `${field} 必须是远端绝对路径或 ~/ 开头（远端相对路径无法可靠解析，当前 ${text}）`,
      { field },
    );
  }
  const normalized = text.replace(/\/+$/, '');
  return normalized.length > 0 ? normalized : '/';
}

function normalizeProjectSsh(raw, backend) {
  if (backend === 'local') return null;
  const ssh = isPlainObject(raw) ? raw : {};
  const host = nonEmptyString(ssh.host);
  if (!host) {
    throw new BackendError(
      'INVALID_ARGUMENT',
      '远程项目必须配置 ssh.host（ssh config alias、host 或 user@host）',
      { field: 'ssh.host' },
    );
  }
  const user = nonEmptyString(ssh.user) || null;
  let port = ssh.port ?? null;
  if (port !== null && port !== undefined && port !== '') {
    port = Number(port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new BackendError('INVALID_ARGUMENT', `ssh.port 必须是 1-65535 的整数（当前 ${ssh.port}）`, {
        field: 'ssh.port',
      });
    }
  } else {
    port = null;
  }
  const keyPathRaw = nonEmptyString(ssh.keyPath);
  return {
    host,
    user,
    port,
    keyPath: keyPathRaw ? expandLocalPath(keyPathRaw, 'ssh.keyPath') : null,
  };
}

/**
 * Validate + normalize a project definition coming from the UI.
 *
 * Local projects require existing paths (source root directory, database file,
 * shard directory, optional compdb/python/caRepo). Remote projects only require
 * remote-absolute paths — existence is checked on activation/probe, not here.
 *
 * @param {Record<string, any>} input
 * @param {{now?: () => number, generateId?: () => string, previous?: Record<string, any>|null}} [options]
 * @returns {Record<string, any>} normalized project (with id/createdAt/updatedAt)
 */
export function normalizeProject(input, options = {}) {
  if (!isPlainObject(input)) {
    throw new BackendError('INVALID_ARGUMENT', 'saveProject 需要项目对象参数');
  }
  const now = typeof options.now === 'function' ? options.now : Date.now;
  const generateId = typeof options.generateId === 'function' ? options.generateId : () => createProjectId();
  const previous = isPlainObject(options.previous) ? options.previous : null;

  const backend = input.backend;
  if (!PROJECT_BACKENDS.includes(backend)) {
    throw new BackendError(
      'INVALID_ARGUMENT',
      `项目 backend 必须是 'local' 或 'remote'（当前 ${JSON.stringify(backend)}）`,
      { field: 'backend' },
    );
  }
  const name = sanitizeProjectName(input.name);

  const sourceRootRaw = nonEmptyString(input.sourceRoot);
  if (!sourceRootRaw) {
    throw new BackendError('INVALID_ARGUMENT', '项目 sourceRoot（源码根目录）必填', {
      field: 'sourceRoot',
    });
  }
  let sourceRoot;
  if (backend === 'local') {
    sourceRoot = expandLocalPath(sourceRootRaw, 'sourceRoot');
    if (!isDirectory(sourceRoot)) {
      throw new BackendError('INVALID_ARGUMENT', `源码根目录不存在或不是目录: ${sourceRoot}`, {
        field: 'sourceRoot',
        path: sourceRoot,
      });
    }
  } else {
    sourceRoot = normalizeRemotePath(sourceRootRaw, 'sourceRoot', { required: true });
  }

  const dbRaw = nonEmptyString(input.db);
  const dbDirRaw = nonEmptyString(input.dbDir);
  if (Boolean(dbRaw) === Boolean(dbDirRaw)) {
    throw new BackendError(
      'INVALID_ARGUMENT',
      '数据库必须且只能设置一项：db（单库文件）或 dbDir（分片目录）',
      { field: 'db' },
    );
  }
  let db = null;
  let dbDir = null;
  let databaseValidation = null;
  if (backend === 'local') {
    const kind = dbRaw ? 'db' : 'dbDir';
    const selected = dbRaw ? expandLocalPath(dbRaw, 'db') : expandLocalPath(dbDirRaw, 'dbDir');
    // Existence/extension/shard checks shared with the database helper.
    databaseValidation = validateLocalDatabaseTarget({ kind, path: selected });
    if (kind === 'db') db = databaseValidation.path;
    else dbDir = databaseValidation.path;
  } else if (dbRaw) {
    db = normalizeRemotePath(dbRaw, 'db', { required: true });
  } else {
    dbDir = normalizeRemotePath(dbDirRaw, 'dbDir', { required: true });
  }

  let compdb = null;
  const compdbRaw = nonEmptyString(input.compdb);
  if (compdbRaw) {
    if (backend === 'local') {
      compdb = expandLocalPath(compdbRaw, 'compdb');
      if (!isFile(compdb) && !isDirectory(compdb)) {
        throw new BackendError('INVALID_ARGUMENT', `compdb 路径不存在: ${compdb}`, {
          field: 'compdb',
          path: compdb,
        });
      }
    } else {
      compdb = normalizeRemotePath(compdbRaw, 'compdb');
    }
  }

  const ssh = normalizeProjectSsh(input.ssh, backend);

  let python = null;
  const pythonRaw = nonEmptyString(input.python);
  if (pythonRaw) {
    if (backend === 'local') {
      python = expandLocalPath(pythonRaw, 'python');
      if (!isExecutableFile(python)) {
        throw new BackendError('INVALID_ARGUMENT', `python 解释器不存在或不可执行: ${python}`, {
          field: 'python',
          path: python,
        });
      }
    } else {
      python = normalizeRemotePath(pythonRaw, 'python');
    }
  }

  let caRepoPath = null;
  const caRepoRaw = nonEmptyString(input.caRepoPath);
  if (caRepoRaw) {
    if (backend === 'local') {
      caRepoPath = expandLocalPath(caRepoRaw, 'caRepoPath');
      if (!isDirectory(caRepoPath)) {
        throw new BackendError('INVALID_ARGUMENT', `CA 检出目录不存在: ${caRepoPath}`, {
          field: 'caRepoPath',
          path: caRepoPath,
        });
      }
    } else {
      caRepoPath = normalizeRemotePath(caRepoRaw, 'caRepoPath');
    }
  }

  const hasInputId = input.id !== undefined && input.id !== null && input.id !== '';
  let id;
  if (hasInputId) {
    const requestedId = nonEmptyString(input.id);
    if (!requestedId || requestedId.length > PROJECT_ID_MAX_LENGTH) {
      throw new BackendError('INVALID_ARGUMENT', `项目 id 无效: ${String(input.id)}`, { field: 'id' });
    }
    if (!previous || previous.id !== requestedId) {
      throw new BackendError('PROJECT_NOT_FOUND', `项目不存在: ${requestedId}`, {
        field: 'id',
        id: requestedId,
      });
    }
    id = requestedId;
  } else if (previous?.id) {
    id = previous.id;
  } else {
    id = generateId();
  }

  const createdAt =
    Number.isFinite(previous?.createdAt) && previous.createdAt > 0 ? previous.createdAt : now();
  return {
    id,
    name,
    backend,
    sourceRoot,
    db,
    dbDir,
    compdb,
    ssh,
    python,
    caRepoPath,
    createdAt,
    updatedAt: now(),
  };
}

/**
 * Map a project onto the backend configuration patch consumed by
 * `manager.configure()`.
 *
 * `sourceRoot` becomes `--repo` (deep-tool source context); `caRepoPath` becomes
 * the CA checkout used to find Python/`ca` (local.repoRoot / remote.repoPath).
 *
 * @param {Record<string, any>} project
 * @returns {Record<string, any>}
 */
export function projectToBackendPatch(project) {
  if (!isPlainObject(project)) {
    throw new BackendError('INVALID_ARGUMENT', 'projectToBackendPatch 需要项目对象');
  }
  const db = nonEmptyString(project.db);
  const dbDir = nonEmptyString(project.dbDir);
  const common = {
    db: db || null,
    dbDir: db ? null : dbDir || null,
    repo: nonEmptyString(project.sourceRoot) || null,
    compdb: nonEmptyString(project.compdb) || null,
    python: nonEmptyString(project.python) || null,
  };
  if (project.backend === 'remote') {
    const ssh = isPlainObject(project.ssh) ? project.ssh : {};
    return {
      backend: 'remote',
      remote: {
        ...common,
        ssh: {
          host: nonEmptyString(ssh.host) || null,
          user: nonEmptyString(ssh.user) || null,
          port: ssh.port === null || ssh.port === undefined ? null : Number(ssh.port),
          keyPath: nonEmptyString(ssh.keyPath) || null,
        },
        repoPath: nonEmptyString(project.caRepoPath) || null,
      },
    };
  }
  return {
    backend: 'local',
    local: {
      ...common,
      repoRoot: nonEmptyString(project.caRepoPath) || null,
    },
  };
}

/**
 * Default project name from the configured database target:
 * `/home/yu/CA_OH/index` -> "index", `/data/openharmony.db` -> "openharmony".
 * @param {{db?: string|null, dbDir?: string|null}} target
 */
export function defaultProjectNameFromTarget(target = {}) {
  const dbDir = nonEmptyString(target.dbDir);
  if (dbDir) {
    const base = path.posix.basename(dbDir.replace(/\/+$/, ''));
    if (base) return base;
  }
  const db = nonEmptyString(target.db);
  if (db) {
    const base = path.posix.basename(db.replace(/\/+$/, ''));
    const stem = base.replace(/\.db$/i, '');
    if (stem) return stem;
  }
  return 'default';
}

/**
 * Build the default project for a settings file that predates the project
 * model. Lenient on purpose: upgrade must not fail because a legacy path is
 * missing; the user edits the generated project in the UI.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config resolved config
 * @param {'local'|'remote'|'unconfigured'} options.mode value of resolveConfiguredMode(config)
 * @param {() => number} [options.now]
 * @param {() => string} [options.generateId]
 * @returns {Record<string, any>|null}
 */
export function buildProjectFromLegacyConfig(options) {
  const config = options.config || {};
  const mode = options.mode;
  if (mode !== 'local' && mode !== 'remote') return null;
  const now = typeof options.now === 'function' ? options.now : Date.now;
  const generateId = typeof options.generateId === 'function' ? options.generateId : () => createProjectId();
  const section = mode === 'remote' ? config.remote || {} : config.local || {};
  const db = nonEmptyString(section.db);
  const dbDir = nonEmptyString(section.dbDir);
  if (!db && !dbDir) return null;

  const repo = nonEmptyString(section.repo);
  const repoRoot = nonEmptyString(section.repoRoot);
  const repoPath = nonEmptyString(section.repoPath);
  const sourceRoot =
    repo ||
    repoRoot ||
    repoPath ||
    dbDir ||
    (db ? path.posix.dirname(db) : null) ||
    '/';
  const ssh =
    mode === 'remote'
      ? {
          host: nonEmptyString(section.ssh?.host) || null,
          user: nonEmptyString(section.ssh?.user) || null,
          port:
            section.ssh?.port === null || section.ssh?.port === undefined
              ? null
              : Number(section.ssh.port),
          keyPath: nonEmptyString(section.ssh?.keyPath) || null,
        }
      : null;
  if (mode === 'remote' && !ssh.host) return null;

  const name = uniqueProjectName([], defaultProjectNameFromTarget({ db, dbDir }));
  const timestamp = now();
  return {
    id: generateId(),
    name,
    backend: mode,
    sourceRoot,
    db: db || null,
    dbDir: db ? null : dbDir || null,
    compdb: nonEmptyString(section.compdb) || null,
    ssh,
    python: nonEmptyString(section.python) || null,
    caRepoPath:
      mode === 'remote'
        ? repoPath || nonEmptyString(section.installDir) || null
        : repoRoot || null,
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

/**
 * Lenient on-disk project sanitization (no filesystem checks: a remote host or
 * a local path may be temporarily unavailable at load time; activation and save
 * validate paths). Returns null for structurally unusable entries.
 * @param {unknown} raw
 */
export function sanitizeStoredProject(raw) {
  if (!isPlainObject(raw)) return null;
  const id = nonEmptyString(raw.id);
  const backend = raw.backend;
  const sourceRoot = nonEmptyString(raw.sourceRoot);
  if (!id || id.length > PROJECT_ID_MAX_LENGTH) return null;
  if (!PROJECT_BACKENDS.includes(backend)) return null;
  if (!sourceRoot) return null;
  let name;
  try {
    name = sanitizeProjectName(raw.name);
  } catch {
    return null;
  }
  const db = nonEmptyString(raw.db);
  const dbDir = nonEmptyString(raw.dbDir);
  if (Boolean(db) === Boolean(dbDir)) return null;
  const ssh = backend === 'remote' ? normalizeProjectSshStored(raw.ssh) : null;
  if (backend === 'remote' && !ssh) return null;
  return {
    id,
    name,
    backend,
    sourceRoot,
    db: db || null,
    dbDir: db ? null : dbDir || null,
    compdb: nonEmptyString(raw.compdb) || null,
    ssh,
    python: nonEmptyString(raw.python) || null,
    caRepoPath: nonEmptyString(raw.caRepoPath) || null,
    createdAt: Number.isFinite(raw.createdAt) && raw.createdAt > 0 ? raw.createdAt : Date.now(),
    updatedAt: Number.isFinite(raw.updatedAt) && raw.updatedAt > 0 ? raw.updatedAt : Date.now(),
  };
}

function normalizeProjectSshStored(raw) {
  if (!isPlainObject(raw)) return null;
  const host = nonEmptyString(raw.host);
  if (!host) return null;
  const port = Number(raw.port);
  return {
    host,
    user: nonEmptyString(raw.user) || null,
    port: Number.isInteger(port) && port >= 1 && port <= 65535 ? port : null,
    keyPath: nonEmptyString(raw.keyPath) || null,
  };
}

// ------------------------------------------------------------------------ //
// simple projects ("pick one directory")
// ------------------------------------------------------------------------ //

/** Slug used for the auto-generated DB directory of a simple project. */
export function projectSlug(value) {
  const text = String(value ?? '')
    .normalize('NFKD')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[-.]+|[-.]+$/g, '')
    .toLowerCase()
    .slice(0, 48);
  return text.length > 0 ? text : 'project';
}

/** Default plugin state dir (`$DSH_HOME/plugin-data/dsh-ca`). */
export function defaultStateDir() {
  const dshHome = nonEmptyString(process.env.DSH_HOME) || path.join(os.homedir(), '.dsh');
  return path.join(dshHome, 'plugin-data', 'dsh-ca');
}

function simpleProjectName(directory, name) {
  const fromInput = nonEmptyString(name);
  if (fromInput) return sanitizeProjectName(fromInput);
  const trimmed = String(directory ?? '').replace(/[/\\]+$/, '');
  const base = path.posix.basename(trimmed);
  return sanitizeProjectName(base || 'root');
}

/**
 * Build the minimal project a user gets by picking one directory.
 *
 * The database path is generated (single DB per project, created empty before
 * the backend starts; `ca index` fills it later):
 *   local:  `$DSH_HOME/plugin-data/dsh-ca/data/<slug>/index.db`
 *   remote: `<remote home>/.dsh-ca/data/<slug>/index.db`
 *
 * `python`/`caRepoPath` stay null: the installer fills them after installing CA.
 *
 * @param {object} options
 * @param {'local'|'remote'} options.kind
 * @param {string} options.directory source tree the user picked
 * @param {string} [options.name] display name (default: directory basename)
 * @param {string|object|null} [options.ssh] remote SSH auth (password stripped)
 * @param {string} [options.home] remote home directory (from remote.connect)
 * @param {string} [options.stateDir] plugin state dir override (tests)
 * @param {string|number} [options.slugSuffix] uniqueness suffix for the DB slug
 * @param {() => number} [options.now]
 * @param {() => string} [options.generateId]
 * @returns {Record<string, any>} project (not persisted)
 */
export function buildSimpleProject(options = {}) {
  const kind = options.kind;
  if (kind !== 'local' && kind !== 'remote') {
    throw new BackendError('INVALID_ARGUMENT', `kind 必须是 'local' 或 'remote'（当前 ${JSON.stringify(kind)}）`, {
      field: 'kind',
    });
  }
  const directoryRaw = nonEmptyString(options.directory);
  if (!directoryRaw) {
    throw new BackendError('INVALID_ARGUMENT', 'directory（项目目录）不能为空', { field: 'directory' });
  }
  const directory =
    kind === 'local' ? expandLocalPath(directoryRaw, 'directory') : normalizeRemotePath(directoryRaw, 'directory', { required: true });
  let localStat = null;
  if (kind === 'local') {
    localStat = (() => {
      try {
        return fs.statSync(directory);
      } catch {
        return null;
      }
    })();
    if (!localStat || !localStat.isDirectory()) {
      throw new BackendError('INVALID_ARGUMENT', `本机项目目录不存在或不是目录: ${directory}`, {
        field: 'directory',
        path: directory,
      });
    }
  }
  const name = simpleProjectName(directory, options.name);
  const now = typeof options.now === 'function' ? options.now : Date.now;
  const generateId = typeof options.generateId === 'function' ? options.generateId : () => createProjectId();
  const slugBase = projectSlug(name);
  const suffix = options.slugSuffix === undefined || options.slugSuffix === null ? '' : String(options.slugSuffix);
  const slug = suffix ? `${slugBase}-${suffix}` : slugBase;

  let db;
  let ssh = null;
  if (kind === 'local') {
    const stateDir = nonEmptyString(options.stateDir) || defaultStateDir();
    db = path.join(stateDir, 'data', slug, 'index.db');
  } else {
    const home = nonEmptyString(options.home);
    if (!home) {
      throw new BackendError(
        'INVALID_ARGUMENT',
        '远程项目需要 remote home（先连接服务器获取 home），或调用 remote.connect 后重试',
        { field: 'home' },
      );
    }
    const normalizedHome = home === '~' || home.startsWith('~/') ? normalizeRemotePath(home, 'home', { required: true }) : home.replace(/\/+$/, '');
    db = path.posix.join(normalizedHome, '.dsh-ca', 'data', slug, 'index.db');
    ssh = publicSshConfig(options.ssh);
    if (!ssh.host) {
      throw new BackendError('INVALID_ARGUMENT', '远程项目必须提供 ssh.host', { field: 'ssh.host' });
    }
  }

  let compdb = null;
  if (kind === 'local') {
    const candidate = path.join(directory, 'compile_commands.json');
    try {
      if (fs.statSync(candidate).isFile()) compdb = candidate;
    } catch {
      compdb = null;
    }
  }

  const timestamp = now();
  return {
    id: generateId(),
    name,
    backend: kind,
    sourceRoot: directory,
    db,
    dbDir: null,
    compdb,
    ssh,
    python: null,
    caRepoPath: null,
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

/**
 * Create the project's DB parent directory (and an empty SQLite file) before
 * starting the backend.
 *
 * @param {Record<string, any>} project
 * @param {{deps?: object, logger?: any, ssh?: string|object|null}} [options]
 * @returns {Promise<{kind: 'local'|'remote', dir: string, db: string, created: boolean}>}
 */
export async function ensureDefaultDirs(project, options = {}) {
  if (!isPlainObject(project)) {
    throw new BackendError('INVALID_ARGUMENT', 'ensureDefaultDirs 需要项目对象');
  }
  const db = nonEmptyString(project.db);
  if (!db) {
    if (nonEmptyString(project.dbDir)) {
      return { kind: project.backend, dir: project.dbDir, db: null, created: false };
    }
    throw new BackendError('INVALID_ARGUMENT', '项目未配置数据库路径（db）');
  }
  if (project.backend === 'local') {
    const dir = path.dirname(db);
    fs.mkdirSync(dir, { recursive: true });
    let created = false;
    if (!fs.existsSync(db)) {
      fs.writeFileSync(db, '', { mode: 0o600 });
      created = true;
    }
    return { kind: 'local', dir, db, created };
  }
  const dir = path.posix.dirname(db);
  const ssh = options.ssh || project.ssh;
  const script = [
    'set -e',
    `DIR=${remotePathExpression(dir)}`,
    `DB=${remotePathExpression(db)}`,
    'mkdir -p "$DIR"',
    'if [ ! -f "$DB" ]; then : > "$DB"; fi',
    'echo "CA_DIRS_OK=1"',
  ].join('\n');
  const result = await runSshWithAuth({
    ssh,
    script,
    deps: options.deps,
    logger: options.logger,
    timeoutMs: 30000,
    label: 'prepare remote project directories',
  });
  if (result?.code !== 0 || result?.spawnError) {
    throw new BackendError(
      result?.cancelled ? 'CANCELLED' : 'REMOTE_DIRS_FAILED',
      `无法创建远程项目目录 ${dir}：${String(result?.stderr || result?.stdout || '').trim().slice(-1000) || 'ssh 失败'}`,
      { dir, db },
    );
  }
  return { kind: 'remote', dir, db, created: true };
}

/** Exposed for RPC/UI: stable ssh key used for in-memory password lookup. */
export { sshTargetKey };
