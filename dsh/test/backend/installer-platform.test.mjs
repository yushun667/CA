import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import {
  HOST_PLATFORM_IDS,
  MIN_PYTHON,
  detectHostPlatform,
  describeInstallerEnvironment,
  findBundledCaWheel,
  formatPythonVersion,
  installerPaths,
  isSupportedPython,
  listWheelhousePlatforms,
  parseCaWheelVersion,
  parsePythonVersion,
  platformInstallHints,
  probePythonInterpreters,
  pythonCandidates,
  resolveWheelhouse,
  validatePythonExecutable,
  venvLayout,
} from '../../lib/host/backend/installer/platform.js';
import { cleanupDir, makeTempDir, writeExecutable } from './helpers/fakes.mjs';

function touch(file, content = 'data') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function fakeRunProcess(handler) {
  const calls = [];
  const run = async (options) => {
    calls.push(options);
    const result = await handler(options, calls.length - 1);
    return {
      command: options.command,
      args: options.args,
      code: 0,
      signal: null,
      stdout: '',
      stderr: '',
      spawnError: null,
      timedOut: false,
      cancelled: false,
      ...(result || {}),
    };
  };
  run.calls = calls;
  return run;
}

test('detectHostPlatform maps hosts to wheelhouse platform ids', () => {
  assert.deepEqual(detectHostPlatform('darwin', 'arm64'), {
    os: 'darwin',
    arch: 'arm64',
    platformId: 'macos-arm64',
    label: 'macOS arm64',
    supported: true,
    sep: '/',
  });
  assert.equal(detectHostPlatform('darwin', 'x64').platformId, 'macos-x64');
  assert.equal(detectHostPlatform('linux', 'x64').platformId, 'linux-x64');
  assert.equal(detectHostPlatform('linux', 'arm64').platformId, 'linux-arm64');
  const windows = detectHostPlatform('win32', 'x64');
  assert.equal(windows.platformId, 'windows-x64');
  assert.equal(windows.sep, '\\');
  const unsupported = detectHostPlatform('freebsd', 'x64');
  assert.equal(unsupported.supported, false);
  assert.equal(unsupported.platformId, null);
  assert.deepEqual(Object.keys(HOST_PLATFORM_IDS).sort(), [
    'darwin-arm64',
    'darwin-x64',
    'linux-arm64',
    'linux-x64',
    'win32-x64',
  ]);
});

test('pythonCandidates prefers newer versions and the Windows py launcher', () => {
  const win = pythonCandidates('win32').map((candidate) => candidate.label);
  assert.deepEqual(win.slice(0, 3), ['py -3.13', 'py -3.12', 'py -3.11']);
  assert.ok(win.includes('python'));
  const posix = pythonCandidates('darwin').map((candidate) => candidate.command);
  assert.deepEqual(posix.slice(0, 3), ['python3.13', 'python3.12', 'python3.11']);
  assert.ok(posix.includes('python3'));
});

test('python version parsing and >= 3.11 predicate', () => {
  assert.deepEqual(parsePythonVersion('Python 3.11.7'), { major: 3, minor: 11, patch: 7, raw: 'Python 3.11.7' });
  assert.deepEqual(parsePythonVersion('Python 3.13.0rc1'), { major: 3, minor: 13, patch: 0, raw: 'Python 3.13.0' });
  assert.equal(parsePythonVersion('no version here'), null);
  assert.equal(isSupportedPython({ major: 3, minor: 11, patch: 0 }), true);
  assert.equal(isSupportedPython({ major: 3, minor: 10, patch: 9 }), false);
  assert.equal(isSupportedPython({ major: 4, minor: 0, patch: 0 }), true);
  assert.equal(isSupportedPython(null), false);
  assert.equal(formatPythonVersion({ major: 3, minor: 12, patch: 4 }), '3.12.4');
  assert.deepEqual(MIN_PYTHON, { major: 3, minor: 11 });
});

test('probePythonInterpreters selects the first supported interpreter', async () => {
  const runProcess = fakeRunProcess((options) => {
    if (options.command === 'python3.13') return { code: 1, stderr: 'not found' };
    if (options.command === 'python3.12') return { stdout: 'Python 3.12.4' };
    return { stdout: 'Python 3.11.7' };
  });
  const probed = await probePythonInterpreters({ runProcess, platform: 'darwin' });
  assert.equal(probed.selected.command, 'python3.12');
  assert.equal(probed.selected.versionText, '3.12.4');
  assert.equal(probed.checked.length, 5);
  assert.equal(probed.checked[0].ok, false);
  assert.equal(probed.checked[1].ok, true);
  assert.equal(runProcess.calls[0].args.at(-1), '-V');
});

test('probePythonInterpreters reports no selection when every candidate is too old', async () => {
  const runProcess = fakeRunProcess(() => ({ stdout: 'Python 3.9.18' }));
  const probed = await probePythonInterpreters({ runProcess, platform: 'linux' });
  assert.equal(probed.selected, null);
  assert.ok(probed.checked.every((entry) => entry.supported === false));
});

test('validatePythonExecutable rejects missing/old/unparseable interpreters', async () => {
  const good = await validatePythonExecutable({
    python: '/opt/py3.12',
    runProcess: fakeRunProcess(() => ({ stdout: 'Python 3.12.1' })),
  });
  assert.equal(good.versionText, '3.12.1');

  await assert.rejects(
    validatePythonExecutable({
      python: '/nope/python',
      runProcess: fakeRunProcess(() => ({ spawnError: new Error('spawn ENOENT') })),
    }),
    (error) => error.code === 'PYTHON_NOT_FOUND',
  );
  await assert.rejects(
    validatePythonExecutable({
      python: '/opt/py3.9',
      runProcess: fakeRunProcess(() => ({ stdout: 'Python 3.9.18' })),
    }),
    (error) => error.code === 'PYTHON_VERSION_UNSUPPORTED',
  );
  await assert.rejects(
    validatePythonExecutable({
      python: '/opt/mystery',
      runProcess: fakeRunProcess(() => ({ stdout: 'hello' })),
    }),
    (error) => error.code === 'PYTHON_NOT_FOUND',
  );
});

test('venvLayout switches bin/Scripts and executable suffixes', () => {
  const posix = venvLayout('/data/ca', { platform: 'linux' });
  assert.equal(posix.venvDir, '/data/ca/venv');
  assert.equal(posix.python, '/data/ca/venv/bin/python');
  assert.equal(posix.caScript, '/data/ca/venv/bin/ca');
  assert.equal(posix.isWindows, false);

  const win = venvLayout('C:\\data\\ca', { platform: 'win32' });
  assert.ok(win.python.endsWith('Scripts/python.exe'));
  assert.ok(win.caScript.endsWith('Scripts/ca.exe'));
  assert.equal(win.isWindows, true);
});

test('findBundledCaWheel returns the newest wheel and parseCaWheelVersion works', () => {
  const root = makeTempDir();
  try {
    const wheels = path.join(root, 'wheels');
    touch(path.join(wheels, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'new');
    touch(path.join(wheels, 'ca_analyzer-0.0.9-py3-none-any.whl'), 'old');
    touch(path.join(wheels, 'notes.txt'), 'x');
    const found = findBundledCaWheel({ dir: wheels });
    assert.equal(found.name, 'ca_analyzer-0.1.0-py3-none-any.whl');
    assert.equal(found.version, '0.1.0');
    assert.equal(found.bytes, 3);
    assert.equal(parseCaWheelVersion('ca_analyzer-1.2.3-py3-none-any.whl'), '1.2.3');
    assert.equal(parseCaWheelVersion('duckdb-1.0.whl'), null);
    assert.equal(findBundledCaWheel({ dir: path.join(root, 'missing') }), null);
  } finally {
    cleanupDir(root);
  }
});

test('resolveWheelhouse finds explicit dirs, archives and the state default', () => {
  const root = makeTempDir();
  try {
    const stateDir = path.join(root, 'state');
    const wh = path.join(stateDir, 'wheelhouse', 'macos-arm64');
    touch(path.join(wh, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'wheel');
    touch(path.join(wh, 'duckdb-1.5.6.whl'), 'wheel');
    touch(path.join(wh, 'manifest.json'), JSON.stringify({ platform: 'macos-arm64' }));
    const resolved = resolveWheelhouse({ stateDir, platformId: 'macos-arm64' });
    assert.equal(resolved.exists, true);
    assert.equal(resolved.kind, 'dir');
    assert.equal(resolved.wheelCount, 2);
    assert.equal(resolved.caWheel, 'ca_analyzer-0.1.0-py3-none-any.whl');
    assert.equal(resolved.platform, 'macos-arm64');

    const archive = path.join(root, 'wh.tar.gz');
    fs.writeFileSync(archive, 'tar');
    const archiveResolved = resolveWheelhouse({ explicitPath: archive, stateDir });
    assert.equal(archiveResolved.kind, 'archive');
    assert.equal(archiveResolved.exists, true);

    const missing = resolveWheelhouse({ explicitPath: path.join(root, 'nope'), stateDir });
    assert.equal(missing.exists, false);

    const platforms = listWheelhousePlatforms({ stateDir });
    assert.equal(platforms.length, 1);
    assert.equal(platforms[0].platform, 'macos-arm64');
    assert.equal(platforms[0].wheelCount, 2);
  } finally {
    cleanupDir(root);
  }
});

test('platformInstallHints give brew/apt/winget repair commands', () => {
  assert.ok(platformInstallHints('darwin').some((hint) => /brew install python/.test(hint)));
  assert.ok(platformInstallHints('linux').some((hint) => /apt install python3/.test(hint)));
  assert.ok(platformInstallHints('win32').some((hint) => /winget install Python/.test(hint)));
  assert.ok(
    platformInstallHints('linux', { offline: true, platformId: 'linux-x64', step: 'wheel' }).some(
      (hint) => /build-wheelhouse\.mjs --platform linux-x64/.test(hint),
    ),
  );
  assert.ok(platformInstallHints('freebsd').some((hint) => /不受支持/.test(hint)));
});

test('installerPaths and describeInstallerEnvironment expose wheel/wheelhouse availability', async () => {
  const root = makeTempDir();
  try {
    const stateDir = path.join(root, 'state');
    const wheels = path.join(root, 'wheels');
    touch(path.join(wheels, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'wheel');
    const wh = path.join(stateDir, 'wheelhouse', 'macos-arm64');
    touch(path.join(wh, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'cache');
    const defaults = installerPaths({ stateDir, platformId: 'macos-arm64' });
    assert.equal(defaults.installDir, path.join(stateDir, 'backends', 'local'));
    assert.equal(defaults.wheelhouseDir, path.join(stateDir, 'wheelhouse', 'macos-arm64'));

    const report = await describeInstallerEnvironment({
      paths: { stateDir },
      platform: 'darwin',
      arch: 'arm64',
      bundledWheelDir: wheels,
      runProcess: fakeRunProcess((options) =>
        options.command === 'python3.13'
          ? { stdout: 'Python 3.13.0' }
          : { code: 1, stderr: 'missing' },
      ),
    });
    assert.equal(report.platform.platformId, 'macos-arm64');
    assert.equal(report.wheel.bundled, true);
    assert.equal(report.wheel.version, '0.1.0');
    assert.equal(report.wheelhouse.exists, true);
    assert.equal(report.wheelhouse.platforms.length, 1);
    assert.equal(report.python.selected.command, 'python3.13');
    assert.equal(report.supported, true);
  } finally {
    cleanupDir(root);
  }
});
