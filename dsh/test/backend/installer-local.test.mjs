import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { createLogger } from '../../lib/host/backend/log.js';
import {
  createLocalInstallStatus,
  createLocalInstallSteps,
  performLocalInstall,
} from '../../lib/host/backend/installer/local.js';
import { cleanupDir, makeTempDir, writeExecutable } from './helpers/fakes.mjs';

const logger = createLogger({ dir: null, ringSize: 300 });

function touch(file, content = 'data') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function makeWheelDir(root) {
  const dir = path.join(root, 'wheels');
  touch(path.join(dir, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'WHEEL');
  return dir;
}

function makeWheelhouse(root, platform = 'macos-arm64', manifest = {}) {
  const dir = path.join(root, 'wheelhouse', platform);
  touch(path.join(dir, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'WHEEL');
  touch(path.join(dir, 'duckdb-1.5.6.whl'), 'WHEEL');
  touch(path.join(dir, 'manifest.json'), JSON.stringify({ platform, ...manifest }));
  return dir;
}

/**
 * Fake runProcess covering python probing, venv creation (creates the venv
 * executables on disk), pip and `ca version --json`.
 */
function makeRunner(root, options = {}) {
  const calls = [];
  const run = async (input) => {
    calls.push({ command: input.command, args: input.args.slice(), input });
    const args = input.args;
    const stdoutLine = input.onStdoutLine;
    const failPip = options.failPip === true;
    const verifyBroken = options.verifyBroken === true;
    // Python probing (`-V`)
    if (args.at(-1) === '-V') {
      if (options.pythonVersion) return { code: 0, stdout: `Python ${options.pythonVersion}`, stderr: '' };
      if (input.command === 'python3.13') return { code: 1, stdout: '', stderr: 'not found' };
      if (input.command === 'python3.12') return { code: 0, stdout: 'Python 3.12.4', stderr: '' };
      return { code: 0, stdout: 'Python 3.11.9', stderr: '' };
    }
    // venv creation
    if (args.includes('venv')) {
      const venvDir = args.at(-1);
      const windows =
        options.platform === 'win32' ||
        String(venvDir).includes('Scripts') ||
        process.platform === 'win32';
      const bin = path.join(venvDir, windows ? 'Scripts' : 'bin');
      writeExecutable(path.join(bin, windows ? 'python.exe' : 'python'), '#!/bin/sh\nexit 0\n');
      writeExecutable(path.join(bin, windows ? 'ca.exe' : 'ca'), '#!/bin/sh\nexit 0\n');
      writeExecutable(path.join(bin, windows ? 'pip.exe' : 'pip'), '#!/bin/sh\nexit 0\n');
      stdoutLine?.('venv created');
      return { code: 0, stdout: 'venv ok', stderr: '' };
    }
    if (args.includes('pip')) {
      if (failPip) return { code: 1, stdout: 'Collecting', stderr: 'ERROR: Could not find a version' };
      stdoutLine?.('pip install ok');
      return { code: 0, stdout: 'Successfully installed', stderr: '' };
    }
    // version verification
    if (args.includes('version')) {
      if (verifyBroken) return { code: 1, stdout: '', stderr: 'ModuleNotFoundError: ca' };
      return {
        code: 0,
        stdout: JSON.stringify({ tool: 'ca-analyzer', version: '0.1.0', schema_version: '2', python: '3.12.4' }),
        stderr: '',
      };
    }
    return { code: 0, stdout: '', stderr: '' };
  };
  run.calls = calls;
  return run;
}

function setupInstall({ root, platform = 'darwin', arch = 'arm64', installOptions = {}, runnerOptions = {}, deps = {} } = {}) {
  const wheelDir = makeWheelDir(root);
  const events = [];
  const runner = makeRunner(root, { ...runnerOptions, platform });
  return {
    deps: {
      runProcess: runner,
      hostPlatform: platform,
      hostArch: arch,
      bundledWheelDir: wheelDir,
      ...deps,
    },
    options: {
      paths: { stateDir: path.join(root, 'state') },
      deps: {
        runProcess: runner,
        hostPlatform: platform,
        hostArch: arch,
        bundledWheelDir: wheelDir,
        ...deps,
      },
      installOptions: { installDir: path.join(root, 'install'), ...installOptions },
      onEvent: (event) => events.push(event),
      logger,
    },
    events,
    runner,
    wheelDir,
  };
}

test('local install status/steps helpers are fresh and ordered', () => {
  const steps = createLocalInstallSteps();
  assert.deepEqual(
    steps.map((step) => step.id),
    ['environment', 'wheel', 'venv', 'dependencies', 'verify'],
  );
  assert.ok(steps.every((step) => step.status === 'pending'));
  const status = createLocalInstallStatus();
  assert.equal(status.scope, 'local');
  assert.equal(status.state, 'idle');
});

test('local online install creates a venv and installs the bundled wheel', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({ root });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.equal(report.mode, 'online');
    assert.equal(report.offline, false);
    assert.equal(report.platform.platformId, 'macos-arm64');
    assert.equal(report.installDir, path.join(root, 'install'));
    assert.ok(report.venvPython.endsWith('bin/python'));
    assert.ok(report.caScript.endsWith('bin/ca'));
    assert.equal(report.version.version, '0.1.0');
    assert.equal(report.version.schemaVersion, '2');
    assert.ok(report.wheel.name.startsWith('ca_analyzer-'));
    assert.deepEqual(
      report.steps.map((step) => `${step.id}:${step.status}`),
      [
        'environment:succeeded',
        'wheel:succeeded',
        'venv:succeeded',
        'dependencies:succeeded',
        'verify:succeeded',
      ],
    );
    const venvCall = context.runner.calls.find((call) => call.args.includes('venv'));
    assert.ok(venvCall.args.includes('-m'));
    const pipCall = context.runner.calls.find((call) => call.args.includes('pip') && call.args.includes('install') && call.args.some((arg) => /ca_analyzer-.*\.whl$/.test(arg)));
    assert.ok(pipCall, 'pip installs the bundled CA wheel');
    assert.ok(!pipCall.args.includes('--no-index'), 'online install does not use --no-index');
    assert.ok(context.events.some((event) => event.type === 'step'));
    assert.ok(context.events.some((event) => event.type === 'log'));
  } finally {
    cleanupDir(root);
  }
});

test('local offline install uses --no-index --find-links and accepts a wheelhouse', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const wheelhouse = makeWheelhouse(root);
    const context = setupInstall({
      root,
      installOptions: { offline: true, wheelhousePath: wheelhouse },
    });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.equal(report.mode, 'offline');
    assert.equal(report.offline, true);
    assert.equal(report.wheelhouse.path, wheelhouse);
    assert.equal(report.wheelhouse.wheelCount, 2);
    const pipCall = context.runner.calls.find(
      (call) => call.args.includes('pip') && call.args.includes('install') && call.args.includes('--no-index'),
    );
    assert.ok(pipCall, 'offline install uses --no-index');
    assert.ok(pipCall.args.includes('--find-links'));
    assert.ok(pipCall.args.includes(wheelhouse));
  } finally {
    cleanupDir(root);
  }
});

test('local offline install fails clearly when the wheelhouse is missing', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({
      root,
      installOptions: { offline: true, wheelhousePath: path.join(root, 'missing-wheelhouse') },
    });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'WHEELHOUSE_NOT_FOUND');
    assert.equal(report.steps.find((step) => step.id === 'wheel').status, 'failed');
    assert.ok(report.error.details.hints.some((hint) => /build-wheelhouse\.mjs/.test(hint)));
  } finally {
    cleanupDir(root);
  }
});

test('local install fails early when no Python >= 3.11 is found', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({ root, platform: 'darwin', runnerOptions: { pythonVersion: '3.9.18' } });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'PYTHON_NOT_FOUND');
    assert.equal(report.steps.find((step) => step.id === 'environment').status, 'failed');
    assert.ok(report.error.details.hints.some((hint) => /brew install python/.test(hint)));
    assert.ok(!context.runner.calls.some((call) => call.args.includes('venv')), 'no venv before Python is found');
  } finally {
    cleanupDir(root);
  }
});

test('local install rejects unsupported hosts with a clear error', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({ root, platform: 'freebsd', arch: 'x64' });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'UNSUPPORTED_PLATFORM');
    assert.equal(report.steps.find((step) => step.id === 'environment').status, 'failed');
  } finally {
    cleanupDir(root);
  }
});

test('local install uses Windows Scripts layout on win32', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({ root, platform: 'win32', arch: 'x64' });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.ok(report.venvPython.includes('Scripts'));
    assert.ok(report.venvPython.endsWith('python.exe') || report.venvPython.endsWith('python'));
    assert.ok(report.installDir === path.join(root, 'install'));
    const venvCall = context.runner.calls.find((call) => call.args.includes('venv'));
    assert.equal(venvCall.command, 'py', 'Windows probing uses the py launcher when available');
    assert.deepEqual(venvCall.args.slice(0, 1), ['-3.13']);
  } finally {
    cleanupDir(root);
  }
});

test('local install is idempotent for an existing working venv', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({ root });
    // First install to populate the venv.
    const first = await performLocalInstall(context.options);
    assert.equal(first.ok, true);
    const callsAfterFirst = context.runner.calls.length;
    const second = await performLocalInstall(context.options);
    assert.equal(second.ok, true);
    assert.equal(second.skipped, true);
    assert.equal(second.mode, 'existing');
    assert.equal(second.steps.find((step) => step.id === 'venv').status, 'skipped');
    assert.equal(second.steps.find((step) => step.id === 'dependencies').status, 'skipped');
    assert.ok(context.runner.calls.length > callsAfterFirst, 'version probe ran');
    assert.ok(
      !context.runner.calls.slice(callsAfterFirst).some((call) => call.args.includes('venv')),
      'no venv recreation for a healthy install',
    );
  } finally {
    cleanupDir(root);
  }
});

test('local install repairs a broken venv', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({ root });
    const first = await performLocalInstall(context.options);
    assert.equal(first.ok, true);
    // Broken verify until the venv is recreated, healthy afterwards.
    let venvRecreated = false;
    const baseRunner = makeRunner(root);
    const runner = async (input) => {
      if (input.args.includes('venv')) venvRecreated = true;
      if (input.args.includes('version')) {
        if (!venvRecreated) return { code: 1, stdout: '', stderr: 'ModuleNotFoundError: ca' };
        return {
          code: 0,
          stdout: JSON.stringify({ version: '0.1.0', schema_version: '2' }),
          stderr: '',
        };
      }
      return baseRunner(input);
    };
    runner.calls = baseRunner.calls;
    const repair = await performLocalInstall({
      ...context.options,
      deps: { ...context.options.deps, runProcess: runner },
    });
    assert.equal(repair.ok, true, JSON.stringify(repair.error));
    assert.equal(repair.steps.find((step) => step.id === 'venv').status, 'succeeded');
    assert.ok(runner.calls.some((call) => call.args.includes('venv')), 'broken venv is recreated');
  } finally {
    cleanupDir(root);
  }
});

test('local install reports pip/verify failures with hints', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const pipFailure = setupInstall({ root, runnerOptions: { failPip: true } });
    const pipReport = await performLocalInstall(pipFailure.options);
    assert.equal(pipReport.ok, false);
    assert.equal(pipReport.error.code, 'INSTALL_STEP_FAILED');
    assert.equal(pipReport.steps.find((step) => step.id === 'dependencies').status, 'failed');
    assert.ok(pipReport.error.details.hints.length > 0);
  } finally {
    cleanupDir(root);
  }

  const root2 = makeTempDir('dshca-installer-local-');
  try {
    const runner = makeRunner(root2);
    const originalRun = runner;
    const verifyFailureRunner = async (input) => {
      if (input.args.includes('version')) return { code: 0, stdout: 'not json', stderr: '' };
      return originalRun(input);
    };
    verifyFailureRunner.calls = runner.calls;
    const context = setupInstall({ root: root2 });
    const report = await performLocalInstall({
      ...context.options,
      deps: { ...context.options.deps, runProcess: verifyFailureRunner },
    });
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'VERIFY_FAILED');
    assert.equal(report.steps.find((step) => step.id === 'verify').status, 'failed');
  } finally {
    cleanupDir(root2);
  }
});

test('local install fails when the bundled wheel is missing', async () => {
  const root = makeTempDir('dshca-installer-local-');
  try {
    const context = setupInstall({ root });
    const report = await performLocalInstall({
      ...context.options,
      deps: { ...context.options.deps, bundledWheelDir: path.join(root, 'no-wheels') },
    });
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'WHEEL_NOT_FOUND');
    assert.equal(report.steps.find((step) => step.id === 'wheel').status, 'failed');
  } finally {
    cleanupDir(root);
  }
});

test('offline wheelhouse built for another Python major/minor selects a matching interpreter', async () => {
  const root = makeTempDir('dshca-installer-abi-');
  try {
    const wheelhouse = makeWheelhouse(root, 'macos-arm64', { python: '3.11' });
    const context = setupInstall({
      root,
      installOptions: { offline: true, wheelhousePath: wheelhouse },
    });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, true, JSON.stringify(report.error));
    const venvCall = context.runner.calls.find((call) => call.args.includes('venv'));
    assert.equal(venvCall.command, 'python3.11', 'venv is created with the wheelhouse Python ABI');
    assert.match(report.steps.find((step) => step.id === 'environment').detail, /3\.11/);
  } finally {
    cleanupDir(root);
  }
});

test('offline wheelhouse ABI mismatch without a matching interpreter fails clearly', async () => {
  const root = makeTempDir('dshca-installer-abi-mismatch-');
  try {
    const wheelhouse = makeWheelhouse(root, 'macos-arm64', { python: '3.11' });
    const context = setupInstall({
      root,
      installOptions: { offline: true, wheelhousePath: wheelhouse },
      runnerOptions: { pythonVersion: '3.12.4' },
    });
    const report = await performLocalInstall(context.options);
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'WHEELHOUSE_PYTHON_MISMATCH');
    assert.equal(report.steps.find((step) => step.id === 'wheel').status, 'failed');
    assert.ok(report.error.details.hints.some((hint) => /build-wheelhouse\.mjs/.test(hint)));
  } finally {
    cleanupDir(root);
  }
});
