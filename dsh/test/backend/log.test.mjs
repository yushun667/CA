import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import { createLogger } from '../../lib/host/backend/log.js';
import { cleanupDir, makeTempDir } from './helpers/fakes.mjs';

test('logger keeps a bounded ring and notifies subscribers', () => {
  const entries = [];
  const logger = createLogger({ dir: null, ringSize: 16 });
  const unsubscribe = logger.subscribe((entry) => entries.push(entry));
  for (let index = 0; index < 20; index += 1) logger.info(`line-${index}`);
  assert.equal(logger.history(100).length, 16);
  assert.deepEqual(
    logger.history(100).map((entry) => entry.line),
    Array.from({ length: 16 }, (_, index) => `line-${index + 4}`),
  );
  assert.equal(entries.length, 20, 'subscribers see every line even when the ring overflows');
  unsubscribe();
  logger.info('after-unsubscribe');
  assert.equal(entries.length, 20);
  assert.equal(logger.filePath, null);
});

test('logger writes a dated file and rotates on size', () => {
  const dir = makeTempDir();
  try {
    const logger = createLogger({
      dir,
      fileBase: 'backend',
      maxBytes: 4096,
      keepFiles: 2,
      ringSize: 10,
    });
    const file = logger.filePath;
    assert.ok(path.basename(file).startsWith('backend.'));
    assert.ok(file.endsWith('.log'));
    // Enough payload to force at least two rotations.
    for (let index = 0; index < 200; index += 1) {
      logger.info(`line-${index}-${'x'.repeat(60)}`);
    }
    const files = fs.readdirSync(dir).filter((name) => name.startsWith('backend.'));
    assert.ok(files.length >= 2, `expected rotated files, got ${files.join(',')}`);
    assert.ok(files.some((name) => name.endsWith('.log.1')));
    const latest = files.find((name) => name.endsWith('.log'));
    assert.ok(fs.statSync(path.join(dir, latest)).size <= 8192, 'rotation bounds the live file');
    logger.close();
  } finally {
    cleanupDir(dir);
  }
});

test('logger swallows filesystem errors instead of throwing', () => {
  const logger = createLogger({ dir: '/definitely/not/writable/dsh-ca-test', ringSize: 5 });
  assert.doesNotThrow(() => logger.info('still works'));
  assert.deepEqual(
    logger.history(5).map((entry) => entry.line),
    ['still works'],
  );
});

test('BackendError carries code and JSON payload', () => {
  const error = new BackendError('SPAWN_FAILED', 'cannot spawn', { command: 'ca' });
  assert.equal(error.name, 'BackendError');
  assert.equal(error.code, 'SPAWN_FAILED');
  assert.deepEqual(error.toJSON(), {
    code: 'SPAWN_FAILED',
    message: 'cannot spawn',
    details: { command: 'ca' },
  });
  const bare = new BackendError('', '');
  assert.equal(bare.code, 'INTERNAL');
  assert.equal(bare.details, undefined);
});
