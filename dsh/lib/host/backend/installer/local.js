/**
 * Local CA backend installer (Windows / macOS / Linux hosts).
 *
 * Flow: platform + Python detection → wheel/wheelhouse resolution → venv
 * creation (or repair) → `pip install` the bundled CA wheel (online: PyPI
 * dependencies; offline: `--no-index --find-links <wheelhouse>`) → `ca version
 * --json` verification.
 *
 * The installer is idempotent: an existing working venv is reused (`skipped`),
 * a broken venv is recreated. Every step reports status and logs through
 * `onEvent`, and failures carry platform-specific repair hints. All child
 * processes go through the injectable one-shot runner (`deps.runProcess`), so
 * tests fake Python/pip without spawning anything.
 */

import fs from 'node:fs';
import path from 'node:path';

import { BackendError, describeCause, normalizeError } from '../errors.js';
import { nonEmptyString } from '../util.js';
import {
  BUNDLED_WHEEL_DIR,
  detectHostPlatform,
  findBundledCaWheel,
  installerPaths,
  isVenvPythonPresent,
  platformInstallHints,
  probePythonInterpreters,
  readCaVersionOutput,
  resolveWheelhouse,
  validatePythonExecutable,
  venvLayout,
} from './platform.js';

/**
 * Install steps in execution order.
 * @type {ReadonlyArray<{id: string, title: string}>}
 */
export const LOCAL_INSTALL_STEP_DEFS = Object.freeze([
  { id: 'environment', title: '检测平台与 Python' },
  { id: 'wheel', title: '解析 CA wheel / wheelhouse' },
  { id: 'venv', title: '创建/修复 venv' },
  { id: 'dependencies', title: '安装 CA 与依赖' },
  { id: 'verify', title: '校验 ca version' },
]);

const STEP_TIMEOUTS = Object.freeze({
  python: 8000,
  venv: 5 * 60 * 1000,
  pip: 30 * 60 * 1000,
  verify: 60 * 1000,
});

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** Fresh step list for status snapshots. */
export function createLocalInstallSteps() {
  return LOCAL_INSTALL_STEP_DEFS.map(({ id, title }) => ({
    id,
    title,
    status: 'pending',
    detail: null,
    error: null,
    startedAt: null,
    finishedAt: null,
  }));
}

/** Fresh install status snapshot for the manager/UI. */
export function createLocalInstallStatus() {
  return {
    scope: 'local',
    state: 'idle',
    mode: null,
    offline: null,
    startedAt: null,
    finishedAt: null,
    steps: createLocalInstallSteps(),
    logs: [],
    error: null,
    result: null,
  };
}

/** Bounded projection of an install report (manager status snapshots). */
export function summarizeLocalInstallReport(report) {
  if (!report) return null;
  return {
    ok: report.ok,
    scope: 'local',
    mode: report.mode,
    offline: report.offline,
    platform: report.platform,
    python: report.python
      ? { command: report.python.command, version: report.python.versionText || report.python.version }
      : null,
    installDir: report.installDir,
    venvDir: report.venvDir,
    venvPython: report.venvPython,
    caScript: report.caScript,
    wheel: report.wheel,
    version: report.version,
    skipped: report.skipped === true,
    startedAt: report.startedAt,
    finishedAt: report.finishedAt,
    steps: clone(report.steps),
    error: report.error || null,
  };
}

/**
 * Install CA into a local managed venv.
 *
 * @param {object} options
 * @param {{stateDir: string}} options.paths plugin state paths
 * @param {object} [options.deps] injectables: runProcess, env, hostPlatform, hostArch, bundledWheelDir, fsImpl
 * @param {object} [options.installOptions] {installDir, python, offline, wheelhousePath, force, pipArgs}
 * @param {(event: object) => void} [options.onEvent]
 * @param {AbortSignal} [options.signal]
 * @param {any} [options.logger]
 */
/** Runtime imports the analyzer needs beyond the console script. */
export const RUNTIME_DEPENDENCY_PROBE = ['-c', 'import clang.cindex, duckdb'];

async function hasRuntimeDependencies(run, python, timeoutMs) {
  if (typeof run !== 'function') return true;
  try {
    const result = await run(python, RUNTIME_DEPENDENCY_PROBE, { timeoutMs });
    return result?.code === 0;
  } catch {
    return false;
  }
}

export async function performLocalInstall(options) {
  const { paths, logger } = options;
  const deps = options.deps || {};
  const installOptions = options.installOptions || {};
  const onEvent = typeof options.onEvent === 'function' ? options.onEvent : () => {};
  const fsImpl = deps.fsImpl || fs;
  const runProcessFn = deps.runProcess;
  const platform = detectHostPlatform(deps.hostPlatform || process.platform, deps.hostArch || process.arch);
  const startedAt = Date.now();
  const force = installOptions.force === true;
  const offline = installOptions.offline === true;
  const steps = createLocalInstallSteps();

  function emitStep(step) {
    onEvent({ type: 'step', step: clone(step), steps: clone(steps) });
  }
  function emitLog(stream, line) {
    const text = String(line ?? '');
    if (text) onEvent({ type: 'log', stream, line: text });
  }
  function findStep(id) {
    return steps.find((entry) => entry.id === id);
  }
  function startStep(id, detail = null) {
    const step = findStep(id);
    step.status = 'running';
    step.startedAt = Date.now();
    step.detail = detail;
    step.error = null;
    emitStep(step);
  }
  function succeedStep(id, detail = null) {
    const step = findStep(id);
    step.status = 'succeeded';
    step.finishedAt = Date.now();
    step.detail = detail;
    emitStep(step);
  }
  function skipStep(id, detail = null) {
    const step = findStep(id);
    step.status = 'skipped';
    step.finishedAt = Date.now();
    step.detail = detail;
    emitStep(step);
  }
  function failStep(id, error) {
    const step = findStep(id);
    if (!step) return;
    step.status = 'failed';
    step.finishedAt = Date.now();
    step.error = { code: error.code || 'INSTALL_STEP_FAILED', message: error.message };
    step.detail = error.message;
    emitStep(step);
  }

  const base = {
    ok: false,
    scope: 'local',
    mode: null,
    offline,
    platform,
    python: null,
    installDir: null,
    venvDir: null,
    venvPython: null,
    caScript: null,
    wheel: null,
    wheelhouse: null,
    version: null,
    skipped: false,
    steps,
    startedAt,
    finishedAt: null,
    error: null,
  };

  function finish(payload) {
    return { ...base, ...payload, steps: steps.map(clone), finishedAt: Date.now() };
  }

  function currentStepId() {
    const running = steps.find((step) => step.status === 'running');
    return running ? running.id : null;
  }

  function platformHints(step) {
    return platformInstallHints(platform.os, { step, offline, platformId: platform.platformId });
  }

  function stepError(stepId, code, message, extra = {}) {
    const hints =
      Array.isArray(extra.hints) && extra.hints.length > 0 ? extra.hints : platformHints(stepId);
    return new BackendError(code, message, { step: stepId, ...extra, hints });
  }

  function failWithError(stepId, error) {
    const normalized = normalizeError(error, 'INSTALL_STEP_FAILED', '本地 CA 后端安装失败');
    failStep(stepId || normalized.details?.step || currentStepId(), normalized);
    logger?.error?.(`[installer:local] ${stepId || 'install'} 失败：${normalized.message}`);
    return finish({ ok: false, mode: offline ? 'offline' : 'online', error: normalized.toJSON() });
  }

  async function run(command, args, runOptions = {}) {
    return runProcessFn({
      command,
      args,
      timeoutMs: runOptions.timeoutMs,
      signal: options.signal,
      logger,
      maxOutputBytes: 256 * 1024,
      onStdoutLine: (line) => emitLog('stdout', line),
      onStderrLine: (line) => emitLog('stderr', line),
    });
  }

  function assertCommand(result, stepId, context, extraHints = []) {
    if (result?.cancelled) {
      throw stepError(stepId, 'CANCELLED', `${context} 已取消`, { hints: [] });
    }
    const hints = extraHints.length > 0 ? extraHints : platformHints(stepId);
    if (result?.spawnError) {
      throw stepError(
        stepId,
        'INSTALL_STEP_FAILED',
        `${context} 失败：无法启动 ${result.command || '子进程'}（${describeCause(result.spawnError)}）`,
        { tail: describeCause(result.spawnError), hints },
      );
    }
    if (result?.timedOut) {
      throw stepError(stepId, 'INSTALL_STEP_FAILED', `${context} 超时`, {
        timeout: true,
        tail: String(result.stderr || '').slice(-2000),
        hints,
      });
    }
    if (result?.code !== 0) {
      const tail = `${result?.stderr || ''}\n${result?.stdout || ''}`.trim().slice(-4000);
      throw stepError(
        stepId,
        'INSTALL_STEP_FAILED',
        `${context} 失败（exitCode=${result?.code}）：${tail || '无输出'}`,
        { exitCode: result?.code, tail, hints },
      );
    }
  }

  let currentStep = null;
  try {
    // -- environment ---------------------------------------------------- //
    currentStep = 'environment';
    startStep('environment', platform.label);
    if (!platform.supported) {
      throw stepError(
        'environment',
        'UNSUPPORTED_PLATFORM',
        `不支持的宿主平台：${platform.os}/${platform.arch}（支持 Windows/macOS/Linux x64/arm64）`,
      );
    }
    if (typeof runProcessFn !== 'function') {
      throw new BackendError('INSTALLER_MISCONFIGURED', '本地安装器缺少 runProcess 依赖', {
        step: 'environment',
      });
    }
    const defaults = installerPaths({ stateDir: paths?.stateDir, platformId: platform.platformId });
    const installDir = installOptions.installDir
      ? path.resolve(String(installOptions.installDir))
      : defaults.installDir;
    const layout = venvLayout(installDir, { platform: platform.os });
    base.installDir = installDir;
    base.venvDir = layout.venvDir;
    base.venvPython = layout.python;
    base.caScript = layout.caScript;

    let python;
    let probed = null;
    const configuredPython = nonEmptyString(installOptions.python);
    if (configuredPython) {
      try {
        python = await validatePythonExecutable({
          python: configuredPython,
          runProcess: runProcessFn,
          signal: options.signal,
          logger,
        });
      } catch (error) {
        throw stepError('environment', error.code || 'PYTHON_NOT_FOUND', error.message, {
          python: configuredPython,
        });
      }
      base.python = { ...python, source: 'configured' };
    } else {
      probed = await probePythonInterpreters({
        runProcess: runProcessFn,
        platform: platform.os,
        timeoutMs: STEP_TIMEOUTS.python,
        signal: options.signal,
        logger,
      });
      if (!probed.selected) {
        const checked = probed.checked
          .map((entry) => `${entry.label}: ${entry.version || entry.error || '不可用'}`)
          .join('；');
        throw stepError(
          'environment',
          'PYTHON_NOT_FOUND',
          `未找到 Python >= 3.11（宿主 ${platform.label}）。已检测：${checked || '无'}`,
          { checked: probed.checked },
        );
      }
      python = probed.selected;
      base.python = { ...python, source: 'detected', checked: probed.checked };
    }
    succeedStep('environment', `${platform.label} · ${python.label} (Python ${python.versionText})`);

    // -- wheel / wheelhouse --------------------------------------------- //
    currentStep = 'wheel';
    startStep('wheel');
    const wheel = findBundledCaWheel({ dir: deps.bundledWheelDir || BUNDLED_WHEEL_DIR, fsImpl });
    if (!wheel) {
      throw stepError(
        'wheel',
        'WHEEL_NOT_FOUND',
        '插件内置的 CA wheel 缺失（安装包不完整）；请重新安装插件或从仓库构建 wheel',
        { wheelDir: deps.bundledWheelDir || BUNDLED_WHEEL_DIR },
      );
    }
    base.wheel = { path: wheel.path, name: wheel.name, version: wheel.version, bytes: wheel.bytes };
    let wheelhouse = null;
    if (offline) {
      wheelhouse = resolveWheelhouse({
        explicitPath: installOptions.wheelhousePath,
        stateDir: paths?.stateDir,
        platformId: platform.platformId,
        fsImpl,
      });
      if (!wheelhouse.exists || wheelhouse.kind !== 'dir') {
        const reason =
          wheelhouse.kind === 'archive'
            ? '检测到 .tar.gz 离线包：请先解压为目录（pip --find-links 需要目录）'
            : '目录中应包含 CA wheel 与全部依赖 wheel';
        throw stepError(
          'wheel',
          'WHEELHOUSE_NOT_FOUND',
          `离线安装需要 wheelhouse（当前 ${wheelhouse.path || '(未找到)'}）；${reason}`,
          { wheelhousePath: wheelhouse.path, platformId: platform.platformId },
        );
      }
      base.wheelhouse = {
        path: wheelhouse.dir,
        kind: wheelhouse.kind,
        wheelCount: wheelhouse.wheelCount,
        caWheel: wheelhouse.caWheel,
        platform: wheelhouse.platform,
        python: wheelhouse.manifest?.python || null,
      };
      // Wheelhouses contain interpreter-ABI-specific dependency wheels (e.g.
      // duckdb cp311 vs cp312). Select a matching interpreter instead of
      // letting pip fail with "no matching distribution".
      const wheelhousePython = nonEmptyString(wheelhouse.manifest?.python);
      if (wheelhousePython && !configuredPython) {
        const versionMatch = /(\d+)\.(\d+)/.exec(String(python.versionText || ''));
        const selectedMinor = versionMatch ? `${versionMatch[1]}.${versionMatch[2]}` : null;
        if (selectedMinor && selectedMinor !== wheelhousePython) {
          const alternative = (probed?.checked || []).find(
            (entry) => entry.ok && String(entry.version || '').startsWith(`${wheelhousePython}.`),
          );
          if (!alternative) {
            throw stepError(
              'wheel',
              'WHEELHOUSE_PYTHON_MISMATCH',
              `离线 wheelhouse 是按 Python ${wheelhousePython} 构建的，但当前只找到 Python ${selectedMinor}；请用相同 Python 版本重新生成 wheelhouse，或安装/指定 Python ${wheelhousePython}`,
              {
                wheelhousePython,
                selectedPython: selectedMinor,
                wheelhousePath: wheelhouse.dir,
                hints: [
                  `node dsh/scripts/build-wheelhouse.mjs --python-version ${selectedMinor}`,
                  `或把项目/安装选项的 python 指向 Python ${wheelhousePython}`,
                ],
              },
            );
          }
          python = {
            command: alternative.command,
            args: alternative.args.slice(),
            label: alternative.label,
            versionText: alternative.version,
            version: python.version,
            source: 'wheelhouse',
          };
          base.python = { ...python };
          succeedStep('environment', `${platform.label} · ${python.label} (${alternative.version})`);
          logger?.info?.(
            `[installer:local] 离线 wheelhouse 需要 Python ${wheelhousePython}，改用 ${python.label}`,
          );
        }
      }
    }
    succeedStep(
      'wheel',
      offline
        ? `${wheel.name} + wheelhouse（${wheelhouse.wheelCount} wheels）`
        : `${wheel.name}（在线安装依赖）`,
    );

    // -- venv (idempotent detection + repair) --------------------------- //
    currentStep = 'venv';
    let repair = false;
    if (!force && isVenvPythonPresent(layout.python)) {
      startStep('venv', '检测现有 venv');
      const probe = await run(layout.python, ['-m', 'ca', 'version', '--json'], {
        timeoutMs: STEP_TIMEOUTS.verify,
      });
      const existing = readCaVersionOutput(`${probe?.stdout || ''}\n${probe?.stderr || ''}`);
      const depsOk = existing ? await hasRuntimeDependencies(run, layout.python, STEP_TIMEOUTS.verify) : false;
      if (probe?.code === 0 && existing && depsOk) {
        skipStep('venv', '已有可用 venv（幂等复用）');
        skipStep('dependencies', '已有可用 CA 安装（幂等跳过；force=true 可重装）');
        startStep('verify', '复用已安装版本');
        succeedStep('verify', `ca-analyzer ${existing.version}`);
        base.version = existing;
        base.skipped = true;
        base.mode = 'existing';
        logger?.info?.(
          `[installer:local] 复用已有安装 ${layout.venvDir}（ca-analyzer ${existing.version}）`,
        );
        onEvent({
          type: 'result',
          result: { ok: true, skipped: true, version: existing.version, installDir },
        });
        return finish({ ok: true });
      }
      if (existing && !depsOk) {
        logger?.warn?.('[installer:local] 现有 venv 缺少运行时依赖（clang/libclang/duckdb），将重建');
      }
      logger?.warn?.('[installer:local] 现有 venv 不可用，将重建');
      repair = true;
      try {
        fsImpl.rmSync(layout.venvDir, { recursive: true, force: true });
      } catch (removeError) {
        logger?.warn?.(`[installer:local] 删除损坏 venv 失败: ${removeError?.message || removeError}`);
      }
      const venvStep = findStep('venv');
      venvStep.status = 'pending';
      venvStep.startedAt = null;
      venvStep.detail = null;
      emitStep(venvStep);
    }
    startStep('venv', repair ? '重建损坏的 venv' : force ? '强制重建 venv' : '创建 venv');
    const venvResult = await run(python.command, [...python.args, '-m', 'venv', layout.venvDir], {
      timeoutMs: STEP_TIMEOUTS.venv,
    });
    assertCommand(venvResult, 'venv', `创建 venv（${python.label} -m venv）`, platformHints('venv'));
    if (!isVenvPythonPresent(layout.python)) {
      throw stepError('venv', 'VENV_FAILED', `venv 创建后未找到解释器: ${layout.python}`, {
        venvDir: layout.venvDir,
      });
    }
    succeedStep('venv', layout.venvDir);

    // -- dependencies --------------------------------------------------- //
    currentStep = 'dependencies';
    startStep('dependencies', offline ? '离线 pip install（--no-index）' : '在线 pip install');
    if (!offline) {
      const pipUpgrade = await run(
        layout.python,
        ['-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip'],
        { timeoutMs: STEP_TIMEOUTS.pip },
      );
      if (pipUpgrade?.code !== 0) {
        logger?.warn?.(
          `[installer:local] pip 自升级失败（继续安装）: exitCode=${pipUpgrade?.code ?? 'null'}`,
        );
      }
    }
    const pipArgs = ['-m', 'pip', 'install', '--disable-pip-version-check'];
    if (Array.isArray(installOptions.pipArgs)) {
      for (const arg of installOptions.pipArgs) pipArgs.push(String(arg));
    }
    if (offline) pipArgs.push('--no-index', '--find-links', wheelhouse.dir);
    pipArgs.push(wheel.path);
    const pipResult = await run(layout.python, pipArgs, { timeoutMs: STEP_TIMEOUTS.pip });
    assertCommand(pipResult, 'dependencies', 'pip install CA wheel', [
      ...platformHints('dependencies'),
      offline
        ? `wheelhouse 需包含 requirements.txt 的全部依赖（当前: ${wheelhouse.dir}）`
        : '在线安装失败请检查网络/PyPI 代理，或改用离线 wheelhouse',
    ]);
    succeedStep('dependencies', `pip install ${wheel.name}${offline ? '（离线）' : ''}`);

    // -- verify --------------------------------------------------------- //
    currentStep = 'verify';
    startStep('verify');
    const verifyResult = await run(layout.python, ['-m', 'ca', 'version', '--json'], {
      timeoutMs: STEP_TIMEOUTS.verify,
    });
    assertCommand(verifyResult, 'verify', 'ca version 校验', platformHints('verify'));
    const version = readCaVersionOutput(`${verifyResult.stdout || ''}\n${verifyResult.stderr || ''}`);
    if (!version) {
      throw stepError(
        'verify',
        'VERIFY_FAILED',
        `ca version 校验失败：无法解析输出（${
          String(verifyResult.stdout || verifyResult.stderr || '').slice(-500) || '(空)'
        }）`,
      );
    }
    const depsOk = await hasRuntimeDependencies(run, layout.python, STEP_TIMEOUTS.verify);
    if (!depsOk) {
      throw stepError(
        'verify',
        'RUNTIME_DEPS_MISSING',
        'CA 已安装但运行时依赖缺失（clang/libclang/duckdb）；重新安装/修复后端即可',
        { venvDir: layout.venvDir, probe: RUNTIME_DEPENDENCY_PROBE.join(' ') },
      );
    }
    const consoleOk = fsImpl.existsSync(layout.caScript);
    if (!consoleOk) {
      logger?.warn?.(
        `[installer:local] venv console script 不可用（${layout.caScript}）；将使用 python -m ca，不影响功能`,
      );
    }
    base.version = version;
    base.mode = offline ? 'offline' : 'online';
    succeedStep('verify', `ca-analyzer ${version.version}${consoleOk ? '' : '（python -m ca）'}`);
    logger?.info?.(
      `[installer:local] 安装完成 ${layout.venvDir} · ca-analyzer ${version.version}（${base.mode}）`,
    );
    onEvent({
      type: 'result',
      result: { ok: true, version: version.version, installDir, mode: base.mode },
    });
    return finish({ ok: true });
  } catch (error) {
    return failWithError(currentStep, error);
  }
}
