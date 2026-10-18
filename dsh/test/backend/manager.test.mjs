import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { createBackendManager } from '../../lib/host/backend/manager.js';
import { BackendError } from '../../lib/host/backend/errors.js';
import {
  cleanupDir,
  createFakeFetch,
  createFakeSpawn,
  createSseResponse,
  healthResponse,
  jsonResponse,
  makePaths,
  makeTempDir,
  nextTick,
} from './helpers/fakes.mjs';

const READY_URL = 'http://127.0.0.1:48123';
const READY_LINE = `[serve] CA backend listening on ${READY_URL} (db=/tmp/ca.db; federated=no)\n`;

function makeTarget(overrides = {}) {
  return {
    db: '/tmp/ca.db',
    dbDir: null,
    shards: 0,
    repoRoot: '/repo',
    repoSource: 'configured',
    python: { path: '/usr/bin/python3', source: 'configured' },
    cli: { kind: null, path: null, source: null },
    cwd: '/repo',
    env: {},
    stateDb: null,
    repo: null,
    compdb: null,
    warnings: [],
    ...overrides,
  };
}

function makeRuntime(overrides = {}) {
  return {
    repoRoot: '/repo',
    repoSource: 'configured',
    python: { path: '/usr/bin/python3', source: 'configured' },
    cli: { kind: null, path: null, source: null },
    cwd: '/repo',
    warnings: [],
    ...overrides,
  };
}

/**
 * Create a manager wired to fakes. Returns cleanup helpers so each test owns a
 * temp state dir; tests must call `await manager.dispose()` and cleanupDir(root).
 */
function setup(explicit = {}, options = {}) {
  const root = options.root || makeTempDir();
  const paths = options.paths || makePaths(root);
  const fakeSpawn =
    options.fakeSpawn ||
    createFakeSpawn({
      onSpawn(call) {
        call.child.emitData('stderr', READY_LINE);
      },
    });
  const sse = createSseResponse();
  const fetchImpl =
    options.fetchImpl ||
    createFakeFetch((call) => {
      const pathname = call.urlObject.pathname;
      if (pathname === '/api/health') return healthResponse();
      if (pathname === '/events') return sse.response;
      return jsonResponse({ ok: true, data: { path: pathname } });
    });
  const manager = createBackendManager(null, explicit, {
    paths,
    cwd: '/repo',
    env: {},
    homedir: '/tmp',
    now: () => 1700000000000,
    spawn: fakeSpawn.spawn,
    fetch: fetchImpl,
    resolveLocalTarget: async () => options.target || makeTarget(),
    resolveRuntime: () => options.runtime || makeRuntime(),
    findFreePort: async () => 43210,
    runProcess:
      options.runProcess ||
      (async () => ({
        command: 'ca',
        args: ['version'],
        code: 0,
        signal: null,
        stdout: JSON.stringify({ version: '0.1.0', schema_version: '2' }),
        stderr: '',
        spawnError: null,
      })),
  });
  return { manager, root, paths, fakeSpawn, fetchImpl, sse };
}

async function teardown(context) {
  try {
    await context.manager.dispose();
  } finally {
    cleanupDir(context.root);
  }
}

test('unconfigured manager exposes the documented status shape', async () => {
  const context = setup({});
  try {
    const status = context.manager.getStatus();
    assert.equal(status.mode, 'unconfigured');
    assert.equal(status.state, 'stopped');
    assert.equal(status.baseUrl, null);
    assert.equal(status.db, null);
    assert.equal(status.error, null);
    assert.equal(typeof status.since, 'number');
    assert.equal(status.version, null);
    assert.equal(status.features, null);
    assert.match(status.hint, /尚未配置/);
    assert.match(context.manager.config.paths.settingsPath, /settings\.json$/);
    assert.ok(Array.isArray(context.manager.getLogs(10)));
  } finally {
    await teardown(context);
  }
});

test('start on an unconfigured backend fails with BACKEND_NOT_CONFIGURED', async () => {
  const context = setup({});
  try {
    await assert.rejects(
      context.manager.start(),
      (error) => error instanceof BackendError && error.code === 'BACKEND_NOT_CONFIGURED',
    );
    assert.equal(context.manager.getStatus().mode, 'unconfigured');
  } finally {
    await teardown(context);
  }
});

test('configure persists a sparse patch and is picked up by a new manager', async () => {
  const root = makeTempDir();
  const paths = makePaths(root);
  const first = setup({}, { root, paths });
  try {
    const result = first.manager.configure({
      backend: 'local',
      local: { db: '/tmp/ca.db' },
    });
    assert.equal(result.changed, true);
    assert.equal(result.restartRequired, false);
    assert.equal(result.config.backend, 'local');
    assert.equal(result.config.local.db, '/tmp/ca.db');

    const stored = JSON.parse(fs.readFileSync(paths.settingsPath, 'utf8'));
    assert.equal(stored.version, 1);
    assert.deepEqual(stored.config, { backend: 'local', local: { db: '/tmp/ca.db' } });

    await first.manager.dispose();
    const second = setup({}, { root, paths });
    try {
      assert.equal(second.manager.config.backend, 'local');
      assert.equal(second.manager.config.local.db, '/tmp/ca.db');
    } finally {
      await second.manager.dispose();
    }
  } finally {
    cleanupDir(root);
  }
});

test('configure rejects invalid patches without touching settings.json', async () => {
  const context = setup({});
  try {
    context.manager.configure({ backend: 'local', local: { db: '/tmp/ca.db' } });
    const before = fs.readFileSync(context.paths.settingsPath, 'utf8');
    assert.throws(
      () => context.manager.configure({ backend: 'invalid-mode' }),
      (error) => error instanceof BackendError && error.code === 'CONFIG_INVALID',
    );
    assert.equal(fs.readFileSync(context.paths.settingsPath, 'utf8'), before);
    assert.equal(context.manager.config.backend, 'local');
  } finally {
    await teardown(context);
  }
});

test('local lifecycle through the manager: start, request, events, stop', async () => {
  const context = setup({ backend: 'local', local: { db: '/tmp/ca.db' } });
  const changes = [];
  const unsubscribe = context.manager.onChange((status) => changes.push(status.state));
  try {
    await assert.rejects(
      context.manager.request('/api/health'),
      (error) => error.code === 'BACKEND_NOT_RUNNING',
    );

    const started = await context.manager.start();
    assert.equal(started.mode, 'local');
    assert.equal(started.state, 'running');
    assert.equal(started.baseUrl, READY_URL);
    assert.equal(started.version, '0.1.0');

    const envelope = await context.manager.request('/api/search', {
      query: { q: 'syscall', limit: 5 },
    });
    assert.deepEqual(envelope, { ok: true, data: { path: '/api/search' } });
    const searchCall = context.fetchImpl.calls.find((call) => call.urlObject.pathname === '/api/search');
    assert.equal(searchCall.urlObject.searchParams.get('q'), 'syscall');
    assert.equal(searchCall.urlObject.searchParams.get('limit'), '5');

    // SSE through the manager
    const events = [];
    const stream = await context.manager.requestStream('/events', {
      onEvent: (event) => events.push(event),
    });
    context.sse.push('id: 1\ndata: {"type":"job.updated"}\n\n');
    await nextTick();
    stream.close();
    context.sse.close();
    await stream.done;
    assert.equal(events.length, 1);
    assert.equal(events[0].id, '1');

    const status = context.manager.getStatus();
    assert.equal(status.configChanged, false);
    assert.ok(
      context.manager.getLogs(50).some((entry) => /ca serve 就绪/.test(entry.line)),
      'local lifecycle is logged',
    );

    const stopped = await context.manager.stop();
    assert.equal(stopped.state, 'stopped');
    assert.equal(stopped.baseUrl, null);
    assert.ok(changes.includes('running'));
    assert.ok(changes.includes('stopped'));
    unsubscribe();
  } finally {
    await teardown(context);
  }
});

test('request validates paths and manager routes execCa to the local runtime', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call, index) {
      if (index === 0) {
        call.child.emitData('stderr', READY_LINE);
      } else {
        call.child.emitData('stdout', 'done\n');
        call.child.exit(0, null);
      }
    },
  });
  const context = setup({ backend: 'local', local: { db: '/tmp/ca.db' } }, { fakeSpawn });
  try {
    await context.manager.start();
    await assert.rejects(
      context.manager.request('http://evil.example/api/health'),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    await assert.rejects(context.manager.request(''), (error) => error.code === 'INVALID_ARGUMENT');

    const result = await context.manager.execCa(['version']);
    assert.equal(result.exitCode, 0);
    assert.deepEqual(fakeSpawn.last().args, ['-m', 'ca', 'version']);
  } finally {
    await teardown(context);
  }
});

test('configure while running reports restartRequired and marks configChanged', async () => {
  const context = setup({ backend: 'local', local: { db: '/tmp/ca.db' } });
  try {
    await context.manager.start();
    const result = context.manager.configure({ local: { db: '/tmp/other.db' } });
    assert.equal(result.changed, true);
    assert.equal(result.restartRequired, true);
    const status = context.manager.getStatus();
    assert.equal(status.configChanged, true);
    assert.equal(status.state, 'running', 'existing serve keeps running until restart');
  } finally {
    await teardown(context);
  }
});

test('auto mode prefers a configured remote host and probeRemote is non-throwing', async () => {
  const context = setup({
    backend: 'auto',
    local: { db: '/tmp/ca.db' },
    remote: { ssh: { host: 'linux-box' }, db: '/home/yu/CA_OH/openharmony.db' },
  });
  try {
    assert.equal(context.manager.getStatus().mode, 'remote');
    const report = await context.manager.probeRemote();
    assert.equal(report.target, 'remote');
    assert.equal(typeof report.ok, 'boolean');
    assert.ok(Array.isArray(report.checks));
    assert.deepEqual(context.manager.getInstallStatus().state, 'idle');
    await assert.rejects(
      context.manager.start(),
      (error) => error instanceof BackendError,
    );
  } finally {
    await teardown(context);
  }
});

test('probeLocal reports structural details and execCa rejects for unconfigured mode', async () => {
  const context = setup({});
  try {
    const report = await context.manager.probeLocal();
    assert.equal(report.target, 'local');
    assert.equal(typeof report.ok, 'boolean');
    assert.ok(Array.isArray(report.checks));
    assert.ok(report.checks.some((check) => check.name === 'database'));
    await assert.rejects(
      context.manager.execCa(['version']),
      (error) => error.code === 'BACKEND_NOT_CONFIGURED',
    );
  } finally {
    await teardown(context);
  }
});

test('dispose releases resources and blocks further operations', async () => {
  const context = setup({ backend: 'local', local: { db: '/tmp/ca.db' } });
  try {
    await context.manager.start();
    await context.manager.dispose();
    assert.equal(context.manager.getStatus().disposed, true);
    assert.equal(context.manager.getStatus().state, 'stopped');
    await assert.rejects(
      context.manager.start(),
      (error) => error.code === 'DISPOSED',
    );
    await assert.rejects(
      context.manager.request('/api/health'),
      (error) => error.code === 'DISPOSED',
    );
    await context.manager.dispose();
  } finally {
    cleanupDir(context.root);
  }
});

test('remote mode is wired end to end and failure paths are BackendErrors', async () => {
  const context = setup({
    backend: 'remote',
    remote: { ssh: { host: 'linux-box' }, db: '/home/yu/x.db' },
  });
  try {
    await assert.rejects(
      context.manager.start(),
      (error) => error instanceof BackendError,
    );
    const started = await context.manager.startRemoteInstall({});
    assert.equal(typeof started.started, 'boolean');
    // Concurrent foreground install rejects while the background one runs.
    await assert.rejects(
      context.manager.installRemote({}),
      (error) => error instanceof BackendError,
    );
    const installStatus = context.manager.getInstallStatus();
    assert.ok(['running', 'succeeded', 'failed'].includes(installStatus.state));
  } finally {
    await teardown(context);
  }
});


