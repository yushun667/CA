import assert from 'node:assert/strict';
import test from 'node:test';

import { BackendError } from '../../../lib/host/backend/errors.js';
import { createLogger } from '../../../lib/host/backend/log.js';
import {
  listRemoteDatabases,
  validateRemoteDatabase,
} from '../../../lib/host/backend/remote/databases.js';
import { createRemoteExecutor } from '../../../lib/host/backend/remote/exec.js';
import { createFakeRunner, remoteTestConfig } from './helpers.mjs';

const logger = createLogger({ dir: null, ringSize: 100 });

function markerEntries(entries) {
  return [
    'CA_DB_SCAN_ROOT=/home/yu/CA_OH/index',
    ...entries.map((entry) => `CA_DB_ENTRY:${JSON.stringify(entry)}`),
    'CA_DB_DONE=1',
  ].join('\n');
}

function makeExecutor(runner) {
  return createRemoteExecutor({ deps: { runProcess: runner }, logger });
}

const SCAN_ENTRIES = [
  {
    kind: 'dbDir',
    path: '/home/yu/CA_OH/index',
    name: 'index',
    sizeBytes: null,
    mtime: 1700000000000,
    dbCount: 15,
    source: 'scan',
  },
  {
    kind: 'db',
    path: '/home/yu/CA_OH/index/oh_S1.db',
    name: 'oh_S1.db',
    sizeBytes: 4096,
    mtime: 1700000000000,
    source: 'scan',
  },
  {
    kind: 'dbDir',
    path: '/home/yu/CA_OH/index/functional',
    name: 'functional',
    sizeBytes: null,
    mtime: 1700000000000,
    dbCount: 3,
    source: 'scan',
  },
];

test('listRemoteDatabases scans one ssh round trip and marks the current target', async () => {
  const runner = createFakeRunner(() => ({
    code: 0,
    stdout: markerEntries(SCAN_ENTRIES),
    stderr: '',
  }));
  const config = remoteTestConfig({
    remote: {
      ssh: { host: 'linux-box', user: 'yu' },
      repoPath: '/home/yu/CA',
      db: null,
      dbDir: '/home/yu/CA_OH/index',
      serveRemotePort: 0,
      localPort: 0,
      readyTimeoutMs: 2000,
      maxRestarts: 0,
      restartBackoffMs: 10,
      env: {},
    },
  });
  const report = await listRemoteDatabases({
    config,
    executor: makeExecutor(runner),
    logger,
  });
  assert.equal(report.ok, true);
  assert.equal(report.mode, 'remote');
  assert.deepEqual(report.current, { db: null, dbDir: '/home/yu/CA_OH/index' });
  assert.equal(report.candidates[0].path, '/home/yu/CA_OH/index');
  assert.equal(report.candidates[0].kind, 'dbDir');
  assert.equal(report.candidates[0].current, true);
  assert.equal(report.candidates[0].source, 'configured');
  assert.equal(report.candidates[0].dbCount, 15);
  assert.deepEqual(report.roots, ['/home/yu/CA_OH/index']);
  assert.equal(runner.calls.length, 1, 'exactly one ssh round trip');
  assert.match(runner.calls[0].script, /CA_DB_ENTRY/);
  assert.match(runner.calls[0].script, /\/home\/yu\/CA_OH\/index/);
  assert.ok(runner.calls[0].args.includes('yu@linux-box'));
});

test('listRemoteDatabases uses the configured single DB directory as root', async () => {
  const runner = createFakeRunner(() => ({
    code: 0,
    stdout: markerEntries([
      {
        kind: 'db',
        path: '/home/yu/CA_OH/layered-oh.db',
        name: 'layered-oh.db',
        sizeBytes: 99,
        mtime: 1700000000000,
        source: 'scan',
      },
    ]),
    stderr: '',
  }));
  const config = remoteTestConfig({
    remote: {
      ssh: { host: 'linux-box' },
      repoPath: '/home/yu/CA',
      db: '/home/yu/CA_OH/layered-oh.db',
      dbDir: null,
      serveRemotePort: 0,
      localPort: 0,
      readyTimeoutMs: 2000,
      maxRestarts: 0,
      restartBackoffMs: 10,
      env: {},
    },
  });
  const report = await listRemoteDatabases({ config, executor: makeExecutor(runner), logger });
  assert.equal(report.ok, true);
  assert.equal(report.candidates[0].path, '/home/yu/CA_OH/layered-oh.db');
  assert.equal(report.candidates[0].kind, 'db');
  assert.match(runner.calls[0].script, /ROOT=\/home\/yu\/CA_OH/);
});

test('listRemoteDatabases returns structured errors without throwing', async () => {
  const noConfig = await listRemoteDatabases({
    config: remoteTestConfig({ remote: { ssh: { host: 'linux-box' }, db: null, dbDir: null } }),
    executor: makeExecutor(createFakeRunner()),
    logger,
  });
  assert.equal(noConfig.ok, false);
  assert.equal(noConfig.error.code, 'BACKEND_NOT_CONFIGURED');

  const authFailure = await listRemoteDatabases({
    config: remoteTestConfig(),
    executor: makeExecutor(
      createFakeRunner(() => ({ code: 255, stdout: '', stderr: 'Permission denied (publickey).' })),
    ),
    logger,
  });
  assert.equal(authFailure.ok, false);
  assert.equal(authFailure.error.code, 'SSH_AUTH_FAILED');
  assert.deepEqual(authFailure.candidates, []);

  const missingRoot = await listRemoteDatabases({
    config: remoteTestConfig(),
    executor: makeExecutor(createFakeRunner(() => ({ code: 0, stdout: 'CA_DB_ERROR=root_missing', stderr: '' }))),
    logger,
  });
  assert.equal(missingRoot.ok, false);
  assert.equal(missingRoot.error.code, 'DB_NOT_FOUND');
  assert.match(missingRoot.error.message, /不存在/);

  const incomplete = await listRemoteDatabases({
    config: remoteTestConfig(),
    executor: makeExecutor(createFakeRunner(() => ({ code: 0, stdout: 'CA_DB_SCAN_ROOT=/x', stderr: '' }))),
    logger,
  });
  assert.equal(incomplete.ok, false);
  assert.equal(incomplete.error.code, 'DB_SCAN_FAILED');
});

test('listRemoteDatabases caps candidates and flags truncation', async () => {
  const many = [];
  for (let index = 0; index < 20; index += 1) {
    many.push({
      kind: 'db',
      path: `/home/yu/CA_OH/index/shard_${index}.db`,
      name: `shard_${index}.db`,
      sizeBytes: 100 + index,
      mtime: 1700000000000 + index,
      source: 'scan',
    });
  }
  const runner = createFakeRunner(() => ({ code: 0, stdout: markerEntries(many), stderr: '' }));
  const report = await listRemoteDatabases({
    config: remoteTestConfig(),
    executor: makeExecutor(runner),
    logger,
    maxItems: 5,
  });
  assert.equal(report.ok, true);
  assert.equal(report.candidates.length, 5);
  assert.equal(report.truncated, true);
  assert.equal(report.maxItems, 5);
});

test('validateRemoteDatabase accepts valid targets and throws for invalid ones', async () => {
  const okRunner = createFakeRunner((call) => {
    if (call.script?.includes('CA_DB_VALID')) return { code: 0, stdout: 'CA_DB_VALID=1:OK:3', stderr: '' };
    return { code: 0, stdout: '', stderr: '' };
  });
  const dirResult = await validateRemoteDatabase({
    config: remoteTestConfig(),
    executor: makeExecutor(okRunner),
    kind: 'dbDir',
    path: '/home/yu/CA_OH/index',
    logger,
  });
  assert.equal(dirResult.ok, true);
  assert.equal(dirResult.dbCount, 3);
  assert.equal(okRunner.calls.length, 1);

  const dbRunner = createFakeRunner(() => ({ code: 0, stdout: 'CA_DB_VALID=1:OK:1:4096', stderr: '' }));
  const dbResult = await validateRemoteDatabase({
    config: remoteTestConfig(),
    executor: makeExecutor(dbRunner),
    kind: 'db',
    path: '/home/yu/CA_OH/layered-oh.db',
    logger,
  });
  assert.equal(dbResult.ok, true);
  assert.equal(dbResult.sizeBytes, 4096);

  const invalidRunner = createFakeRunner(() => ({ code: 0, stdout: 'CA_DB_VALID=0:NO_DB', stderr: '' }));
  await assert.rejects(
    validateRemoteDatabase({
      config: remoteTestConfig(),
      executor: makeExecutor(invalidRunner),
      kind: 'dbDir',
      path: '/home/yu/empty',
      logger,
    }),
    (error) => {
      assert.ok(error instanceof BackendError);
      assert.equal(error.code, 'DB_NOT_FOUND');
      assert.equal(error.details.reason, 'NO_DB');
      return true;
    },
  );

  const sshFailure = createFakeRunner(() => ({ code: 255, stdout: '', stderr: 'Connection refused' }));
  await assert.rejects(
    validateRemoteDatabase({
      config: remoteTestConfig(),
      executor: makeExecutor(sshFailure),
      kind: 'dbDir',
      path: '/home/yu/index',
      logger,
    }),
    (error) => error.code === 'SSH_UNREACHABLE',
  );
});

test('validateRemoteDatabase validates its arguments before touching ssh', async () => {
  const runner = createFakeRunner();
  await assert.rejects(
    validateRemoteDatabase({
      config: remoteTestConfig(),
      executor: makeExecutor(runner),
      kind: 'nope',
      path: '/x',
      logger,
    }),
    (error) => error.code === 'INVALID_ARGUMENT',
  );
  await assert.rejects(
    validateRemoteDatabase({
      config: remoteTestConfig(),
      executor: makeExecutor(runner),
      kind: 'db',
      path: '',
      logger,
    }),
    (error) => error.code === 'INVALID_ARGUMENT',
  );
  assert.equal(runner.calls.length, 0);
});
