/**
 * Discovery for the local CA installation: repository root, Python
 * interpreter, `ca` console script, databases, plus the probe report shown by
 * the backend settings page.
 *
 * Discovery never guesses a database silently: `start()` fails with an
 * actionable BACKEND_NOT_CONFIGURED/DB_NOT_FOUND error, while `probe()` lists
 * candidate DBs so the user can pick one in the UI. All child-process work in
 * the probe is delegated through an injected `execCa` function so tests never
 * touch a real Python.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { BackendError, describeCause } from './errors.js';
import {
  formatBytes,
  isDirectory,
  isExecutableFile,
  isFile,
  nonEmptyString,
  tailText,
} from './util.js';

const CA_MANIFEST_RE = /name\s*=\s*["']ca-analyzer["']/;
const MAX_REPO_WALK = 24;

/** Does `dir` look like a CA checkout? */
export function looksLikeCaRepo(dir) {
  try {
    const manifest = path.join(dir, 'pyproject.toml');
    if (isFile(manifest)) {
      const text = fs.readFileSync(manifest, 'utf8');
      if (CA_MANIFEST_RE.test(text)) return true;
    }
  } catch {
    // unreadable manifest: fall through to the src/ca heuristic
  }
  return isFile(path.join(dir, 'src', 'ca', '__init__.py'));
}

/**
 * Walk up from each start directory looking for a CA checkout.
 * @param {{startDirs?: string[], maxDepth?: number}} [options]
 * @returns {string|null}
 */
export function findRepoRoot(options = {}) {
  const maxDepth = Math.max(1, Math.trunc(Number(options.maxDepth) || MAX_REPO_WALK));
  for (const start of options.startDirs || []) {
    const text = nonEmptyString(start);
    if (!text) continue;
    let dir = path.resolve(text);
    for (let depth = 0; depth < maxDepth; depth += 1) {
      if (looksLikeCaRepo(dir)) return dir;
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  return null;
}

/**
 * Resolve an executable name (`ca`, `python3`) against PATH.
 * @param {string} name
 * @param {{env?: Record<string,string|undefined>}} [options]
 * @returns {string|null}
 */
export function resolveExecutableOnPath(name, options = {}) {
  const env = options.env || process.env;
  const pathValue = env.PATH || '';
  for (const dir of pathValue.split(path.delimiter)) {
    const trimmed = dir.trim();
    if (!trimmed) continue;
    const candidate = path.join(trimmed, name);
    if (isExecutableFile(candidate)) return candidate;
  }
  return null;
}

/**
 * Resolve repo + interpreter + CLI without touching the database.
 *
 * Order: configured values first, then the repository venv, then PATH. A
 * configured-but-invalid value is a hard error (fail fast with a precise
 * message) instead of silently falling back.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config resolved config
 * @param {string} [options.cwd]
 * @param {Record<string,string|undefined>} [options.env]
 * @returns {{repoRoot: string|null, repoSource: string|null, python: {path: string, source: string},
 *            cli: {kind: 'console'|'path'|null, path: string|null, source: string|null},
 *            cwd: string, warnings: string[]}}
 */
export function resolveRuntime(options) {
  const config = options.config;
  const cwd = options.cwd || process.cwd();
  const env = options.env || process.env;
  const local = config.local || {};
  const warnings = [];

  let repoRoot = null;
  let repoSource = null;
  if (local.repoRoot) {
    if (!isDirectory(local.repoRoot)) {
      throw new BackendError(
        'REPO_NOT_FOUND',
        `local.repoRoot 不是目录: ${local.repoRoot}；请改为 CA 检出目录（含 pyproject.toml）`,
        { path: local.repoRoot, field: 'local.repoRoot' },
      );
    }
    if (!looksLikeCaRepo(local.repoRoot)) {
      throw new BackendError(
        'REPO_NOT_FOUND',
        `local.repoRoot 不是 CA 仓库: ${local.repoRoot}（缺少 name = "ca-analyzer" 的 pyproject.toml 或 src/ca/__init__.py）`,
        { path: local.repoRoot, field: 'local.repoRoot' },
      );
    }
    repoRoot = local.repoRoot;
    repoSource = 'configured';
  } else {
    repoRoot = findRepoRoot({ startDirs: [cwd] });
    if (repoRoot) repoSource = 'discovered';
  }

  let pythonPath = null;
  let pythonSource = null;
  if (local.python) {
    if (!isExecutableFile(local.python)) {
      throw new BackendError(
        'PYTHON_NOT_FOUND',
        `local.python 不存在或不可执行: ${local.python}；请填写 CA venv 的 python（如 <repo>/.venv/bin/python）`,
        { path: local.python, field: 'local.python' },
      );
    }
    pythonPath = local.python;
    pythonSource = 'configured';
  } else if (repoRoot) {
    const venvPython = path.join(repoRoot, '.venv', 'bin', 'python');
    if (isExecutableFile(venvPython)) {
      pythonPath = venvPython;
      pythonSource = 'repo-venv';
    }
  }
  if (!pythonPath) {
    pythonPath = resolveExecutableOnPath('python3', { env });
    if (pythonPath) pythonSource = 'path';
  }
  if (!pythonPath) {
    throw new BackendError(
      'PYTHON_NOT_FOUND',
      '找不到可用的 Python 解释器：请配置 local.python，或在 CA 检出中运行 scripts/bootstrap.sh 创建 .venv，并确保 python3 在 PATH 中',
      { field: 'local.python', cwd },
    );
  }

  /** @type {{kind: 'console'|'path'|null, path: string|null, source: string|null}} */
  const cli = { kind: null, path: null, source: null };
  if (repoRoot) {
    const consoleScript = path.join(repoRoot, '.venv', 'bin', 'ca');
    if (isExecutableFile(consoleScript)) {
      cli.kind = 'console';
      cli.path = consoleScript;
      cli.source = 'repo-venv';
    }
  }
  if (!cli.path) {
    const onPath = resolveExecutableOnPath('ca', { env });
    if (onPath) {
      cli.kind = 'path';
      cli.path = onPath;
      cli.source = 'path';
    }
  }
  if (!cli.path) {
    warnings.push(
      `未找到 ca console script，将使用 "${path.basename(pythonPath)} -m ca"；若模块未安装会启动失败。`,
    );
  }
  if (!repoRoot) {
    warnings.push('未找到 CA 仓库（local.repoRoot 未设置且当前目录向上没有 pyproject.toml）；将依赖已安装的 ca 模块。');
  }

  return {
    repoRoot,
    repoSource,
    python: { path: pythonPath, source: pythonSource },
    cli,
    cwd: repoRoot || cwd,
    warnings,
  };
}

/**
 * Build the argv for a CA invocation.
 * @param {{python: {path: string}, cli: {kind: string|null, path: string|null}}} runtime
 * @param {string[]} args CA CLI args (without the executable)
 * @returns {{command: string, args: string[]}}
 */
export function buildCaArgv(runtime, args) {
  const list = Array.isArray(args) ? args.map(String) : [];
  if (runtime?.cli?.kind === 'console' && runtime.cli.path) {
    return { command: runtime.cli.path, args: list };
  }
  if (runtime?.cli?.kind === 'path' && runtime.cli.path) {
    return { command: runtime.cli.path, args: list };
  }
  return { command: runtime.python.path, args: ['-m', 'ca', ...list] };
}

/** Count `*.db` files directly inside `dir` (no recursion; shards are flat). */
export function countDbFiles(dir) {
  try {
    return fs
      .readdirSync(dir, { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.endsWith('.db')).length;
  } catch {
    return 0;
  }
}

/**
 * Check the configured local DB/dbDir and return the serve target.
 * Throws actionable BackendErrors so `start()` fails before spawning anything.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config resolved config
 * @returns {{db: string|null, dbDir: string|null, shards: number}}
 */
export function resolveLocalDatabase(options) {
  const local = options.config.local || {};
  if (local.db && local.dbDir) {
    throw new BackendError(
      'CONFIG_INVALID',
      'local.db 与 local.dbDir 互斥：单库用 db，分片联邦用 dbDir',
      { field: 'local.db' },
    );
  }
  if (local.db) {
    if (!isFile(local.db)) {
      throw new BackendError(
        'DB_NOT_FOUND',
        `本地索引数据库不存在: ${local.db}；请先运行 ca index 生成数据库，或在设置中改正 local.db`,
        { path: local.db, field: 'local.db' },
      );
    }
    return { db: local.db, dbDir: null, shards: 0 };
  }
  if (local.dbDir) {
    if (!isDirectory(local.dbDir)) {
      throw new BackendError(
        'DB_NOT_FOUND',
        `本地分片目录不存在: ${local.dbDir}；请在设置中改正 local.dbDir`,
        { path: local.dbDir, field: 'local.dbDir' },
      );
    }
    const shards = countDbFiles(local.dbDir);
    if (shards === 0) {
      throw new BackendError(
        'DB_NOT_FOUND',
        `本地分片目录中没有 *.db 文件: ${local.dbDir}；请先运行 ca index 或改用单库 local.db`,
        { path: local.dbDir, field: 'local.dbDir', shards: 0 },
      );
    }
    return { db: null, dbDir: local.dbDir, shards };
  }
  throw new BackendError(
    'BACKEND_NOT_CONFIGURED',
    '本地 CA 后端未配置数据库：请在设置中填写 local.db（单库）或 local.dbDir（分片目录），或切换到远程后端。',
    { field: 'local.db' },
  );
}

/**
 * Full local serve target: runtime + database + non-fatal warnings about the
 * optional deep-tool paths.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config
 * @param {string} [options.cwd]
 * @param {Record<string,string|undefined>} [options.env]
 */
export function resolveLocalTarget(options) {
  const runtime = resolveRuntime(options);
  const database = resolveLocalDatabase(options);
  const local = options.config.local || {};
  const warnings = [...runtime.warnings];
  if (local.repo && !isDirectory(local.repo)) {
    warnings.push(`local.repo 不存在或不是目录: ${local.repo}（deep tools 可能不可用）`);
  }
  if (local.compdb && !isFile(local.compdb)) {
    warnings.push(`local.compdb 不存在: ${local.compdb}（ensure_deep/prewarm 可能不可用）`);
  }
  return {
    ...runtime,
    ...database,
    stateDb: local.stateDb || null,
    repo: local.repo || null,
    compdb: local.compdb || null,
    warnings,
  };
}

function describeStat(target) {
  try {
    const stat = fs.statSync(target);
    return { sizeBytes: stat.size, mtimeMs: stat.mtimeMs };
  } catch {
    return { sizeBytes: null, mtimeMs: null };
  }
}

/**
 * Scan a few well-known top-level locations for CA databases so the settings
 * page can offer candidates. Deliberately shallow: the OpenHarmony source tree
 * must never be walked.
 *
 * @param {{homedir?: string, cwd?: string, maxCandidates?: number}} [options]
 * @returns {{candidates: Array<{path: string, kind: 'db'|'dbDir', shards?: number,
 *            sizeBytes: number|null, mtimeMs: number|null, source: string}>}}
 */
export function scanDatabaseCandidates(options = {}) {
  const homedir = options.homedir || os.homedir();
  const cwd = options.cwd || process.cwd();
  const maxCandidates = Math.max(1, Math.trunc(Number(options.maxCandidates) || 8));
  const roots = [
    { dir: path.join(homedir, 'CA_OH'), source: 'home' },
    { dir: path.join(homedir, 'CA'), source: 'home' },
    { dir: cwd, source: 'cwd' },
  ];
  /** @type {Array<Record<string, any>>} */
  const candidates = [];
  const seen = new Set();
  for (const root of roots) {
    let entries;
    try {
      entries = fs.readdirSync(root.dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = path.join(root.dir, entry.name);
      if (seen.has(full)) continue;
      if (entry.isFile() && entry.name.endsWith('.db')) {
        if (entry.name.includes('.serve-state.db')) continue;
        const stat = describeStat(full);
        if (!stat.sizeBytes) continue;
        seen.add(full);
        candidates.push({ path: full, kind: 'db', ...stat, source: root.source });
      } else if (entry.isDirectory() && (entry.name === 'index' || entry.name === 'shards')) {
        const shards = countDbFiles(full);
        if (shards === 0) continue;
        const stat = describeStat(full);
        seen.add(full);
        candidates.push({
          path: full,
          kind: 'dbDir',
          shards,
          sizeBytes: stat.sizeBytes,
          mtimeMs: stat.mtimeMs,
          source: root.source,
        });
      }
    }
  }
  candidates.sort((left, right) => (right.mtimeMs || 0) - (left.mtimeMs || 0));
  return { candidates: candidates.slice(0, maxCandidates) };
}

/**
 * Project `/api/health` data into the fields exposed on backend status.
 *
 * The current CA payload is
 * `{status,server,version,started_at,uptime_seconds,database:{paths,federated,state_db},variant,...}`
 * and has no `features` object; derive one from the endpoint/job fields so UIs
 * written against the documented `features` shape keep working.
 */
export function healthFields(healthData) {
  const data = healthData && typeof healthData === 'object' ? healthData : {};
  const database = data.database && typeof data.database === 'object' ? data.database : null;
  let features = null;
  if (data.features && typeof data.features === 'object') {
    features = { ...data.features };
  } else if (Object.keys(data).length > 0) {
    features = {
      jobs: Boolean(data.jobs),
      sse: data.events_endpoint !== undefined ? Boolean(data.events_endpoint) : true,
      mcp: data.mcp_endpoint !== undefined ? Boolean(data.mcp_endpoint) : true,
      federated: Boolean(database?.federated),
    };
  }
  return {
    version: typeof data.version === 'string' ? data.version : null,
    features,
    database,
    server: typeof data.server === 'string' ? data.server : null,
    status: typeof data.status === 'string' ? data.status : null,
  };
}

/**
 * Environment probe for the settings page. Never throws for environment
 * problems; it returns a report with checks/hints instead so the UI can show
 * exactly what is missing.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config
 * @param {string} [options.cwd]
 * @param {string} [options.homedir]
 * @param {Record<string,string|undefined>} [options.env]
 * @param {(argv: string[], opts: {timeoutMs: number}) => Promise<{code: number|null, stdout: string, stderr: string, spawnError?: Error|null, timedOut?: boolean, cancelled?: boolean}>} [options.execCa]
 * @returns {Promise<Record<string, any>>}
 */
export async function probeLocalEnvironment(options) {
  const config = options.config;
  const cwd = options.cwd || process.cwd();
  const homedir = options.homedir || os.homedir();
  const env = options.env || process.env;
  const local = config.local || {};

  /** @type {Array<{name: string, ok: boolean, required: boolean, value?: unknown, detail?: string}>} */
  const checks = [];
  const hints = [];
  const warnings = [];

  let runtime = null;
  try {
    runtime = resolveRuntime({ config, cwd, env });
    checks.push({
      name: 'repo',
      ok: Boolean(runtime.repoRoot),
      required: false,
      value: runtime.repoRoot,
      detail: runtime.repoRoot ? `来源: ${runtime.repoSource}` : '未找到 CA 仓库，将依赖已安装模块',
    });
    checks.push({
      name: 'python',
      ok: Boolean(runtime.python.path),
      required: true,
      value: runtime.python.path,
      detail: `来源: ${runtime.python.source}`,
    });
    checks.push({
      name: 'ca-cli',
      ok: true,
      required: false,
      value: runtime.cli.path || `${path.basename(runtime.python.path)} -m ca`,
      detail: runtime.cli.kind ? `来源: ${runtime.cli.source}` : '使用 python -m ca 回退',
    });
    warnings.push(...runtime.warnings);
  } catch (error) {
    const normalized = error instanceof BackendError ? error : null;
    checks.push({
      name: 'python',
      ok: false,
      required: true,
      detail: normalized ? normalized.message : describeCause(error),
    });
    hints.push(
      '未找到可用的 Python/CA 环境：请配置 local.python，或在 CA 检出中运行 scripts/bootstrap.sh 创建 .venv。',
    );
  }

  const versionInfo = { ok: false, version: null, schemaVersion: null, detail: null };
  if (runtime) {
    if (typeof options.execCa === 'function') {
      try {
        const argv = buildCaArgv(runtime, ['version', '--json']);
        const result = await options.execCa(argv, { timeoutMs: 5000 });
        const stdout = String(result?.stdout || '').trim();
        let parsed = null;
        if (stdout.startsWith('{')) {
          try {
            parsed = JSON.parse(stdout);
          } catch {
            parsed = null;
          }
        }
        if (result?.spawnError) {
          versionInfo.detail = `无法执行 ${argv.command}: ${describeCause(result.spawnError)}`;
        } else if (result?.timedOut) {
          versionInfo.detail = `ca version 超时（5s）：${argv.command}`;
        } else if (result?.code !== 0) {
          versionInfo.detail = `ca version 退出码 ${result?.code}：${tailText(
            [result?.stderr, result?.stdout].filter(Boolean).join('\n').trim(),
            400,
          )}`;
        } else if (parsed && typeof parsed === 'object') {
          versionInfo.ok = true;
          versionInfo.version = parsed.version || null;
          versionInfo.schemaVersion = parsed.schema_version || null;
          versionInfo.detail = `ca-analyzer ${versionInfo.version} (schema ${versionInfo.schemaVersion})`;
          versionInfo.python = typeof parsed.python === 'string' ? parsed.python : null;
          versionInfo.platform = typeof parsed.platform === 'string' ? parsed.platform : null;
        } else {
          versionInfo.ok = true;
          versionInfo.detail = tailText(stdout, 200) || 'ca version 返回空输出';
        }
      } catch (error) {
        versionInfo.detail = describeCause(error);
      }
    } else {
      versionInfo.detail = '未提供 execCa 回调，跳过版本检查';
    }
    checks.push({
      name: 'ca-version',
      ok: versionInfo.ok,
      required: false,
      value: versionInfo.version,
      detail: versionInfo.detail,
    });
    if (!versionInfo.ok) {
      hints.push(
        `CA CLI 无法正常运行（${buildCaArgv(runtime, ['version']).command}）：请检查依赖安装（pip install -r requirements.txt && pip install -e .）。`,
      );
    }
  }

  let database = { ok: false, db: null, dbDir: null, shards: 0, detail: null };
  try {
    const resolved = resolveLocalDatabase({ config });
    database = { ok: true, ...resolved, detail: resolved.db || `${resolved.dbDir}（${resolved.shards} 个分片）` };
  } catch (error) {
    database.detail = error instanceof BackendError ? error.message : describeCause(error);
    if (error?.code === 'BACKEND_NOT_CONFIGURED') {
      hints.push('尚未配置本地数据库：填写 local.db（单库）或 local.dbDir（分片目录），或从下方候选选择。');
    } else if (error?.code === 'DB_NOT_FOUND') {
      hints.push('配置的本地数据库不可用：确认路径存在且是 CA 生成的 *.db 文件/分片目录。');
    }
  }
  checks.push({
    name: 'database',
    ok: database.ok,
    required: true,
    value: database.db || database.dbDir,
    detail: database.detail,
  });

  const { candidates } = scanDatabaseCandidates({ homedir, cwd });
  if (!database.ok && candidates.length > 0) {
    hints.push(`发现 ${candidates.length} 个候选数据库，可在设置中选用（见 candidates）。`);
  }

  const scanDir = (target) => {
    if (!target) return { path: null, exists: false, readable: false, writable: false };
    try {
      fs.accessSync(target, fs.constants.R_OK | fs.constants.W_OK);
      return { path: target, exists: true, readable: true, writable: true };
    } catch {
      return { path: target, exists: isDirectory(target) || isFile(target), readable: false, writable: false };
    }
  };
  const stateDirInfo = scanDir(options.paths?.stateDir || path.join(homedir, '.dsh', 'plugin-data', 'dsh-ca'));

  const requiredOk = checks.filter((check) => check.required).every((check) => check.ok);
  return {
    ok: requiredOk,
    target: 'local',
    platform: process.platform,
    arch: process.arch,
    node: process.version,
    cwd,
    repoRoot: runtime?.repoRoot || null,
    repoSource: runtime?.repoSource || null,
    python: runtime
      ? { ...runtime.python, version: versionInfo.python || null }
      : { path: local.python || null, source: null, version: null },
    cli: runtime
      ? {
          kind: runtime.cli.kind,
          path: runtime.cli.path,
          effective: runtime.cli.path || `${path.basename(runtime.python.path)} -m ca`,
        }
      : null,
    ca: versionInfo.ok
      ? {
          version: versionInfo.version,
          schemaVersion: versionInfo.schemaVersion,
          python: versionInfo.python || null,
          platform: versionInfo.platform || null,
        }
      : null,
    database: {
      db: database.db,
      dbDir: database.dbDir,
      shards: database.shards,
      configured: Boolean(local.db || local.dbDir),
    },
    stateDir: stateDirInfo,
    disk: stateDirInfo.writable ? 'ok' : 'unknown',
    candidates,
    checks,
    warnings,
    hints,
    scannedAt: Date.now(),
    databaseSize: database.db && isFile(database.db) ? formatBytes(fs.statSync(database.db).size) : null,
  };
}
