/**
 * Remote Linux environment probe.
 *
 * One ssh round trip runs a marker script that inspects the OS, Python/pip,
 * rsync, the configured CA checkout, venv state, database paths, libclang and
 * free disk. The result is a structured report (never a throw for environment
 * problems) that the settings UI renders as checks/hints.
 */

import { describeCaVersion, parseCaVersionJson } from '../ca-protocol.js';
import { BackendError } from '../errors.js';
import { formatBytes, nonEmptyString } from '../util.js';
import {
  buildProbeScript,
  classifySshFailure,
  normalizeSshConfig,
  outputTailOf,
  parseProbeOutput,
} from './commands.js';

const DISK_WARN_BYTES = 512 * 1024 * 1024;
const DEFAULT_PROBE_TIMEOUT_MS = 20000;

/**
 * @param {object} options
 * @param {Record<string, any>} options.config resolved full config
 * @param {ReturnType<import('./exec.js').createRemoteExecutor>} options.executor
 * @param {any} [options.logger]
 * @param {string|object|null} [options.sshOverride]
 * @param {number} [options.timeoutMs]
 */
export async function probeRemote(options) {
  const config = options.config;
  const remote = config.remote || {};
  const logger = options.logger || null;

  let ssh;
  try {
    ssh = normalizeSshConfig(options.sshOverride ?? remote.ssh, remote.ssh);
  } catch (error) {
    const message =
      error instanceof BackendError ? error.message : '远程 SSH 配置无效';
    return failureReport({
      ssh: null,
      error: { code: 'BACKEND_NOT_CONFIGURED', message },
      hints: ['请先在设置中配置 remote.ssh.host（ssh alias 或 user@host）。'],
    });
  }

  const repoPath = nonEmptyString(remote.repoPath);
  const installDir = nonEmptyString(remote.installDir);
  const script = buildProbeScript({
    repoPath,
    installDir,
    python: nonEmptyString(remote.python),
    db: nonEmptyString(remote.db),
    dbDir: nonEmptyString(remote.dbDir),
    env: remote.env,
  });

  const result = await options.executor.runSsh(ssh, script, {
    timeoutMs: options.timeoutMs || DEFAULT_PROBE_TIMEOUT_MS,
    fallbackSsh: remote.ssh,
    label: 'probe remote environment',
    maxOutputBytes: 256 * 1024,
    onStdoutLine: (line) => logger?.info?.(`[remote] probe: ${line}`),
    onStderrLine: (line) => logger?.warn?.(`[remote] probe stderr: ${line}`),
  });

  const sshFailed =
    result.spawnError || result.timedOut || result.cancelled || result.code !== 0;
  if (sshFailed) {
    const failure = classifySshFailure(result);
    return failureReport({
      ssh,
      error: failure,
      hints: [failure.hint].filter(Boolean),
      rawOutput: outputTailOf(result, 6),
    });
  }

  const parsed = parseProbeOutput(result.stdout);
  const markers = parsed.markers;
  if (markers.begin !== '1') {
    return failureReport({
      ssh,
      error: {
        code: 'PROBE_FAILED',
        message: `远程探测脚本没有返回预期标记（exitCode=${result.code}）；输出: ${outputTailOf(result, 4) || '(空)'}`,
      },
      hints: ['在远端手动执行 sh -c "<probe script>" 排查 shell 环境'],
      rawOutput: outputTailOf(result, 6),
    });
  }

  // -- build report ----------------------------------------------------- //
  const os = nonEmptyString(markers.os);
  const isLinux = os === 'Linux';
  const systemPython = nonEmptyString(markers.python3);
  const pythonVersion = nonEmptyString(markers.python3_version);
  const pip = nonEmptyString(markers.pip);
  const rsync = nonEmptyString(markers.rsync);
  const caOnPath = nonEmptyString(markers.ca_on_path);
  const nproc = Number(markers.nproc) || null;
  const repoDir = markers.repo_dir === '1';
  const repoManifest = markers.repo_manifest === '1';
  const repoCa = markers.repo_ca === '1';
  const repoWritable = markers.repo_writable === '1';
  const venvPython = markers.venv_python === '1';
  const venvCa = markers.venv_ca === '1';
  const installExists = markers.install_dir === '1';
  const installParentWritable = markers.install_parent_writable === '1';
  const dbFile = markers.db_file === '1';
  const dbBytes = Number(markers.db_bytes) || null;
  const dbDir = markers.db_dir === '1';
  const dbShards = Number(markers.db_shards) || 0;
  const pythonCandidates = ['313', '312', '311']
    .map((suffix) => ({
      path: nonEmptyString(markers[`python${suffix}`]),
      version: nonEmptyString(markers[`python${suffix}_version`]),
    }))
    .filter((candidate) => candidate.path);
  const versionAtLeast311 = (text) => {
    const match = /Python (\d+)\.(\d+)/.exec(String(text || ''));
    if (!match) return false;
    const major = Number(match[1]);
    const minor = Number(match[2]);
    return major > 3 || (major === 3 && minor >= 11);
  };
  const preferredPython = pythonCandidates.find((candidate) => versionAtLeast311(candidate.version)) || null;
  const pythonUsed = nonEmptyString(markers.python_used);
  const libclangChecked = Boolean(pythonUsed);
  const libclangOk = markers.libclang === 'ok';
  const diskKb = Number(markers.disk_kb) || null;
  const diskAvailableBytes = diskKb !== null ? diskKb * 1024 : null;
  const versionRecord = parseCaVersionJson(markers.ca_version_json || '');
  const ca = versionRecord
    ? {
        version: versionRecord.version || null,
        schemaVersion: versionRecord.schema_version ?? null,
        python: versionRecord.python || null,
        platform: versionRecord.platform || null,
        description: describeCaVersion(versionRecord),
      }
    : null;

  /** @type {Array<{name: string, ok: boolean, required: boolean, value?: unknown, detail?: string}>} */
  const checks = [];
  /** @type {string[]} */
  const hints = [];
  /** @type {string[]} */
  const warnings = [];

  checks.push({ name: 'ssh', ok: true, required: true, value: ssh.target, detail: 'SSH 连接成功' });
  checks.push({
    name: 'os',
    ok: isLinux,
    required: true,
    value: os || null,
    detail: isLinux ? `Linux (${markers.arch || 'unknown'}, ${nproc || '?'} cpus)` : `远端不是 Linux（${os || 'unknown'}）；CA 远程后端要求 Linux`,
  });
  const pythonOk = Boolean(systemPython) || markers.python_configured === '1';
  const systemPythonVersion = (() => {
    const match = /Python (\d+)\.(\d+)/.exec(String(pythonVersion || ''));
    return match ? { major: Number(match[1]), minor: Number(match[2]) } : null;
  })();
  const systemPythonSupported =
    systemPythonVersion === null ||
    (systemPythonVersion.major > 3 ||
      (systemPythonVersion.major === 3 && systemPythonVersion.minor >= 11));
  checks.push({
    name: 'python3',
    ok: pythonOk,
    required: true,
    value: systemPython || remote.python || null,
    detail: pythonVersion || (systemPython ? 'python3 可用' : '未找到 python3'),
  });
  if (!systemPythonSupported && !pythonUsed) {
    warnings.push(
      `远端系统 python3 版本为 ${pythonVersion || '?'}，CA 需要 >= 3.11；请安装 python3.11+ 或配置 remote.python 指向 3.11+ 的解释器/venv。`,
    );
  }
  checks.push({
    name: 'pip',
    ok: Boolean(pip),
    required: false,
    value: pip,
    detail: pip ? 'pip 可用' : '未找到 pip3/pip（远程安装时会失败）',
  });
  checks.push({
    name: 'rsync',
    ok: Boolean(rsync),
    required: false,
    value: rsync,
    detail: rsync ? 'rsync 可用' : '未找到 rsync（从本机同步源码时需要）',
  });
  if (repoPath) {
    checks.push({
      name: 'repo',
      ok: repoDir && repoCa,
      required: true,
      value: repoPath,
      detail: repoDir
        ? repoCa
          ? `CA 检出可用${repoWritable ? '（可写）' : '（只读）'}`
          : '目录存在但不像 CA 仓库（缺少 pyproject.toml 或 name="ca-analyzer"）'
        : '配置的 remote.repoPath 在远端不存在',
    });
  }
  if (!repoPath) {
    checks.push({
      name: 'install-dir',
      ok: Boolean(installDir) && (installExists || installParentWritable),
      required: true,
      value: installDir || null,
      detail: installDir
        ? installExists
          ? '安装目录已存在'
          : installParentWritable
            ? '安装目录父级可写'
            : '安装目录父级不可写'
        : '未配置 remote.repoPath 或 remote.installDir，无法确定远端 CA 位置',
    });
  }
  const dbConfigured = Boolean(remote.db || remote.dbDir);
  const dbOk = remote.db ? dbFile : remote.dbDir ? dbDir && dbShards > 0 : false;
  checks.push({
    name: 'database',
    ok: dbConfigured && dbOk,
    required: true,
    value: remote.db || remote.dbDir || null,
    detail: remote.db
      ? dbFile
        ? `数据库存在（${formatBytes(dbBytes || 0)}）`
        : 'remote.db 在远端不存在'
      : remote.dbDir
        ? dbDir
          ? `分片目录存在（${dbShards} 个 *.db）`
          : 'remote.dbDir 在远端不存在'
        : '未配置 remote.db / remote.dbDir',
  });
  checks.push({
    name: 'libclang',
    ok: libclangChecked ? libclangOk : null,
    required: false,
    value: venvPython ? libclangOk : null,
    detail: libclangChecked
      ? libclangOk
        ? 'libclang 可用（索引/deep 分析可用）'
        : '未检测到 libclang（查询可用；索引/ensure_deep/prewarm 不可用）'
      : '远端未安装 CA venv，跳过 libclang 检查（查询不需要 libclang）',
  });
  checks.push({
    name: 'disk',
    ok: diskAvailableBytes === null ? null : diskAvailableBytes >= DISK_WARN_BYTES,
    required: false,
    value: diskAvailableBytes,
    detail: diskAvailableBytes === null ? '无法读取磁盘容量' : `可用 ${formatBytes(diskAvailableBytes)}`,
  });
  checks.push({
    name: 'ca-version',
    ok: ca !== null,
    required: false,
    value: ca?.version || null,
    detail: ca
      ? `${ca.description || `ca-analyzer ${ca.version}`}（${pythonUsed ? `运行于 ${pythonUsed}` : venvCa ? 'console script 可用' : 'venv python 可运行'}）`
      : caOnPath
        ? `PATH 中有 ca（${caOnPath}），但无法验证版本`
        : '远端尚未安装可运行的 CA（可执行远程安装）',
  });

  if (!isLinux) hints.push('远程后端要求 Linux 主机；macOS/Windows 请使用本地后端。');
  if (!pythonOk) hints.push('安装 Python 3.11+（sudo apt install python3 python3-venv python3-pip）。');
  if (pythonOk && !pip) hints.push('安装 pip：sudo apt install python3-pip（或 python3 -m ensurepip）。');
  if (!rsync) warnings.push('未找到 rsync：从本机同步源码前需要安装 rsync（sudo apt install rsync）。');
  if (repoPath && !(repoDir && repoCa)) {
    hints.push(
      'remote.repoPath 不可用：改为远端已有 CA 检出的绝对路径，或清空该配置并设置 remote.installDir 后调用远程安装。',
    );
  }
  if (!repoPath && !installDir) {
    hints.push('设置 remote.repoPath（已有远端 CA 检出）或 remote.installDir（安装目标目录）。');
  }
  if (!dbConfigured) {
    hints.push('设置 remote.db（远端单库）或 remote.dbDir（远端分片目录），或先在远端运行 ca index。');
  } else if (!dbOk) {
    hints.push('远端数据库路径不可用：确认文件/分片存在，或先在远端运行 ca index。');
  }
  if (libclangChecked && !libclangOk) {
    warnings.push(
      '远端 CA venv 未检测到 libclang：查询/UI 可用；索引与 ensure_deep/prewarm 需要 libclang（pip 已含 libclang wheel，或设置 CA_LIBCLANG_LIBRARY）。',
    );
  }
  if (diskAvailableBytes !== null && diskAvailableBytes < DISK_WARN_BYTES) {
    warnings.push(`远端磁盘可用空间不足 ${formatBytes(DISK_WARN_BYTES)}（当前 ${formatBytes(diskAvailableBytes)}）。`);
  }
  if (!ca && (repoDir || installExists)) {
    hints.push('远端 CA 尚未安装/可运行：可执行远程安装（rsync → venv → pip install -r → pip install -e .）。');
  }

  const requiredOk = checks.filter((check) => check.required).every((check) => check.ok);
  return {
    ok: requiredOk,
    target: 'remote',
    platform: isLinux ? 'linux' : os ? os.toLowerCase() : null,
    ssh: {
      target: ssh.target,
      host: ssh.host,
      user: ssh.user,
      port: ssh.port,
      keyPath: ssh.keyPath,
    },
    os: {
      name: os,
      arch: nonEmptyString(markers.arch),
      cpus: nproc,
      home: nonEmptyString(markers.home),
    },
    python: {
      system: systemPython,
      version: pythonVersion,
      configured: remote.python || null,
      supported: systemPythonSupported || Boolean(pythonUsed),
      preferred: preferredPython ? { path: preferredPython.path, version: preferredPython.version } : null,
      venv: pythonUsed || (venvPython ? `${repoPath}/.venv/bin/python` : null),
      caPython: ca?.python || null,
    },
    pip,
    rsync,
    caOnPath,
    ca,
    paths: {
      repoPath: repoPath
        ? { path: repoPath, exists: repoDir, isCaRepo: repoCa, writable: repoWritable }
        : null,
      installDir: installDir
        ? { path: installDir, exists: installExists, parentWritable: installParentWritable }
        : null,
      db: remote.db
        ? { path: remote.db, exists: dbFile, bytes: dbBytes }
        : null,
      dbDir: remote.dbDir
        ? { path: remote.dbDir, exists: dbDir, shards: dbShards }
        : null,
    },
    disk: {
      path: repoPath || installDir || nonEmptyString(markers.home) || null,
      availableBytes: diskAvailableBytes,
      availableText: diskAvailableBytes === null ? null : formatBytes(diskAvailableBytes),
    },
    libclang: {
      checked: libclangChecked,
      available: libclangChecked ? libclangOk : null,
    },
    checks,
    hints,
    warnings,
    error: null,
    scannedAt: Date.now(),
  };
}

function failureReport({ ssh, error, hints = [], rawOutput = '' }) {
  return {
    ok: false,
    target: 'remote',
    platform: null,
    ssh: ssh
      ? { target: ssh.target, host: ssh.host, user: ssh.user, port: ssh.port, keyPath: ssh.keyPath }
      : null,
    checks: [
      {
        name: 'ssh',
        ok: false,
        required: true,
        detail: error?.message || 'SSH 连接失败',
      },
    ],
    hints,
    warnings: [],
    error: { code: error?.code || 'SSH_FAILED', message: error?.message || 'SSH 连接失败' },
    rawOutput,
    scannedAt: Date.now(),
  };
}
