/**
 * BackendManager: single entry point used by the host wiring, RPC layer, tools
 * and task manager.
 *
 * Responsibilities:
 *   - resolve settings (defaults + host wiring config + persisted user patch)
 *     and keep them in `$DSH_HOME/plugin-data/dsh-ca/settings.json`;
 *   - own the shared bounded logger and HTTP client;
 *   - route start/stop/restart/exec/request calls to the local or remote
 *     backend implementation, recreating a backend when settings changed;
 *   - expose one status shape and one status/log event stream regardless of
 *     which backend is active.
 *
 * Both backend implementations are constructed lazily so an unused remote
 * backend never opens sockets. The manager itself performs no I/O at creation
 * time beyond reading settings.json and creating the log directory.
 */

import os from 'node:os';
import path from 'node:path';

import {
  applyConfigPatch,
  DEFAULT_CONFIG,
  describeConfigurationHint,
  loadStoredSettings,
  publicConfig,
  resolveConfig,
  resolveConfigPaths,
  resolveConfiguredMode,
  saveStoredSettings,
  validateConfig,
} from '../config.js';
import { createDatabaseErrorReport } from './databases.js';
import { BackendError, normalizeError, toErrorPayload } from './errors.js';
import { createHttpClient } from './http.js';
import { createLocalBackend } from './local.js';
import { createLogger } from './log.js';
import {
  buildProjectFromLegacyConfig,
  createProjectId,
  findProjectById,
  normalizeProject,
  projectNameKey,
  projectToBackendPatch,
  sanitizeStoredProject,
} from './projects.js';
import { createRemoteBackend } from './remote/index.js';
import { cloneJson, isPlainObject, nonEmptyString } from './util.js';

export { BackendError } from './errors.js';

const BACKEND_MODES = ['local', 'remote'];

/**
 * @param {object|null} ctx DSH host context (kept for wiring symmetry/future use)
 * @param {Record<string, any>} [configInput] deployment config from the Cordis patch
 * @param {object} [deps] injectable dependencies (spawn/fetch/clock/paths/logger)
 */
export function createBackendManager(ctx, configInput, deps = {}) {
  const safeDeps = deps && typeof deps === 'object' ? deps : {};
  const env = safeDeps.env || process.env;
  const cwd = safeDeps.cwd || process.cwd();
  const homedir = safeDeps.homedir || os.homedir();
  const now = typeof safeDeps.now === 'function' ? safeDeps.now : Date.now;
  const paths = safeDeps.paths || resolveConfigPaths({ env, homedir });

  // -- settings --------------------------------------------------------- //
  const stored = loadStoredSettings(paths);
  let storedPatch = stored.patch;
  let configError = null;
  let config;
  try {
    config = resolveConfig({ explicit: configInput, stored: storedPatch, env, homedir, cwd });
  } catch (error) {
    configError = normalizeError(error, 'CONFIG_INVALID', 'CA 后端配置无效');
    try {
      const fallback = JSON.parse(JSON.stringify(DEFAULT_CONFIG));
      config = validateConfig(fallback, { cwd, homedir });
    } catch {
      config = JSON.parse(JSON.stringify(DEFAULT_CONFIG));
    }
  }

  const ownsLogger = !safeDeps.logger;
  const logger =
    safeDeps.logger ||
    createLogger({
      dir: paths.logDir,
      maxBytes: config.log.maxBytes,
      keepFiles: config.log.keepFiles,
      ringSize: config.log.ringSize,
      now,
    });
  if (stored.error) {
    logger.warn(`[backend] settings.json 读取失败，使用默认设置: ${stored.error.message}`);
  }
  if (configError) {
    logger.error(`[backend] ${configError.message}`);
  }

  // -- project store (product model) ----------------------------------- //
  /** @type {{projects: Array<Record<string, any>>, activeProjectId: string|null}} */
  const projectState = { projects: [], activeProjectId: null };

  const generateProjectId = () =>
    typeof safeDeps.generateProjectId === 'function' ? safeDeps.generateProjectId() : createProjectId();

  /** Snapshot of the project store for settings persistence/rollback. */
  function projectStoreState() {
    return {
      projects: projectState.projects.map((project) => cloneJson(project)),
      activeProjectId: projectState.activeProjectId,
    };
  }

  function persistSettings(patch = storedPatch) {
    saveStoredSettings(paths, patch, projectStoreState());
  }

  if (stored.projects !== null) {
    const sanitized = stored.projects.map((raw) => sanitizeStoredProject(raw)).filter(Boolean);
    if (sanitized.length !== stored.projects.length) {
      logger.warn(
        `[backend] settings.json 中有 ${stored.projects.length - sanitized.length} 个无效项目已忽略`,
      );
    }
    projectState.projects = sanitized;
    if (stored.activeProjectId && sanitized.some((project) => project.id === stored.activeProjectId)) {
      projectState.activeProjectId = stored.activeProjectId;
    }
  } else if (stored.exists) {
    // Upgrade path: the settings file predates the project model. Generate a
    // default project so the existing backend configuration keeps working.
    const legacyMode = resolveConfiguredMode(config);
    const migrated = buildProjectFromLegacyConfig({
      config,
      mode: legacyMode,
      now,
      generateId: generateProjectId,
    });
    if (migrated) {
      projectState.projects = [migrated];
      projectState.activeProjectId = migrated.id;
      try {
        saveStoredSettings(paths, storedPatch, projectStoreState());
        logger.info(
          `[backend] 旧配置已迁移为默认项目 "${migrated.name}"（${migrated.backend}，${
            migrated.dbDir || migrated.db
          }）`,
        );
      } catch (migrationError) {
        logger.warn(
          `[backend] 迁移项目写入 settings.json 失败: ${migrationError?.message || migrationError}`,
        );
      }
    }
  }

  const http = createHttpClient({
    fetchImpl: typeof safeDeps.fetch === 'function' ? safeDeps.fetch : globalThis.fetch,
    defaultTimeoutMs: config.requestTimeoutMs,
    connectTimeoutMs: config.requestTimeoutMs,
  });

  /** @type {Record<'local'|'remote', {backend: any, version: number, unsubscribe: () => void}|null>} */
  const backends = { local: null, remote: null };
  let configVersion = 0;
  let disposed = false;
  const createdAt = now();
  /** @type {Map<string, Set<(payload: any) => void>>} */
  const eventListeners = new Map();

  function emit(event, payload) {
    const direct = eventListeners.get(event);
    if (direct) {
      for (const listener of direct) {
        try {
          listener(payload);
        } catch {
          // event consumers must not break the manager
        }
      }
    }
    const wildcard = eventListeners.get('*');
    if (wildcard) {
      for (const listener of wildcard) {
        try {
          listener({ type: event, ...payload });
        } catch {
          // ignore
        }
      }
    }
  }

  function on(event, listener) {
    if (typeof event !== 'string' || typeof listener !== 'function') return () => {};
    let set = eventListeners.get(event);
    if (!set) {
      set = new Set();
      eventListeners.set(event, set);
    }
    set.add(listener);
    return () => {
      set.delete(listener);
      if (set.size === 0 && eventListeners.get(event) === set) eventListeners.delete(event);
    };
  }

  function backendDeps() {
    return {
      ...safeDeps,
      env,
      cwd,
      homedir,
      now,
    };
  }

  /**
   * Return a backend instance built from the current config snapshot, creating
   * (or recreating after a config change) it as needed.
   * @param {'local'|'remote'} mode
   */
  async function ensureBackend(mode) {
    const existing = backends[mode];
    if (existing && existing.version === configVersion) return existing.backend;
    if (existing) {
      try {
        existing.unsubscribe();
      } catch {
        // ignore
      }
      try {
        await existing.backend.dispose();
      } catch (error) {
        logger.warn(`[backend] 释放旧 ${mode} 后端失败: ${error?.message || error}`);
      }
      backends[mode] = null;
    }
    const backend =
      mode === 'local'
        ? createLocalBackend({ config, paths, logger, deps: backendDeps() })
        : createRemoteBackend({ config, paths, logger, deps: backendDeps() });
    const unsubscribe = backend.subscribe((status, meta) => {
      emit('status', { ...status, mode, configChanged: false }, meta || { reason: 'changed' });
    });
    backends[mode] = { backend, version: configVersion, unsubscribe };
    return backend;
  }

  function activeEntry() {
    for (const mode of BACKEND_MODES) {
      const entry = backends[mode];
      if (!entry) continue;
      if (entry.backend.getStatus().state !== 'stopped') return { mode, entry };
    }
    return null;
  }

  function statusForUnconfigured(extra = {}) {
    return {
      mode: 'unconfigured',
      state: 'stopped',
      baseUrl: null,
      db: null,
      error: configError ? configError.message : null,
      since: createdAt,
      version: null,
      features: null,
      configChanged: false,
      hint: configError ? '修正 CA 后端配置后重试（backend.configure）' : describeConfigurationHint(config),
      ...extra,
    };
  }

  function getStatus() {
    if (disposed) {
      return {
        ...statusForUnconfigured({ error: 'CA 后端管理器已释放（dispose）' }),
        disposed: true,
        logFile: logger.filePath,
      };
    }
    const active = activeEntry();
    if (active) {
      const status = active.entry.backend.getStatus();
      return {
        ...status,
        mode: active.mode,
        configChanged: active.entry.version !== configVersion,
        logFile: status.logFile || logger.filePath,
      };
    }
    const configuredMode = resolveConfiguredMode(config);
    if (configuredMode === 'unconfigured') {
      return { ...statusForUnconfigured(), logFile: logger.filePath };
    }
    const entry = backends[configuredMode];
    if (entry) {
      const status = entry.backend.getStatus();
      return {
        ...status,
        mode: configuredMode,
        configChanged: entry.version !== configVersion,
        logFile: status.logFile || logger.filePath,
      };
    }
    const fallbackDb =
      configuredMode === 'local'
        ? config.local.db || config.local.dbDir || null
        : config.remote.db || config.remote.dbDir || null;
    return {
      mode: configuredMode,
      state: 'stopped',
      baseUrl: null,
      db: fallbackDb,
      error: configError ? configError.message : null,
      since: createdAt,
      version: null,
      features: null,
      configChanged: false,
      logFile: logger.filePath,
    };
  }

  function assertTargetConfigured(target) {
    if (target === 'remote') {
      const host = config.remote?.ssh?.host;
      if (!host) {
        throw new BackendError(
          'BACKEND_NOT_CONFIGURED',
          '远程后端未配置 SSH 主机：请设置 remote.ssh.host（支持 ssh config alias 或 user@host）以及 remote.db/remote.dbDir。',
          { field: 'remote.ssh.host' },
        );
      }
      return;
    }
    if (target === 'local') {
      if (!config.local?.db && !config.local?.dbDir) {
        throw new BackendError(
          'BACKEND_NOT_CONFIGURED',
          '本地后端未配置数据库：请设置 local.db（单库）或 local.dbDir（分片目录），或改用远程后端。',
          { field: 'local.db' },
        );
      }
      return;
    }
    throw new BackendError(
      'BACKEND_NOT_CONFIGURED',
      describeConfigurationHint(config) || 'CA 后端未配置',
      { mode: target },
    );
  }

  function requireRunning() {
    const status = getStatus();
    if (status.state !== 'running' || !status.baseUrl) {
      const suffix = status.error ? `：${status.error}` : '';
      throw new BackendError(
        'BACKEND_NOT_RUNNING',
        `CA 后端未运行（mode=${status.mode}, state=${status.state}）${suffix}；请先调用 backend.start`,
        { mode: status.mode, state: status.state, error: status.error || null },
      );
    }
    return status;
  }

  function normalizeRequestPath(pathname) {
    if (typeof pathname !== 'string' || pathname.trim().length === 0) {
      throw new BackendError('INVALID_ARGUMENT', 'CA 请求路径不能为空（应为 /api/... 相对路径）');
    }
    const text = pathname.trim();
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
      throw new BackendError('INVALID_ARGUMENT', `CA 请求路径必须是相对路径（当前 ${text}）`);
    }
    if (text.startsWith('//')) {
      throw new BackendError('INVALID_ARGUMENT', `CA 请求路径不能以 // 开头（当前 ${text}）`);
    }
    return text.startsWith('/') ? text : `/${text}`;
  }

  // -- public API ------------------------------------------------------- //
  async function start(startOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法启动');
    const target = startOptions.target || resolveConfiguredMode(config);
    assertTargetConfigured(target);

    for (const other of BACKEND_MODES) {
      if (other === target) continue;
      const entry = backends[other];
      if (entry && entry.backend.getStatus().state !== 'stopped') {
        logger.info(`[backend] 切换后端，先停止 ${other}`);
        await entry.backend.stop();
      }
    }

    const existing = backends[target];
    const existingStatus = existing ? existing.backend.getStatus() : null;
    if (existing && existing.version !== configVersion && existingStatus.state !== 'stopped') {
      logger.info('[backend] 配置已变更，停止旧后端以便按新设置启动');
      await existing.backend.stop();
    }
    const backend = await ensureBackend(target);
    if (backend.getStatus().state === 'running') return getStatus();
    logger.info(`[backend] 启动 ${target} 后端（backend=${config.backend}）`);
    try {
      await backend.start({ reason: startOptions.reason || 'manual', signal: startOptions.signal });
    } catch (error) {
      throw normalizeError(error, 'BACKEND_START_FAILED', 'CA 后端启动失败');
    }
    return getStatus();
  }

  async function stop() {
    if (disposed) return getStatus();
    for (const mode of BACKEND_MODES) {
      const entry = backends[mode];
      if (!entry) continue;
      if (entry.backend.getStatus().state === 'stopped') continue;
      try {
        await entry.backend.stop();
      } catch (error) {
        logger.warn(`[backend] 停止 ${mode} 后端失败: ${error?.message || error}`);
      }
    }
    return getStatus();
  }

  async function restart(restartOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法重启');
    const target = restartOptions.target || resolveConfiguredMode(config);
    assertTargetConfigured(target);
    await stop();
    const backend = await ensureBackend(target);
    try {
      await backend.start({ reason: 'restart', signal: restartOptions.signal });
    } catch (error) {
      throw normalizeError(error, 'BACKEND_START_FAILED', 'CA 后端重启失败');
    }
    return getStatus();
  }

  async function request(pathname, requestOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法请求');
    const status = requireRunning();
    return http.request(status.baseUrl, normalizeRequestPath(pathname), {
      method: requestOptions.method || 'GET',
      query: requestOptions.query,
      body: requestOptions.body,
      timeoutMs:
        requestOptions.timeoutMs === undefined ? config.requestTimeoutMs : requestOptions.timeoutMs,
      signal: requestOptions.signal,
      headers: requestOptions.headers,
    });
  }

  async function requestStream(pathname, streamOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法订阅事件');
    const status = requireRunning();
    return http.requestStream(status.baseUrl, normalizeRequestPath(pathname), {
      query: streamOptions.query,
      onEvent: streamOptions.onEvent,
      signal: streamOptions.signal,
      headers: streamOptions.headers,
      lastEventId: streamOptions.lastEventId,
      timeoutMs: streamOptions.timeoutMs,
    });
  }

  async function execCa(spec, execOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法执行 CA CLI');
    const target = resolveConfiguredMode(config);
    assertTargetConfigured(target);
    const entry = backends[target];
    const live = entry && entry.backend.getStatus().state !== 'stopped';
    if (live) return entry.backend.execCa(spec, execOptions);
    const backend = await ensureBackend(target);
    return backend.execCa(spec, execOptions);
  }

  async function probeLocal() {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法探测');
    const backend = createLocalBackend({ config, paths, logger, deps: backendDeps() });
    try {
      return await backend.probe();
    } finally {
      try {
        await backend.dispose();
      } catch {
        // probe-only instance holds no processes, but never mask the report
      }
    }
  }

  async function probeRemote(sshOverride) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法探测');
    const backend = createRemoteBackend({ config, paths, logger, deps: backendDeps() });
    try {
      return await backend.probe(sshOverride);
    } finally {
      try {
        await backend.dispose();
      } catch {
        // ignore
      }
    }
  }

  async function remoteBackendForAction() {
    const entry = backends.remote;
    if (entry && entry.version === configVersion) return entry.backend;
    if (entry && entry.backend.getStatus().state !== 'stopped') return entry.backend;
    return ensureBackend('remote');
  }

  function assertRemoteInstallable(installOptions = {}) {
    if (installOptions && installOptions.ssh) return;
    if (!config.remote?.ssh?.host) {
      throw new BackendError(
        'BACKEND_NOT_CONFIGURED',
        '远程安装需要 SSH 主机：请设置 remote.ssh.host，或在安装参数中传 ssh。',
        { field: 'remote.ssh.host' },
      );
    }
  }

  async function installRemote(installOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法安装');
    assertRemoteInstallable(installOptions);
    const backend = await remoteBackendForAction();
    return backend.install(installOptions);
  }

  async function startRemoteInstall(installOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法安装');
    assertRemoteInstallable(installOptions);
    const backend = await remoteBackendForAction();
    return backend.startInstall(installOptions);
  }

  function getInstallStatus() {
    const entry = backends.remote;
    if (entry && typeof entry.backend.getInstallStatus === 'function') {
      return entry.backend.getInstallStatus();
    }
    return {
      state: 'idle',
      startedAt: null,
      finishedAt: null,
      steps: [],
      logs: [],
      error: null,
      result: null,
    };
  }

  /**
   * List database/project candidates for the active (or requested) backend.
   *
   * Never throws: configuration, ssh or scan problems come back as
   * `{ok:false, error:{code,message,details?}}` so the settings page can render
   * them. The currently configured target is always `candidates[0]`.
   *
   * @param {{mode?: 'local'|'remote', maxItems?: number, ssh?: string|object, timeoutMs?: number}} [listOptions]
   */
  async function listDatabases(listOptions = {}) {
    const optionsObject = listOptions && typeof listOptions === 'object' ? listOptions : {};
    const requestedMode = optionsObject.mode;
    if (requestedMode !== undefined && !['local', 'remote'].includes(requestedMode)) {
      return createDatabaseErrorReport({
        mode: 'unconfigured',
        maxItems: optionsObject.maxItems,
        error: {
          code: 'INVALID_ARGUMENT',
          message: `mode 必须是 'local' 或 'remote'（当前 ${JSON.stringify(requestedMode)}）`,
        },
      });
    }
    if (disposed) {
      return createDatabaseErrorReport({
        mode: requestedMode || resolveConfiguredMode(config),
        maxItems: optionsObject.maxItems,
        error: { code: 'DISPOSED', message: 'CA 后端管理器已释放，无法扫描数据库' },
      });
    }
    const mode = requestedMode || resolveConfiguredMode(config);
    if (mode === 'unconfigured') {
      return createDatabaseErrorReport({
        mode: 'unconfigured',
        maxItems: optionsObject.maxItems,
        error: {
          code: 'BACKEND_NOT_CONFIGURED',
          message:
            describeConfigurationHint(config) ||
            'CA 后端尚未配置：请设置本地 DB（local.db/local.dbDir）或远程服务器（remote.ssh.host + remote.db/remote.dbDir）。',
        },
      });
    }
    const db = mode === 'local' ? config.local.db : config.remote.db;
    const dbDir = mode === 'local' ? config.local.dbDir : config.remote.dbDir;
    try {
      const backend = await ensureBackend(mode);
      return await backend.listDatabases({ ...optionsObject, mode });
    } catch (error) {
      const normalized = normalizeError(error, 'DB_SCAN_FAILED', '数据库扫描失败');
      logger.warn(`[backend] ${normalized.message}`);
      return createDatabaseErrorReport({
        mode,
        db,
        dbDir,
        maxItems: optionsObject.maxItems,
        error: {
          code: normalized.code,
          message: normalized.message,
          details: normalized.details,
        },
      });
    }
  }

  /**
   * Switch the backend to a listed database/directory.
   *
   * Order matters: validate the target first (one ssh round trip for remote),
   * then persist + restart. If validation, persistence or restart fails the
   * previous settings/config are restored (and the old backend is restarted
   * best-effort), so a bad selection never bricks the plugin.
   *
   * @param {{kind: 'db'|'dbDir', path: string, mode?: 'local'|'remote', ssh?: string|object}} selection
   * @returns {Promise<{config: Record<string, any>, status: Record<string, any>, validation: Record<string, any>}>}
   */
  async function useDatabase(selection = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法切换数据库');
    if (!selection || typeof selection !== 'object' || Array.isArray(selection)) {
      throw new BackendError('INVALID_ARGUMENT', 'useDatabase() 需要对象参数 {kind, path}');
    }
    const kind = selection.kind;
    const selectedPath = typeof selection.path === 'string' ? selection.path.trim() : '';
    if (kind !== 'db' && kind !== 'dbDir') {
      throw new BackendError(
        'INVALID_ARGUMENT',
        `kind 必须是 'db'（单库）或 'dbDir'（分片目录），当前 ${JSON.stringify(kind)}`,
        { field: 'kind' },
      );
    }
    if (!selectedPath) {
      throw new BackendError('INVALID_ARGUMENT', 'path 不能为空', { field: 'path' });
    }
    if (!path.isAbsolute(selectedPath)) {
      throw new BackendError('INVALID_ARGUMENT', `数据库路径必须是绝对路径: ${selectedPath}`, {
        field: 'path',
        path: selectedPath,
      });
    }
    const configuredMode = resolveConfiguredMode(config);
    const explicitMode = selection.mode;
    if (explicitMode !== undefined && !['local', 'remote'].includes(explicitMode)) {
      throw new BackendError('INVALID_ARGUMENT', `mode 必须是 'local' 或 'remote'（当前 ${JSON.stringify(explicitMode)}）`);
    }
    let targetMode = explicitMode || configuredMode;
    if (targetMode === 'unconfigured') {
      targetMode = nonEmptyString(config.remote?.ssh?.host) ? 'remote' : 'local';
    }
    /** @type {Record<string, any>} sparse patch, same shape as settings.json */
    const patch = {};
    if (configuredMode === 'unconfigured' || (explicitMode && explicitMode !== configuredMode)) {
      patch.backend = targetMode;
    }
    patch[targetMode] =
      kind === 'db' ? { db: selectedPath, dbDir: null } : { db: null, dbDir: selectedPath };

    // 1) Validate the candidate path before touching persisted settings.
    const backend = await ensureBackend(targetMode);
    const validation = await backend.validateDatabase({ kind, path: selectedPath });

    // 2) Snapshot rollback state, then persist + restart.
    const rollback = {
      storedPatch,
      config,
      configVersion,
      projects: projectState.projects.map((project) => cloneJson(project)),
      activeProjectId: projectState.activeProjectId,
    };
    let configureResult;
    try {
      configureResult = configure(patch);
      if (!configureResult.changed && getStatus().state === 'running') {
        return { config: publicConfig(config, paths), status: getStatus(), validation };
      }
      const status = await restart();
      logger.info(`[backend] 已切换数据库（mode=${targetMode}, ${kind}=${selectedPath}）`);
      return { config: publicConfig(config, paths), status, validation };
    } catch (rawError) {
      const cause = normalizeError(rawError, 'DATABASE_SWITCH_FAILED', '切换数据库失败');
      logger.error(`[backend] 切换数据库失败，回滚设置：${cause.message}`);
      const restore = await restoreAfterConfigFailure(rollback);
      throw new BackendError(
        'DATABASE_SWITCH_FAILED',
        `切换数据库失败：${cause.message}` +
          (restore.restored
            ? '；已恢复原配置并重启旧后端'
            : '；已恢复原配置，但旧后端未能自动重启，请重试 backend.start'),
        {
          cause: cause.toJSON(),
          restored: restore.restored,
          validation,
          status: getStatus(),
          restoreError: restore.restoreError ? restore.restoreError.message : null,
        },
      );
    }
  }

  /**
   * Restore config/settings/backend after a failed switch (database or project
   * activation). Best effort: never throws; returns whether the previous
   * backend is running again so callers can phrase the final error.
   *
   * @param {{storedPatch: Record<string, any>, config: Record<string, any>, configVersion: number,
   *          projects?: Array<Record<string, any>>, activeProjectId?: string|null}} snapshot
   */
  async function restoreAfterConfigFailure(snapshot) {
    const restoredProjects = Array.isArray(snapshot.projects)
      ? snapshot.projects.map((project) => cloneJson(project))
      : projectState.projects.map((project) => cloneJson(project));
    const restoredActive =
      snapshot.activeProjectId !== undefined ? snapshot.activeProjectId : projectState.activeProjectId;
    try {
      saveStoredSettings(paths, snapshot.storedPatch, {
        projects: restoredProjects,
        activeProjectId: restoredActive,
      });
    } catch (writeError) {
      logger.error(`[backend] 回滚 settings.json 失败: ${writeError?.message || writeError}`);
    }
    storedPatch = snapshot.storedPatch;
    config = snapshot.config;
    configVersion += 1;
    projectState.projects = restoredProjects;
    projectState.activeProjectId = restoredActive;
    for (const modeKey of BACKEND_MODES) {
      const entry = backends[modeKey];
      if (!entry) continue;
      try {
        entry.unsubscribe();
      } catch {
        // ignore
      }
      try {
        await entry.backend.dispose();
      } catch (disposeError) {
        logger.warn(`[backend] 回滚时释放 ${modeKey} 后端失败: ${disposeError?.message || disposeError}`);
      }
      backends[modeKey] = null;
    }
    let restored = false;
    let restoreError = null;
    try {
      const restoreMode = resolveConfiguredMode(config);
      if (restoreMode !== 'unconfigured') {
        const restoredStatus = await start();
        restored = restoredStatus.state === 'running';
      }
    } catch (restoreFailure) {
      restoreError = normalizeError(restoreFailure, 'BACKEND_START_FAILED');
      logger.warn(`[backend] 回滚后重启旧后端失败: ${restoreError.message}`);
    }
    emit('config', { config: publicConfig(config, paths), changed: true });
    emit('status', { ...getStatus(), configChanged: false }, { reason: 'rolled-back' });
    return { restored, restoreError };
  }

  // -- project API (product main flow) ---------------------------------- //

  /**
   * List projects with an `active` flag; the active project additionally gets
   * a compact runtime `status`. Never throws.
   * @returns {{projects: Array<Record<string, any>>, activeProjectId: string|null}}
   */
  function listProjects() {
    const runtime = getStatus();
    const projects = projectState.projects.map((project) => {
      const active = project.id === projectState.activeProjectId;
      /** @type {Record<string, any>} */
      const entry = { ...cloneJson(project), active };
      if (active) {
        entry.status = {
          mode: runtime.mode,
          state: runtime.state,
          baseUrl: runtime.baseUrl,
          version: runtime.version,
          error: runtime.error,
        };
      }
      return entry;
    });
    return { projects, activeProjectId: projectState.activeProjectId };
  }

  /** The active project, or null when none is selected. */
  function getActiveProject() {
    const project = findProjectById(projectState.projects, projectState.activeProjectId);
    return project ? cloneJson(project) : null;
  }

  /**
   * Create or update a project definition. Name is unique (case-insensitive);
   * local paths must exist, remote paths are format-checked only.
   * @param {Record<string, any>} input full project object; include `id` to update
   */
  function saveProject(input) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法保存项目');
    const hasId =
      isPlainObject(input) && input.id !== undefined && input.id !== null && input.id !== '';
    let previous = null;
    if (hasId) {
      const requestedId = nonEmptyString(input.id);
      if (!requestedId) {
        throw new BackendError('INVALID_ARGUMENT', '项目 id 无效', { field: 'id' });
      }
      previous = findProjectById(projectState.projects, requestedId);
      if (!previous) {
        throw new BackendError('PROJECT_NOT_FOUND', `项目不存在: ${requestedId}`, {
          field: 'id',
          id: requestedId,
        });
      }
    }
    const project = normalizeProject(input, { now, generateId: generateProjectId, previous });
    const conflict = projectState.projects.find(
      (candidate) =>
        candidate.id !== project.id && projectNameKey(candidate.name) === projectNameKey(project.name),
    );
    if (conflict) {
      throw new BackendError(
        'INVALID_ARGUMENT',
        `项目名 "${project.name}" 已存在（忽略大小写），请换一个名字`,
        { field: 'name', reason: 'PROJECT_NAME_CONFLICT', conflictingId: conflict.id },
      );
    }
    const previousProjects = projectState.projects.map((projectEntry) => cloneJson(projectEntry));
    const index = projectState.projects.findIndex((candidate) => candidate.id === project.id);
    if (index >= 0) projectState.projects[index] = project;
    else projectState.projects.push(project);
    try {
      persistSettings();
    } catch (error) {
      projectState.projects = previousProjects;
      throw error;
    }
    logger.info(`[backend] 已保存项目 "${project.name}"（${project.backend}）`);
    emit('projects', listProjects(), { reason: previous ? 'updated' : 'created' });
    return {
      project: cloneJson(project),
      projects: listProjects().projects,
      activeProjectId: projectState.activeProjectId,
    };
  }

  /**
   * Delete a project. Deleting the active project clears `activeProjectId` but
   * leaves the backend running on its last configuration (the UI shows "no
   * project selected").
   * @param {string} id
   */
  function deleteProject(id) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法删除项目');
    const projectId = nonEmptyString(id);
    if (!projectId) {
      throw new BackendError('INVALID_ARGUMENT', '项目 id 不能为空', { field: 'id' });
    }
    const index = projectState.projects.findIndex((project) => project.id === projectId);
    if (index < 0) {
      throw new BackendError('PROJECT_NOT_FOUND', `项目不存在: ${projectId}`, { id: projectId });
    }
    const previousState = projectStoreState();
    const [removed] = projectState.projects.splice(index, 1);
    const activeCleared = projectState.activeProjectId === projectId;
    if (activeCleared) projectState.activeProjectId = null;
    try {
      persistSettings();
    } catch (error) {
      projectState.projects = previousState.projects.map((project) => cloneJson(project));
      projectState.activeProjectId = previousState.activeProjectId;
      throw error;
    }
    logger.info(
      `[backend] 已删除项目 "${removed.name}"` +
        (activeCleared ? '（原为活动项目：activeProjectId 已清空，后端继续运行旧配置）' : ''),
    );
    emit('projects', listProjects(), { reason: 'deleted' });
    return {
      deleted: cloneJson(removed),
      projects: listProjects().projects,
      activeProjectId: projectState.activeProjectId,
      activeCleared,
    };
  }

  /**
   * Activate a project: re-validate it, map it onto backend config, persist and
   * restart. On any failure the previous config/settings/project selection are
   * restored (best-effort restart of the old backend) and a
   * `PROJECT_ACTIVATION_FAILED` BackendError is thrown.
   * @param {string} id
   * @returns {Promise<{project: Record<string, any>, config: Record<string, any>, status: Record<string, any>}>}
   */
  async function activateProject(id) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法激活项目');
    const projectId = nonEmptyString(id);
    if (!projectId) {
      throw new BackendError('INVALID_ARGUMENT', '项目 id 不能为空', { field: 'id' });
    }
    const project = findProjectById(projectState.projects, projectId);
    if (!project) {
      throw new BackendError('PROJECT_NOT_FOUND', `项目不存在: ${projectId}`, { id: projectId });
    }
    // Re-validate the stored definition: local paths may have disappeared since
    // the project was saved, remote paths are format-checked only.
    const validated = normalizeProject(
      { ...project, id: project.id },
      { now, generateId: () => project.id, previous: project },
    );
    const patch = projectToBackendPatch(validated);
    try {
      applyConfigPatch({ currentPatch: storedPatch, patch, explicit: configInput, env, homedir, cwd });
    } catch (error) {
      throw normalizeError(error, 'CONFIG_INVALID', `项目 "${validated.name}" 的后端配置无效`);
    }
    const snapshot = {
      storedPatch,
      config,
      configVersion,
      projects: projectState.projects.map((entry) => cloneJson(entry)),
      activeProjectId: projectState.activeProjectId,
    };
    projectState.activeProjectId = validated.id;
    try {
      configure(patch);
      const status = await restart();
      logger.info(`[backend] 已激活项目 "${validated.name}"（${validated.backend}）`);
      emit('projects', listProjects(), { reason: 'activated' });
      return { project: cloneJson(project), config: publicConfig(config, paths), status };
    } catch (rawError) {
      const cause = normalizeError(
        rawError,
        'PROJECT_ACTIVATION_FAILED',
        `激活项目 "${validated.name}" 失败`,
      );
      logger.error(`[backend] ${cause.message}；回滚设置`);
      const restore = await restoreAfterConfigFailure(snapshot);
      emit('projects', listProjects(), { reason: 'activation-rolled-back' });
      throw new BackendError(
        'PROJECT_ACTIVATION_FAILED',
        `激活项目 "${validated.name}" 失败：${cause.message}` +
          (restore.restored
            ? '；已恢复原配置并重启旧后端'
            : '；已恢复原配置，但旧后端未能自动重启，请重试 backend.start'),
        {
          cause: cause.toJSON(),
          project: cloneJson(project),
          restored: restore.restored,
          status: getStatus(),
          restoreError: restore.restoreError ? restore.restoreError.message : null,
        },
      );
    }
  }

  /**
   * Persist sparse user settings and resolve them. While a backend is running
   * the new settings only take effect on the next start/restart; the return
   * value says so via `restartRequired`.
   */
  function configure(patch) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法修改设置');
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) {
      throw new BackendError('INVALID_ARGUMENT', 'configure(patch) 需要对象参数，例如 {backend:"remote"}');
    }
    let result;
    try {
      result = applyConfigPatch({
        currentPatch: storedPatch,
        patch,
        explicit: configInput,
        env,
        homedir,
        cwd,
      });
    } catch (error) {
      throw normalizeError(error, 'CONFIG_INVALID', 'CA 后端设置无效');
    }
    persistSettings(result.patch);
    const previous = config;
    const changed = JSON.stringify(previous) !== JSON.stringify(result.config);
    storedPatch = result.patch;
    config = result.config;
    configError = null;
    if (changed) configVersion += 1;
    const running = ['running', 'starting'].includes(getStatus().state);
    logger.info(
      `[backend] 设置已保存到 ${paths.settingsPath}（changed=${changed}, backend=${config.backend}）`,
    );
    emit('config', { config: publicConfig(config, paths), changed });
    emit('status', { ...getStatus(), configChanged: false }, { reason: 'configured' });
    return {
      config: publicConfig(config, paths),
      changed,
      restartRequired: Boolean(changed && running),
    };
  }

  /** Subscribe to backend status changes; returns an unsubscribe function. */
  function onChange(listener) {
    if (typeof listener !== 'function') return () => {};
    return on('status', (status) => listener(status));
  }

  function getLogs(count = 200) {
    return logger.history(count);
  }

  async function dispose() {
    if (disposed) return;
    disposed = true;
    for (const mode of BACKEND_MODES) {
      const entry = backends[mode];
      if (!entry) continue;
      try {
        entry.unsubscribe();
      } catch {
        // ignore
      }
      try {
        await entry.backend.dispose();
      } catch (error) {
        logger.warn(`[backend] 释放 ${mode} 后端失败: ${error?.message || error}`);
      }
      backends[mode] = null;
    }
    eventListeners.clear();
    if (ownsLogger) {
      try {
        logger.close();
      } catch {
        // ignore
      }
    }
  }

  const manager = {
    ctx: ctx || null,
    paths,
    get config() {
      return publicConfig(config, paths);
    },
    getConfig() {
      return publicConfig(config, paths);
    },
    getStatus,
    start,
    stop,
    restart,
    request,
    requestStream,
    execCa,
    probeLocal,
    probeRemote,
    installRemote,
    startRemoteInstall,
    getInstallStatus,
    listDatabases,
    useDatabase,
    listProjects,
    getActiveProject,
    saveProject,
    deleteProject,
    activateProject,
    configure,
    onChange,
    on,
    getLogs,
    dispose,
    /** Diagnostics helper used by error reports/UI. */
    describeError(error) {
      return toErrorPayload(error);
    },
  };
  return manager;
}
