/**
 * Remote CA backend (Linux server) for DSH on macOS.
 *
 * Composes three pieces behind the same manager-facing surface as the local
 * backend:
 *   - ./serve.js: remote `ca serve` + `ssh -N -L` tunnel lifecycle;
 *   - ./probe.js: one-round-trip environment report;
 *   - ./install.js: idempotent rsync → venv → pip install -r → pip install -e → verify.
 *
 * `getInstallStatus()` is a synchronous snapshot for UI/RPC polling: install is
 * a long action, so `startInstall()` returns immediately and streamed progress
 * is kept in a bounded log/step buffer.
 */

import { BackendError, normalizeError } from '../errors.js';
import { listRemoteDatabases, validateRemoteDatabase } from './databases.js';
import { createRemoteExecutor } from './exec.js';
import { createInstallSteps, performRemoteInstall } from './install.js';
import { probeRemote } from './probe.js';
import { createRemoteServeController } from './serve.js';

const INSTALL_LOG_LIMIT = 80;

function clone(value) {
  return value === undefined ? value : JSON.parse(JSON.stringify(value));
}

function freshInstallStatus() {
  return {
    state: 'idle',
    startedAt: null,
    finishedAt: null,
    steps: createInstallSteps(),
    logs: [],
    error: null,
    result: null,
  };
}

/**
 * @param {object} options
 * @param {Record<string, any>} options.config resolved full config
 * @param {{stateDir: string, logDir: string, settingsPath: string}} [options.paths]
 * @param {any} [options.logger]
 * @param {object} [options.deps]
 */
export function createRemoteBackend(options) {
  const config = options.config;
  const logger = options.logger || null;
  const deps = options.deps || {};
  const now = typeof deps.now === 'function' ? deps.now : Date.now;
  const executor = createRemoteExecutor({ deps, logger });
  const controller = createRemoteServeController({ config, deps, logger, executor });

  let disposed = false;
  let installStatus = freshInstallStatus();
  /** @type {Promise<any>|null} */
  let installPromise = null;
  /** @type {AbortController|null} */
  let installAbort = null;

  function trackInstallEvent(event) {
    if (!event || typeof event !== 'object') return;
    if (event.type === 'step' && Array.isArray(event.steps)) {
      installStatus.steps = clone(event.steps);
      return;
    }
    if (event.type === 'log') {
      installStatus.logs.push({
        at: now(),
        stream: event.stream || 'stdout',
        line: String(event.line || ''),
      });
      if (installStatus.logs.length > INSTALL_LOG_LIMIT) {
        installStatus.logs.splice(0, installStatus.logs.length - INSTALL_LOG_LIMIT);
      }
    }
  }

  function beginInstallStatus() {
    installStatus = freshInstallStatus();
    installStatus.state = 'running';
    installStatus.startedAt = now();
    installAbort = new AbortController();
  }

  function finishInstallStatus(report, failure) {
    installStatus.finishedAt = now();
    if (failure) {
      installStatus.state = 'failed';
      installStatus.error = {
        code: failure.code || 'INSTALL_FAILED',
        message: failure.message,
        details: failure.details || undefined,
      };
    } else {
      installStatus.state = report?.ok ? 'succeeded' : 'failed';
      installStatus.error = report?.error || null;
    }
    installStatus.result = report
      ? {
          ok: report.ok,
          mode: report.mode || null,
          repoPath: report.repoPath || null,
          venvPath: report.venvPath || null,
          version: report.version || null,
          skipped: report.skipped === true,
          startedAt: report.startedAt,
          finishedAt: report.finishedAt,
          steps: clone(report.steps),
          error: report.error || null,
          probed: report.probed || null,
        }
      : null;
    installAbort = null;
  }

  async function runInstall(installOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 远端后端已释放，无法安装');
    if (installPromise) {
      throw new BackendError(
        'ALREADY_RUNNING',
        '远程安装正在进行中；请用 backend.installStatus 轮询进度',
        { startedAt: installStatus.startedAt },
      );
    }
    beginInstallStatus();
    installPromise = performRemoteInstall({
      config,
      executor,
      logger,
      installOptions,
      onEvent: trackInstallEvent,
      signal: installAbort?.signal,
      localCwd: deps.cwd,
    })
      .then((report) => {
        finishInstallStatus(report, null);
        if (!report.ok) {
          throw new BackendError(
            report.error?.code || 'INSTALL_FAILED',
            report.error?.message || '远程安装失败',
            {
              steps: report.steps,
              hints: report.error?.details?.hints || [],
              probed: report.probed,
            },
          );
        }
        return report;
      })
      .catch((rawError) => {
        const normalized = normalizeError(rawError, 'INSTALL_FAILED', '远程安装失败');
        if (installStatus.state !== 'failed') finishInstallStatus(null, normalized);
        throw normalized;
      })
      .finally(() => {
        installPromise = null;
      });
    return installPromise;
  }

  function startInstall(installOptions = {}) {
    if (disposed) throw new BackendError('DISPOSED', 'CA 远端后端已释放，无法安装');
    if (installPromise) {
      return { started: false, running: true, startedAt: installStatus.startedAt };
    }
    const startedAt = now();
    installPromise = runInstall(installOptions);
    // Background start: failures are recorded in installStatus, not unhandled.
    installPromise.catch(() => {});
    return { started: true, running: true, startedAt };
  }

  function install(installOptions = {}) {
    return runInstall(installOptions);
  }

  return {
    getStatus: controller.getStatus,
    subscribe: controller.subscribe,
    start: controller.start,
    stop: controller.stop,
    restart: controller.restart,
    execCa: controller.execCa,
    probe(sshOverride) {
      return probeRemote({
        config,
        sshOverride,
        executor,
        logger,
        timeoutMs: Number(deps.probeTimeoutMs) > 0 ? Number(deps.probeTimeoutMs) : undefined,
      });
    },
    /** Database candidates (never throws; failures come back as a report). */
    listDatabases(listOptions = {}) {
      return listRemoteDatabases({
        config,
        executor,
        logger,
        sshOverride: listOptions?.ssh,
        maxItems: listOptions?.maxItems,
        timeoutMs: Number(listOptions?.timeoutMs) > 0 ? Number(listOptions.timeoutMs) : undefined,
      });
    },
    /** Validate a switch target over ssh; throws BackendError on failure. */
    validateDatabase(selection = {}) {
      return validateRemoteDatabase({
        config,
        executor,
        logger,
        sshOverride: selection?.ssh,
        kind: selection?.kind,
        path: selection?.path,
      });
    },
    install,
    startInstall,
    getInstallStatus() {
      return clone(installStatus);
    },
    async dispose() {
      if (disposed) return;
      disposed = true;
      try {
        installAbort?.abort();
      } catch {
        // ignore
      }
      await installPromise?.catch(() => {});
      await controller.dispose();
    },
  };
}
