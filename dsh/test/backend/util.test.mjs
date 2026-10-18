import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import {
  atomicWriteJson,
  clampInt,
  createDeferred,
  createLineDecoder,
  createRingBuffer,
  deepMerge,
  diffAgainstBase,
  expandPath,
  formatBytes,
  isExecutableFile,
  readJsonFile,
  sleep,
} from '../../lib/host/backend/util.js';
import { cleanupDir, makeTempDir, writeExecutable } from './helpers/fakes.mjs';

test('ring buffer keeps the newest items in oldest-first order', () => {
  const ring = createRingBuffer(3);
  for (const item of ['a', 'b', 'c', 'd', 'e']) ring.push(item);
  assert.deepEqual(ring.toArray(), ['c', 'd', 'e']);
  assert.deepEqual(ring.last(2), ['d', 'e']);
  assert.deepEqual(ring.last(10), ['c', 'd', 'e']);
  assert.equal(ring.size, 3);
  ring.clear();
  assert.deepEqual(ring.toArray(), []);
});

test('line decoder handles split chunks, CRLF, partial flush and truncation', () => {
  const lines = [];
  const decoder = createLineDecoder((line, info) => lines.push({ line, truncated: info.truncated }));
  decoder.push('one\ntw');
  decoder.push('o\r\nthree');
  decoder.flush();
  assert.deepEqual(
    lines.map((entry) => entry.line),
    ['one', 'two', 'three'],
  );

  const long = [];
  const truncating = createLineDecoder((line, info) => long.push({ line, truncated: info.truncated }), {
    maxLineLength: 256,
  });
  truncating.push('x'.repeat(100));
  truncating.push('y'.repeat(100));
  truncating.push('z'.repeat(100));
  truncating.push('\n');
  assert.equal(long.length, 1);
  assert.equal(long[0].truncated, true);
  assert.equal(long[0].line.length, 256);
  assert.ok(long[0].line.endsWith('z'.repeat(100)));
});

test('deepMerge merges nested objects and replaces arrays/scalars', () => {
  const base = { a: 1, nested: { x: 1, y: 2 }, list: [1, 2] };
  const merged = deepMerge(base, { nested: { y: 3 }, list: [9], extra: true });
  assert.deepEqual(merged, { a: 1, nested: { x: 1, y: 3 }, list: [9], extra: true });
  assert.deepEqual(base, { a: 1, nested: { x: 1, y: 2 }, list: [1, 2] }, 'base is not mutated');
  assert.deepEqual(deepMerge(base, { a: null }), { ...base, a: null });
});

test('diffAgainstBase keeps only changed leaves', () => {
  const base = { a: 1, nested: { x: 1, y: 2 }, list: [1] };
  const patch = { a: 2, nested: { x: 1, y: 9 }, list: [1], added: 'yes' };
  assert.deepEqual(diffAgainstBase(base, patch), { a: 2, nested: { y: 9 }, added: 'yes' });
});

test('expandPath resolves ~ and relative paths', () => {
  const previousHome = process.env.HOME;
  process.env.HOME = '/home/tester';
  try {
    assert.equal(expandPath('~/ca.db', '/tmp'), '/home/tester/ca.db');
    assert.equal(expandPath('./db/ca.db', '/tmp/work'), '/tmp/work/db/ca.db');
    assert.equal(expandPath('/abs/ca.db', '/tmp'), '/abs/ca.db');
    assert.equal(expandPath('', '/tmp'), null);
    assert.equal(expandPath(null, '/tmp'), null);
  } finally {
    process.env.HOME = previousHome;
  }
});

test('clampInt and formatBytes are defensive', () => {
  assert.equal(clampInt('5', 0, 10, 1), 5);
  assert.equal(clampInt(50, 0, 10, 1), 10);
  assert.equal(clampInt('nope', 0, 10, 3), 3);
  assert.equal(formatBytes(512), '512 B');
  assert.equal(formatBytes(1536), '1.5 KB');
  assert.equal(formatBytes(-1), '0 B');
});

test('atomicWriteJson writes parseable JSON with no leftover temp files', () => {
  const dir = makeTempDir();
  try {
    const file = path.join(dir, 'nested', 'settings.json');
    atomicWriteJson(file, { version: 1, config: { backend: 'local' } });
    atomicWriteJson(file, { version: 1, config: { backend: 'remote' } });
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.deepEqual(parsed, { version: 1, config: { backend: 'remote' } });
    const leftovers = fs.readdirSync(path.dirname(file)).filter((name) => name.endsWith('.tmp'));
    assert.deepEqual(leftovers, []);
  } finally {
    cleanupDir(dir);
  }
});

test('readJsonFile distinguishes missing and corrupt files', () => {
  const dir = makeTempDir();
  try {
    const missing = readJsonFile(path.join(dir, 'nope.json'));
    assert.equal(missing.exists, false);
    assert.equal(missing.error, null);

    const corrupt = path.join(dir, 'corrupt.json');
    fs.writeFileSync(corrupt, '{not json');
    const result = readJsonFile(corrupt);
    assert.equal(result.exists, true);
    assert.ok(result.error instanceof Error);
  } finally {
    cleanupDir(dir);
  }
});

test('isExecutableFile checks file mode', () => {
  const dir = makeTempDir();
  try {
    const plain = path.join(dir, 'plain');
    fs.writeFileSync(plain, 'x');
    fs.chmodSync(plain, 0o644);
    assert.equal(isExecutableFile(plain), false);
    const exec = path.join(dir, 'exec');
    writeExecutable(exec);
    assert.equal(isExecutableFile(exec), true);
    assert.equal(isExecutableFile(dir), false);
    assert.equal(isExecutableFile(path.join(dir, 'missing')), false);
  } finally {
    cleanupDir(dir);
  }
});

test('sleep rejects with AbortError when the signal aborts', async () => {
  const controller = new AbortController();
  const promise = sleep(5000, controller.signal);
  controller.abort();
  await assert.rejects(promise, (error) => error.name === 'AbortError');
});

test('createDeferred resolves/rejects once', async () => {
  const deferred = createDeferred();
  deferred.resolve(42);
  deferred.reject(new Error('ignored after resolve'));
  assert.equal(await deferred.promise, 42);
});
