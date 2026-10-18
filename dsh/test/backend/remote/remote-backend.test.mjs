import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../../lib/host/backend/errors.js';
import { createBackendManager } from '../../../lib/host/backend/manager.js';
import { createRemoteBackend } from '../../../lib/host/backend/remote/index.js';
import { createLogger } from '../../../lib/host/backend/log.js';
import {
  cleanupDir,
  createFakeFetch,
  createFakeSpawn,
  healthResponse,
  makePaths,
  makeTempDir,
  waitFor,
  writeExecutable,
} from '../helpers/fakes.mjs';
import {
  createFakeRunner,
  probeExistingInstall,
  probeNeedInstall,
  remoteTestConfig,
  verifyStdout,
} from './helpers.mjs';

const READY_LINE =
  '[serve] CA backend listening on http://127.0.0.1:39001 (db=/home/yu/CA_OH/openharmony.db)\n';
const logger = createLogger({ dir: null, ringSize: 300 });

function makeLocalCheckout() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dshca-checkout-'));
  fs.mkdirSync(path.join(dir, 'src', 'ca'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'src', 'ca', '__init__.py'), '');
  fs.writeFileSync(path.join(dir, 'pyproject.toml'), '[project]\nname = "ca-analyzer"\n');
  fs.writeFileSync(path.join(dir, 'requirements.txt'), 'clang==18.1.8\n');
  writeExecutable(path.join(dir, '.venv', 'bin', 'python'));
  writeExecutable(path.join(dir, '.venv', 'bin', 'ca'));
  return dir;
}

function fullRunner({ probe = probeNeedInstall(), failPip = false } = {}) {
  return createFakeRunner((call) => {
    if (call.command === 'rsync') return { code: 0, stdout: '', stderr: 'sent files' };
    if (call.script?.includes('CA_PROBE:begin')) return { code: 0, stdout: probe, stderr: '' };
    if (call.script?.includes('CA_VERIFY_DONE')) return { code: 0, stdout: verifyStdout(), stderr: '' };
    if (failPip && call.script?.includes('requirements.txt')) {
      return { code: 1, stdout: '', stderr: 'ERROR: network unreachable' };
    }
    if (call.script?.includes('CA_STOP_DONE')) return { code: 0, stdout: 'CA_STOP_DONE' };
    if (call.script?.includes('getsockname')) return { code: 0, stdout: '39555' };
    return { code: 0, stdout: '' };
  });
}

function setupBackend({ config = remoteTestConfig(), runner = fullRunner(), fakeSpawn } = {}) {
  const spawn =
    fakeSpawn ||
    createFakeSpawn({
      onSpawn(call) {
        if (!call.args.includes('-N')) call.child.emitData('stderr', READY_LINE);
      },
    });
  const fetchImpl = createFakeFetch(() => healthResponse());
  const deps = {
    spawn: spawn.spawn,
    fetch: fetchImpl,
    runProcess: runner,
    findFreePort: async () => 45678,
    env: {},
    cwd: '/nonexistent-local-cwd',
  };
  const backend = createRemoteBackend({ config, logger, deps });
  return { backend, spawn, runner, fetchImpl, deps };
}

test('remote backend probe delegates to the SSH probe report', async () => {
  const runner = createFakeRunner(() => ({ code: 0, stdout: probeExistingInstall(), stderr: '' }));
  const { backend } = setupBackend({ runner });
  try {
    const report = await backend.probe();
    assert.equal(report.target, 'remote');
    assert.equal(report.ok, true);
    const overridden = await backend.probe('other@other-box');
    assert.equal(overridden.ssh.target, 'other@other-box');
  } finally {
    await backend.dispose();
  }
});

test('remote backend installs, tracks install status and exposes the result', async () => {
  const checkout = makeLocalCheckout();
  try {
    const config = remoteTestConfig({
      remote: {
        ssh: { host: 'linux-box', user: 'yu' },
        repoPath: null,
        installDir: '/home/yu/ca-install',
        db: '/home/yu/CA_OH/openharmony.db',
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 2000,
        maxRestarts: 0,
        restartBackoffMs: 10,
        env: {},
      },
    });
    const { backend } = setupBackend({ config });
    try {
      assert.equal(backend.getInstallStatus().state, 'idle');
      const report = await backend.install({ localRepo: checkout, timeoutMs: 5000 });
      assert.equal(report.ok, true);
      assert.equal(report.mode, 'rsync');
      assert.equal(report.version.version, '0.1.0');
      const status = backend.getInstallStatus();
      assert.equal(status.state, 'succeeded');
      assert.equal(status.steps.length, 7);
      assert.equal(status.result.repoPath, '/home/yu/ca-install');
      assert.ok(status.finishedAt >= status.startedAt);
    } finally {
      await backend.dispose();
    }
  } finally {
    cleanupDir(checkout);
  }
});

test('remote backend startInstall returns immediately and can be polled', async () => {
  const checkout = makeLocalCheckout();
  try {
    const config = remoteTestConfig({
      remote: {
        ssh: { host: 'linux-box', user: 'yu' },
        repoPath: null,
        installDir: '/home/yu/ca-install',
        db: '/home/yu/CA_OH/openharmony.db',
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 2000,
        maxRestarts: 0,
        restartBackoffMs: 10,
        env: {},
      },
    });
    const { backend } = setupBackend({ config });
    try {
      const first = backend.startInstall({ localRepo: checkout, timeoutMs: 5000 });
      assert.equal(first.started, true);
      const second = backend.startInstall({ localRepo: checkout });
      assert.equal(second.started, false);
      assert.equal(second.running, true);
      await waitFor(() => backend.getInstallStatus().state !== 'running', {
        label: 'background install finished',
      });
      assert.equal(backend.getInstallStatus().state, 'succeeded');
    } finally {
      await backend.dispose();
    }
  } finally {
    cleanupDir(checkout);
  }
});

test('remote backend install failures are thrown and recorded in install status', async () => {
  const checkout = makeLocalCheckout();
  try {
    const config = remoteTestConfig({
      remote: {
        ssh: { host: 'linux-box', user: 'yu' },
        repoPath: null,
        installDir: '/home/yu/ca-install',
        db: '/home/yu/CA_OH/openharmony.db',
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 2000,
        maxRestarts: 0,
        restartBackoffMs: 10,
        env: {},
      },
    });
    const { backend } = setupBackend({ config, runner: fullRunner({ failPip: true }) });
    try {
      await assert.rejects(
        backend.install({ localRepo: checkout, timeoutMs: 5000 }),
        (error) => error instanceof BackendError && error.code === 'INSTALL_STEP_FAILED',
      );
      const status = backend.getInstallStatus();
      assert.equal(status.state, 'failed');
      assert.ok(status.error);
      assert.equal(status.steps.find((step) => step.id === 'dependencies').status, 'failed');
    } finally {
      await backend.dispose();
    }
  } finally {
    cleanupDir(checkout);
  }
});

test('remote backend lifecycle start/stop over the fake ssh tunnel', async () => {
  const { backend, spawn } = setupBackend();
  try {
    const started = await backend.start();
    assert.equal(started.state, 'running');
    assert.equal(started.baseUrl, 'http://127.0.0.1:45678');
    assert.equal(started.version, '0.1.0');
    const stopped = await backend.stop();
    assert.equal(stopped.state, 'stopped');
    const tunnelChild = spawn.calls.find((call) => call.args.includes('-N')).child;
    assert.ok(tunnelChild.killSignals.includes('SIGTERM'));
  } finally {
    await backend.dispose();
  }
});

test('manager wires the remote backend: probe, install, start, request, status', async () => {
  const root = makeTempDir('dshca-manager-remote-');
  const paths = makePaths(root);
  const checkout = makeLocalCheckout();
  const runner = fullRunner();
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      if (!call.args.includes('-N')) call.child.emitData('stderr', READY_LINE);
    },
  });
  const fetchImpl = createFakeFetch((call) => {
    if (call.urlObject.pathname === '/api/health') return healthResponse();
    return { ok: true, status: 200, headers: new Headers(), text: async () => JSON.stringify({ ok: true, data: {} }) };
  });
  const explicit = {
    backend: 'remote',
    requestTimeoutMs: 5000,
    readyTimeoutMs: 5000,
    healthIntervalMs: 50,
    remote: {
      ssh: { host: 'linux-box', user: 'yu' },
      repoPath: '/home/yu/CA',
      installDir: null,
      db: '/home/yu/CA_OH/openharmony.db',
      serveRemotePort: 0,
      localPort: 0,
      readyTimeoutMs: 2000,
      maxRestarts: 0,
      restartBackoffMs: 10,
    },
  };
  const manager = createBackendManager(null, explicit, {
    paths,
    env: {},
    cwd: '/nonexistent-local-cwd',
    homedir: root,
    spawn: fakeSpawn.spawn,
    fetch: fetchImpl,
    runProcess: runner,
    findFreePort: async () => 45678,
  });
  try {
    assert.equal(manager.getStatus().mode, 'remote');
    assert.equal(manager.getStatus().state, 'stopped');

    const probe = await manager.probeRemote();
    assert.equal(probe.target, 'remote');
    assert.equal(typeof probe.ok, 'boolean');

    const install = await manager.installRemote({
      localRepo: checkout,
      repoPath: null,
      installDir: '/home/yu/ca-install',
      timeoutMs: 5000,
    });
    assert.equal(install.ok, true);
    assert.equal(manager.getInstallStatus().state, 'succeeded');

    const started = await manager.start();
    assert.equal(started.mode, 'remote');
    assert.equal(started.state, 'running');
    assert.equal(started.baseUrl, 'http://127.0.0.1:45678');
    const health = await manager.request('/api/health');
    assert.equal(health.ok, true);
    assert.equal(manager.getStatus().version, '0.1.0');

    const stopped = await manager.stop();
    assert.equal(stopped.state, 'stopped');
  } finally {
    await manager.dispose();
    cleanupDir(checkout);
    cleanupDir(root);
  }
});

test('manager installRemote accepts an ssh override when config has no host', async () => {
  const root = makeTempDir('dshca-manager-install-');
  const paths = makePaths(root);
  const checkout = makeLocalCheckout();
  const runner = fullRunner();
  const manager = createBackendManager(
    null,
    { backend: 'auto', remote: { installDir: '/home/yu/ca-install', db: '/home/yu/x.db' } },
    {
      paths,
      env: {},
      cwd: '/nonexistent-local-cwd',
      homedir: root,
      spawn: createFakeSpawn({ onSpawn() {} }).spawn,
      fetch: createFakeFetch(() => healthResponse()),
      runProcess: runner,
      findFreePort: async () => 45678,
    },
  );
  try {
    await assert.rejects(
      manager.installRemote({ localRepo: checkout }),
      (error) => error.code === 'BACKEND_NOT_CONFIGURED',
    );
    const report = await manager.installRemote({
      ssh: 'yu@linux-box',
      localRepo: checkout,
      repoPath: null,
      installDir: '/home/yu/ca-install',
      timeoutMs: 5000,
    });
    assert.equal(report.ok, true);
    assert.equal(runner.calls[0].args.includes('yu@linux-box'), true);
  } finally {
    await manager.dispose();
    cleanupDir(checkout);
    cleanupDir(root);
  }
});
