/**
 * Cross-platform installer primitives for the CA backend.
 *
 * The plugin can install CA itself on the local host (Windows/macOS/Linux) or
 * on a remote Linux server. This module owns everything that differs between
 * hosts, so the install flows stay platform-agnostic:
 *   - host platform ids aligned with `dsh/scripts/build-wheelhouse.mjs`;
 *   - Python >= 3.11 candidate lists and interpreter probing;
 *   - venv layout (POSIX `bin/` vs Windows `Scripts/`, `.exe` suffixes);
 *   - the bundled CA wheel and offline wheelhouse resolution;
 *   - platform-specific repair hints (brew/apt/winget/python.org).
 *
 * It only uses Node built-ins and is fully injectable (`runProcess`, fs
 * directory overrides), so tests never start a real Python or pip.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { parseCaVersionJson } from '../ca-protocol.js';
import { BackendError, describeCause } from '../errors.js';
import { isExecutableFile, nonEmptyString } from '../util.js';

/** Minimum Python required by CA (`requires-python = ">=3.11"`). */
export const MIN_PYTHON = Object.freeze({ major: 3, minor: 11 });

/** Map of host platform/arch to the wheelhouse platform id. */
export const HOST_PLATFORM_IDS = Object.freeze({
  'darwin-arm64': 'macos-arm64',
  'darwin-x64': 'macos-x64',
  'linux-x64': 'linux-x64',
  'linux-arm64': 'linux-arm64',
  'win32-x64': 'windows-x64',
});

export const INSTALLER_DIR = path.dirname(fileURLToPath(import.meta.url));
/** Plugin package root (`dsh/`). */
export const PACKAGE_ROOT = path.resolve(INSTALLER_DIR, '..', '..', '..', '..');
/** Bundled CA wheel directory (`dsh/backend/wheels/`). */
export const BUNDLED_WHEEL_DIR = path.join(PACKAGE_ROOT, 'backend', 'wheels');

/**
 * Detect the host platform in the same id space as the wheelhouse builder.
 *
 * @param {NodeJS.Platform} [platform] defaults to process.platform
 * @param {string} [arch] defaults to process.arch
 * @returns {{os: string, arch: string, platformId: string|null, label: string, supported: boolean, sep: string}}
 */
export function detectHostPlatform(platform = process.platform, arch = process.arch) {
  const platformId = HOST_PLATFORM_IDS[`${platform}-${arch}`] || null;
  const labels = {
    'macos-arm64': 'macOS arm64',
    'macos-x64': 'macOS x86_64',
    'linux-x64': 'Linux x86_64',
    'linux-arm64': 'Linux aarch64',
    'windows-x64': 'Windows x86_64',
  };
  return {
    os: platform,
    arch,
    platformId,
    label: platformId ? labels[platformId] : `${platform} ${arch}`,
    supported: platformId !== null,
    sep: platform === 'win32' ? '\\' : '/',
  };
}

/**
 * Python interpreter candidates, newest first. `args` are prepended before
 * `-m ...`; on Windows the `py` launcher selects a specific minor version.
 *
 * @param {string} [platform]
 * @returns {Array<{command: string, args: string[], label: string}>}
 */
export function pythonCandidates(platform = process.platform) {
  if (platform === 'win32') {
    return [
      { command: 'py', args: ['-3.13'], label: 'py -3.13' },
      { command: 'py', args: ['-3.12'], label: 'py -3.12' },
      { command: 'py', args: ['-3.11'], label: 'py -3.11' },
      { command: 'python', args: [], label: 'python' },
      { command: 'python3', args: [], label: 'python3' },
    ];
  }
  return [
    { command: 'python3.13', args: [], label: 'python3.13' },
    { command: 'python3.12', args: [], label: 'python3.12' },
    { command: 'python3.11', args: [], label: 'python3.11' },
    { command: 'python3', args: [], label: 'python3' },
    { command: 'python', args: [], label: 'python' },
  ];
}

/**
 * Parse `Python 3.11.7` / `Python 3.13.0rc1`.
 * @param {unknown} text
 * @returns {{major: number, minor: number, patch: number, raw: string}|null}
 */
export function parsePythonVersion(text) {
  const match = /Python\s+(\d+)\.(\d+)(?:\.(\d+))?/i.exec(String(text ?? ''));
  if (!match) return null;
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3] || 0),
    raw: match[0],
  };
}

/** True when `version` satisfies CA's `>= 3.11` requirement. */
export function isSupportedPython(version) {
  if (!version || typeof version !== 'object') return false;
  if (version.major > MIN_PYTHON.major) return true;
  if (version.major < MIN_PYTHON.major) return false;
  return version.minor >= MIN_PYTHON.minor;
}

/** Human version string, e.g. "3.11.7". */
export function formatPythonVersion(version) {
  if (!version) return null;
  return `${version.major}.${version.minor}.${version.patch}`;
}

/**
 * Probe Python candidates by running `<command> <args> -V`.
 * Returns the first supported interpreter plus the full checked list so the UI
 * can show why detection failed.
 *
 * @param {object} options
 * @param {Array<{command: string, args: string[], label: string}>} [options.candidates]
 * @param {Function} options.runProcess one-shot runner from ../process.js
 * @param {number} [options.timeoutMs]
 * @param {AbortSignal} [options.signal]
 * @param {any} [options.logger]
 * @returns {Promise<{selected: object|null, checked: Array<Record<string, any>>}>}
 */
export async function probePythonInterpreters(options = {}) {
  const candidates = options.candidates || pythonCandidates(options.platform || process.platform);
  const runProcess = options.runProcess;
  if (typeof runProcess !== 'function') {
    throw new BackendError('INSTALLER_MISCONFIGURED', 'Python 探测需要注入 runProcess');
  }
  const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : 8000;
  /** @type {Array<Record<string, any>>} */
  const checked = [];
  let selected = null;
  for (const candidate of candidates) {
    const result = await runProcess({
      command: candidate.command,
      args: [...candidate.args, '-V'],
      timeoutMs,
      signal: options.signal,
      logger: options.logger,
      maxOutputBytes: 64 * 1024,
    });
    const output = `${result?.stdout || ''}\n${result?.stderr || ''}`.trim();
    const version = parsePythonVersion(output);
    const entry = {
      label: candidate.label,
      command: candidate.command,
      args: candidate.args.slice(),
      ok: Boolean(version && isSupportedPython(version)) && !result?.spawnError,
      supported: isSupportedPython(version),
      version: formatPythonVersion(version),
      output: output.slice(0, 300),
      error: result?.spawnError ? describeCause(result.spawnError) : null,
    };
    checked.push(entry);
    if (entry.ok && !selected) {
      selected = {
        command: candidate.command,
        args: candidate.args.slice(),
        label: candidate.label,
        version,
        versionText: entry.version,
      };
    }
  }
  return { selected, checked };
}

/**
 * Validate an explicitly configured Python executable by running `-V`.
 * @param {object} options
 * @param {string} options.python path/command to validate
 * @param {Function} options.runProcess
 * @param {AbortSignal} [options.signal]
 * @returns {Promise<{command: string, args: string[], version: object, versionText: string, label: string}>}
 */
export async function validatePythonExecutable(options) {
  const python = nonEmptyString(options.python);
  if (!python) {
    throw new BackendError('INVALID_ARGUMENT', 'python 解释器路径不能为空', { field: 'python' });
  }
  const result = await options.runProcess({
    command: python,
    args: ['-V'],
    timeoutMs: 8000,
    signal: options.signal,
    logger: options.logger,
    maxOutputBytes: 64 * 1024,
  });
  const output = `${result?.stdout || ''}\n${result?.stderr || ''}`.trim();
  const version = parsePythonVersion(output);
  if (result?.spawnError) {
    throw new BackendError('PYTHON_NOT_FOUND', `无法执行 Python 解释器 ${python}：${describeCause(result.spawnError)}`, {
      field: 'python',
      python,
    });
  }
  if (!version) {
    throw new BackendError('PYTHON_NOT_FOUND', `无法识别 Python 版本：${python}（输出: ${output || '(空)'}）`, {
      field: 'python',
      python,
    });
  }
  if (!isSupportedPython(version)) {
    throw new BackendError(
      'PYTHON_VERSION_UNSUPPORTED',
      `Python 版本过低：${python} 是 ${formatPythonVersion(version)}，CA 需要 >= ${MIN_PYTHON.major}.${MIN_PYTHON.minor}`,
      { field: 'python', python, version: formatPythonVersion(version) },
    );
  }
  return {
    command: python,
    args: [],
    label: python,
    version,
    versionText: formatPythonVersion(version),
  };
}

/**
 * Venv layout for one install directory.
 * @param {string} installDir
 * @param {{platform?: string, venvDirName?: string}} [options]
 */
export function venvLayout(installDir, options = {}) {
  const platform = options.platform || process.platform;
  const venvDir = path.join(installDir, options.venvDirName || 'venv');
  const windows = platform === 'win32';
  const python = path.join(venvDir, windows ? 'Scripts' : 'bin', windows ? 'python.exe' : 'python');
  const caScript = path.join(venvDir, windows ? 'Scripts' : 'bin', windows ? 'ca.exe' : 'ca');
  return {
    installDir,
    venvDir,
    python,
    caScript,
    pipArgs: [python, '-m', 'pip'],
    isWindows: windows,
  };
}

/** Parse the CA version out of a wheel file name; null when unknown. */
export function parseCaWheelVersion(name) {
  const match = /^ca_analyzer-([0-9][^-]*)-py3-none-any\.whl$/i.exec(String(name ?? ''));
  return match ? match[1] : null;
}

/**
 * Find the newest CA wheel baked into the plugin.
 * @param {{dir?: string, fsImpl?: typeof import('node:fs')}} [options]
 * @returns {{path: string, name: string, version: string|null, bytes: number}|null}
 */
export function findBundledCaWheel(options = {}) {
  const fsImpl = options.fsImpl || fs;
  const dir = options.dir || BUNDLED_WHEEL_DIR;
  let names;
  try {
    names = fsImpl.readdirSync(dir);
  } catch {
    return null;
  }
  const wheels = names
    .filter((name) => /^ca_analyzer-.*\.whl$/i.test(name))
    .sort();
  for (const name of wheels.reverse()) {
    const fullPath = path.join(dir, name);
    try {
      const stat = fsImpl.statSync(fullPath);
      if (!stat.isFile()) continue;
      return { path: fullPath, name, version: parseCaWheelVersion(name), bytes: stat.size };
    } catch {
      // try the next candidate
    }
  }
  return null;
}

/**
 * Resolve an offline wheelhouse directory (or a prebuilt .tar.gz).
 *
 * Order: explicit path (must exist) → `$DSH_HOME/plugin-data/dsh-ca/wheelhouse/<platformId>`.
 *
 * @param {object} options
 * @param {string} [options.explicitPath]
 * @param {string} [options.stateDir] plugin state dir
 * @param {string} [options.platformId]
 * @param {typeof import('node:fs')} [options.fsImpl]
 * @returns {{kind: 'dir'|'archive', dir: string|null, path: string, manifest: object|null,
 *            wheelCount: number, caWheel: string|null, platform: string|null, exists: boolean}}
 */
export function resolveWheelhouse(options = {}) {
  const fsImpl = options.fsImpl || fs;
  const platformId = options.platformId || detectHostPlatform().platformId;
  const defaultDir = nonEmptyString(options.stateDir)
    ? path.join(options.stateDir, 'wheelhouse', platformId || 'unknown')
    : null;
  const candidate = nonEmptyString(options.explicitPath) || defaultDir;
  const base = {
    kind: 'dir',
    dir: candidate,
    path: candidate || '',
    manifest: null,
    wheelCount: 0,
    caWheel: null,
    platform: platformId || null,
    exists: false,
    explicit: Boolean(nonEmptyString(options.explicitPath)),
  };
  if (!candidate) return base;
  let stat;
  try {
    stat = fsImpl.statSync(candidate);
  } catch {
    return base;
  }
  if (stat.isFile()) {
    const isArchive = /\.(tar\.gz|tgz)$/i.test(candidate);
    return {
      ...base,
      kind: isArchive ? 'archive' : 'file',
      dir: isArchive ? null : path.dirname(candidate),
      exists: isArchive,
    };
  }
  if (!stat.isDirectory()) return base;
  let names = [];
  try {
    names = fsImpl.readdirSync(candidate);
  } catch {
    return base;
  }
  const wheels = names.filter((name) => name.endsWith('.whl'));
  let manifest = null;
  try {
    manifest = JSON.parse(fsImpl.readFileSync(path.join(candidate, 'manifest.json'), 'utf8'));
  } catch {
    manifest = null;
  }
  return {
    ...base,
    dir: candidate,
    exists: wheels.length > 0,
    wheelCount: wheels.length,
    manifest,
    caWheel: wheels.find((name) => /^ca_analyzer-.*\.whl$/i.test(name)) || null,
    platform: manifest?.platform || platformId || null,
  };
}

/**
 * List wheelhouse platforms already present under the plugin state dir.
 * @param {{stateDir?: string, fsImpl?: typeof import('node:fs')}} [options]
 * @returns {Array<{platform: string, dir: string, wheelCount: number, caWheel: string|null}>}
 */
export function listWheelhousePlatforms(options = {}) {
  const fsImpl = options.fsImpl || fs;
  const root = nonEmptyString(options.stateDir)
    ? path.join(options.stateDir, 'wheelhouse')
    : null;
  if (!root) return [];
  let entries;
  try {
    entries = fsImpl.readdirSync(root, { withFileTypes: true });
  } catch {
    return [];
  }
  const result = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const resolved = resolveWheelhouse({
      explicitPath: path.join(root, entry.name),
      stateDir: options.stateDir,
      platformId: entry.name,
      fsImpl,
    });
    result.push({
      platform: entry.name,
      dir: resolved.dir,
      wheelCount: resolved.wheelCount,
      caWheel: resolved.caWheel,
    });
  }
  return result;
}

/**
 * Platform-specific repair hints shown when a step fails.
 * @param {string} platform process.platform-style value
 * @param {{step?: string, offline?: boolean, platformId?: string|null}} [context]
 * @returns {string[]}
 */
export function platformInstallHints(platform = process.platform, context = {}) {
  const hints = [];
  if (platform === 'darwin') {
    hints.push('macOS 安装 Python ≥ 3.11：brew install python@3.12（或从 python.org 安装）');
    if (context.step === 'venv' || context.step === 'dependencies') {
      hints.push('若 venv/pip 异常：brew reinstall python@3.12 后重试');
    }
  } else if (platform === 'linux') {
    hints.push('Linux 安装 Python ≥ 3.11 与构建工具：sudo apt install python3 python3-venv python3-pip');
    hints.push('Debian/Ubuntu 若 python3 版本过低，可加 deadsnakes PPA 安装 python3.12');
  } else if (platform === 'win32') {
    hints.push('Windows 安装 Python ≥ 3.11：winget install Python.Python.3.12（或从 python.org 安装并勾选 Add to PATH）');
    hints.push('若提示找不到 python：在“应用执行别名”中启用 python.exe / py.exe');
  } else {
    hints.push('当前宿主平台不受支持：仅支持 Windows/macOS/Linux（x64/arm64）');
  }
  if (context.offline) {
    hints.push(
      `离线安装需要一个包含 CA wheel 与依赖 wheel 的 wheelhouse，可用：node dsh/scripts/build-wheelhouse.mjs --platform ${
        context.platformId || '<platform>'
      }`,
    );
  }
  return hints;
}

/**
 * Default installer paths for the local backend.
 * @param {{stateDir: string, platformId?: string|null}} paths
 */
export function installerPaths(paths) {
  const platformId = paths.platformId || detectHostPlatform().platformId;
  return {
    installDir: path.join(paths.stateDir, 'backends', 'local'),
    wheelhouseDir: platformId ? path.join(paths.stateDir, 'wheelhouse', platformId) : null,
    platformId,
  };
}

/**
 * One-shot environment report for `probeLocal()` / the install UI.
 *
 * @param {object} options
 * @param {{stateDir: string}} options.paths
 * @param {Function} [options.runProcess]
 * @param {AbortSignal} [options.signal]
 * @param {any} [options.logger]
 * @param {string} [options.platform] override for tests
 * @param {string} [options.arch]
 * @param {string} [options.bundledWheelDir]
 * @param {typeof import('node:fs')} [options.fsImpl]
 */
export async function describeInstallerEnvironment(options) {
  const platform = detectHostPlatform(
    options.platform || process.platform,
    options.arch || process.arch,
  );
  const defaults = installerPaths({ stateDir: options.paths?.stateDir, platformId: platform.platformId });
  const wheel = findBundledCaWheel({ dir: options.bundledWheelDir, fsImpl: options.fsImpl });
  const wheelhouse = resolveWheelhouse({
    stateDir: options.paths?.stateDir,
    platformId: platform.platformId,
    fsImpl: options.fsImpl,
  });
  let pythons = { selected: null, checked: [] };
  if (typeof options.runProcess === 'function') {
    try {
      pythons = await probePythonInterpreters({
        runProcess: options.runProcess,
        platform: options.platform || process.platform,
        signal: options.signal,
        logger: options.logger,
      });
    } catch (error) {
      pythons = { selected: null, checked: [], error: describeCause(error) };
    }
  }
  return {
    platform,
    supported: platform.supported,
    python: pythons,
    installDir: defaults.installDir,
    wheel: wheel
      ? { bundled: true, path: wheel.path, name: wheel.name, version: wheel.version, bytes: wheel.bytes }
      : { bundled: false, path: null, name: null, version: null, bytes: 0 },
    wheelhouse: {
      defaultPath: defaults.wheelhouseDir,
      exists: wheelhouse.exists,
      kind: wheelhouse.kind,
      wheelCount: wheelhouse.wheelCount,
      caWheel: wheelhouse.caWheel,
      platforms: listWheelhousePlatforms({ stateDir: options.paths?.stateDir, fsImpl: options.fsImpl }),
    },
    hints: platform.supported ? [] : platformInstallHints(platform.os, {}),
  };
}

/** Parse a CA version payload produced by `ca version --json`. */
export function readCaVersionOutput(text) {
  const record = parseCaVersionJson(text);
  if (!record || !record.version) return null;
  return {
    version: record.version,
    schemaVersion: record.schema_version ?? null,
    python: record.python || null,
    platform: record.platform || null,
  };
}

/** True when the path looks like an executable Python inside a venv. */
export function isVenvPythonPresent(venvPython) {
  return isExecutableFile(venvPython);
}
