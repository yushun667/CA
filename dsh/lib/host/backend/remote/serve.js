/**
 * Remote CA serve lifecycle over SSH:
 *
 *   ssh <target> 'cd <repo> && echo $$ > .dsh-ca/serve.pid && exec python -m ca serve ...'
 *   ssh -N -L <localPort>:127.0.0.1:<remotePort> <target>
 *   GET http://127.0.0.1:<localPort>/api/health   (through the tunnel)
 *
 * The server process lives on the Linux host; the local ssh process is only a
 * transport. Stopping goes through the remote PID file, because killing the
 * local ssh client does not reliably terminate the remote process. A bounded
 * crash-restart loop restarts both the remote server and the tunnel, cleaning
 * up a possibly stale remote server first.
 */

import { spawn as nodeSpawn } from 'node:child_process';

import { parseReadyLine } from '../ca-protocol.js';
import { BackendError, describeCause, normalizeError } from '../errors.js';
import { healthFields } from '../discover.js';
import { createHttpClient } from '../http.js';
import { buildChildEnv, findFreePort, spawnManaged } from '../process.js';
import { createAbortError, sleep, tailText } from '../util.js';
import { normalizeCaArgs } from '../local.js';
import {
  buildRemoteCaScript,
  buildRemoteServeScript,
  buildRemoteStopScript,
  buildSshArgv,
  buildSshTunnelArgv,
  normalizeSshConfig,
  remotePathExpression,
  shellQuote,
} from './commands.js';

const STOP_GRACE_MS = 8000;
const HEALTH_POLL_TIMEOUT_MS = 2000;
const STABLE_RESET_MS = 10000;

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function logsTail(logger, lines = 12) {
  try {
    return tailText(
      (logger?.history ? logger.history(lines) : [])
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
 * @param {any} [options.logger]
 * @param {object} [options.deps]
 * @param {ReturnType<import('./exec.js').createRemoteExecutor>} options.executor
 */
export function createRemoteServeController(options) {
  const config = options.config;
  const remote = config.remote || {};
  const logger = options.logger || null;
  const deps = options.deps || {};
  const executor = options.executor;
  const now = typeof deps.now === 'function' ? deps.now : Date.now;
  const spawn = typeof deps.spawn === 'function' ? deps.spawn : nodeSpawn;
  const env = deps.env || process.env;
  const freePort = typeof deps.findFreePort === 'function' ? deps.findFreePort : findFreePort;
  const http = createHttpClient({
    fetchImpl: typeof deps.fetch === 'function' ? deps.fetch : globalThis.fetch,
    defaultTimeoutMs: config.requestTimeoutMs,
    connectTimeoutMs: config.requestTimeoutMs,
  });

  let state = 'stopped';
  let baseUrl = null;
  let db = remote.db || remote.dbDir || null;
  let error = null;
  let since = now();
  let healthData = null;
  let restarts = 0;
  let remoteInfo = null;

  /** @type {{handle: any, ready: {url: string|null, port: number|null}}|null} */
  let serve = null;
  /** @type {any|null} */
  let tunnel = null;
  let startPromise = null;
  let stopPromise = null;
  let startAbort = null;
  let restartTimer = null;
  let stableTimer = null;
  let intentionalStop = false;
  let disposed = false;
  let generation = 0;
  let taskCounter = 0;
  /** True while a remote `ca serve` process may still exist (best-effort cleanup). */
  let remoteMayBeRunning = false;
  /** Per-instance remote PID-file stem + repo path, so cleanup targets our own server. */
  /** @type {{token: string, repoPath: string, ssh: any}|null} */
  let activeRemotePid = null;
  let pidTokenCounter = 0;

  function nextPidToken() {
    pidTokenCounter += 1;
    return `serve-${Date.now().toString(36)}-${process.pid.toString(36)}-${pidTokenCounter.toString(36)}`;
  }
  /** @type {Set<(status: any, meta: {reason: string}) => void>} */
  const listeners = new Set();

  function getStatus() {
    const fields = healthFields(healthData);
    return {
      mode: 'remote',
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
      remote: remoteInfo ? clone(remoteInfo) : null,
    };
  }

  function notify(reason) {
    const status = getStatus();
    for (const listener of listeners) {
      try {
        listener(status, { reason });
      } catch {
        // ignore listener errors
      }
    }
  }

  function subscribe(listener) {
    if (typeof listener !== 'function') return () => {};
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function setStatus(patch, reason) {
    if (patch.state !== undefined && patch.state !== state) {
      state = patch.state;
      since = now();
    }
    if ('baseUrl' in patch) baseUrl = patch.baseUrl;
    if ('db' in patch) db = patch.db;
    if ('error' in patch) error = patch.error;
    if ('health' in patch) healthData = patch.health;
    if ('remoteInfo' in patch) remoteInfo = patch.remoteInfo;
    notify(reason);
  }

  function requireSsh() {
    return normalizeSshConfig(remote.ssh);
  }

  function requireRepoPath() {
    const repoPath = remote.repoPath || remote.installDir;
    if (!repoPath) {
      throw new BackendError(
        'BACKEND_NOT_CONFIGURED',
        '远程 CA 检出路径未配置：请设置 remote.repoPath（已有检出）或 remote.installDir（安装目录）',
        { field: 'remote.repoPath' },
      );
    }
    return repoPath;
  }

  function requireDatabase() {
    if (remote.db) return { db: remote.db, dbDir: null };
    if (remote.dbDir) return { db: null, dbDir: remote.dbDir };
    throw new BackendError(
      'BACKEND_NOT_CONFIGURED',
      '远程 CA 后端未配置数据库：请设置 remote.db（远端单库）或 remote.dbDir（远端分片目录），或先在远端运行 ca index。',
      { field: 'remote.db' },
    );
  }

  function pythonPath(repoPath) {
    return remote.python || `${repoPath}/.venv/bin/python`;
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

  /** Resolve an optional password-auth session for a long-lived ssh process. */
  async function resolveSpawnSession(ssh) {
    if (typeof deps.resolveSshAuth !== 'function') return null;
    return deps.resolveSshAuth(ssh);
  }

  async function spawnSshProcess(ssh, script, label) {
    const session = await resolveSpawnSession(ssh);
    const built = buildSshArgv(session ? session.ssh : ssh, script, {
      sshBinary: deps.sshBinary,
      batchMode: session ? session.batchMode : true,
      extraOptions: session?.extraOptions || [],
    });
    logger?.info?.(`[remote] ${label}: ssh ${built.ssh.target}（${session?.method || 'agent/key'}）`);
    const ready = { url: null, port: null };
    const handle = spawnManaged({
      spawn,
      command: built.command,
      args: built.args,
      env: buildChildEnv(session?.env || {}, env),
      logger,
      onStderrLine(line) {
        const parsed = parseReadyLine(line);
        if (parsed) {
          ready.url = parsed.url;
          ready.port = parsed.port;
          logger?.info?.(`[remote] ca serve 报告就绪: ${parsed.url}`);
        }
      },
    });
    // The askpass helper is only needed during authentication; keep it until
    // the ssh process exits so retries inside ssh still work.
    handle.exited.finally(() => {
      try {
        session?.cleanup?.();
      } catch {
        // ignore
      }
    });
    return { built, handle, ready };
  }

  async function spawnTunnelProcess(ssh, localPort, remotePort) {
    const session = await resolveSpawnSession(ssh);
    const built = buildSshTunnelArgv(session ? session.ssh : ssh, {
      localPort,
      remotePort,
      sshBinary: deps.sshBinary,
      batchMode: session ? session.batchMode : true,
      extraOptions: session?.extraOptions || [],
    });
    logger?.info?.(`[remote] 建立 SSH 隧道 127.0.0.1:${localPort} → ${ssh.target}:127.0.0.1:${remotePort}`);
    const handle = spawnManaged({
      spawn,
      command: built.command,
      args: built.args,
      env: buildChildEnv(session?.env || {}, env),
      logger,
    });
    handle.exited.finally(() => {
      try {
        session?.cleanup?.();
      } catch {
        // ignore
      }
    });
    return { built, handle };
  }

  function serveExitedError(handle) {
    return Promise.resolve(handle.exited).then((info) => {
      const detail = logsTail(logger, 10);
      return new BackendError(
        'SERVE_EXITED',
        `远程 ca serve 在就绪前退出（exitCode=${info?.code ?? 'null'}, signal=${info?.signal ?? 'null'}）` +
          `${detail ? `；日志末尾: ${detail}` : ''}`,
        {
          exitCode: info?.code ?? null,
          signal: info?.signal ?? null,
          sshHost: remote.ssh?.host || null,
          repoPath: remote.repoPath || remote.installDir || null,
          logs: detail,
          spawnError: info?.spawnError ? describeCause(info.spawnError) : null,
        },
      );
    });
  }

  function needsStaleCleanup(ssh, repoPath, pidFile = 'serve') {
    // Best effort: remove a server left behind by a crashed local ssh client.
    return executor
      .runSsh(ssh, buildRemoteStopScript({ repoPath, pidFile }), {
        timeoutMs: 15000,
        label: 'cleanup stale remote ca serve',
      })
      .then((result) => {
        if (result && result.code === 0) remoteMayBeRunning = false;
        return result;
      })
      .catch((cleanupError) => {
        logger?.warn?.(`[remote] 清理旧 serve 进程失败（忽略）: ${cleanupError?.message || cleanupError}`);
      });
  }

  /** Best-effort remote cleanup from paths stored in the current config. */
  async function cleanupRemoteBestEffort(reason) {
    if (!remoteMayBeRunning) return;
    const target = activeRemotePid;
    if (!target) return;
    try {
      logger?.info?.(`[remote] 清理可能残留的远端 ca serve（${reason}）`);
      await needsStaleCleanup(target.ssh, target.repoPath, target.token);
    } catch (cleanupError) {
      logger?.warn?.(`[remote] 远端清理失败（忽略）: ${cleanupError?.message || cleanupError}`);
    }
  }

  async function pickRemoteFreePort(ssh, repoPath) {
    const python = pythonPath(repoPath);
    const script = [
      'set -e',
      `${remotePathExpression(python)} -c ${shellQuote(
        'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()',
      )}`,
    ].join('\n');
    const result = await executor.runSsh(ssh, script, { timeoutMs: 15000, label: 'pick remote free port' });
    if (result.code !== 0 || result.spawnError) {
      throw new BackendError(
        'PORT_UNAVAILABLE',
        `无法在远端选择空闲端口：${tailText(`${result.stderr || ''}\n${result.stdout || ''}`, 400) || '无输出'}`,
        { candidate: python },
      );
    }
    const port = Number(String(result.stdout || '').trim().split(/\s+/).pop());
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new BackendError('PORT_UNAVAILABLE', `远端未返回有效端口: ${result.stdout || '(空)'}`);
    }
    return port;
  }

  async function waitForLocalHealth(localPort, deadline, gen, signal, handles) {
    let lastError = null;
    const url = `http://127.0.0.1:${localPort}`;
    for (;;) {
      if (signal?.aborted) throw createAbortError();
      if (gen !== generation) throw new BackendError('CANCELLED', '远程后端启动已取消');
      for (const handle of handles) {
        if (handle.settled) {
          throw new BackendError(
            'SERVE_EXITED',
            `SSH 隧道/远端 serve 进程在就绪前退出（${handle.command}）；请检查 SSH 隧道与远端 ca serve 日志`,
            { logs: logsTail(logger, 12) },
          );
        }
      }
      const remaining = deadline - now();
      if (remaining <= 0) {
        throw new BackendError(
          'HEALTH_TIMEOUT',
          `远程 CA 后端在 ${remote.readyTimeoutMs}ms 内未就绪（隧道 127.0.0.1:${localPort} 的 /api/health 无响应）` +
            `${lastError ? `；最后一次错误: ${lastError.message}` : ''}`,
          {
            readyTimeoutMs: remote.readyTimeoutMs,
            localPort,
            logs: logsTail(logger, 12),
            lastError: lastError ? { code: lastError.code, message: lastError.message } : null,
          },
        );
      }
      try {
        const envelope = await http.request(url, '/api/health', {
          timeoutMs: Math.min(HEALTH_POLL_TIMEOUT_MS, Math.max(250, remaining)),
          signal,
        });
        if (envelope.ok && envelope.data) return { baseUrl: url, data: envelope.data };
        lastError = new BackendError(
          'HEALTH_FAILED',
          `健康检查返回错误: ${envelope.error?.code || 'UNKNOWN'} ${envelope.error?.message || ''}`.trim(),
        );
      } catch (requestError) {
        const normalized = normalizeError(requestError, 'HEALTH_FAILED');
        if (normalized.code === 'CANCELLED') throw normalized;
        lastError = normalized;
      }
      const sleepMs = Math.max(25, Math.min(config.healthIntervalMs, deadline - now()));
      await sleep(sleepMs, signal);
    }
  }

  async function waitForServePort(handleState, gen, signal, fixedPort, deadline) {
    if (fixedPort > 0) return fixedPort;
    for (;;) {
      if (signal?.aborted) throw createAbortError();
      if (gen !== generation) throw new BackendError('CANCELLED', '远程后端启动已取消');
      if (handleState.ready.port) return handleState.ready.port;
      if (handleState.handle.settled) throw await serveExitedError(handleState.handle);
      if (now() >= deadline) {
        throw new BackendError(
          'HEALTH_TIMEOUT',
          `远程 ca serve 在 ${remote.readyTimeoutMs}ms 内没有报告就绪 URL（无法确定远端端口，无法建立隧道）`,
          {
            readyTimeoutMs: remote.readyTimeoutMs,
            sawReadyLine: false,
            logs: logsTail(logger, 12),
          },
        );
      }
      await sleep(Math.max(25, Math.min(config.healthIntervalMs, deadline - now())), signal);
    }
  }

  function attachCrashWatcher(gen, serveState, tunnelHandle) {
    const onExit = (kind) => (info) => {
      if (gen !== generation || disposed || intentionalStop) return;
      const detail = logsTail(logger, 8);
      const message =
        `${kind} 意外退出（exitCode=${info?.code ?? 'null'}, signal=${info?.signal ?? 'null'}）` +
        `${detail ? `；日志末尾: ${detail}` : ''}`;
      logger?.error?.(`[remote] ${message}`);
      healthData = null;
      setStatus({ state: 'error', baseUrl: null, error: message, health: null }, 'crashed');
      // The remote server may outlive the ssh transport; best-effort cleanup
      // (the restart below re-cleans anyway before spawning a replacement).
      cleanupRemoteBestEffort('ssh 进程意外退出').catch(() => {});
      scheduleRestart();
    };
    serveState.handle.exited.then(onExit('远程 ca serve'));
    if (tunnelHandle) tunnelHandle.exited.then(onExit('SSH 隧道'));
  }

  function scheduleRestart() {
    if (disposed || intentionalStop) return;
    const maxRestarts = Number(remote.maxRestarts) || 0;
    if (restarts >= maxRestarts) {
      logger?.warn?.(
        `[remote] 连续失败 ${restarts} 次，已达自动重启上限 ${maxRestarts}；请手动 backend.restart`,
      );
      return;
    }
    restarts += 1;
    const delay = Math.max(0, Number(remote.restartBackoffMs) || 0) * restarts;
    logger?.warn?.(`[remote] ${delay}ms 后尝试第 ${restarts}/${maxRestarts} 次自动重启`);
    clearRestartTimer();
    restartTimer = setTimeout(() => {
      restartTimer = null;
      if (disposed || intentionalStop) return;
      start({ reason: 'restart' }).catch((restartError) => {
        logger?.error?.(`[remote] 自动重启失败: ${restartError?.message || restartError}`);
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
      const ssh = requireSsh();
      const repoPath = requireRepoPath();
      const database = requireDatabase();
      db = database.db || database.dbDir || null;
      const python = pythonPath(repoPath);
      const configuredPort = Number(remote.serveRemotePort) || 0;

      let attempt = 0;
      let remotePort = configuredPort;
      for (;;) {
        if (abort.signal.aborted) throw createAbortError();
        // Fresh deadline per attempt so the fallback port retry gets its own
        // readiness window instead of inheriting an already-expired one.
        const deadline = now() + Number(remote.readyTimeoutMs || config.readyTimeoutMs);
        remoteMayBeRunning = true;
        const pidToken = nextPidToken();
        const pidScript = buildRemoteServeScript({
          repoPath,
          python,
          db: database.db,
          dbDir: database.dbDir,
          stateDb: remote.stateDb || null,
          repo: remote.repo || null,
          compdb: remote.compdb || null,
          host: '127.0.0.1',
          port: remotePort,
          env: remote.env,
          pidFile: pidToken,
        });
        const serveState = await spawnSshProcess(ssh, pidScript, '启动远程 ca serve');
        serve = serveState;
        activeRemotePid = { token: pidToken, repoPath, ssh };
        let tunnelHandle = null;
        try {
          const port = await waitForServePort(serveState, gen, abort.signal, remotePort, deadline);
          const localPort =
            Number(remote.localPort) > 0
              ? Number(remote.localPort)
              : await freePort({ host: '127.0.0.1' });
          const tunnelState = await spawnTunnelProcess(ssh, localPort, port);
          tunnelHandle = tunnelState.handle;
          tunnel = tunnelHandle;
          const readyInfo = await waitForLocalHealth(
            localPort,
            deadline,
            gen,
            abort.signal,
            [serveState.handle, tunnelHandle],
          );
          setStatus(
            {
              state: 'running',
              baseUrl: readyInfo.baseUrl,
              db: database.db || database.dbDir,
              error: null,
              health: readyInfo.data,
              remoteInfo: {
                target: ssh.target,
                user: ssh.user,
                host: ssh.host,
                remotePort: port,
                localPort,
                repoPath,
              },
            },
            reason === 'restart' ? 'restarted' : 'started',
          );
          if (reason !== 'restart') restarts = 0;
          logger?.info?.(
            `[remote] CA 后端就绪 ${readyInfo.baseUrl}（隧道→${ssh.target}:${port}; repo=${repoPath}）`,
          );
          armStableTimer(gen);
          attachCrashWatcher(gen, serveState, tunnelHandle);
          return getStatus();
        } catch (startError) {
          const normalized = normalizeError(startError, 'SERVE_FAILED');
          serveState.handle.kill('SIGTERM');
          if (tunnelHandle) tunnelHandle.kill('SIGTERM');
          await Promise.race([serveState.handle.exited, sleep(2000).catch(() => {})]);
          if (tunnelHandle) {
            try {
              await Promise.race([tunnelHandle.exited, sleep(2000).catch(() => {})]);
            } catch {
              // ignore
            }
          }
          if (serve === serveState) serve = null;
          tunnel = null;
          // The remote process is not a child of this machine: remove it via
          // its per-instance PID file before giving up (or before retry).
          await needsStaleCleanup(ssh, repoPath, pidToken);
          if (activeRemotePid?.token === pidToken) activeRemotePid = null;
          const canFallback =
            attempt === 0 &&
            configuredPort === 0 &&
            normalized.code === 'HEALTH_TIMEOUT' &&
            !serveState.ready.url &&
            !abort.signal.aborted &&
            gen === generation;
          if (canFallback) {
            attempt += 1;
            try {
              remotePort = await pickRemoteFreePort(ssh, repoPath);
              logger?.warn?.(
                `[remote] 未解析到远端就绪端口，回退到远端自选端口 ${remotePort} 重新启动`,
              );
              continue;
            } catch (portError) {
              throw normalizeError(portError, 'PORT_UNAVAILABLE', '远端选择空闲端口失败');
            }
          }
          throw normalized;
        }
      }
    } catch (startError) {
      const normalized = normalizeError(startError, 'SERVE_FAILED');
      if (gen === generation && state !== 'running') {
        if (normalized.code === 'CANCELLED' || abort.signal.aborted) {
          setStatus({ state: 'stopped', baseUrl: null, error: null, health: null, remoteInfo: null }, 'cancelled');
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
    if (disposed) throw new BackendError('DISPOSED', 'CA 远端后端已释放，无法启动');
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
    intentionalStop = true;
    generation += 1;
    clearRestartTimer();
    clearStableTimer();
    startAbort?.abort();
    stopPromise = (async () => {
      const tunnelHandle = tunnel;
      const serveState = serve;
      tunnel = null;
      serve = null;
      if (tunnelHandle && !tunnelHandle.settled) {
        logger?.info?.('[remote] 关闭 SSH 隧道');
        tunnelHandle.kill('SIGTERM');
        await Promise.race([tunnelHandle.exited, sleep(STOP_GRACE_MS).catch(() => {})]);
      }
      // Stop the actual server through the remote PID file.
      try {
        const ssh = requireSsh();
        const repoPath = remote.repoPath || remote.installDir;
        if (repoPath) {
          const pidToken = activeRemotePid?.token || 'serve';
          const result = await executor.runSsh(
            ssh,
            buildRemoteStopScript({ repoPath, pidFile: pidToken }),
            { timeoutMs: 20000, label: 'stop remote ca serve' },
          );
          if (activeRemotePid?.token === pidToken) activeRemotePid = null;
          if (result.code === 0) {
            remoteMayBeRunning = false;
          } else {
            logger?.warn?.(`[remote] 停止远端 serve 返回 exitCode=${result.code}；继续清理本地 ssh`);
          }
        }
      } catch (stopError) {
        logger?.warn?.(`[remote] 停止远端 serve 失败（忽略）: ${stopError?.message || stopError}`);
      }
      if (serveState && !serveState.handle.settled) {
        serveState.handle.kill('SIGTERM');
        await Promise.race([serveState.handle.exited, sleep(STOP_GRACE_MS).catch(() => {})]);
      }
      const inFlight = startPromise;
      if (inFlight) await inFlight.catch(() => {});
      await Promise.resolve();
      if (state !== 'stopped') {
        setStatus(
          { state: 'stopped', baseUrl: null, error: null, health: null, remoteInfo: null },
          'stopped',
        );
      }
      return getStatus();
    })().finally(() => {
      stopPromise = null;
    });
    return stopPromise;
  }

  async function restart(restartOptions = {}) {
    await stop({ reason: 'restart' });
    return start({ reason: 'restart', signal: restartOptions.signal });
  }

  function nextTaskTag() {
    taskCounter += 1;
    return `task-${Date.now().toString(36)}-${taskCounter}`;
  }

  async function remoteKillBestEffort(ssh, repoPath, pidTag) {
    try {
      await executor.runSsh(ssh, buildRemoteStopScript({ repoPath, pidFile: pidTag }), {
        timeoutMs: 15000,
        label: `cancel remote task ${pidTag}`,
      });
    } catch (killError) {
      logger?.warn?.(`[remote] 取消远端任务失败（忽略）: ${killError?.message || killError}`);
    }
  }

  /**
   * Execute a CA CLI command on the remote host (long tasks such as index).
   * Returns `{exitCode,signal,cancelled,...}`; spawn failures throw.
   */
  async function execCa(spec, execOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 远端后端已释放，无法执行命令');
    const args = normalizeCaArgs(spec);
    const ssh = requireSsh();
    const repoPath = requireRepoPath();
    const python = pythonPath(repoPath);
    const pidTag = nextTaskTag();
    const script = buildRemoteCaScript({
      repoPath,
      python,
      args,
      env: remote.env,
      pidTag,
    });
    const session = await resolveSpawnSession(ssh);
    const built = buildSshArgv(session ? session.ssh : ssh, script, {
      sshBinary: deps.sshBinary,
      batchMode: session ? session.batchMode : true,
      extraOptions: session?.extraOptions || [],
    });
    logger?.info?.(`[remote] 远程执行 ca ${args.join(' ')}（${ssh.target}）`);
    const handle = spawnManaged({
      spawn,
      command: built.command,
      args: built.args,
      env: buildChildEnv(session?.env || {}, env),
      logger,
      onStdoutLine: (line, info) => {
        try {
          execOptions.onStdout?.(line, info);
        } catch {
          // consumer callback errors are ignored
        }
      },
      onStderrLine: (line, info) => {
        try {
          execOptions.onStderr?.(line, info);
        } catch {
          // ignore
        }
      },
    });
    let cancelled = false;
    let timedOut = false;
    let forceTimer = null;
    let timeoutTimer = null;
    const kill = () => {
      if (!handle.settled) handle.kill('SIGTERM');
      if (!forceTimer) {
        forceTimer = setTimeout(() => {
          forceTimer = null;
          if (!handle.settled) handle.kill('SIGKILL');
        }, 5000);
      }
    };
    const onAbort = () => {
      cancelled = true;
      kill();
      Promise.resolve().then(() => remoteKillBestEffort(ssh, repoPath, pidTag));
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
        Promise.resolve().then(() => remoteKillBestEffort(ssh, repoPath, pidTag));
      }, timeoutMs);
    }
    try {
      const info = await handle.exited;
      if (info.spawnError) {
        throw new BackendError(
          'SPAWN_FAILED',
          `无法启动 ssh 客户端：${describeCause(info.spawnError)}；请确认本机装有 OpenSSH`,
          { command: built.command },
        );
      }
      logger?.info?.(
        `[remote] 远程 CA CLI 结束 exitCode=${info.code ?? 'null'} signal=${info.signal ?? 'null'}` +
          `${cancelled ? '（已取消）' : ''}${timedOut ? '（超时）' : ''}`,
      );
      return {
        exitCode: info.code,
        signal: info.signal,
        cancelled,
        timedOut,
        command: `ssh ${ssh.target}`,
        args,
      };
    } finally {
      clearTimeout(timeoutTimer);
      clearTimeout(forceTimer);
      if (externalSignal && typeof externalSignal.removeEventListener === 'function') {
        externalSignal.removeEventListener('abort', onAbort);
      }
      try {
        session?.cleanup?.();
      } catch {
        // ignore
      }
    }
  }

  async function dispose() {
    if (disposed) return;
    disposed = true;
    intentionalStop = true;
    generation += 1;
    clearRestartTimer();
    clearStableTimer();
    startAbort?.abort();
    // Killing the local ssh transport does not stop the remote process, so run
    // the PID-file stop script while ssh credentials/config are still usable.
    await cleanupRemoteBestEffort('dispose');
    for (const handle of [tunnel, serve?.handle].filter(Boolean)) {
      if (!handle.settled) {
        try {
          handle.kill('SIGTERM');
          await Promise.race([handle.exited, sleep(2000).catch(() => {})]);
        } catch {
          // ignore
        }
      }
    }
    tunnel = null;
    serve = null;
    try {
      await startPromise;
    } catch {
      // expected during dispose
    }
    try {
      await stopPromise;
    } catch {
      // expected during dispose
    }
    listeners.clear();
    state = 'stopped';
    baseUrl = null;
    healthData = null;
    remoteInfo = null;
  }

  return {
    getStatus,
    subscribe,
    start,
    stop,
    restart,
    execCa,
    dispose,
    /** Exposed for tests/diagnostics. */
    getRemoteInfo: () => (remoteInfo ? clone(remoteInfo) : null),
  };
}
