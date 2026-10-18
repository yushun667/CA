import assert from 'node:assert/strict';
import test from 'node:test';

import { BackendError } from '../../../lib/host/backend/errors.js';
import { createLogger } from '../../../lib/host/backend/log.js';
import { createRemoteServeController } from '../../../lib/host/backend/remote/serve.js';
import { createRemoteExecutor } from '../../../lib/host/backend/remote/exec.js';
import {
  createFakeFetch,
  createFakeSpawn,
  healthResponse,
  waitFor,
} from '../helpers/fakes.mjs';
import { createFakeRunner, remoteTestConfig } from './helpers.mjs';

const REMOTE_PORT = 39001;
const READY_LINE = `[serve] CA backend listening on http://127.0.0.1:${REMOTE_PORT} (db=/home/yu/CA_OH/openharmony.db; federated=no; state=/home/yu/CA_OH/openharmony.db.serve-state.db)\n`;
const logger = createLogger({ dir: null, ringSize: 300 });

function isTunnelCall(call) {
  return call.args.includes('-N');
}

/**
 * Build a remote serve controller with fake spawn/fetch/runProcess.
 * Serve ssh spawns print the readiness line unless `silentServe` is true.
 */
function setup({ config = remoteTestConfig(), silentServe = false, serveExit = null, runner } = {}) {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      if (isTunnelCall(call)) return;
      if (silentServe) return;
      call.child.emitData('stderr', READY_LINE);
      if (serveExit !== null) call.child.exit(serveExit, null);
    },
  });
  const fetchImpl = createFakeFetch(() => healthResponse());
  const runProcess =
    runner ||
    createFakeRunner((call) => {
      if (call.script?.includes('CA_STOP_DONE')) return { code: 0, stdout: 'CA_STOP_DONE' };
      if (call.script?.includes('getsockname')) return { code: 0, stdout: '39555' };
      return { code: 0, stdout: '' };
    });
  const deps = {
    spawn: fakeSpawn.spawn,
    fetch: fetchImpl,
    runProcess,
    findFreePort: async () => 45678,
    env: {},
    now: Date.now,
  };
  const executor = createRemoteExecutor({ deps, logger });
  const controller = createRemoteServeController({ config, deps, logger, executor });
  return { controller, fakeSpawn, fetchImpl, runProcess, executor, config };
}

test('remote serve starts the server over ssh, opens the tunnel and reports health', async () => {
  const { controller, fakeSpawn, fetchImpl } = setup();
  try {
    const started = await controller.start();
    assert.equal(started.mode, 'remote');
    assert.equal(started.state, 'running');
    assert.equal(started.baseUrl, 'http://127.0.0.1:45678');
    assert.equal(started.version, '0.1.0');
    assert.equal(started.db, '/home/yu/CA_OH/openharmony.db');
    assert.deepEqual(started.remote.remotePort, REMOTE_PORT);
    assert.equal(started.remote.localPort, 45678);
    assert.equal(started.remote.target, 'yu@linux-box');

    const spawns = fakeSpawn.calls;
    assert.equal(spawns.length, 2, 'one ssh for serve, one for the tunnel');
    const serveCall = spawns.find((call) => !isTunnelCall(call));
    assert.match(serveCall.script || serveCall.args.at(-1), /exec .* -m ca serve/);
    assert.match(serveCall.args.at(-1), /--port 0/);
    const tunnelCall = spawns.find(isTunnelCall);
    assert.ok(tunnelCall.args.includes('45678:127.0.0.1:39001'));
    assert.ok(tunnelCall.args.includes('ExitOnForwardFailure=yes'));
    assert.ok(fetchImpl.calls.some((call) => call.url === 'http://127.0.0.1:45678/api/health'));

    const stopped = await controller.stop();
    assert.equal(stopped.state, 'stopped');
    assert.equal(stopped.baseUrl, null);
    assert.equal(stopped.remote, null);
    const tunnelChild = tunnelCall.child;
    assert.ok(tunnelChild.killSignals.includes('SIGTERM'));
  } finally {
    await controller.dispose();
  }
});

test('remote serve uses a fixed remote and local port without a readiness line', async () => {
  const { controller, fakeSpawn } = setup({
    config: remoteTestConfig({
      remote: {
        ssh: { host: 'linux-box', user: 'yu' },
        repoPath: '/home/yu/CA',
        db: '/home/yu/CA_OH/openharmony.db',
        serveRemotePort: 39002,
        localPort: 47000,
        readyTimeoutMs: 2000,
        maxRestarts: 0,
        restartBackoffMs: 10,
        env: {},
      },
    }),
    silentServe: true,
  });
  try {
    const started = await controller.start();
    assert.equal(started.state, 'running');
    assert.equal(started.baseUrl, 'http://127.0.0.1:47000');
    const tunnelCall = fakeSpawn.calls.find(isTunnelCall);
    assert.ok(tunnelCall.args.includes('47000:127.0.0.1:39002'));
  } finally {
    await controller.dispose();
  }
});

test('remote serve falls back to a remote-chosen port when the readiness line is missing', async () => {
  // First serve ssh prints nothing, later attempts print the fallback URL.
  let serveAttempts = 0;
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      if (isTunnelCall(call)) return;
      serveAttempts += 1;
      if (serveAttempts > 1) {
        call.child.emitData(
          'stderr',
          '[serve] CA backend listening on http://127.0.0.1:39555 (db=/home/yu/CA_OH/openharmony.db)\n',
        );
      }
    },
  });
  const fetchImpl = createFakeFetch(() => healthResponse());
  const runProcess = createFakeRunner((call) => {
    if (call.script?.includes('getsockname')) return { code: 0, stdout: '39555' };
    if (call.script?.includes('CA_STOP_DONE')) return { code: 0, stdout: 'CA_STOP_DONE' };
    return { code: 0, stdout: '' };
  });
  const deps = {
    spawn: fakeSpawn.spawn,
    fetch: fetchImpl,
    runProcess,
    findFreePort: async () => 45678,
    env: {},
  };
  const executor = createRemoteExecutor({ deps, logger });
  const controller = createRemoteServeController({
    config: remoteTestConfig({
      remote: {
        ssh: { host: 'linux-box' },
        repoPath: '/home/yu/CA',
        db: '/home/yu/CA_OH/openharmony.db',
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 80,
        maxRestarts: 0,
        restartBackoffMs: 10,
        env: {},
      },
    }),
    deps,
    logger,
    executor,
  });
  try {
    const started = await controller.start();
    assert.equal(started.state, 'running');
    assert.equal(started.remote.remotePort, 39555);
    assert.equal(fakeSpawn.calls.filter((call) => !isTunnelCall(call)).length, 2, 'serve restarted once');
    const tunnelCall = fakeSpawn.calls.find(isTunnelCall);
    assert.ok(tunnelCall.args.includes('45678:127.0.0.1:39555'));
    assert.ok(runProcess.calls.some((call) => /getsockname/.test(call.script || '')));
  } finally {
    await controller.dispose();
  }
});

test('remote serve restarts after a crash within maxRestarts', async () => {
  const { controller, fakeSpawn } = setup({
    config: remoteTestConfig({
      remote: {
        ssh: { host: 'linux-box', user: 'yu' },
        repoPath: '/home/yu/CA',
        db: '/home/yu/CA_OH/openharmony.db',
        serveRemotePort: 39003,
        localPort: 0,
        readyTimeoutMs: 2000,
        maxRestarts: 1,
        restartBackoffMs: 10,
        env: {},
      },
    }),
    silentServe: true,
  });
  try {
    await controller.start();
    const firstServe = fakeSpawn.calls.find((call) => !isTunnelCall(call)).child;
    firstServe.exit(9, null);
    await waitFor(() => controller.getStatus().state === 'error', { label: 'crash recorded' });
    await waitFor(
      () => fakeSpawn.calls.filter((call) => !isTunnelCall(call)).length === 2,
      { label: 'remote serve restarted' },
    );
    await waitFor(() => controller.getStatus().state === 'running', { label: 'running after restart' });
    assert.equal(controller.getStatus().restarts, 1);

    const secondServe = fakeSpawn.calls.filter((call) => !isTunnelCall(call))[1].child;
    secondServe.exit(9, null);
    await waitFor(() => controller.getStatus().state === 'error', { label: 'second crash' });
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.equal(
      fakeSpawn.calls.filter((call) => !isTunnelCall(call)).length,
      2,
      'restart limit stops the crash loop',
    );
  } finally {
    await controller.dispose();
  }
});

test('remote serve execCa streams output and returns the exit code', async () => {
  const { controller, fakeSpawn } = setup();
  const execSpawn = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stdout', 'indexing TU 1\nindexing TU 2\n');
      call.child.exit(0, null);
    },
  });
  const deps = {
    spawn: execSpawn.spawn,
    fetch: createFakeFetch(() => healthResponse()),
    runProcess: createFakeRunner(),
    findFreePort: async () => 45678,
    env: {},
  };
  const executor = createRemoteExecutor({ deps, logger });
  const execController = createRemoteServeController({
    config: remoteTestConfig(),
    deps,
    logger,
    executor,
  });
  try {
    const lines = [];
    const result = await execController.execCa(['index', '--repo', '/home/yu/src/openharmony'], {
      onStdout: (line) => lines.push(line),
    });
    assert.equal(result.exitCode, 0);
    assert.equal(result.cancelled, false);
    assert.deepEqual(lines, ['indexing TU 1', 'indexing TU 2']);
    const script = execSpawn.calls[0].args.at(-1);
    assert.match(script, /exec .* -m ca index --repo \/home\/yu\/src\/openharmony/);
    assert.match(script, /task-.*\.pid/);
    assert.ok(fakeSpawn.calls.length === 0, 'serve lifecycle untouched by exec');
  } finally {
    await execController.dispose();
  }
});

test('remote serve execCa cancel kills ssh and the remote pid', async () => {
  const spawn = createFakeSpawn({ onSpawn() {} });
  const runProcess = createFakeRunner((call) => {
    if (call.script?.includes('CA_STOP_DONE')) return { code: 0, stdout: 'CA_STOP_DONE' };
    return { code: 0, stdout: '' };
  });
  const deps = {
    spawn: spawn.spawn,
    fetch: createFakeFetch(() => healthResponse()),
    runProcess,
    findFreePort: async () => 45678,
    env: {},
  };
  const executor = createRemoteExecutor({ deps, logger });
  const controller = createRemoteServeController({ config: remoteTestConfig(), deps, logger, executor });
  try {
    const abort = new AbortController();
    const promise = controller.execCa(['index', '--repo', '/r'], { signal: abort.signal });
    await waitFor(() => spawn.calls.length === 1, { label: 'exec started' });
    abort.abort();
    const result = await promise;
    assert.equal(result.cancelled, true);
    assert.ok(spawn.calls[0].child.killSignals.includes('SIGTERM'));
    await waitFor(
      () => runProcess.calls.some((call) => call.script?.includes('CA_STOP_DONE')),
      { label: 'remote pid killed via ssh' },
    );
  } finally {
    await controller.dispose();
  }
});

test('remote serve start fails with actionable configuration errors', async () => {
  const { controller } = setup();
  const noDb = setup({
    config: remoteTestConfig({
      remote: {
        ssh: { host: 'linux-box' },
        repoPath: '/home/yu/CA',
        db: null,
        dbDir: null,
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 500,
        maxRestarts: 0,
        restartBackoffMs: 10,
        env: {},
      },
    }),
  });
  try {
    await assert.rejects(
      noDb.controller.start(),
      (error) => error instanceof BackendError && error.code === 'BACKEND_NOT_CONFIGURED',
    );
  } finally {
    await noDb.controller.dispose();
    await controller.dispose();
  }
});

test('dispose stops a running remote serve and cleans listeners', async () => {
  const { controller, fakeSpawn } = setup();
  await controller.start();
  const serveChild = fakeSpawn.calls.find((call) => !isTunnelCall(call)).child;
  await controller.dispose();
  assert.equal(controller.getStatus().state, 'stopped');
  assert.ok(serveChild.killSignals.length > 0 || serveChild._exited);
  await controller.dispose();
});
