import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import { createLocalBackend, normalizeCaArgs, READY_LINE_RE } from '../../lib/host/backend/local.js';
import { createLogger } from '../../lib/host/backend/log.js';
import {
  cleanupDir,
  createFakeFetch,
  createFakeSpawn,
  healthResponse,
  jsonResponse,
  makeTempDir,
  testConfig,
  waitFor,
  writeExecutable,
} from './helpers/fakes.mjs';

const READY_URL = 'http://127.0.0.1:48123';
const READY_LINE = `[serve] CA backend listening on ${READY_URL} (db=/tmp/ca.db; federated=no; state=/tmp/ca.db.serve-state.db)\n`;

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

function makeConfig(overrides = {}) {
  return testConfig({
    backend: 'local',
    requestTimeoutMs: 500,
    readyTimeoutMs: 400,
    healthIntervalMs: 10,
    local: { db: '/tmp/ca.db' },
    ...overrides,
  });
}

function makeBackend(options = {}) {
  const config = options.config || makeConfig();
  const fakeSpawn = options.fakeSpawn || createFakeSpawn({ onSpawn() {} });
  const fetchImpl =
    options.fetchImpl || createFakeFetch(() => healthResponse());
  const logger = createLogger({ dir: null, ringSize: 300 });
  const backend = createLocalBackend({
    config,
    logger,
    deps: {
      spawn: fakeSpawn.spawn,
      fetch: fetchImpl,
      cwd: '/repo',
      env: {},
      homedir: '/tmp',
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
    },
  });
  return { backend, fakeSpawn, fetchImpl, logger, config };
}

test('READY_LINE_RE extracts the URL from the CA serve stderr line', () => {
  const match = READY_LINE_RE.exec(READY_LINE);
  assert.equal(match[1], READY_URL);
  assert.equal(READY_LINE_RE.exec('unrelated output'), null);
});

test('normalizeCaArgs accepts arrays and {args} and rejects garbage', () => {
  assert.deepEqual(normalizeCaArgs(['index', '--repo', '/r']), ['index', '--repo', '/r']);
  assert.deepEqual(normalizeCaArgs({ args: ['version'] }), ['version']);
  assert.throws(() => normalizeCaArgs([]), (error) => error.code === 'INVALID_ARGUMENT');
  assert.throws(() => normalizeCaArgs({}), (error) => error.code === 'INVALID_ARGUMENT');
  assert.throws(() => normalizeCaArgs('index'), (error) => error.code === 'INVALID_ARGUMENT');
});

test('local backend starts ca serve, waits for the ready line + health, then stops', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stderr', READY_LINE);
    },
  });
  const fetchImpl = createFakeFetch(() => healthResponse());
  const { backend } = makeBackend({ fakeSpawn, fetchImpl });
  const states = [];
  backend.subscribe((status) => states.push(status.state));
  try {
    const started = await backend.start();
    assert.equal(started.mode, 'local');
    assert.equal(started.state, 'running');
    assert.equal(started.baseUrl, READY_URL);
    assert.equal(started.db, '/tmp/ca.db');
    assert.equal(started.version, '0.1.0');
    assert.equal(started.features.jobs, true);
    assert.deepEqual(fakeSpawn.calls[0].args, [
      '-m',
      'ca',
      'serve',
      '--host',
      '127.0.0.1',
      '--port',
      '0',
      '--db',
      '/tmp/ca.db',
    ]);
    assert.ok(
      fetchImpl.calls.some((call) => call.url === `${READY_URL}/api/health`),
      'health was polled on the reported URL',
    );
    assert.deepEqual(states.slice(0, 2), ['starting', 'running']);

    const stopped = await backend.stop();
    assert.equal(stopped.state, 'stopped');
    assert.equal(stopped.baseUrl, null);
    assert.ok(fakeSpawn.calls[0].child.killSignals.includes('SIGTERM'));
    assert.equal(states.at(-1), 'stopped');
  } finally {
    await backend.dispose();
  }
});

test('local backend start is idempotent while starting', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stderr', READY_LINE);
    },
  });
  const { backend } = makeBackend({ fakeSpawn });
  try {
    const [first, second] = await Promise.all([backend.start(), backend.start()]);
    assert.equal(first.state, 'running');
    assert.equal(second.state, 'running');
    assert.equal(fakeSpawn.calls.length, 1);
  } finally {
    await backend.dispose();
  }
});

test('local backend reports SERVE_EXITED when ca serve dies before readiness', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stderr', 'error: cannot bind 127.0.0.1:8765\n');
      call.child.exit(1, null);
    },
  });
  const { backend } = makeBackend({ fakeSpawn, config: makeConfig({ readyTimeoutMs: 1000 }) });
  try {
    await assert.rejects(
      backend.start(),
      (error) => {
        assert.ok(error instanceof BackendError);
        assert.equal(error.code, 'SERVE_EXITED');
        assert.match(error.message, /exitCode=1/);
        assert.match(error.details.logs, /cannot bind/);
        return true;
      },
    );
    const status = backend.getStatus();
    assert.equal(status.state, 'error');
    assert.match(status.error, /意外退出|退出/);
  } finally {
    await backend.dispose();
  }
});

test('local backend falls back to a chosen port when stderr URL is missing', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call, index) {
      if (index > 0) {
        call.child.emitData(
          'stderr',
          '[serve] CA backend listening on http://127.0.0.1:43210 (db=/tmp/ca.db)\n',
        );
      }
    },
  });
  const fetchImpl = createFakeFetch(() => healthResponse());
  const { backend } = makeBackend({
    fakeSpawn,
    fetchImpl,
    config: makeConfig({ readyTimeoutMs: 80, healthIntervalMs: 5 }),
  });
  try {
    const status = await backend.start();
    assert.equal(status.state, 'running');
    assert.equal(status.baseUrl, 'http://127.0.0.1:43210');
    assert.equal(fakeSpawn.calls.length, 2);
    assert.ok(fakeSpawn.calls[1].args.includes('43210'), 'fallback port passed to ca serve');
    assert.ok(fakeSpawn.calls[0].child.killSignals.length > 0, 'first attempt was killed');
  } finally {
    await backend.dispose();
  }
});

test('local backend polls a fixed port when no ready line is parsed', async () => {
  const fakeSpawn = createFakeSpawn({ onSpawn() {} });
  const fetchImpl = createFakeFetch(() => healthResponse());
  const config = makeConfig({ local: { db: '/tmp/ca.db', port: 8123 } });
  const { backend } = makeBackend({ fakeSpawn, fetchImpl, config });
  try {
    const status = await backend.start();
    assert.equal(status.baseUrl, 'http://127.0.0.1:8123');
    assert.equal(fakeSpawn.calls.length, 1);
    assert.ok(fakeSpawn.calls[0].args.includes('8123'));
    assert.ok(fetchImpl.calls.some((call) => call.url.startsWith('http://127.0.0.1:8123/api/health')));
  } finally {
    await backend.dispose();
  }
});

test('local backend restarts after a crash up to maxRestarts', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stderr', READY_LINE);
    },
  });
  const config = makeConfig({
    local: { db: '/tmp/ca.db', maxRestarts: 1, restartBackoffMs: 10 },
  });
  const { backend } = makeBackend({ fakeSpawn, config });
  try {
    await backend.start();
    assert.equal(fakeSpawn.calls.length, 1);
    fakeSpawn.last().child.exit(9, null);
    await waitFor(() => backend.getStatus().state === 'error', { label: 'crash recorded' });
    assert.match(backend.getStatus().error, /exitCode=9/);

    await waitFor(() => fakeSpawn.calls.length === 2, { label: 'automatic restart' });
    await waitFor(() => backend.getStatus().state === 'running', { label: 'restarted backend running' });
    assert.equal(backend.getStatus().restarts, 1);

    fakeSpawn.last().child.exit(9, null);
    await waitFor(() => backend.getStatus().state === 'error', { label: 'second crash recorded' });
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.equal(fakeSpawn.calls.length, 2, 'restart limit stops the crash loop');
  } finally {
    await backend.dispose();
  }
});

test('execCa streams stdout/stderr lines and returns the exit code', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stdout', 'parsed TU 1\nparsed TU 2\n');
      call.child.emitData('stderr', 'progress 50%\n');
      call.child.exit(0, null);
    },
  });
  const { backend } = makeBackend({ fakeSpawn });
  try {
    const stdout = [];
    const stderr = [];
    const result = await backend.execCa(['index', '--repo', '/r'], {
      onStdout: (line) => stdout.push(line),
      onStderr: (line) => stderr.push(line),
    });
    assert.equal(result.exitCode, 0);
    assert.equal(result.cancelled, false);
    assert.deepEqual(stdout, ['parsed TU 1', 'parsed TU 2']);
    assert.deepEqual(stderr, ['progress 50%']);
    assert.deepEqual(fakeSpawn.calls[0].args, ['-m', 'ca', 'index', '--repo', '/r']);
  } finally {
    await backend.dispose();
  }
});

test('execCa resolves non-zero exits and rejects spawn failures', async () => {
  const nonzero = makeBackend({
    fakeSpawn: createFakeSpawn({
      onSpawn(call) {
        call.child.exit(2, null);
      },
    }),
  });
  try {
    assert.equal((await nonzero.backend.execCa(['query'])).exitCode, 2);
  } finally {
    await nonzero.backend.dispose();
  }

  const broken = makeBackend({
    fakeSpawn: createFakeSpawn({
      onSpawn(call) {
        const error = new Error('spawn /usr/bin/python3 ENOENT');
        error.code = 'ENOENT';
        call.child.emit('error', error);
      },
    }),
  });
  try {
    await assert.rejects(
      broken.backend.execCa(['version']),
      (error) => error instanceof BackendError && error.code === 'SPAWN_FAILED',
    );
  } finally {
    await broken.backend.dispose();
  }
});

test('execCa cancels a long task through the abort signal', async () => {
  const fakeSpawn = createFakeSpawn({ onSpawn() {} });
  const { backend } = makeBackend({ fakeSpawn });
  try {
    const controller = new AbortController();
    const promise = backend.execCa(['index', '--repo', '/r'], { signal: controller.signal });
    await waitFor(() => fakeSpawn.calls.length === 1, { label: 'exec started' });
    controller.abort();
    const result = await promise;
    assert.equal(result.cancelled, true);
    assert.ok(fakeSpawn.last().child.killsignals?.includes?.('SIGTERM') || fakeSpawn.last().child.killed);
  } finally {
    await backend.dispose();
  }
});

test('probeLocalEnvironment is reachable through backend.probe with injected runner', async () => {
  const root = makeTempDir();
  try {
    const repo = path.join(root, 'CA');
    fs.mkdirSync(path.join(repo, 'src', 'ca'), { recursive: true });
    fs.writeFileSync(path.join(repo, 'src', 'ca', '__init__.py'), '');
    fs.writeFileSync(path.join(repo, 'pyproject.toml'), '[project]\nname = "ca-analyzer"\n');
    writeExecutable(path.join(repo, '.venv', 'bin', 'python'));
    writeExecutable(path.join(repo, '.venv', 'bin', 'ca'));
    const db = path.join(root, 'ca.db');
    fs.writeFileSync(db, 'sqlite');
    const config = makeConfig({ local: { repoRoot: repo, db } });
    const { backend } = makeBackend({
      config,
      runProcess: async () => ({
        command: 'ca',
        args: ['version', '--json'],
        code: 0,
        signal: null,
        stdout: JSON.stringify({ version: '0.1.0', schema_version: '2' }),
        stderr: '',
        spawnError: null,
      }),
    });
    try {
      const report = await backend.probe();
      assert.equal(report.ok, true);
      assert.equal(report.ca.version, '0.1.0');
      assert.equal(report.checks.find((check) => check.name === 'database').ok, true);
    } finally {
      await backend.dispose();
    }
  } finally {
    cleanupDir(root);
  }
});

test('dispose stops a running serve and is idempotent', async () => {
  const fakeSpawn = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stderr', READY_LINE);
    },
  });
  const { backend } = makeBackend({ fakeSpawn });
  await backend.start();
  await backend.dispose();
  assert.equal(backend.getStatus().state, 'stopped');
  assert.ok(fakeSpawn.calls[0].child.killSignals.includes('SIGTERM'));
  await backend.dispose();
  await assert.rejects(backend.start(), (error) => error.code === 'DISPOSED');
});

test('buildServeArgs includes optional state/repo/compdb flags', () => {
  const config = makeConfig({
    local: {
      db: '/tmp/ca.db',
      stateDb: '/tmp/jobs.db',
      repo: '/src/openharmony',
      compdb: '/src/compdb.json',
      port: 0,
    },
  });
  const { backend } = makeBackend({ config });
  const args = backend.buildServeArgs({ db: '/tmp/ca.db', dbDir: null, stateDb: '/tmp/jobs.db' }, 0);
  assert.deepEqual(args, [
    'serve',
    '--host',
    '127.0.0.1',
    '--port',
    '0',
    '--db',
    '/tmp/ca.db',
    '--state-db',
    '/tmp/jobs.db',
    '--repo',
    '/src/openharmony',
    '--compdb',
    '/src/compdb.json',
  ]);
  const federated = backend.buildServeArgs({ db: null, dbDir: '/tmp/shards', stateDb: null }, 0);
  assert.deepEqual(federated.slice(3, 6), ['--port', '0', '--db-dir']);
  assert.equal(federated[6], '/tmp/shards');
});


