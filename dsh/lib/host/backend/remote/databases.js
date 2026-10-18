/**
 * Remote database discovery and switch-target validation.
 *
 * Both operations use exactly one ssh round trip:
 *   - list: one scan script (python3 primary, POSIX/stat fallback) under the
 *     configured `remote.dbDir` (or the directory of `remote.db`);
 *   - validate: one marker script checking existence, file extension and shard
 *     directory contents before anything is persisted.
 *
 * Failures are returned as structured reports for listing (task-11 contract:
 * `listDatabases()` never throws); validation throws BackendError so the
 * manager can reject a switch before touching settings.
 */

import path from 'node:path';

import {
  buildDatabaseCatalog,
  createDatabaseErrorReport,
  describeValidationFailure,
  parseRemoteDatabaseScanOutput,
  parseRemoteDatabaseValidationOutput,
} from '../databases.js';
import { BackendError, normalizeError } from '../errors.js';
import { nonEmptyString } from '../util.js';
import {
  buildListDatabasesScript,
  buildValidateDatabaseScript,
  classifySshFailure,
  normalizeSshConfig,
} from './commands.js';

const LIST_TIMEOUT_MS = 30000;
const VALIDATE_TIMEOUT_MS = 20000;

/**
 * Scan a remote Linux host for database candidates.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config resolved full config
 * @param {ReturnType<import('./exec.js').createRemoteExecutor>} options.executor
 * @param {any} [options.logger]
 * @param {string|object|null} [options.sshOverride]
 * @param {number} [options.maxItems]
 * @param {number} [options.timeoutMs]
 * @returns {Promise<Record<string, any>>} catalog report (never throws)
 */
export async function listRemoteDatabases(options) {
  const config = options.config;
  const remote = config.remote || {};
  const db = nonEmptyString(remote.db);
  const dbDir = nonEmptyString(remote.dbDir);
  const maxItems = options.maxItems;
  if (!db && !dbDir) {
    return createDatabaseErrorReport({
      mode: 'remote',
      db,
      dbDir,
      maxItems,
      error: {
        code: 'BACKEND_NOT_CONFIGURED',
        message:
          '远程后端未配置数据库：请先在设置中填写 remote.db（远端单库）或 remote.dbDir（远端分片目录）。',
      },
    });
  }
  const root = dbDir || path.posix.dirname(db);
  let ssh;
  try {
    ssh = normalizeSshConfig(options.sshOverride ?? remote.ssh, remote.ssh);
  } catch (error) {
    return createDatabaseErrorReport({
      mode: 'remote',
      db,
      dbDir,
      roots: [root],
      maxItems,
      error: toReportError(error, 'BACKEND_NOT_CONFIGURED'),
    });
  }
  let script;
  try {
    script = buildListDatabasesScript({
      root,
      maxEntries: Math.max(80, Number(maxItems) || 80) * 3,
    });
  } catch (error) {
    return createDatabaseErrorReport({
      mode: 'remote',
      db,
      dbDir,
      roots: [root],
      maxItems,
      error: toReportError(error, 'INVALID_ARGUMENT'),
    });
  }
  const result = await options.executor.runSsh(ssh, script, {
    timeoutMs: Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : LIST_TIMEOUT_MS,
    fallbackSsh: remote.ssh,
    label: `list databases under ${root}`,
    maxOutputBytes: 512 * 1024,
    onStdoutLine: (line) => options.logger?.info?.(`[remote] db-scan: ${line}`),
    onStderrLine: (line) => options.logger?.warn?.(`[remote] db-scan stderr: ${line}`),
  });
  if (result.spawnError || result.timedOut || result.cancelled || result.code !== 0) {
    const failure = classifySshFailure(result);
    return createDatabaseErrorReport({
      mode: 'remote',
      db,
      dbDir,
      roots: [root],
      maxItems,
      error: {
        code: failure.code,
        message: `远程数据库扫描失败：${failure.message}`,
        details: {
          exitCode: result.code ?? null,
          stderr: tailOf(result),
          hint: failure.hint,
        },
      },
    });
  }
  const parsed = parseRemoteDatabaseScanOutput(result.stdout);
  if (parsed.error) {
    return createDatabaseErrorReport({
      mode: 'remote',
      db,
      dbDir,
      roots: [root],
      maxItems,
      error: {
        code: 'DB_NOT_FOUND',
        message:
          parsed.error === 'root_missing'
            ? `远端数据库目录不存在: ${root}`
            : `无法读取远端数据库目录 ${root}：${parsed.error}`,
        details: { path: root, reason: parsed.error },
      },
    });
  }
  if (!parsed.done) {
    return createDatabaseErrorReport({
      mode: 'remote',
      db,
      dbDir,
      roots: [root],
      maxItems,
      error: {
        code: 'DB_SCAN_FAILED',
        message: `远端数据库扫描脚本没有完成（缺少 CA_DB_DONE 标记）；输出: ${tailOf(result, 4) || '(空)'}`,
        details: { path: root },
      },
    });
  }
  return buildDatabaseCatalog({
    mode: 'remote',
    db,
    dbDir,
    items: parsed.entries,
    roots: [root],
    maxItems,
  });
}

/**
 * Validate a remote database switch target with one ssh round trip.
 * Throws BackendError('DB_NOT_FOUND'/'INVALID_ARGUMENT'/'SSH_*').
 *
 * @param {object} options
 * @param {Record<string, any>} options.config
 * @param {ReturnType<import('./exec.js').createRemoteExecutor>} options.executor
 * @param {'db'|'dbDir'} options.kind
 * @param {string} options.path
 * @param {any} [options.logger]
 * @param {string|object|null} [options.sshOverride]
 * @returns {Promise<{ok: true, kind: 'db'|'dbDir', path: string, dbCount: number, sizeBytes: number|null}>}
 */
export async function validateRemoteDatabase(options) {
  const remote = options.config.remote || {};
  const kind = options.kind;
  const target = nonEmptyString(options.path);
  if (kind !== 'db' && kind !== 'dbDir') {
    throw new BackendError('INVALID_ARGUMENT', `kind 必须是 'db' 或 'dbDir'（当前 ${JSON.stringify(kind)}）`);
  }
  if (!target) throw new BackendError('INVALID_ARGUMENT', '数据库路径不能为空');
  const ssh = normalizeSshConfig(options.sshOverride ?? remote.ssh, remote.ssh);
  const script = buildValidateDatabaseScript({ kind, path: target });
  const result = await options.executor.runSsh(ssh, script, {
    timeoutMs: VALIDATE_TIMEOUT_MS,
    fallbackSsh: remote.ssh,
    label: `validate database ${target}`,
    maxOutputBytes: 64 * 1024,
    onStderrLine: (line) => options.logger?.warn?.(`[remote] db-validate stderr: ${line}`),
  });
  if (result.spawnError || result.timedOut || result.cancelled || result.code !== 0) {
    const failure = classifySshFailure(result);
    throw new BackendError(failure.code, failure.message, {
      kind,
      path: target,
      hint: failure.hint,
      exitCode: result.code ?? null,
      stderr: tailOf(result),
    });
  }
  const parsed = parseRemoteDatabaseValidationOutput(result.stdout);
  if (!parsed.parsed) {
    throw new BackendError(
      'DB_NOT_FOUND',
      `无法校验远端路径 ${target}：校验脚本没有返回结果（输出: ${tailOf(result, 3) || '(空)'}）`,
      { kind, path: target },
    );
  }
  if (!parsed.ok) {
    throw new BackendError('DB_NOT_FOUND', `${describeValidationFailure(parsed.reason)}: ${target}`, {
      kind,
      path: target,
      reason: parsed.reason,
    });
  }
  const dbCount = kind === 'db' ? 1 : Number(parsed.numbers[0] || 0);
  const sizeBytes = kind === 'db' ? Number(parsed.numbers[1] || 0) : null;
  return { ok: true, kind, path: target, dbCount, sizeBytes };
}

function tailOf(result, lines = 6) {
  const text = [result?.stderr, result?.stdout]
    .filter((part) => part && String(part).trim().length > 0)
    .map((part) => String(part).trimEnd())
    .join('\n');
  if (!text) return '';
  return text.split('\n').slice(-lines).join('\n').slice(-2000);
}

function toReportError(error, fallbackCode) {
  const normalized = normalizeError(error, fallbackCode);
  return { code: normalized.code, message: normalized.message, details: normalized.details };
}
