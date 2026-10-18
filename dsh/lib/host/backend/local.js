/**
 * Local CA backend lifecycle: spawn `ca serve`, wait for readiness, health
 * check, bounded crash restarts, graceful stop, and local CLI execution.
 *
 * Readiness (per Lead-confirmed CA behaviour):
 *   1. `ca serve` is started with `--port 0` so the OS assigns a port;
 *   2. the actual URL is parsed from the stderr line
 *      `[serve] CA backend listening on http://127.0.0.1:<port> (...)`;
 *   3. `GET /api/health` must answer `{ok:true}` using that URL.
 * If the URL line never appears (older/patched CA, stdout redirection, ...),
 * `/api/health` is polled on the configured port; when that port was 0 the
 * start is retried once with a locally chosen free port.
 *
 * The backend never owns the logger or HTTP client; they are injected by the
 * manager so status/log/stream APIs stay consistent across local and remote.
 */

import { spawn as nodeSpawn } from 'node:child_process';

import { READY_LINE_RE } from './ca-protocol.js';
import { BackendError, describeCause, normalizeError } from './errors.js';
import {
  buildCaArgv,
  healthFields,
  probeLocalEnvironment,
  resolveLocalTarget,
  resolveRuntime,
} from './discover.js';
import { createHttpClient } from './http.js';
import { buildChildEnv, findFreePort, runProcess, spawnManaged } from './process.js';
import { createAbortError, sleep, tailText } from './util.js';

export { READY_LINE_RE } from './ca-protocol.js';

const STOP_GRACE_MS = 5000;
const HEALTH_POLL_TIMEOUT_MS = 2000;
/** After this long in `running`, a crash-restart counter is considered reset. */
const STABLE_RESET_MS = 10000;

/**
 * Turn a bind host into a host the local client can connect to.
 * @param {string} host
 */
function clientHost(host) {
  const text = String(host || '127.0.0.1');
  if (text === '0.0.0.0' || text === '::' || text === '[::]') return '127.0.0.1';
  return text;
}

/** Normalize execCa's first argument to a plain argv array. */
export function normalizeCaArgs(spec) {
  if (Array.isArray(spec)) {
    if (spec.length === 0) throw new BackendError('INVALID_ARGUMENT', 'execCa 参数不能为空数组');
    return spec.map((item) => String(item));
  }
  if (spec && typeof spec === 'object' && Array.isArray(spec.args)) {
    if (spec.args.length === 0) throw new BackendError('INVALID_ARGUMENT', 'execCa 参数不能为空数组');
    return spec.args.map((item) => String(item));
  }
  throw new BackendError(
    'INVALID_ARGUMENT',
    'execCa 第一个参数必须是 CA CLI 参数数组（string[]），例如 ["index","--repo","/repo","--db","/db"]',
    { received: typeof spec },
  );
}

function safeCallback(callback, line, info) {
  if (typeof callback !== 'function') return;
  try {
    callback(line, info);
  } catch {
    // consumer callbacks must not break the process lifecycle
  }
}

function logsTail(logger, lines = 12) {
  try {
    const history = logger?.history ? logger.history(lines) : [];
    return tailText(
      history
        .map((entry) => entry.line)
        .filter(Boolean)
        .join('\n'),
      4000,
    );
  } catch {
    return '';
  }
}

/**
 * @param {object} options
 * @param {Record<string, any>} options.config resolved full config
 * @param {{stateDir: string, logDir: string, settingsPath: string}} [options.paths]
 * @param {any} [options.logger] bounded logger from ./log.js
 * @param {object} [options.deps] injectable spawn/fetch/clock/discovery hooks
 */
export function createLocalBackend(options) {
  const config = options.config;
  const settings = config.local;
  const logger = options.logger || null;
  const deps = options.deps || {};
  const now = typeof deps.now === 'function' ? deps.now : Date.now;
  const spawn = typeof deps.spawn === 'function' ? deps.spawn : nodeSpawn;
  const env = deps.env || process.env;
  const cwd = deps.cwd || process.cwd();
  const homedir = deps.homedir;
  const resolveTarget =
    typeof deps.resolveLocalTarget === 'function'
      ? deps.resolveLocalTarget
      : (targetOptions) => resolveLocalTarget(targetOptions);
  const resolveRuntimeFn =
    typeof deps.resolveRuntime === 'function'
      ? deps.resolveRuntime
      : (runtimeOptions) => resolveRuntime(runtimeOptions);
  const runProcessFn = typeof deps.runProcess === 'function' ? deps.runProcess : runProcess;
  const freePort = typeof deps.findFreePort === 'function' ? deps.findFreePort : findFreePort;
  const http = createHttpClient({
    fetchImpl: typeof deps.fetch === 'function' ? deps.fetch : globalThis.fetch,
    defaultTimeoutMs: config.requestTimeoutMs,
    connectTimeoutMs: config.requestTimeoutMs,
  });

  /** @type {'stopped'|'starting'|'running'|'error'} */
  let state = 'stopped';
  let baseUrl = null;
  /** Configured target shown even before the first start (diagnostics/UI). */
  let db = settings.db || settings.dbDir || null;
  let error = null;
  let since = now();
  let healthData = null;
  let restarts = 0;

  /** @type {ReturnType<typeof spawnManaged>|null} */
  let serveHandle = null;
  /** @type {Promise<any>|null} */
  let startPromise = null;
  /** @type {Promise<any>|null} */
  let stopPromise = null;
  /** @type {AbortController|null} */
  let startAbort = null;
  /** @type {ReturnType<typeof setTimeout>|null} */
  let restartTimer = null;
  /** @type {ReturnType<typeof setTimeout>|null} */
  let stableTimer = null;
  let intentionalStop = false;
  let disposed = false;
  let generation = 0;
  /** @type {Set<(status: any, meta: {reason: string}) => void>} */
  const listeners = new Set();

  function getStatus() {
    const fields = healthFields(healthData);
    return {
      mode: 'local',
      state,
      baseUrl,
      db,
      error,
      since,
      version: fields.version,
      features: fields.features,
      health: fields.database ? { database: fields.database, server: fields.server, status: fields.status } : null,
      restarts,
      workers: 1,
      logFile: logger?.filePath || null,
    };
  }

  function notify(reason) {
    const status = getStatus();
    for (const listener of listeners) {
      try {
        listener(status, { reason });
      } catch {
        // listener errors never affect lifecycle
      }
    }
  }

  function subscribe(listener) {
    if (typeof listener !== 'function') return () => {};
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function setStatus(patch, reason) {
    if (patch.state !== undefined) {
      if (patch.state !== state) {
        state = patch.state;
        since = now();
      }
    }
    if ('baseUrl' in patch) baseUrl = patch.baseUrl;
    if ('db' in patch) db = patch.db;
    if ('error' in patch) error = patch.error;
    if ('health' in patch) healthData = patch.health;
    notify(reason);
  }

  function clearRestartTimer() {
    if (restartTimer) {
      clearTimeout(restartTimer);
      restartTimer = null;
    }
  }

  function clearStableTimer() {
    if (stableTimer) {
      clearTimeout(stableTimer);
      stableTimer = null;
    }
  }

  function armStableTimer(gen) {
    clearStableTimer();
    stableTimer = setTimeout(() => {
      stableTimer = null;
      if (gen === generation && state === 'running') restarts = 0;
    }, STABLE_RESET_MS);
    if (typeof stableTimer.unref === 'function') stableTimer.unref();
  }

  function buildServeArgs(target, port) {
    const args = ['serve', '--host', settings.host || '127.0.0.1', '--port', String(port)];
    if (target.db) args.push('--db', target.db);
    else if (target.dbDir) args.push('--db-dir', target.dbDir);
    if (target.stateDb) args.push('--state-db', target.stateDb);
    if (settings.repo) args.push('--repo', settings.repo);
    if (settings.compdb) args.push('--compdb', settings.compdb);
    return args;
  }

  /**
   * Spawn `ca serve` and return the managed handle plus a mutable readiness
   * record the stderr parser writes into.
   */
  function spawnServe(target, port) {
    const argv = buildCaArgv(target, buildServeArgs(target, port));
    const childEnv = buildChildEnv(settings.env, env);
    /** @type {{sawReadyLine: boolean, readyUrl: string|null}} */
    const ready = { sawReadyLine: false, readyUrl: null };
    logger?.info?.(`[local] 启动 ${argv.command} ${argv.args.join(' ')}`);
    const handle = spawnManaged({
      spawn,
      command: argv.command,
      args: argv.args,
      cwd: target.cwd,
      env: childEnv,
      logger,
      onStderrLine(line) {
        const match = READY_LINE_RE.exec(line);
        if (match) {
          ready.sawReadyLine = true;
          ready.readyUrl = match[1];
          logger?.info?.(`[local] ca serve 报告就绪: ${match[1]}`);
        }
      },
    });
    return { argv, handle, ready };
  }

  function serveExitedError(handle, target) {
    return Promise.resolve(handle.exited).then((info) => {
      const detail = logsTail(logger, 10);
      return new BackendError(
        'SERVE_EXITED',
        `ca serve 在就绪前退出（exitCode=${info?.code ?? 'null'}, signal=${info?.signal ?? 'null'}）` +
          `${detail ? `；日志末尾: ${detail}` : ''}`,
        {
          exitCode: info?.code ?? null,
          signal: info?.signal ?? null,
          db: target?.db || target?.dbDir || null,
          spawnError: info?.spawnError ? describeCause(info.spawnError) : null,
          logs: detail,
        },
      );
    });
  }

  function cancelledError() {
    return new BackendError('CANCELLED', 'CA 后端启动已取消');
  }

  /**
   * Poll `/api/health` until the serve process answers, the process exits, the
   * deadline passes, or the start is aborted.
   */
  async function waitForReady(handle, ready, gen, signal, target) {
    const deadline = now() + config.readyTimeoutMs;
    const candidates = [];
    if (ready.readyUrl) candidates.push(ready.readyUrl);
    if (settings.port > 0) {
      candidates.push(`http://${clientHost(settings.host)}:${settings.port}`);
    }
    let lastError = null;
    let lastAttemptAt = 0;
    for (;;) {
      if (signal?.aborted) throw createAbortError();
      if (gen !== generation) throw cancelledError();
      if (handle.settled) throw await serveExitedError(handle, target);

      // The readiness line may arrive between polls; re-evaluate each loop.
      if (ready.readyUrl && !candidates.includes(ready.readyUrl)) candidates.unshift(ready.readyUrl);

      const remaining = deadline - now();
      if (remaining <= 0) {
        throw new BackendError(
          'HEALTH_TIMEOUT',
          `ca serve 在 ${config.readyTimeoutMs}ms 内未就绪（/api/health 无响应）` +
            `${lastError ? `；最后一次错误: ${lastError.message}` : ''}；请查看日志或手动运行 ca serve 排查`,
          {
            readyTimeoutMs: config.readyTimeoutMs,
            sawReadyLine: ready.sawReadyLine,
            readyUrl: ready.readyUrl,
            logs: logsTail(logger, 12),
            lastError: lastError ? { code: lastError.code, message: lastError.message } : null,
          },
        );
      }

      if (candidates.length > 0 && now() - lastAttemptAt >= Math.min(config.healthIntervalMs, 250)) {
        lastAttemptAt = now();
        for (const candidate of candidates) {
          try {
            const envelope = await http.request(candidate, '/api/health', {
              timeoutMs: Math.min(HEALTH_POLL_TIMEOUT_MS, Math.max(250, deadline - now())),
              signal,
            });
            if (envelope.ok && envelope.data) {
              return { baseUrl: candidate, data: envelope.data };
            }
            lastError = new BackendError(
              'HEALTH_FAILED',
              `健康检查返回错误: ${envelope.error?.code || 'UNKNOWN'} ${envelope.error?.message || ''}`.trim(),
            );
          } catch (requestError) {
            const normalized = normalizeError(requestError, 'HEALTH_FAILED');
            if (normalized.code === 'CANCELLED') throw normalized;
            lastError = normalized;
          }
        }
      }

      const sleepMs = Math.max(25, Math.min(config.healthIntervalMs, deadline - now()));
      try {
        await sleep(sleepMs, signal);
      } catch (abortError) {
        if (signal?.aborted) throw createAbortError();
        throw abortError;
      }
    }
  }

  /**
   * Register the crash watcher for a serve process that reached `running`.
   * Stale generations (stop/restart/dispose) are ignored.
   */
  function attachExitWatcher(gen, handle) {
    handle.exited.then((info) => {
      if (gen !== generation || disposed || intentionalStop) return;
      if (serveHandle === handle) serveHandle = null;
      const detail = logsTail(logger, 8);
      const message =
        `ca serve 意外退出（exitCode=${info?.code ?? 'null'}, signal=${info?.signal ?? 'null'}）` +
        `${detail ? `；日志末尾: ${detail}` : ''}`;
      logger?.error?.(`[local] ${message}`);
      healthData = null;
      setStatus({ state: 'error', baseUrl: null, error: message, health: null }, 'crashed');
      scheduleRestart();
    });
  }

  function scheduleRestart() {
    if (disposed || intentionalStop) return;
    if (restarts >= settings.maxRestarts) {
      logger?.warn?.(
        `[local] ca serve 连续崩溃 ${restarts} 次，已达自动重启上限 ${settings.maxRestarts}；请调用 backend.restart 手动重试`,
      );
      return;
    }
    restarts += 1;
    const delay = Math.max(0, settings.restartBackoffMs) * restarts;
    logger?.warn?.(`[local] ${delay}ms 后尝试第 ${restarts}/${settings.maxRestarts} 次自动重启`);
    clearRestartTimer();
    restartTimer = setTimeout(() => {
      restartTimer = null;
      if (disposed || intentionalStop) return;
      start({ reason: 'restart' }).catch((restartError) => {
        logger?.error?.(`[local] 自动重启失败: ${restartError?.message || String(restartError)}`);
        if (!disposed && !intentionalStop) scheduleRestart();
      });
    }, delay);
    if (typeof restartTimer.unref === 'function') restartTimer.unref();
  }

  async function doStart(reason, externalSignal) {
    generation += 1;
    const gen = generation;
    intentionalStop = false;
    const abort = new AbortController();
    startAbort = abort;
    let detachExternal = () => {};
    if (externalSignal) {
      if (externalSignal.aborted) abort.abort();
      else if (typeof externalSignal.addEventListener === 'function') {
        const onAbort = () => abort.abort();
        externalSignal.addEventListener('abort', onAbort, { once: true });
        detachExternal = () => externalSignal.removeEventListener?.('abort', onAbort);
      }
    }
    setStatus({ state: 'starting', baseUrl: null, error: null, health: null }, 'starting');

    try {
      const target = await resolveTarget({ config, cwd, env, homedir });
      if (abort.signal.aborted) throw cancelledError();
      db = target.db || target.dbDir || null;
      for (const warning of target.warnings || []) logger?.warn?.(`[local] ${warning}`);
      if (!target.db && !target.dbDir) {
        throw new BackendError(
          'BACKEND_NOT_CONFIGURED',
          '本地 CA 后端缺少数据库配置（local.db / local.dbDir）',
        );
      }

      let attempt = 0;
      let port = settings.port;
      for (;;) {
        if (abort.signal.aborted) throw cancelledError();
        const { handle, ready, argv } = spawnServe(target, port);
        serveHandle = handle;
        try {
          const readyInfo = await waitForReady(handle, ready, gen, abort.signal, target);
          if (gen !== generation) {
            handle.kill('SIGTERM');
            throw cancelledError();
          }
          setStatus(
            {
              state: 'running',
              baseUrl: readyInfo.baseUrl,
              db: target.db || target.dbDir,
              error: null,
              health: readyInfo.data,
            },
            reason === 'restart' ? 'restarted' : 'started',
          );
          if (reason !== 'restart') restarts = 0;
          logger?.info?.(
            `[local] ca serve 就绪 ${readyInfo.baseUrl} (db=${target.db || target.dbDir}; ` +
              `port=${port === 0 ? 'auto' : port}; cmd=${argv.command})`,
          );
          armStableTimer(gen);
          attachExitWatcher(gen, handle);
          return getStatus();
        } catch (startError) {
          const normalized = normalizeError(startError, 'SERVE_FAILED');
          const timeout = normalized.code === 'HEALTH_TIMEOUT';
          const canFallback =
            attempt === 0 &&
            settings.port === 0 &&
            timeout &&
            !ready.sawReadyLine &&
            !abort.signal.aborted &&
            gen === generation;
          handle.kill('SIGTERM');
          await handle.terminate(1000).catch(() => {});
          if (serveHandle === handle) serveHandle = null;
          if (canFallback) {
            attempt += 1;
            try {
              port = await freePort({ host: clientHost(settings.host) });
              logger?.warn?.(
                `[local] 未从 stderr 解析到就绪 URL，回退到自选端口 ${port} 重新启动 ca serve`,
              );
              continue;
            } catch (portError) {
              throw normalizeError(portError, 'PORT_UNAVAILABLE', '自动选择空闲端口失败');
            }
          }
          throw normalized;
        }
      }
    } catch (startError) {
      const normalized = normalizeError(startError, 'SERVE_FAILED');
      if (gen === generation && state !== 'running') {
        const cancelled = normalized.code === 'CANCELLED' || abort.signal.aborted;
        if (cancelled) {
          setStatus({ state: 'stopped', baseUrl: null, error: null, health: null }, 'cancelled');
        } else {
          setStatus({ state: 'error', baseUrl: null, error: normalized.message, health: null }, 'failed');
        }
      }
      throw normalized;
    } finally {
      detachExternal();
      if (startAbort === abort) startAbort = null;
    }
  }

  async function start(startOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法启动');
    if (stopPromise) await stopPromise.catch(() => {});
    if (state === 'running' && baseUrl) return getStatus();
    if (startPromise) return startPromise;
    clearRestartTimer();
    startPromise = doStart(startOptions.reason || 'manual', startOptions.signal).finally(() => {
      startPromise = null;
    });
    return startPromise;
  }

  async function stop(stopOptions = {}) {
    if (stopPromise) return stopPromise;
    const timeoutMs = Number(stopOptions.timeoutMs) > 0 ? Number(stopOptions.timeoutMs) : STOP_GRACE_MS;
    intentionalStop = true;
    generation += 1;
    clearRestartTimer();
    clearStableTimer();
    startAbort?.abort();

    stopPromise = (async () => {
      const handle = serveHandle;
      serveHandle = null;
      if (handle && !handle.settled) {
        logger?.info?.('[local] 正在停止 ca serve（SIGTERM）');
        const info = await handle.terminate(timeoutMs);
        logger?.info?.(
          `[local] ca serve 已退出（exitCode=${info?.code ?? 'null'}, signal=${info?.signal ?? 'null'}）`,
        );
      }
      const inFlight = startPromise;
      if (inFlight) await inFlight.catch(() => {});
      await Promise.resolve();
      if (state !== 'stopped') {
        setStatus({ state: 'stopped', baseUrl: null, error: null, health: null }, 'stopped');
      }
      return getStatus();
    })().finally(() => {
      stopPromise = null;
    });
    return stopPromise;
  }

  async function restart(restartOptions = {}) {
    await stop({ reason: 'restart' });
    return start({ signal: restartOptions.signal, reason: 'restart' });
  }

  /**
   * Execute a CA CLI command locally (index/update/ensure-deep/...).
   * Non-zero exits are reported in the result; only spawn failures throw.
   */
  async function execCa(spec, execOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 后端管理器已释放，无法执行命令');
    const args = normalizeCaArgs(spec);
    const runtime = resolveRuntimeFn({ config, cwd, env, homedir });
    const argv = buildCaArgv(runtime, args);
    const childEnv = buildChildEnv(settings.env, env);
    logger?.info?.(`[local] 执行 ${argv.command} ${argv.args.join(' ')}`);

    const handle = spawnManaged({
      spawn,
      command: argv.command,
      args: argv.args,
      cwd: runtime.repoRoot || cwd,
      env: childEnv,
      logger,
      onStdoutLine: (line, info) => safeCallback(execOptions.onStdout, line, info),
      onStderrLine: (line, info) => safeCallback(execOptions.onStderr, line, info),
    });

    let cancelled = false;
    let timedOut = false;
    let forceTimer = null;
    let timeoutTimer = null;
    const kill = () => {
      handle.kill('SIGTERM');
      if (!forceTimer) {
        forceTimer = setTimeout(() => {
          forceTimer = null;
          if (!handle.settled) handle.kill('SIGKILL');
        }, STOP_GRACE_MS);
      }
    };
    const onAbort = () => {
      cancelled = true;
      kill();
    };
    const externalSignal = execOptions.signal;
    if (externalSignal) {
      if (externalSignal.aborted) onAbort();
      else if (typeof externalSignal.addEventListener === 'function') {
        externalSignal.addEventListener('abort', onAbort, { once: true });
      }
    }
    const timeoutMs = Math.max(0, Number(execOptions.timeoutMs) || 0);
    if (timeoutMs > 0) {
      timeoutTimer = setTimeout(() => {
        timedOut = true;
        kill();
      }, timeoutMs);
    }

    try {
      const info = await handle.exited;
      if (info.spawnError) {
        throw new BackendError(
          'SPAWN_FAILED',
          `无法启动 CA CLI（${argv.command}）：${describeCause(info.spawnError)}；请检查 local.python / local.repoRoot，或先运行 scripts/bootstrap.sh 安装依赖`,
          { command: argv.command, args: argv.args, cause: describeCause(info.spawnError) },
        );
      }
      if (info.code === null && info.signal === null && !cancelled) {
        throw new BackendError(
          'EXEC_FAILED',
          `CA CLI 未能完成执行（${argv.command}）；请查看日志`,
          { command: argv.command, args: argv.args },
        );
      }
      logger?.info?.(
        `[local] CA CLI 结束 exitCode=${info.code ?? 'null'} signal=${info.signal ?? 'null'}` +
          `${cancelled ? '（已取消）' : ''}${timedOut ? '（超时）' : ''}`,
      );
      return {
        exitCode: info.code,
        signal: info.signal,
        cancelled,
        timedOut,
        command: argv.command,
        args: argv.args,
      };
    } finally {
      clearTimeout(timeoutTimer);
      clearTimeout(forceTimer);
      if (externalSignal && typeof externalSignal.removeEventListener === 'function') {
        externalSignal.removeEventListener('abort', onAbort);
      }
    }
  }

  async function probe() {
    return probeLocalEnvironment({
      config,
      cwd,
      homedir,
      env,
      paths: options.paths,
      execCa: (argv, probeOptions) =>
        runProcessFn({
          spawn,
          command: argv.command,
          args: argv.args,
          cwd,
          env: buildChildEnv(settings.env, env),
          timeoutMs: probeOptions?.timeoutMs || 5000,
          logger,
          maxOutputBytes: 64 * 1024,
        }),
    });
  }

  async function dispose() {
    if (disposed) return;
    disposed = true;
    intentionalStop = true;
    generation += 1;
    clearRestartTimer();
    clearStableTimer();
    startAbort?.abort();
    const handle = serveHandle;
    serveHandle = null;
    if (handle && !handle.settled) {
      try {
        const info = await handle.terminate(2000);
        logger?.info?.(`[local] dispose 已停止 ca serve（signal=${info?.signal ?? 'null'}）`);
      } catch {
        // best effort
      }
    }
    try {
      await startPromise;
    } catch {
      // start failure during dispose is expected
    }
    try {
      await stopPromise;
    } catch {
      // stop failure during dispose is expected
    }
    listeners.clear();
    state = 'stopped';
    baseUrl = null;
    healthData = null;
  }

  return {
    getStatus,
    start,
    stop,
    restart,
    execCa,
    probe,
    subscribe,
    dispose,
    /** Exposed for tests: the argv builder without spawning. */
    buildServeArgs: (target, port) => buildServeArgs(target, port === undefined ? settings.port : port),
    /** Resolved DB target used by the current/last start (for diagnostics). */
    getTarget: () => db,
  };
}
