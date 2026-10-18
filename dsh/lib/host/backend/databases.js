/**
 * Database/directory discovery for the local and remote backends.
 *
 * A project index can be either a single SQLite file (`kind:'db'`) or a
 * federated shard directory (`kind:'dbDir'`, containing several `*.db` files).
 * The UI needs to list candidates around the currently configured target and
 * switch between them without editing JSON by hand.
 *
 * This module owns the shared catalog model:
 *   - scanning one directory level into raw entries (local fs);
 *   - parsing the remote scan script output (python JSON or POSIX fallback);
 *   - building the public catalog: dedupe, mark current, classify
 *     ok/empty/failed, sort current-first, cap at 80 candidates.
 *
 * Contract (task-11): `listDatabases()` never throws; the manager wraps errors
 * into `{ok:false, error:{code,message,details?}}`.
 */

import fs from 'node:fs';
import path from 'node:path';

import { BackendError } from './errors.js';
import { formatBytes, nonEmptyString } from './util.js';

/** Hard cap on candidates returned to the UI. */
export const MAX_DATABASE_ITEMS = 80;

/** Directories that never contain useful CA databases. */
const SKIPPED_DIR_NAMES = new Set(['.ca', '.tmp', '.git', '.venv', '__pycache__', 'node_modules']);

/** Names that look like failed/abandoned runs; kept but sorted last. */
const FAILED_NAME_RE = /(^|[-_.])(failed|broken|corrupt|error|tmp|temp|bak|backup|old|deprecated)([-_.]|$)/i;

/**
 * True when a directory is hidden or a well-known non-database directory.
 * @param {string} name
 */
export function isSkippedDirectoryName(name) {
  const text = String(name || '');
  return text.startsWith('.') || SKIPPED_DIR_NAMES.has(text);
}

/**
 * True for `*.db` files that are not SQLite sidecar files.
 * @param {string} name
 */
export function isDatabaseFileName(name) {
  const text = String(name || '');
  return text.endsWith('.db') && !text.endsWith('-wal') && !text.endsWith('-shm');
}

/** Normalize a path for identity comparisons (strip trailing slashes). */
export function normalizeDatabasePath(value) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  if (text === '/') return '/';
  return text.replace(/\/+$/, '');
}

function sameDatabasePath(left, right) {
  const a = normalizeDatabasePath(left);
  const b = normalizeDatabasePath(right);
  if (!a || !b) return false;
  if (a === b) return true;
  // Configured paths may be written as `~/x` while scans report `/home/u/x`.
  if (a.startsWith('~/') && b.endsWith(`/${a.slice(2)}`)) return true;
  if (b.startsWith('~/') && a.endsWith(`/${b.slice(2)}`)) return true;
  return false;
}

/** Classify a catalog entry for display/sorting. */
export function classifyDatabaseEntry({ kind, name, dbCount }) {
  if (kind === 'dbDir' && Number(dbCount) === 0) return 'empty';
  if (FAILED_NAME_RE.test(String(name || ''))) return 'failed';
  return 'ok';
}

function safeStat(target) {
  try {
    return fs.statSync(target);
  } catch {
    return null;
  }
}

/** Human label for an entry; directories get a trailing slash. */
function labelFor(kind, name, fullPath) {
  const base = nonEmptyString(name) || path.basename(normalizeDatabasePath(fullPath)) || fullPath;
  return kind === 'dbDir' ? `${base}/` : base;
}

/**
 * Build the public catalog object.
 *
 * @param {object} options
 * @param {'local'|'remote'|'unconfigured'} options.mode
 * @param {string|null} [options.db] configured single DB
 * @param {string|null} [options.dbDir] configured shard dir
 * @param {Array<object>} [options.items] raw entries `{kind,path,name,sizeBytes?,mtime?,dbCount?,source?}`
 * @param {string[]} [options.roots] scanned root directories
 * @param {number} [options.maxItems]
 * @param {number} [options.scannedAt]
 * @param {Record<string, unknown>} [options.extra]
 */
function numericOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function buildDatabaseCatalog(options = {}) {
  const mode = options.mode || 'unconfigured';
  const db = nonEmptyString(options.db);
  const dbDir = nonEmptyString(options.dbDir);
  const currentPath = normalizeDatabasePath(db || dbDir);
  const maxItems = Math.max(1, Math.min(500, Math.trunc(Number(options.maxItems) || MAX_DATABASE_ITEMS)));
  const scannedAt = Number(options.scannedAt) || Date.now();

  /** @type {Map<string, Record<string, any>>} */
  const byPath = new Map();
  for (const raw of Array.isArray(options.items) ? options.items : []) {
    if (!raw || typeof raw !== 'object') continue;
    const kind = raw.kind === 'dbDir' ? 'dbDir' : 'db';
    const fullPath = normalizeDatabasePath(raw.path);
    if (!fullPath) continue;
    const name = nonEmptyString(raw.name) || path.basename(fullPath) || fullPath;
    const dbCount = kind === 'db' ? 1 : Math.max(0, Math.trunc(Number(raw.dbCount) || 0));
    const entry = {
      kind,
      path: fullPath,
      label: labelFor(kind, name, fullPath),
      sizeBytes: numericOrNull(raw.sizeBytes),
      mtime: numericOrNull(raw.mtime),
      dbCount,
      source: nonEmptyString(raw.source) || 'scan',
      current: false,
      status: classifyDatabaseEntry({ kind, name, dbCount }),
    };
    const existing = byPath.get(fullPath);
    if (!existing) {
      byPath.set(fullPath, entry);
      continue;
    }
    // Prefer richer data when the same path was reported twice (root + child scan).
    if (entry.dbCount > existing.dbCount) existing.dbCount = entry.dbCount;
    if (entry.sizeBytes !== null && existing.sizeBytes === null) existing.sizeBytes = entry.sizeBytes;
    if (entry.mtime !== null) existing.mtime = entry.mtime;
    if (entry.source === 'configured') existing.source = 'configured';
  }

  /** @type {Array<Record<string, any>>} */
  const entries = [...byPath.values()];
  let currentEntry = null;
  if (currentPath) {
    currentEntry = entries.find((entry) => sameDatabasePath(entry.path, currentPath)) || null;
    if (currentEntry) {
      currentEntry.current = true;
      currentEntry.source = 'configured';
    } else {
      // The configured path is always represented, even when it is outside the
      // scanned tree (e.g. `~/...` or a temporarily missing path).
      const kind = db ? 'db' : 'dbDir';
      currentEntry = {
        kind,
        path: currentPath,
        label: labelFor(kind, path.basename(currentPath), currentPath),
        sizeBytes: null,
        mtime: null,
        dbCount: kind === 'db' ? 1 : null,
        source: 'configured',
        current: true,
        status: 'ok',
      };
      entries.unshift(currentEntry);
    }
  }

  const statusRank = { ok: 0, empty: 1, failed: 2 };
  entries.sort((left, right) => {
    if (left.current !== right.current) return left.current ? -1 : 1;
    const rank = (statusRank[left.status] ?? 3) - (statusRank[right.status] ?? 3);
    if (rank !== 0) return rank;
    const countDiff = (right.dbCount || 0) - (left.dbCount || 0);
    if (countDiff !== 0) return countDiff;
    const mtimeDiff = (right.mtime || 0) - (left.mtime || 0);
    if (mtimeDiff !== 0) return mtimeDiff;
    return String(left.label).localeCompare(String(right.label)) || left.path.localeCompare(right.path);
  });

  const candidates = entries.slice(0, maxItems);
  return {
    ok: true,
    mode,
    current: { db: db || null, dbDir: dbDir || null },
    configured: {
      kind: db ? 'db' : dbDir ? 'dbDir' : null,
      path: db || dbDir || null,
    },
    candidates,
    roots: (options.roots || []).map((root) => normalizeDatabasePath(root)).filter(Boolean),
    total: entries.length,
    truncated: entries.length > candidates.length,
    maxItems,
    scannedAt,
    error: null,
    ...(options.extra || {}),
  };
}

/**
 * Structured failure report (listDatabases never throws).
 * @param {object} options
 */
export function createDatabaseErrorReport(options = {}) {
  const db = nonEmptyString(options.db);
  const dbDir = nonEmptyString(options.dbDir);
  const error = options.error || {};
  return {
    ok: false,
    mode: options.mode || 'unconfigured',
    current: { db: db || null, dbDir: dbDir || null },
    configured: {
      kind: db ? 'db' : dbDir ? 'dbDir' : null,
      path: db || dbDir || null,
    },
    candidates: [],
    roots: Array.isArray(options.roots) ? options.roots : [],
    total: 0,
    truncated: false,
    maxItems: Math.trunc(Number(options.maxItems) || MAX_DATABASE_ITEMS),
    scannedAt: Number(options.scannedAt) || Date.now(),
    error: {
      code: error.code || 'DB_SCAN_FAILED',
      message: error.message || '数据库扫描失败',
      ...(error.details !== undefined ? { details: error.details } : {}),
    },
  };
}

/**
 * Scan one local directory level: direct `*.db` files plus subdirectories that
 * contain `*.db` files. Returns raw entries for {@link buildDatabaseCatalog}.
 *
 * @param {string} root absolute directory
 * @param {{source?: string, maxEntries?: number}} [options]
 */
export function scanLocalDirectory(root, options = {}) {
  const fullRoot = normalizeDatabasePath(root);
  /** @type {Array<Record<string, any>>} */
  const entries = [];
  const maxEntries = Math.max(1, Math.trunc(Number(options.maxEntries) || 500));
  let dirEntries;
  try {
    dirEntries = fs.readdirSync(fullRoot, { withFileTypes: true });
  } catch (error) {
    throw new BackendError(
      'DB_NOT_FOUND',
      `无法读取数据库目录 ${fullRoot}：${error?.message || error}`,
      { path: fullRoot },
    );
  }

  let rootDbCount = 0;
  for (const entry of dirEntries) {
    if (!entry.isFile() || !isDatabaseFileName(entry.name)) continue;
    const fullPath = path.join(fullRoot, entry.name);
    const stat = safeStat(fullPath);
    if (!stat || !stat.isFile()) continue;
    rootDbCount += 1;
    entries.push({
      kind: 'db',
      path: fullPath,
      name: entry.name,
      sizeBytes: stat.size,
      mtime: Math.trunc(stat.mtimeMs),
      dbCount: 1,
      source: options.source || 'scan',
    });
    if (entries.length >= maxEntries) break;
  }

  const rootStat = safeStat(fullRoot);
  entries.push({
    kind: 'dbDir',
    path: fullRoot,
    name: path.basename(fullRoot) || fullRoot,
    sizeBytes: null,
    mtime: rootStat ? Math.trunc(rootStat.mtimeMs) : null,
    dbCount: rootDbCount,
    source: options.source === 'repo' ? 'repo' : 'scan',
  });

  for (const entry of dirEntries) {
    if (!entry.isDirectory()) continue;
    if (isSkippedDirectoryName(entry.name)) continue;
    const fullPath = path.join(fullRoot, entry.name);
    let dbCount = 0;
    try {
      for (const child of fs.readdirSync(fullPath, { withFileTypes: true })) {
        if (child.isFile() && isDatabaseFileName(child.name)) dbCount += 1;
      }
    } catch {
      continue;
    }
    if (dbCount === 0) continue;
    const stat = safeStat(fullPath);
    entries.push({
      kind: 'dbDir',
      path: fullPath,
      name: entry.name,
      sizeBytes: null,
      mtime: stat ? Math.trunc(stat.mtimeMs) : null,
      dbCount,
      source: 'scan',
    });
    if (entries.length >= maxEntries) break;
  }
  return entries;
}

/**
 * Local catalog: scan the configured db/dbDir directory plus the repository
 * root (one level each), then build the public catalog.
 *
 * @param {object} options
 * @param {string|null} [options.db]
 * @param {string|null} [options.dbDir]
 * @param {string|null} [options.repoRoot]
 * @param {number} [options.maxItems]
 * @param {'local'} [options.mode]
 */
export function scanLocalDatabaseCatalog(options = {}) {
  const db = nonEmptyString(options.db);
  const dbDir = nonEmptyString(options.dbDir);
  const repoRoot = nonEmptyString(options.repoRoot);
  const targetRoot = dbDir || (db ? path.dirname(db) : null);
  if (!targetRoot) {
    return createDatabaseErrorReport({
      mode: options.mode || 'local',
      db,
      dbDir,
      error: {
        code: 'BACKEND_NOT_CONFIGURED',
        message: '本地后端未配置数据库：请设置 local.db 或 local.dbDir。',
      },
    });
  }
  /** @type {Array<Record<string, any>>} */
  const items = [];
  const roots = [];
  const scanned = new Set();
  const scan = (root, source) => {
    const normalized = normalizeDatabasePath(root);
    if (!normalized || scanned.has(normalized)) return;
    scanned.add(normalized);
    roots.push(normalized);
    items.push(...scanLocalDirectory(normalized, { source }));
  };
  try {
    scan(targetRoot, 'scan');
  } catch (error) {
    return createDatabaseErrorReport({
      mode: options.mode || 'local',
      db,
      dbDir,
      roots,
      maxItems: options.maxItems,
      error: {
        code: error?.code || 'DB_NOT_FOUND',
        message: error?.message || `无法扫描数据库目录 ${targetRoot}`,
        details: error?.details,
      },
    });
  }
  if (repoRoot) {
    try {
      const repoStat = fs.statSync(repoRoot);
      if (repoStat.isDirectory()) scan(repoRoot, 'repo');
    } catch {
      // repoRoot configured but missing: not fatal for catalog listing
    }
  }
  return { ...buildDatabaseCatalog({ mode: options.mode || 'local', db, dbDir, items, roots, maxItems: options.maxItems }), ...{} };
}

/**
 * Validate a local switch target (existence, extension, shard directory).
 * Throws BackendError('DB_NOT_FOUND'/'INVALID_ARGUMENT') on failure.
 *
 * @param {{kind: 'db'|'dbDir', path: string}} target
 * @returns {{ok: true, kind: 'db'|'dbDir', path: string, dbCount: number, sizeBytes: number|null, sizeText: string|null}}
 */
export function validateLocalDatabaseTarget(target) {
  const kind = target?.kind;
  const fullPath = normalizeDatabasePath(target?.path);
  if (kind !== 'db' && kind !== 'dbDir') {
    throw new BackendError('INVALID_ARGUMENT', `kind 必须是 'db' 或 'dbDir'（当前 ${JSON.stringify(kind)}）`);
  }
  if (!fullPath) {
    throw new BackendError('INVALID_ARGUMENT', '数据库路径不能为空');
  }
  if (!path.isAbsolute(fullPath)) {
    throw new BackendError('INVALID_ARGUMENT', `数据库路径必须是绝对路径: ${fullPath}`, { path: fullPath });
  }
  if (kind === 'db') {
    if (!fullPath.endsWith('.db')) {
      throw new BackendError('INVALID_ARGUMENT', `单库路径必须以 .db 结尾: ${fullPath}`, { path: fullPath });
    }
    const stat = safeStat(fullPath);
    if (!stat || !stat.isFile()) {
      throw new BackendError('DB_NOT_FOUND', `本地数据库文件不存在: ${fullPath}`, { path: fullPath });
    }
    return {
      ok: true,
      kind,
      path: fullPath,
      dbCount: 1,
      sizeBytes: stat.size,
      sizeText: formatBytes(stat.size),
    };
  }
  const stat = safeStat(fullPath);
  if (!stat || !stat.isDirectory()) {
    throw new BackendError('DB_NOT_FOUND', `本地分片目录不存在: ${fullPath}`, { path: fullPath });
  }
  let dbCount = 0;
  try {
    for (const entry of fs.readdirSync(fullPath, { withFileTypes: true })) {
      if (entry.isFile() && isDatabaseFileName(entry.name)) dbCount += 1;
    }
  } catch (error) {
    throw new BackendError(
      'DB_NOT_FOUND',
      `无法读取本地分片目录 ${fullPath}：${error?.message || error}`,
      { path: fullPath },
    );
  }
  if (dbCount === 0) {
    throw new BackendError('DB_NOT_FOUND', `本地分片目录中没有 *.db 文件: ${fullPath}`, { path: fullPath });
  }
  return { ok: true, kind, path: fullPath, dbCount, sizeBytes: null, sizeText: null };
}

// -- remote scan output parsing ----------------------------------------- //

/**
 * Parse the output of the remote list script.
 *
 * Formats:
 *   `CA_DB_ENTRY:<json>`            python primary path
 *   `CA_DB_ENTRY:<json>`            POSIX fallback also emits JSON
 *   `CA_DB_ERROR=<message>`         scan root unreadable
 *   `CA_DB_DONE=1`                  completion marker
 *
 * @param {string} stdout
 */
export function parseRemoteDatabaseScanOutput(stdout) {
  /** @type {Array<Record<string, any>>} */
  const entries = [];
  let error = null;
  let done = false;
  for (const rawLine of String(stdout ?? '').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith('CA_DB_ENTRY:')) {
      const payload = line.slice('CA_DB_ENTRY:'.length);
      try {
        const parsed = JSON.parse(payload);
        if (parsed && typeof parsed === 'object') entries.push(parsed);
      } catch {
        // tolerate a malformed fallback line instead of failing the whole scan
      }
      continue;
    }
    if (line.startsWith('CA_DB_ERROR=')) {
      error = line.slice('CA_DB_ERROR='.length).trim() || 'unknown error';
      continue;
    }
    if (line === 'CA_DB_DONE=1') done = true;
  }
  return { entries, error, done };
}

/**
 * Parse the remote validation script output `CA_DB_VALID=<0|1>:<REASON>[:n[:size]]`.
 * @param {string} stdout
 */
export function parseRemoteDatabaseValidationOutput(stdout) {
  const match = /CA_DB_VALID=([01]):([A-Z_]+)(?::(\d+))?(?::(\d+))?/.exec(String(stdout ?? ''));
  if (!match) {
    return { ok: false, reason: 'NO_OUTPUT', numbers: [], parsed: false };
  }
  const numbers = [match[3], match[4]].filter((value) => value !== undefined).map((value) => Number(value));
  return {
    ok: match[1] === '1',
    reason: match[2],
    numbers,
    parsed: true,
  };
}

/** Human description for remote validation failure reasons. */
export function describeValidationFailure(reason) {
  switch (reason) {
    case 'NOT_DIR':
      return '远端路径不存在或不是目录';
    case 'NO_DB':
      return '远端目录中没有 *.db 文件';
    case 'NOT_FILE':
      return '远端文件不存在';
    case 'NOT_DB_EXTENSION':
      return '远端单库路径必须以 .db 结尾';
    case 'NO_OUTPUT':
      return '远端校验脚本没有返回结果';
    default:
      return `远端路径校验失败（${reason}）`;
  }
}
