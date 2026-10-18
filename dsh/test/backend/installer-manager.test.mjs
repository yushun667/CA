import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import { createBackendManager } from '../../lib/host/backend/manager.js';
import { cleanupDir, makePaths, makeTempDir, waitFor, writeExecutable } from './helpers/fakes.mjs';
import { createFakeRunner, probeStdout } from './remote/helpers.mjs';

function touch(file, content = 'data') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function makeWheelDir(root) {
  const dir = path.join(root, 'wheels');
  touch(path.join(dir, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'WHEEL');
  return dir;
}

/** Fake runProcess for the local installer (python/venv/pip/version). */
function makeInstallerRunner({ pythonVersion = '3.12.4', verifyBroken = false } = {}) {
  const calls = [];
  const run = async (input) => {
    calls.push({ command: input.command, args: input.args.slice() });
    const args = input.args;
    if (args.at(-1) === '-V') {
      if (input.command === 'python3.13') return { code: 1, stdout: '', stderr: 'not found' };
      return { code: 0, stdout: `Python ${pythonVersion}`, stderr: '' };
    }
    if (args.includes('venv')) {
      const venvDir = args.at(-1);
      const bin = path.join(venvDir, 'bin');
      writeExecutable(path.join(bin, 'python'), '#!/bin/sh\nexit 0\n');
      writeExecutable(path.join(bin, 'ca'), '#!/bin/sh\nexit 0\n');
      input.onStdoutLine?.('venv created');
      return { code: 0, stdout: 'venv ok', stderr: '' };
    }
    if (args.includes('pip')) {
      input.onStdoutLine?.('pip ok');
      return { code: 0, stdout: 'Successfully installed', stderr: '' };
    }
    if (args.includes('version')) {
      if (verifyBroken) return { code: 1, stdout: '', stderr: 'ModuleNotFoundError: ca' };
      return {
        code: 0,
        stdout: JSON.stringify({ tool: 'ca-analyzer', version: '0.1.0', schema_version: '2' }),
        stderr: '',
      };
    }
    return { code: 0, stdout: '', stderr: '' };
  };
  run.calls = calls;
  return run;
}

/** Fake runProcess for the remote wheel installer (probe/venv/pip/verify scripts). */
function makeRemoteRunner() {
  return createFakeRunner((call) => {
    const script = call.script || '';
    if (script.includes('CA_PROBE:begin')) {
      return {
        code: 0,
        stdout: probeStdout({
          os: 'Linux',
          arch: 'x86_64',
          python3: '/usr/bin/python3',
          python3_version: 'Python 3.11.17',
          pip: '/usr/bin/pip3',
          db_file: '1',
        }),
        stderr: '',
      };
    }
    if (script.includes('CA_PIP_DONE')) return { code: 0, stdout: 'CA_PIP_DONE=1', stderr: '' };
    if (script.includes('CA_VERIFY_DONE')) {
      return {
        code: 0,
        stdout: [
          JSON.stringify({ version: '0.1.0', schema_version: '2' }),
          'CA_CONSOLE_OK=1',
          'CA_VERIFY_DONE=1',
        ].join('\n'),
        stderr: '',
      };
    }
    if (script.includes('CA_VENV_OK')) return { code: 0, stdout: 'CA_VENV_OK=1', stderr: '' };
    return { code: 0, stdout: 'CA_DIR=/home/yu/ca-install', stderr: '' };
  });
}

function makeTransfer() {
  const uploads = [];
  return {
    scpAvailable: true,
    tarAvailable: true,
    uploads,
    async uploadFile(input) {
      uploads.push({ type: 'file', ...input });
      return { strategy: 'scp' };
    },
    async uploadDirectory(input) {
      uploads.push({ type: 'dir', ...input });
      return { strategy: 'scp-recursive' };
    },
  };
}

function setup({ explicit = {}, deps = {} } = {}) {
  const root = makeTempDir('dshca-installer-manager-');
  const paths = makePaths(root);
  const wheelDir = makeWheelDir(root);
  const manager = createBackendManager(null, explicit, {
    paths,
    env: {},
    cwd: '/nonexistent-local-cwd',
    homedir: '/tmp',
    hostPlatform: 'darwin',
    hostArch: 'arm64',
    bundledWheelDir: wheelDir,
    ...deps,
  });
  return { manager, root, paths, wheelDir };
}

async function teardown(context) {
  try {
    await context.manager.dispose();
  } finally {
    cleanupDir(context.root);
  }
}

test('manager.installLocal installs a local backend and reports merged status', async () => {
  const runner = makeInstallerRunner();
  const context = setup({ deps: { runProcess: runner } });
  try {
    const installDir = path.join(context.root, 'backend-local');
    const report = await context.manager.installLocal({ installDir });
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.equal(report.mode, 'online');
    assert.equal(report.installDir, installDir);
    assert.equal(report.version.version, '0.1.0');

    const status = context.manager.getInstallStatus();
    assert.equal(status.local.state, 'succeeded');
    assert.equal(status.local.mode, 'online');
    assert.equal(status.remote.state, 'idle');
    assert.equal(status.state, 'succeeded');
    assert.equal(status.scope, 'local');
    assert.ok(status.steps.length > 0);
    assert.ok(Array.isArray(status.logs));

    const summary = context.manager.getInstallSummary();
    assert.equal(summary.local.installed, true);
    assert.equal(summary.local.installDir, installDir);
    assert.equal(summary.local.version.version, '0.1.0');
    assert.equal(summary.local.state, 'succeeded');
    assert.equal(summary.wheel.bundled, true);
    assert.equal(summary.wheel.version, '0.1.0');
    assert.match(summary.wheelhouse.defaultPath, /wheelhouse/);
    assert.ok(Array.isArray(summary.wheelhouse.availablePlatforms));

    const managerStatus = context.manager.getStatus();
    assert.equal(managerStatus.install.local.installed, true);
    assert.equal(managerStatus.install.remote, null);
  } finally {
    await teardown(context);
  }
});

test('manager.startLocalInstall starts in the background and can be polled', async () => {
  const runner = makeInstallerRunner();
  const context = setup({ deps: { runProcess: runner } });
  try {
    const started = context.manager.startLocalInstall({ installDir: path.join(context.root, 'bg-install') });
    assert.equal(started.started, true);
    assert.equal(started.scope, 'local');
    const second = context.manager.startLocalInstall({ installDir: path.join(context.root, 'bg-install') });
    assert.equal(second.started, false);
    assert.equal(second.running, true);
    await waitFor(() => context.manager.getInstallStatus().local.state !== 'running', {
      label: 'background local install',
    });
    const status = context.manager.getInstallStatus();
    assert.equal(status.local.state, 'succeeded');
    assert.equal(status.local.result.ok, true);
    assert.equal(status.local.result.venvPython.endsWith('bin/python'), true);
  } finally {
    await teardown(context);
  }
});

test('manager.installLocal validates options', async () => {
  const context = setup({ deps: { runProcess: makeInstallerRunner() } });
  try {
    await assert.rejects(
      context.manager.installLocal({ mode: 'source' }),
      (error) => error instanceof BackendError && error.code === 'INVALID_ARGUMENT',
    );
    await assert.rejects(
      context.manager.installLocal({ offline: 'yes' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    await assert.rejects(
      context.manager.installLocal({ installDir: 42 }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
  } finally {
    await teardown(context);
  }
});

test('manager local offline install uses the wheelhouse path', async () => {
  const context = setup({ deps: { runProcess: makeInstallerRunner() } });
  try {
    const wheelhouse = path.join(context.root, 'state', 'wheelhouse', 'macos-arm64');
    touch(path.join(wheelhouse, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'WHEEL');
    touch(path.join(wheelhouse, 'duckdb-1.5.6.whl'), 'WHEEL');
    const report = await context.manager.installLocal({
      installDir: path.join(context.root, 'offline-install'),
      offline: true,
      wheelhousePath: wheelhouse,
    });
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.equal(report.mode, 'offline');
    assert.equal(report.wheelhouse.path, wheelhouse);
    assert.equal(report.steps.find((step) => step.id === 'dependencies').status, 'succeeded');
  } finally {
    await teardown(context);
  }
});

test('manager remote wheel install runs through startRemoteInstall with merged status', async () => {
  const runner = makeRemoteRunner();
  const transfer = makeTransfer();
  const context = setup({
    explicit: {
      backend: 'remote',
      remote: {
        ssh: { host: 'linux-box' },
        repoPath: '/home/yu/CA',
        db: '/home/yu/x.db',
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 2000,
        maxRestarts: 0,
        restartBackoffMs: 10,
      },
    },
    deps: { runProcess: runner, transfer },
  });
  try {
    const started = await context.manager.startRemoteInstall({
      mode: 'wheel',
      installDir: '/home/yu/ca-install',
    });
    assert.equal(started.started, true);
    assert.equal(started.scope, 'remote');
    await waitFor(() => context.manager.getInstallStatus().remote.state !== 'running', {
      label: 'remote wheel install',
    });
    const status = context.manager.getInstallStatus();
    assert.equal(status.remote.state, 'succeeded');
    assert.equal(status.remote.mode, 'wheel');
    assert.equal(status.remote.result.ok, true);
    assert.equal(status.remote.result.installDir, '/home/yu/ca-install');
    assert.equal(status.state, 'succeeded');
    assert.equal(status.scope, 'remote');
    assert.equal(transfer.uploads.filter((upload) => upload.type === 'file').length, 1);
    assert.equal(transfer.uploads.filter((upload) => upload.type === 'dir').length, 0);

    const summary = context.manager.getInstallSummary();
    assert.equal(summary.remote.installed, true);
    assert.equal(summary.remote.version.version, '0.1.0');
    assert.equal(summary.local.installed, false);
  } finally {
    await teardown(context);
  }
});

test('manager.startRemoteInstall defaults to wheel and validates mode', async () => {
  const context = setup({
    explicit: {
      backend: 'remote',
      remote: {
        ssh: { host: 'linux-box' },
        repoPath: '/home/yu/CA',
        db: '/home/yu/x.db',
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 2000,
        maxRestarts: 0,
        restartBackoffMs: 10,
      },
    },
    deps: { runProcess: makeRemoteRunner(), transfer: makeTransfer() },
  });
  try {
    await assert.rejects(
      context.manager.startRemoteInstall({ mode: 'rsync' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    await assert.rejects(
      context.manager.installRemote({ offline: 1 }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    const started = await context.manager.startRemoteInstall({ installDir: '/home/yu/ca-default' });
    assert.equal(started.started, true);
    await waitFor(() => context.manager.getInstallStatus().remote.state !== 'running', {
      label: 'default wheel install',
    });
    assert.equal(context.manager.getInstallStatus().remote.mode, 'wheel');
  } finally {
    await teardown(context);
  }
});
