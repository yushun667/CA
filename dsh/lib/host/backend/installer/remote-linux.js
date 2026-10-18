/**
 * Remote Linux CA backend installer — wheel-centric.
 *
 * Default flow (`mode:'wheel'`):
 *   probe (ssh/Linux/Python>=3.11) → upload the bundled CA wheel (scp when
 *   available, else `ssh 'cat > file'` stdin) → offline: upload the wheelhouse
 *   (scp -r when available, else local gzip tar streamed into `tar -xzf -`)
 *   → remote `python -m venv` → `pip install` the wheel (online: PyPI deps;
 *   offline: `--no-index --find-links <wheelhouse>`) → `ca version --json`.
 *
 * The legacy source/rsync installer is still available with `mode:'source'`
 * (rsync a checkout + editable install) but is no longer the default: a Linux
 * host without network access only needs a CA wheel + wheelhouse now.
 *
 * Upload/tar/ssh command construction lives in this module as exported pure
 * functions so tests can assert argv/strategy selection without a real host.
 */

import { spawn as nodeSpawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

import { parseCaVersionJson } from '../ca-protocol.js';
import { resolveExecutableOnPath } from '../discover.js';
import { BackendError, describeCause, normalizeError } from '../errors.js';
import { buildChildEnv, runProcess as runProcessOneShot } from '../process.js';
import { buildSshArgv, classifySshFailure, normalizeSshConfig, remotePathExpression, shellQuote } from '../remote/commands.js';
import { performRemoteInstall as performSourceInstall } from '../remote/install.js';
import { probeRemote } from '../remote/probe.js';
import { nonEmptyString } from '../util.js';
import {
  BUNDLED_WHEEL_DIR,
  findBundledCaWheel,
  formatPythonVersion,
  isSupportedPython,
  parsePythonVersion,
  platformInstallHints,
  probePythonInterpreters,
  readCaVersionOutput,
  resolveWheelhouse,
} from './platform.js';

/** Step ids for the wheel install flow. */
export const REMOTE_WHEEL_STEP_DEFS = Object.freeze([
  { id: 'probe', title: '探测远程环境' },
  { id: 'python', title: '校验远端 Python ≥ 3.11' },
  { id: 'upload-wheel', title: '上传 CA wheel' },
  { id: 'upload-wheelhouse', title: '上传离线 wheelhouse' },
  { id: 'venv', title: '创建远端 venv' },
  { id: 'pip', title: '安装 CA 与依赖' },
  { id: 'verify', title: '校验 ca version' },
]);

const DEFAULT_REMOTE_INSTALL_DIR = '~/dsh-ca/backend';
const UPLOAD_TIMEOUT_MS = 15 * 60 * 1000;

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** Fresh remote wheel install steps. */
export function createRemoteWheelInstallSteps() {
  return REMOTE_WHEEL_STEP_DEFS.map(({ id, title }) => ({
    id,
    title,
    status: 'pending',
    detail: null,
    error: null,
    startedAt: null,
    finishedAt: null,
  }));
}

/** Remote install layout for display/scripts (remote POSIX paths). */
export function remoteInstallLayout(installDir) {
  const base = nonEmptyString(installDir) || DEFAULT_REMOTE_INSTALL_DIR;
  const venvDir = path.posix.join(base, 'venv');
  return {
    installDir: base,
    wheelDir: path.posix.join(base, 'wheels'),
    wheelhouseDir: path.posix.join(base, 'wheelhouse'),
    venvDir,
    venvPython: `${venvDir}/bin/python`,
    caScript: `${venvDir}/bin/ca`,
    runDir: path.posix.join(base, '.dsh-ca'),
  };
}

// ------------------------------------------------------------------------ //
// pure command builders
// ------------------------------------------------------------------------ //

/** scp option args (`-P` uppercase, unlike ssh `-p`). */
export function buildScpOptionArgs(ssh) {
  const cfg = normalizeSshConfig(ssh);
  const args = [];
  if (cfg.port) args.push('-P', String(cfg.port));
  if (cfg.keyPath) args.push('-i', cfg.keyPath);
  args.push(
    '-o',
    'BatchMode=yes',
    '-o',
    'ConnectTimeout=10',
    '-o',
    'ServerAliveInterval=15',
    '-o',
    'StrictHostKeyChecking=accept-new',
    '-o',
    'LogLevel=ERROR',
  );
  return args;
}

/**
 * scp argv uploading local sources to a remote target path.
 * @param {string|object} sshInput
 * @param {{sources: string[], remoteTarget: string, recursive?: boolean, scpBinary?: string}} options
 */
export function buildScpArgv(sshInput, options) {
  const cfg = normalizeSshConfig(sshInput);
  const sources = (options.sources || []).map((source) => String(source));
  if (sources.length === 0) throw new BackendError('INVALID_ARGUMENT', 'scp 源文件不能为空');
  const remoteTarget = nonEmptyString(options.remoteTarget);
  if (!remoteTarget) throw new BackendError('INVALID_ARGUMENT', 'scp 目标路径不能为空');
  const args = buildScpOptionArgs(sshInput);
  if (options.recursive === true) args.push('-r');
  args.push(...sources, `${cfg.target}:${remoteTarget}`);
  return { command: options.scpBinary || 'scp', args, ssh: cfg };
}

/**
 * Decide how to upload a file/directory:
 *   file: scp (preferred) → ssh cat stdin
 *   dir:  scp -r (preferred) → tar stream (local tar gzip → ssh tar -xzf -)
 *
 * @param {{kind: 'file'|'dir', scpAvailable: boolean, tarAvailable: boolean}} options
 * @returns {{strategy: 'scp'|'scp-recursive'|'ssh-cat'|'tar-stream'|'unsupported', reason: string}}
 */
export function planUploadStrategy(options) {
  if (options.kind === 'dir') {
    if (options.scpAvailable) {
      return { strategy: 'scp-recursive', reason: 'scp available' };
    }
    if (options.tarAvailable) {
      return { strategy: 'tar-stream', reason: 'scp unavailable; stream a local tar.gz through ssh' };
    }
    return { strategy: 'unsupported', reason: '需要 scp 或本地 tar 才能上传 wheelhouse 目录' };
  }
  if (options.scpAvailable) return { strategy: 'scp', reason: 'scp available' };
  return { strategy: 'ssh-cat', reason: 'scp unavailable; write through ssh stdin' };
}

/** tar command producing a gzip stream on stdout. */
export function buildTarCreateArgv(localDir) {
  return { command: 'tar', args: ['-czf', '-', '-C', String(localDir), '.'] };
}

/** Remote script for `cat > <file>` stdin uploads. */
export function buildStdinWriteScript(remotePath) {
  const target = remotePathExpression(remotePath);
  const parent = path.posix.dirname(String(remotePath));
  return [
    'set -e',
    `mkdir -p ${remotePathExpression(parent)}`,
    `cat > ${target}`,
    'echo "CA_UPLOAD_DONE=1"',
  ].join('\n');
}

/** Remote script extracting a gzip tar stream into a directory. */
export function buildTarExtractScript(remoteDir) {
  const dir = remotePathExpression(remoteDir);
  return [
    'set -e',
    `mkdir -p ${dir}`,
    `tar -xzf - -C ${dir}`,
    'echo "CA_EXTRACT_DONE=1"',
  ].join('\n');
}

/** Remote script creating the install directory layout. */
export function buildRemotePrepareScript({ installDir }) {
  const base = remotePathExpression(installDir || DEFAULT_REMOTE_INSTALL_DIR);
  return [
    'set -e',
    `CA_DIR=${base}`,
    'mkdir -p "$CA_DIR/wheels" "$CA_DIR/wheelhouse"',
    'echo "CA_DIR=$CA_DIR"',
  ].join('\n');
}

/** Remote script checking an arbitrary Python interpreter version. */
export function buildRemotePythonCheckScript({ python }) {
  const py = remotePathExpression(python);
  return ['set +e', `PY=${py}`, '"$PY" -V 2>&1', 'echo "CA_PY_CHECK_DONE=1"'].join('\n');
}

/** Remote script creating the venv inside the install directory. */
export function buildRemoteVenvCreateScript({ installDir, python }) {
  const base = remotePathExpression(installDir || DEFAULT_REMOTE_INSTALL_DIR);
  const py = remotePathExpression(python);
  return [
    'set -e',
    `CA_DIR=${base}`,
    `PY=${py}`,
    'mkdir -p "$CA_DIR"',
    '"$PY" -m venv "$CA_DIR/venv"',
    '"$CA_DIR/venv/bin/python" -V',
    'echo "CA_VENV_OK=1"',
  ].join('\n');
}

/**
 * Remote script installing the CA wheel into the venv.
 * @param {object} options
 * @param {string} options.installDir
 * @param {string} options.wheelName file name under `<installDir>/wheels/`
 * @param {boolean} [options.offline]
 * @param {string[]} [options.pipArgs] extra pip args
 */
export function buildRemotePipInstallScript(options) {
  const base = remotePathExpression(options.installDir || DEFAULT_REMOTE_INSTALL_DIR);
  const wheelName = nonEmptyString(options.wheelName);
  if (!wheelName) throw new BackendError('INVALID_ARGUMENT', 'wheelName 不能为空');
  const lines = ['set -e', `CA_DIR=${base}`, 'VP="$CA_DIR/venv/bin/python"'];
  lines.push('"$VP" -m pip install --disable-pip-version-check --upgrade pip');
  const pipArgs = ['-m', 'pip', 'install', '--disable-pip-version-check'];
  for (const arg of Array.isArray(options.pipArgs) ? options.pipArgs : []) pipArgs.push(String(arg));
  if (options.offline === true) pipArgs.push('--no-index', '--find-links', '"$CA_DIR/wheelhouse"');
  pipArgs.push(`"$CA_DIR/wheels/${shellQuote(wheelName)}"`);
  lines.push(`"$VP" ${pipArgs.join(' ')}`);
  lines.push('echo "CA_PIP_DONE=1"');
  return lines.join('\n');
}

/** Remote script verifying `ca version` and the console script. */
export function buildRemoteVerifyScript({ installDir }) {
  const base = remotePathExpression(installDir || DEFAULT_REMOTE_INSTALL_DIR);
  return [
    'set -e',
    `CA_DIR=${base}`,
    'VP="$CA_DIR/venv/bin/python"',
    'echo "CA_PY_VERSION=$("$VP" -V 2>&1)"',
    '"$VP" -m ca version --json',
    'if [ -x "$CA_DIR/venv/bin/ca" ]; then echo "CA_CONSOLE_OK=1"; else echo "CA_CONSOLE_OK=0"; fi',
    'echo "CA_VERIFY_DONE=1"',
  ].join('\n');
}

// ------------------------------------------------------------------------ //
// upload transfer layer
// ------------------------------------------------------------------------ //

/**
 * Default transfer implementation (scp preferred, ssh stdin / tar stream
 * fallbacks). Tests inject `deps.transfer` to bypass real processes.
 *
 * @param {{deps?: object, logger?: any}} [options]
 */
export function createTransfer(options = {}) {
  const deps = options.deps || {};
  const logger = options.logger || null;
  const env = deps.env || process.env;
  const spawnFn = deps.spawn || nodeSpawn;
  const runProcessFn =
    typeof deps.runProcess === 'function'
      ? deps.runProcess
      : (runOptions) => runProcessOneShot({ spawn: spawnFn, ...runOptions });
  const sshBinary = deps.sshBinary || 'ssh';
  const scpBinary = deps.scpBinary || 'scp';
  const scpAvailable =
    deps.scpAvailable !== undefined ? Boolean(deps.scpAvailable) : Boolean(resolveExecutableOnPath(scpBinary, { env }));
  const tarAvailable =
    deps.tarAvailable !== undefined ? Boolean(resolveExecutableOnPath('tar', { env })) : Boolean(resolveExecutableOnPath('tar', { env }));

  async function runSsh(ssh, script, label) {
    const built = buildSshArgv(ssh, script, { sshBinary });
    logger?.info?.(`[installer:remote] ${label}: ssh ${built.ssh.target}`);
    return runProcessFn({
      command: built.command,
      args: built.args,
      timeoutMs: UPLOAD_TIMEOUT_MS,
      logger,
      maxOutputBytes: 256 * 1024,
    });
  }

  async function uploadFile({ ssh, localFile, remotePath, signal }) {
    const selection = planUploadStrategy({ kind: 'file', scpAvailable, tarAvailable });
    if (selection.strategy === 'scp') {
      const built = buildScpArgv(ssh, {
        sources: [localFile],
        remoteTarget: remotePath,
        scpBinary,
      });
      const result = await runProcessFn({
        command: built.command,
        args: built.args,
        timeoutMs: UPLOAD_TIMEOUT_MS,
        signal,
        logger,
        maxOutputBytes: 64 * 1024,
      });
      if (result?.code !== 0 || result?.spawnError) {
        throw new BackendError(
          'INSTALL_STEP_FAILED',
          `scp 上传失败：${String(result?.stderr || result?.spawnError?.message || '').trim().slice(-1000)}`,
          { localFile, remotePath },
        );
      }
      return { strategy: 'scp' };
    }
    if (selection.strategy === 'ssh-cat') {
      await uploadFileViaStdin({ spawnFn, ssh, sshBinary, localFile, remotePath, env, logger });
      return { strategy: 'ssh-cat' };
    }
    throw new BackendError('INSTALL_STEP_FAILED', selection.reason);
  }

  async function uploadDirectory({ ssh, localDir, remoteDir, signal }) {
    const selection = planUploadStrategy({ kind: 'dir', scpAvailable, tarAvailable });
    if (selection.strategy === 'scp-recursive') {
      const parent = path.posix.dirname(remoteDir);
      const base = path.basename(localDir);
      const staged = path.posix.join(parent, base);
      const built = buildScpArgv(ssh, {
        sources: [localDir],
        remoteTarget: parent,
        recursive: true,
        scpBinary,
      });
      const result = await runProcessFn({
        command: built.command,
        args: built.args,
        timeoutMs: UPLOAD_TIMEOUT_MS,
        signal,
        logger,
        maxOutputBytes: 128 * 1024,
      });
      if (result?.code !== 0 || result?.spawnError) {
        throw new BackendError(
          'INSTALL_STEP_FAILED',
          `scp -r 上传失败：${String(result?.stderr || result?.spawnError?.message || '').trim().slice(-1000)}`,
          { localDir, remoteDir },
        );
      }
      if (path.posix.normalize(staged) !== path.posix.normalize(remoteDir)) {
        const script = [
          'set -e',
          `rm -rf ${remotePathExpression(remoteDir)}`,
          `mv ${remotePathExpression(staged)} ${remotePathExpression(remoteDir)}`,
          'echo "CA_UPLOAD_DONE=1"',
        ].join('\n');
        const moved = await runSsh(ssh, script, 'move uploaded wheelhouse');
        if (moved?.code !== 0) throw new BackendError('INSTALL_STEP_FAILED', '重置远端 wheelhouse 目录失败');
      }
      return { strategy: 'scp-recursive' };
    }
    if (selection.strategy === 'tar-stream') {
      await uploadDirectoryViaTarStream({ spawnFn, ssh, sshBinary, localDir, remoteDir, env, logger });
      return { strategy: 'tar-stream' };
    }
    throw new BackendError('INSTALL_STEP_FAILED', selection.reason);
  }

  return { scpAvailable, tarAvailable, uploadFile, uploadDirectory };
}

function collectOutput(child) {
  const chunks = [];
  for (const stream of [child.stdout, child.stderr]) {
    if (stream && typeof stream.on === 'function') {
      stream.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    }
  }
  return () => Buffer.concat(chunks).toString('utf8');
}

function waitForExit(child) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    child.on('error', (error) => done({ code: null, signal: null, error }));
    child.on('exit', (code, signal) => done({ code, signal, error: null }));
  });
}

async function uploadFileViaStdin(options) {
  const { spawnFn, ssh, sshBinary, localFile, remotePath, env, logger } = options;
  const script = buildStdinWriteScript(remotePath);
  const built = buildSshArgv(ssh, script, { sshBinary });
  logger?.info?.(`[installer:remote] ssh 上传 ${localFile} → ${ssh.target}:${remotePath}`);
  const child = spawnFn(built.command, built.args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: buildChildEnv({}, env),
  });
  const output = collectOutput(child);
  const exitPromise = waitForExit(child);
  await new Promise((resolve, reject) => {
    const source = fs.createReadStream(localFile);
    source.on('error', reject);
    source.on('end', resolve);
    source.pipe(child.stdin);
  });
  try {
    child.stdin.end();
  } catch {
    // stdin may already be closed by the remote side
  }
  const info = await exitPromise;
  if (info.error || info.code !== 0) {
    throw new BackendError(
      'INSTALL_STEP_FAILED',
      `ssh 上传失败（${localFile}）：${info.error ? describeCause(info.error) : output().trim().slice(-1000)}`,
      { localFile, remotePath },
    );
  }
}

async function uploadDirectoryViaTarStream(options) {
  const { spawnFn, ssh, sshBinary, localDir, remoteDir, env, logger } = options;
  const tarArgv = buildTarCreateArgv(localDir);
  const script = buildTarExtractScript(remoteDir);
  const built = buildSshArgv(ssh, script, { sshBinary });
  logger?.info?.(`[installer:remote] tar 流上传 ${localDir} → ${ssh.target}:${remoteDir}`);
  const tar = spawnFn(tarArgv.command, tarArgv.args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: buildChildEnv({}, env),
  });
  const sshChild = spawnFn(built.command, built.args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: buildChildEnv({}, env),
  });
  const tarOutput = collectOutput(tar);
  const sshOutput = collectOutput(sshChild);
  const tarExit = waitForExit(tar);
  const sshExit = waitForExit(sshChild);
  if (tar.stdout && sshChild.stdin) {
    tar.stdout.pipe(sshChild.stdin);
  }
  const [tarInfo, sshInfo] = await Promise.all([tarExit, sshExit]);
  if (tarInfo.error || tarInfo.code !== 0) {
    throw new BackendError(
      'INSTALL_STEP_FAILED',
      `本地 tar 打包失败：${tarInfo.error ? describeCause(tarInfo.error) : tarOutput().trim().slice(-1000)}`,
    );
  }
  if (sshInfo.error || sshInfo.code !== 0) {
    throw new BackendError(
      'INSTALL_STEP_FAILED',
      `远端解包失败：${sshInfo.error ? describeCause(sshInfo.error) : sshOutput().trim().slice(-1000)}`,
      { remoteDir },
    );
  }
}

// ------------------------------------------------------------------------ //
// install flow
// ------------------------------------------------------------------------ //

/**
 * Remote install entry point.
 *
 * `installOptions.mode === 'source'` keeps the legacy rsync/editable flow;
 * everything else uses the wheel flow.
 *
 * @param {object} options
 * @param {Record<string, any>} options.config resolved full config
 * @param {ReturnType<import('../remote/exec.js').createRemoteExecutor>} options.executor
 * @param {object} [options.deps]
 * @param {any} [options.logger]
 * @param {object} [options.installOptions]
 * @param {(event: object) => void} [options.onEvent]
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<Record<string, any>>} install report (never throws)
 */
export async function performRemoteWheelInstall(options) {
  const { config, executor, logger } = options;
  const deps = options.deps || {};
  const installOptions = options.installOptions || {};
  const onEvent = typeof options.onEvent === 'function' ? options.onEvent : () => {};
  const mode = installOptions.mode === 'source' ? 'source' : 'wheel';
  if (mode === 'source') {
    const sourceReport = await performSourceInstall({
      config,
      executor,
      logger,
      installOptions,
      onEvent,
      signal: options.signal,
      localCwd: deps.cwd,
    });
    return adaptSourceReport(sourceReport);
  }

  const remote = config.remote || {};
  const offline = installOptions.offline === true;
  const steps = createRemoteWheelInstallSteps();
  const startedAt = Date.now();
  const transfer = deps.transfer || createTransfer({ deps, logger });

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
    scope: 'remote',
    remote: true,
    mode: 'wheel',
    offline,
    platform: null,
    installDir: null,
    venvDir: null,
    venvPython: null,
    caScript: null,
    wheel: null,
    wheelhouse: null,
    version: null,
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
  function hints(step) {
    return platformInstallHints('linux', { step, offline, platformId: base.platform?.platformId });
  }
  function stepError(stepId, code, message, extra = {}) {
    const hintList = Array.isArray(extra.hints) && extra.hints.length > 0 ? extra.hints : hints(stepId);
    return new BackendError(code, message, { step: stepId, ...extra, hints: hintList });
  }
  function failWithError(stepId, error) {
    const normalized = normalizeError(error, 'INSTALL_STEP_FAILED', '远程 CA 后端安装失败');
    failStep(stepId || normalized.details?.step || currentStepId(), normalized);
    logger?.error?.(`[installer:remote] ${stepId || 'install'} 失败：${normalized.message}`);
    return finish({ ok: false, error: normalized.toJSON() });
  }
  async function runSsh(stepId, context, script, timeoutMs = 120000) {
    const result = await executor.runSsh(ssh, script, {
      timeoutMs,
      signal: options.signal,
      fallbackSsh: remote.ssh,
      label: context,
      maxOutputBytes: 512 * 1024,
      onStdoutLine: (line) => emitLog('stdout', line),
      onStderrLine: (line) => emitLog('stderr', line),
    });
    if (result?.cancelled) throw stepError(stepId, 'CANCELLED', `${context} 已取消`, { hints: [] });
    if (result?.timedOut) {
      throw stepError(stepId, 'INSTALL_STEP_FAILED', `${context} 超时`, {
        timeout: true,
        tail: String(result.stderr || '').slice(-2000),
      });
    }
    if (result?.spawnError || result?.code !== 0) {
      const failure = classifySshFailure(result);
      const tail = `${result?.stderr || ''}\n${result?.stdout || ''}`.trim().slice(-4000);
      throw stepError(stepId, failure.code === 'SSH_FAILED' ? 'INSTALL_STEP_FAILED' : failure.code, `${context} 失败：${failure.message}`, {
        tail,
        hint: failure.hint,
      });
    }
    return result;
  }

  let ssh;
  let effectiveConfig;
  try {
    ssh = normalizeSshConfig(
      installOptions.ssh !== undefined ? installOptions.ssh : remote.ssh,
      remote.ssh,
    );
    effectiveConfig = { ...config, remote: { ...remote, ssh } };
  } catch (sshError) {
    const normalized = normalizeError(sshError, 'BACKEND_NOT_CONFIGURED', '远程 SSH 配置无效');
    startStep('probe');
    failStep('probe', normalized);
    return finish({ ok: false, error: normalized.toJSON() });
  }
  try {
    // -- probe ---------------------------------------------------------- //
    startStep('probe');
    const probe = await probeRemote({ config: effectiveConfig, executor, logger, timeoutMs: 60000 });
    const critical = (probe.checks || []).filter(
      (check) => ['ssh', 'os', 'python3'].includes(check.name) && !check.ok,
    );
    if (!probe.ok && critical.length > 0) {
      const failure = probe.error || { code: 'PROBE_FAILED', message: '远程环境不满足安装条件' };
      throw stepError('probe', failure.code, failure.message, {
        failedChecks: critical.map((check) => check.name),
        hints: probe.hints || [],
      });
    }
    const remotePlatformId =
      probe.os?.arch === 'aarch64' ? 'linux-arm64' : probe.os?.arch === 'x86_64' ? 'linux-x64' : null;
    base.platform = {
      os: probe.platform || 'linux',
      name: probe.os?.name || 'Linux',
      arch: probe.os?.arch || null,
      platformId: remotePlatformId,
      label: `Linux ${probe.os?.arch || ''}`.trim(),
    };
    succeedStep('probe', `${base.platform.label} · ssh ${probe.ssh?.target || ''}`);

    // -- python --------------------------------------------------------- //
    startStep('python');
    const configuredPython = nonEmptyString(installOptions.python) || nonEmptyString(remote.python);
    let basePython = configuredPython || nonEmptyString(probe.python?.system) || 'python3';
    if (configuredPython) {
      const check = await runSsh(
        'python',
        '读取远端 Python 版本',
        buildRemotePythonCheckScript({ python: configuredPython }),
        30000,
      );
      const version = parsePythonVersion(`${check.stdout || ''}\n${check.stderr || ''}`);
      if (!version || !isSupportedPython(version)) {
        throw stepError(
          'python',
          'PYTHON_VERSION_UNSUPPORTED',
          `远端 Python ${configuredPython} 版本不满足要求（需要 >= 3.11${version ? `，当前 ${formatPythonVersion(version)}` : ''}）`,
          { python: configuredPython, version: version ? formatPythonVersion(version) : null },
        );
      }
      basePython = configuredPython;
      succeedStep('python', `${configuredPython} · Python ${formatPythonVersion(version)}`);
    } else {
      const version = parsePythonVersion(probe.python?.version || '');
      if (!version || !isSupportedPython(version)) {
        throw stepError(
          'python',
          'PYTHON_VERSION_UNSUPPORTED',
          `远端系统 Python 版本过低或不明确（${probe.python?.version || '未知'}）；请安装 Python >= 3.11 或设置 remote.python`,
          { python: probe.python?.system || null },
        );
      }
      succeedStep('python', `${probe.python.system} · Python ${formatPythonVersion(version)}`);
    }

    // -- wheel / wheelhouse resolution --------------------------------- //
    const installDir = nonEmptyString(installOptions.installDir) || nonEmptyString(remote.installDir) || DEFAULT_REMOTE_INSTALL_DIR;
    const layout = remoteInstallLayout(installDir);
    base.installDir = layout.installDir;
    base.venvDir = layout.venvDir;
    base.venvPython = layout.venvPython;
    base.caScript = layout.caScript;
    const localWheel = findBundledCaWheel({ dir: deps.bundledWheelDir || BUNDLED_WHEEL_DIR });
    if (!localWheel) {
      throw stepError('upload-wheel', 'WHEEL_NOT_FOUND', '插件内置 CA wheel 缺失（安装包不完整）');
    }
    base.wheel = { name: localWheel.name, version: localWheel.version, bytes: localWheel.bytes };
    let localWheelhouse = null;
    if (offline) {
      localWheelhouse = resolveWheelhouse({
        explicitPath: installOptions.wheelhousePath,
        stateDir: deps.paths?.stateDir,
        platformId: remotePlatformId,
      });
      if (!localWheelhouse.exists) {
        throw stepError(
          'upload-wheelhouse',
          'WHEELHOUSE_NOT_FOUND',
          `离线安装需要 wheelhouse（当前 ${localWheelhouse.path || '(未找到)'}），请先用 dsh/scripts/build-wheelhouse.mjs --platform ${
            remotePlatformId || 'linux-x64'
          } 生成`,
          { wheelhousePath: localWheelhouse.path, platformId: remotePlatformId },
        );
      }
      base.wheelhouse = {
        kind: localWheelhouse.kind,
        path: localWheelhouse.dir || localWheelhouse.path,
        wheelCount: localWheelhouse.wheelCount,
        caWheel: localWheelhouse.caWheel,
        platform: localWheelhouse.platform,
      };
    }

    // -- prepare + upload wheel ---------------------------------------- //
    startStep('upload-wheel', localWheel.name);
    await runSsh('upload-wheel', '创建远端安装目录', buildRemotePrepareScript({ installDir }), 60000);
    const uploadWheel = await transfer.uploadFile({
      ssh,
      localFile: localWheel.path,
      remotePath: path.posix.join(layout.wheelDir, localWheel.name),
      signal: options.signal,
    });
    succeedStep('upload-wheel', `${localWheel.name}（${uploadWheel?.strategy || 'upload'}）`);

    // -- wheelhouse upload ---------------------------------------------- //
    startStep('upload-wheelhouse');
    if (!offline) {
      skipStep('upload-wheelhouse', '在线安装无需 wheelhouse');
    } else {
      const uploadDir = await transfer.uploadDirectory({
        ssh,
        localDir: localWheelhouse.dir || localWheelhouse.path,
        remoteDir: layout.wheelhouseDir,
        signal: options.signal,
      });
      succeedStep(
        'upload-wheelhouse',
        `${localWheelhouse.wheelCount} wheels（${uploadDir?.strategy || 'upload'}）`,
      );
    }

    // -- venv ----------------------------------------------------------- //
    startStep('venv');
    await runSsh(
      'venv',
      '创建远端 venv',
      buildRemoteVenvCreateScript({ installDir, python: basePython }),
      5 * 60 * 1000,
    );
    succeedStep('venv', layout.venvDir);

    // -- pip ------------------------------------------------------------ //
    startStep('pip', offline ? '离线 pip install（--no-index）' : '在线 pip install');
    const pipRun = await runSsh(
      'pip',
      '安装 CA wheel 与依赖',
      buildRemotePipInstallScript({
        installDir,
        wheelName: localWheel.name,
        offline,
        pipArgs: Array.isArray(installOptions.pipArgs) ? installOptions.pipArgs : [],
      }),
      30 * 60 * 1000,
    );
    if (!/CA_PIP_DONE=1/.test(pipRun.stdout || '')) {
      logger?.warn?.('[installer:remote] pip 脚本缺少完成标记（继续校验）');
    }
    succeedStep('pip', `pip install ${localWheel.name}${offline ? '（离线）' : ''}`);

    // -- verify --------------------------------------------------------- //
    startStep('verify');
    const verifyRun = await runSsh(
      'verify',
      '校验远端 ca version',
      buildRemoteVerifyScript({ installDir }),
      60000,
    );
    const version = readCaVersionOutput(`${verifyRun.stdout || ''}\n${verifyRun.stderr || ''}`);
    if (!version) {
      throw stepError(
        'verify',
        'VERIFY_FAILED',
        `远端 ca version 校验失败：无法解析输出（${String(verifyRun.stdout || verifyRun.stderr || '').slice(-500) || '(空)'}）`,
      );
    }
    const consoleOk = /CA_CONSOLE_OK=1/.test(verifyRun.stdout || '');
    base.version = version;
    succeedStep('verify', `ca-analyzer ${version.version}${consoleOk ? '' : '（python -m ca）'}`);
    logger?.info?.(
      `[installer:remote] 远端安装完成 ${layout.installDir}（ca-analyzer ${version.version}，${offline ? '离线' : '在线'}）`,
    );
    onEvent({
      type: 'result',
      result: {
        ok: true,
        mode: 'wheel',
        offline,
        installDir: layout.installDir,
        version: version.version,
      },
    });
    return finish({ ok: true, repoPath: layout.installDir });
  } catch (error) {
    return failWithError(currentStepId(), error);
  }
}

/** Adapt the legacy source-install report to the unified install report shape. */
export function adaptSourceReport(sourceReport) {
  const report = sourceReport || {};
  const venvPath = report.venvPath || (report.repoPath ? `${report.repoPath}/.venv` : null);
  return {
    ok: report.ok === true,
    scope: 'remote',
    remote: true,
    mode: 'source',
    offline: false,
    installDir: report.repoPath || null,
    repoPath: report.repoPath || null,
    venvDir: venvPath,
    venvPython: venvPath ? `${venvPath}/bin/python` : null,
    caScript: venvPath ? `${venvPath}/bin/ca` : null,
    version: report.version || null,
    skipped: report.skipped === true,
    steps: Array.isArray(report.steps) ? report.steps : [],
    startedAt: report.startedAt || Date.now(),
    finishedAt: report.finishedAt || Date.now(),
    error: report.error || null,
    probed: report.probed || null,
  };
}

// Keep imports referenced for callers/tests that import via this module.
export { probePythonInterpreters };
