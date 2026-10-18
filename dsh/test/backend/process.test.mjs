import assert from 'node:assert/strict';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import {
  buildChildEnv,
  findFreePort,
  outputTail,
  runProcess,
  spawnManaged,
} from '../../lib/host/backend/process.js';
import { createFakeSpawn, nextTick } from './helpers/fakes.mjs';

test('spawnManaged splits output lines, records them and resolves on exit', async () => {
  const recorded = [];
  const logger = { record: (stream, line) => recorded.push({ stream, line }) };
  const fake = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stdout', 'hello\nwor');
      call.child.emitData('stdout', 'ld\n');
      call.child.emitData('stderr', 'warning: x\n');
      call.child.exit(0, null);
    },
  });
  const stdout = [];
  const stderr = [];
  const handle = spawnManaged({
    spawn: fake.spawn,
    command: 'ca',
    args: ['version'],
    logger,
    onStdoutLine: (line) => stdout.push(line),
    onStderrLine: (line) => stderr.push(line),
  });
  const info = await handle.exited;
  assert.equal(info.code, 0);
  assert.equal(info.signal, null);
  assert.equal(info.spawnError, null);
  assert.deepEqual(stdout, ['hello', 'world']);
  assert.deepEqual(stderr, ['warning: x']);
  assert.deepEqual(
    recorded.map((entry) => `${entry.stream}:${entry.line}`),
    ['stdout:hello', 'stdout:world', 'stderr:warning: x'],
  );
  assert.equal(fake.calls[0].command, 'ca');
  assert.deepEqual(fake.calls[0].args, ['version']);
  assert.equal(fake.calls[0].options.detached, true, 'POSIX children own a process group');
});

test('spawnManaged resolves spawn errors instead of throwing', async () => {
  const fake = createFakeSpawn({
    onSpawn(call) {
      const error = new Error('spawn ca ENOENT');
      error.code = 'ENOENT';
      call.child.emit('error', error);
    },
  });
  const handle = spawnManaged({ spawn: fake.spawn, command: 'ca', args: [] });
  const info = await handle.exited;
  assert.equal(info.code, null);
  assert.ok(info.spawnError);
  assert.match(info.spawnError.message, /ENOENT/);
});

test('spawnManaged kill records the signal and unblocks exit', async () => {
  const fake = createFakeSpawn({ onSpawn() {} });
  const handle = spawnManaged({ spawn: fake.spawn, command: 'ca', args: [] });
  assert.equal(handle.kill('SIGTERM'), true);
  const info = await handle.exited;
  assert.equal(info.signal, 'SIGTERM');
  assert.deepEqual(fake.last().child.killSignals, ['SIGTERM']);
});

test('spawnManaged throws SPAWN_FAILED for empty commands', () => {
  const fake = createFakeSpawn({});
  assert.throws(
    () => spawnManaged({ spawn: fake.spawn, command: '', args: [] }),
    (error) => error instanceof BackendError && error.code === 'SPAWN_FAILED',
  );
  assert.throws(
    () => spawnManaged({ spawn: null, command: 'ca', args: [] }),
    (error) => error.code === 'SPAWN_FAILED',
  );
});

test('runProcess captures output and reports non-zero exits', async () => {
  const fake = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stdout', '{"version":"0.1.0"}\n');
      call.child.emitData('stderr', 'progress 50%\n');
      call.child.exit(2, null);
    },
  });
  const result = await runProcess({
    spawn: fake.spawn,
    command: 'ca',
    args: ['version'],
    timeoutMs: 1000,
  });
  assert.equal(result.code, 2);
  assert.equal(result.timedOut, false);
  assert.equal(result.cancelled, false);
  assert.match(result.stdout, /0\.1\.0/);
  assert.match(result.stderr, /progress/);
  assert.equal(result.spawnError, null);
});

test('runProcess reports spawn errors', async () => {
  const fake = createFakeSpawn({
    onSpawn(call) {
      const error = new Error('spawn python3 ENOENT');
      error.code = 'ENOENT';
      call.child.emit('error', error);
    },
  });
  const result = await runProcess({ spawn: fake.spawn, command: 'python3', args: ['--version'] });
  assert.equal(result.code, null);
  assert.ok(result.spawnError);
});

test('runProcess kills on timeout', async () => {
  const fake = createFakeSpawn({ onSpawn() {} });
  const result = await runProcess({
    spawn: fake.spawn,
    command: 'ca',
    args: ['index'],
    timeoutMs: 20,
  });
  assert.equal(result.timedOut, true);
  assert.equal(result.code, null);
  assert.match(result.signal || '', /SIGTERM/);
  assert.ok(fake.last().child.killed);
});

test('runProcess kills on caller abort', async () => {
  const fake = createFakeSpawn({ onSpawn() {} });
  const controller = new AbortController();
  const promise = runProcess({
    spawn: fake.spawn,
    command: 'ca',
    args: ['index'],
    signal: controller.signal,
  });
  setTimeout(() => controller.abort(), 5);
  const result = await promise;
  assert.equal(result.cancelled, true);
  assert.ok(fake.last().child.killed);
});

test('buildChildEnv merges overrides, drops non-strings and forces unbuffered python', () => {
  const env = buildChildEnv({ CA_LIBCLANG_LIBRARY: '/lib.so', DROP: undefined }, { PATH: '/bin', BAD: 5 });
  assert.equal(env.PATH, '/bin');
  assert.equal(env.CA_LIBCLANG_LIBRARY, '/lib.so');
  assert.equal('BAD' in env, false);
  assert.equal(env.PYTHONUNBUFFERED, '1');
});

test('findFreePort returns a port that can be re-bound', async () => {
  const port = await findFreePort();
  assert.ok(Number.isInteger(port) && port > 0);
  const net = await import('node:net');
  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(port, '127.0.0.1', () => server.close(resolve));
  });
});

test('outputTail returns the last stderr/stdout lines', () => {
  assert.equal(
    outputTail({ stderr: 'a\nb\nc\nd', stdout: '' }, 2),
    'c\nd',
  );
  assert.equal(outputTail({ stderr: '', stdout: '' }), '');
});

test('spawnManaged flush emits a partial trailing line on exit', async () => {
  const fake = createFakeSpawn({
    onSpawn(call) {
      call.child.emitData('stdout', 'no-newline-at-end');
      call.child.exit(0, null);
    },
  });
  const lines = [];
  const handle = spawnManaged({
    spawn: fake.spawn,
    command: 'ca',
    args: [],
    onStdoutLine: (line) => lines.push(line),
  });
  await handle.exited;
  await nextTick();
  assert.deepEqual(lines, ['no-newline-at-end']);
});
