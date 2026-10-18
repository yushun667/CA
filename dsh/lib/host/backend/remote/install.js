/**
 * Idempotent remote installation of CA on a Linux host.
 *
 * Flow (each step is reported with a status + log stream):
 *   probe        one ssh round trip: OS/python/pip/rsync/paths/db/libclang/disk
 *   source       use an existing remote CA checkout, or rsync this checkout
 *   rsync        local CA checkout -> remote directory (excludes .git/.venv/.tmp)
 *   venv         remote `python3 -m venv <repo>/.venv`
 *   dependencies `pip install -r requirements.txt` (optional offline wheels)
 *   editable     `pip install -e .` (editable only: a wheel loses schema.sql/ui)
 *   verify       `ca version --json` + console-script check
 *
 * Editable install is mandatory: pyproject has no package-data, so a built
 * wheel would miss `src/ca/store/schema.sql` and the UI static assets.
 * `scripts/bootstrap.sh` is NOT used because it hardcodes macOS paths.
 */

import fs from 'node:fs';

import { describeCaVersion, parseCaVersionJson } from '../ca-protocol.js';
import { findRepoRoot, looksLikeCaRepo } from '../discover.js';
import { BackendError, normalizeError } from '../errors.js';
import { nonEmptyString } from '../util.js';
import {
  buildRemoteEditableInstallScript,
  buildRemotePipScript,
  buildRemoteVenvScript,
  buildRemoteVerifyScript,
  buildRsyncArgv,
  classifySshFailure,
  outputTailOf,
  shellQuote,
} from './commands.js';
import { probeRemote } from './probe.js';

/** Step definitions in execution order. */
export const INSTALL_STEP_DEFS = Object.freeze([
  { id: 'probe', title: '探测远程环境' },
  { id: 'source', title: '确定远端 CA 源码' },
  { id: 'rsync', title: '同步 CA 源码到远端' },
  { id: 'venv', title: '创建远端 venv' },
  { id: 'dependencies', title: '安装 Python 依赖' },
  { id: 'editable', title: 'editable 安装 CA' },
  { id: 'verify', title: '校验 ca version' },
]);

/** Per-step process timeouts (pip/rsync on a real network can be slow). */
export const INSTALL_TIMEOUTS = Object.freeze({
  probe: 60000,
  rsync: 30 * 60 * 1000,
  venv: 5 * 60 * 1000,
  pip: 30 * 60 * 1000,
  editable: 30 * 60 * 1000,
  verify: 60000,
});

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** Fresh step list with every step pending. */
export function createInstallSteps() {
  return INSTALL_STEP_DEFS.map(({ id, title }) => ({
    id,
    title,
    status: 'pending',
    detail: null,
    error: null,
    startedAt: null,
    finishedAt: null,
  }));
}

/**
 * Run the whole remote install.
 *
 * Never throws for environment/step failures: it returns
 * `{ok:false, error:{code,message,details}, steps, probed}` so the caller can
 * persist a report. Unexpected programming errors are converted too.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config resolved full config
 * @param {ReturnType<import('./exec.js').createRemoteExecutor>} options.executor
 * @param {any} [options.logger]
 * @param {object} [options.installOptions] per-call overrides
 * @param {(event: {type: 'step'|'log', step?: object, steps?: object[], stream?: string, line?: string}) => void} [options.onEvent]
 * @param {AbortSignal} [options.signal]
 * @param {string} [options.localCwd]
 */
export async function performRemoteInstall(options) {
  const config = options.config;
  const remote = config.remote || {};
  const overrides = options.installOptions || {};
  const executor = options.executor;
  const logger = options.logger || null;
  const onEvent = typeof options.onEvent === 'function' ? options.onEvent : () => {};
  const localCwd = options.localCwd || process.cwd();
  const effective = {
    ...config,
    remote: {
      ...remote,
      ssh: overrides.ssh !== undefined ? overrides.ssh : remote.ssh,
      repoPath: overrides.repoPath !== undefined ? overrides.repoPath : remote.repoPath,
      installDir: overrides.installDir !== undefined ? overrides.installDir : remote.installDir,
      python: overrides.python !== undefined ? overrides.python : remote.python,
    },
  };
  const timeoutOverride = Number(overrides.timeoutMs) > 0 ? Number(overrides.timeoutMs) : null;
  const force = overrides.force === true;
  const startedAt = Date.now();
  const steps = createInstallSteps();

  function emitStep(step) {
    onEvent({ type: 'step', step: clone(step), steps: clone(steps) });
  }

  function emitLog(stream, line) {
    const text = String(line ?? '');
    if (!text) return;
    onEvent({ type: 'log', stream, line: text });
  }

  function startStep(id, detail = null) {
    const step = steps.find((entry) => entry.id === id);
    step.status = 'running';
    step.startedAt = Date.now();
    step.detail = detail;
    emitStep(step);
    return step;
  }

  function succeedStep(id, detail = null) {
    const step = steps.find((entry) => entry.id === id);
    step.status = 'succeeded';
    step.finishedAt = Date.now();
    step.detail = detail;
    emitStep(step);
  }

  function skipStep(id, detail = null) {
    const step = steps.find((entry) => entry.id === id);
    step.status = 'skipped';
    step.finishedAt = Date.now();
    step.detail = detail;
    emitStep(step);
  }

  function failStep(id, error) {
    const step = steps.find((entry) => entry.id === id);
    step.status = 'failed';
    step.finishedAt = Date.now();
    step.error = { code: error.code || 'INSTALL_STEP_FAILED', message: error.message };
    step.detail = error.message;
    emitStep(step);
  }

  function finish(payload) {
    return {
      ok: payload.ok === true,
      startedAt,
      finishedAt: Date.now(),
      mode: payload.mode || null,
      repoPath: payload.repoPath || null,
      venvPath: payload.venvPath || null,
      version: payload.version || null,
      skipped: payload.skipped === true,
      steps: steps.map(clone),
      error: payload.error || null,
      probed: payload.probed || null,
    };
  }

  function fail(stepId, error) {
    const normalized = normalizeError(error, 'INSTALL_STEP_FAILED', '远程安装失败');
    if (stepId) failStep(stepId, normalized);
    logger?.error?.(`[remote] 安装失败（${stepId || 'unknown'}）：${normalized.message}`);
    return finish({ ok: false, error: normalized.toJSON() });
  }

  function mapCommandError(stepId, context, result) {
    if (result?.cancelled) {
      return new BackendError('CANCELLED', `远程安装已取消（${context}）`, { step: stepId });
    }
    if (result?.timedOut) {
      return new BackendError('INSTALL_STEP_FAILED', `${context} 超时`, {
        step: stepId,
        timeout: true,
        tail: outputTailOf(result),
      });
    }
    if (result?.spawnError) {
      return new BackendError(
        'INSTALL_STEP_FAILED',
        `${context} 无法启动：${result.spawnError.message || result.spawnError}`,
        { step: stepId },
      );
    }
    if (result?.code !== 0) {
      const failure = classifySshFailure(result);
      const tail = outputTailOf(result, 8);
      if (failure.code !== 'SSH_FAILED') {
        return new BackendError(failure.code, `${context} 失败：${failure.message}`, {
          step: stepId,
          hint: failure.hint,
          tail,
        });
      }
      return new BackendError('INSTALL_STEP_FAILED', `${context} 失败（exitCode=${result.code}）：${tail || '无输出'}`, {
        step: stepId,
        exitCode: result.code,
        tail,
      });
    }
    return null;
  }

  async function runSshStep(stepId, context, script, timeoutMs) {
    const result = await executor.runSsh(effective.remote.ssh, script, {
      timeoutMs: timeoutOverride || timeoutMs,
      signal: options.signal,
      fallbackSsh: remote.ssh,
      label: context,
      onStdoutLine: (line) => emitLog('stdout', line),
      onStderrLine: (line) => emitLog('stderr', line),
    });
    const error = mapCommandError(stepId, context, result);
    if (error) throw error;
    return result;
  }

  function evaluateProbeForInstall(probe) {
    // Only host capabilities are hard requirements here. Whether repoPath is a
    // CA checkout / installDir is writable is decided by the `source` step,
    // which can also rsync into a missing directory.
    const required = new Set(['ssh', 'os', 'python3']);
    const failed = (probe.checks || []).filter((check) => required.has(check.name) && !check.ok);
    if (failed.length === 0) return { ok: true };
    const message = failed.map((check) => `${check.name}: ${check.detail || '检查失败'}`).join('；');
    return {
      ok: false,
      message: probe.error?.message || `远程环境不满足安装条件：${message}`,
      code: probe.error?.code || 'PROBE_FAILED',
      failed: failed.map((check) => check.name),
      hints: probe.hints || [],
    };
  }

  // -- probe ------------------------------------------------------------ //
  startStep('probe', `ssh ${effective.remote.ssh?.host || effective.remote.ssh || '?'}`);
  let probe;
  try {
    probe = await probeRemote({
      config: effective,
      executor,
      logger,
      timeoutMs: timeoutOverride || INSTALL_TIMEOUTS.probe,
    });
  } catch (error) {
    return fail('probe', normalizeError(error, 'PROBE_FAILED', '远程探测失败'));
  }
  const probeVerdict = evaluateProbeForInstall(probe);
  if (!probeVerdict.ok) {
    const error = new BackendError(probeVerdict.code, probeVerdict.message, {
      step: 'probe',
      failedChecks: probeVerdict.failed,
      hints: probeVerdict.hints,
    });
    failStep('probe', error);
    logger?.error?.(`[remote] 远程环境不满足安装条件：${error.message}`);
    return finish({
      ok: false,
      error: error.toJSON(),
      probed: compactProbe(probe),
    });
  }
  succeedStep(
    'probe',
    `${probe.os?.name || 'linux'} / ${probe.os?.arch || '?'} / python ${probe.python?.version || '?'} / ` +
      `${probe.disk?.availableText || '?'} 可用`,
  );

  const probedRepo = probe.paths?.repoPath;
  const configuredRepoPath = nonEmptyString(effective.remote.repoPath);
  const configuredInstallDir = nonEmptyString(effective.remote.installDir);
  let mode;
  let remoteRepoPath = null;

  // -- source ----------------------------------------------------------- //
  startStep('source');
  try {
    if (probedRepo?.exists && probedRepo.isCaRepo) {
      mode = 'existing';
      remoteRepoPath = configuredRepoPath;
      succeedStep('source', `使用远端已有 CA 检出：${remoteRepoPath}`);
    } else if (configuredRepoPath && probedRepo?.exists && !probedRepo.isCaRepo) {
      throw new BackendError(
        'INSTALL_STEP_FAILED',
        `remote.repoPath 目录存在但不是 CA 检出：${configuredRepoPath}；请换一个 installDir，或删除/清空该目录后重试`,
        { step: 'source', hints: ['确认远端目录内容，或设置 remote.installDir 指向空目录'] },
      );
    } else if (configuredRepoPath) {
      mode = 'rsync';
      remoteRepoPath = configuredRepoPath;
    } else if (configuredInstallDir) {
      mode = 'rsync';
      remoteRepoPath = configuredInstallDir;
    } else {
      throw new BackendError(
        'BACKEND_NOT_CONFIGURED',
        '远程 CA 源码位置未配置：请设置 remote.repoPath（已有检出）或 remote.installDir（安装目录）后重试',
        { step: 'source' },
      );
    }

    // Idempotency: an existing remote checkout with a working CA is enough.
    if (mode === 'existing' && !force && probe.ca?.version) {
      succeedStep('source', `远端已有可用 CA 检出（ca-analyzer ${probe.ca.version}），跳过同步与安装`);
      for (const id of ['rsync', 'venv', 'dependencies', 'editable']) {
        skipStep(id, '复用远端已有环境（幂等跳过；force=true 可重装）');
      }
      startStep('verify', '复用已安装版本');
      succeedStep('verify', probe.ca.description || `ca-analyzer ${probe.ca.version}`);
      logger?.info?.(`[remote] 远端 CA 已安装且可用（${probe.ca.version}），跳过安装`);
      return finish({
        ok: true,
        skipped: true,
        mode,
        repoPath: remoteRepoPath,
        venvPath: `${remoteRepoPath}/.venv`,
        version: { version: probe.ca.version, schemaVersion: probe.ca.schemaVersion },
        probed: compactProbe(probe),
      });
    }

    if (mode === 'rsync') {
      const localSource = resolveLocalSource({ overrides, effective, localCwd });
      if (!localSource) {
        throw new BackendError(
          'INSTALL_STEP_FAILED',
          '找不到本机 CA 源码，无法 rsync 到远端：请设置 local.repoRoot 指向 CA 检出（含 pyproject.toml，name="ca-analyzer"），或在设置页手动指定',
          { step: 'source', hints: ['local.repoRoot=/path/to/CA', '或在 CA 仓库根运行/安装 DSH 插件'] },
        );
      }
      succeedStep('source', `本机 ${localSource} → 远端 ${remoteRepoPath}`);
    }
  } catch (error) {
    return fail('source', error);
  }

  const venvPython = `${remoteRepoPath}/.venv/bin/python`;
  const venvCa = `${remoteRepoPath}/.venv/bin/ca`;
  const reuseRemoteVenv = mode === 'existing' && Boolean(probe.python?.venv) && !force;

  // -- rsync ------------------------------------------------------------ //
  startStep('rsync');
  try {
    if (mode !== 'rsync') {
      skipStep('rsync', '使用远端已有检出，无需 rsync');
    } else {
      // rsync does not create the destination path chain; `--mkpath` is not
      // available on every remote rsync, so create the directory over SSH first.
      await runSshStep(
        'rsync',
        '创建远端安装目录',
        `mkdir -p ${shellQuote(remoteRepoPath)}`,
        INSTALL_TIMEOUTS.probe,
      );
      const localSource = resolveLocalSource({ overrides, effective, localCwd });
      const argv = buildRsyncArgv({
        ssh: effective.remote.ssh,
        sourceDir: localSource,
        targetDir: remoteRepoPath,
        excludes: overrides.excludes,
        fallbackSsh: remote.ssh,
      });
      const result = await executor.runLocal(argv.command, argv.args, {
        timeoutMs: timeoutOverride || INSTALL_TIMEOUTS.rsync,
        signal: options.signal,
        label: `rsync → ${remoteRepoPath}`,
        onStdoutLine: (line) => emitLog('stdout', line),
        onStderrLine: (line) => emitLog('stderr', line),
      });
      const error = mapCommandError('rsync', 'rsync 源码同步', result);
      if (error) throw error;
      succeedStep('rsync', `已同步 CA 源码到 ${remoteRepoPath}`);
    }
  } catch (error) {
    return fail('rsync', error);
  }

  // -- venv ------------------------------------------------------------- //
  startStep('venv');
  let venvCreated = false;
  try {
    if (reuseRemoteVenv) {
      skipStep('venv', '远端 venv 已存在（幂等跳过）');
    } else {
      const python =
        nonEmptyString(effective.remote.python) ||
        nonEmptyString(probe.python?.system) ||
        'python3';
      const script = buildRemoteVenvScript({
        repoPath: remoteRepoPath,
        python,
        env: effective.remote.env,
      });
      await runSshStep('venv', '创建远端 venv', script, INSTALL_TIMEOUTS.venv);
      venvCreated = true;
      succeedStep('venv', `${remoteRepoPath}/.venv`);
    }
  } catch (error) {
    return fail('venv', error);
  }

  // -- dependencies ----------------------------------------------------- //
  startStep('dependencies');
  try {
    if (reuseRemoteVenv && probe.ca?.version && !force) {
      skipStep('dependencies', '远端依赖已安装（幂等跳过）');
    } else {
      const script = buildRemotePipScript({
        repoPath: remoteRepoPath,
        venvPython,
        offline: overrides.offline === true,
        wheelDir: overrides.wheelDir || null,
        upgradePip: !venvCreated ? false : undefined,
        env: effective.remote.env,
      });
      await runSshStep('dependencies', '安装 Python 依赖', script, INSTALL_TIMEOUTS.pip);
      succeedStep('dependencies', 'pip install -r requirements.txt 完成');
    }
  } catch (error) {
    return fail('dependencies', error);
  }

  // -- editable install -------------------------------------------------- //
  startStep('editable');
  try {
    if (reuseRemoteVenv && probe.ca?.version && !force) {
      skipStep('editable', '远端 CA 已 editable 安装（幂等跳过）');
    } else {
      const script = buildRemoteEditableInstallScript({
        repoPath: remoteRepoPath,
        venvPython,
        env: effective.remote.env,
      });
      await runSshStep('editable', 'editable 安装 CA（pip install -e .）', script, INSTALL_TIMEOUTS.editable);
      succeedStep('editable', 'pip install -e . 完成（非 wheel，保留 schema/UI 静态资源）');
    }
  } catch (error) {
    return fail('editable', error);
  }

  // -- verify ----------------------------------------------------------- //
  startStep('verify');
  try {
    const script = buildRemoteVerifyScript({
      repoPath: remoteRepoPath,
      venvPython,
      env: effective.remote.env,
    });
    const result = await runSshStep('verify', '校验 ca version', script, INSTALL_TIMEOUTS.verify);
    const versionRecord = parseCaVersionJson(result.stdout);
    if (!versionRecord || !versionRecord.version) {
      throw new BackendError(
        'INSTALL_STEP_FAILED',
        `ca version 校验失败：无法解析版本输出（exitCode=${result.code}）；输出: ${outputTailOf(result, 6) || '(空)'}`,
        { step: 'verify', tail: outputTailOf(result, 6) },
      );
    }
    const consoleOk = /CA_CONSOLE_OK=1/.test(result.stdout);
    if (!consoleOk) {
      logger?.warn?.(
        `[remote] venv console script 不可用（${venvCa}）；将使用 python -m ca 启动/执行，不影响功能`,
      );
    }
    succeedStep(
      'verify',
      `${describeCaVersion(versionRecord) || `ca-analyzer ${versionRecord.version}`}${consoleOk ? '（console script 可用）' : '（python -m ca 可用）'}`,
    );
    logger?.info?.(`[remote] 远程安装完成：${remoteRepoPath} (${describeCaVersion(versionRecord)})`);
    return finish({
      ok: true,
      mode,
      repoPath: remoteRepoPath,
      venvPath: `${remoteRepoPath}/.venv`,
      version: {
        version: versionRecord.version || null,
        schemaVersion: versionRecord.schema_version ?? null,
      },
      probed: compactProbe(probe),
    });
  } catch (error) {
    return fail('verify', error);
  }
}

/** Find a local CA checkout to sync from. */
export function resolveLocalSource({ overrides = {}, effective, localCwd } = {}) {
  const searchRoot = nonEmptyString(localCwd) || process.cwd();
  const candidates = [overrides.localRepo, effective?.local?.repoRoot, searchRoot];
  for (const candidate of candidates) {
    const text = nonEmptyString(candidate);
    if (!text) continue;
    try {
      if (looksLikeCaRepo(text)) return text;
    } catch {
      // ignore unreadable candidates
    }
  }
  return findRepoRoot({ startDirs: [searchRoot] });
}

/** Small, UI-safe projection of a probe report. */
export function compactProbe(report) {
  if (!report) return null;
  return {
    ok: report.ok,
    platform: report.platform,
    ssh: report.ssh,
    os: report.os,
    python: report.python,
    ca: report.ca,
    paths: report.paths,
    disk: report.disk,
    checks: report.checks,
    hints: report.hints,
    warnings: report.warnings,
    error: report.error,
  };
}

/** Best-effort local check used by install before rsync (exported for tests). */
export function isLocalCaCheckout(dir) {
  try {
    if (fs.existsSync(dir)) return looksLikeCaRepo(dir);
  } catch {
    return false;
  }
  return false;
}
