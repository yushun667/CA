import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import {
  MAX_DATABASE_ITEMS,
  buildDatabaseCatalog,
  classifyDatabaseEntry,
  createDatabaseErrorReport,
  isDatabaseFileName,
  isSkippedDirectoryName,
  normalizeDatabasePath,
  parseRemoteDatabaseScanOutput,
  parseRemoteDatabaseValidationOutput,
  scanLocalDatabaseCatalog,
  scanLocalDirectory,
  validateLocalDatabaseTarget,
} from '../../lib/host/backend/databases.js';
import { cleanupDir, makeTempDir } from './helpers/fakes.mjs';

function touch(file, content = 'sqlite') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function makeIndexTree(root) {
  const index = path.join(root, 'index');
  touch(path.join(index, 'oh_S1.db'));
  touch(path.join(index, 'oh_S2.db'));
  touch(path.join(index, 'oh_S3.db'));
  touch(path.join(index, 'oh_S1.db-wal'));
  touch(path.join(index, 'oh_S1.db-shm'));
  touch(path.join(index, 'notes.txt'));
  touch(path.join(index, 'functional', 'dsoftbus.db'));
  touch(path.join(index, 'functional', 'camera.db'));
  touch(path.join(index, 'failed_run', 'broken.db'));
  touch(path.join(index, '.cache', 'hidden.db'));
  touch(path.join(index, '.ca', 'jobs.db'));
  fs.mkdirSync(path.join(index, 'empty_dir'), { recursive: true });
  return index;
}

test('path helpers identify database files and skipped directories', () => {
  assert.equal(isDatabaseFileName('ca.db'), true);
  assert.equal(isDatabaseFileName('ca.db-wal'), false);
  assert.equal(isDatabaseFileName('ca.db-shm'), false);
  assert.equal(isDatabaseFileName('notes.txt'), false);
  assert.equal(isSkippedDirectoryName('.cache'), true);
  assert.equal(isSkippedDirectoryName('.ca'), true);
  assert.equal(isSkippedDirectoryName('.tmp'), true);
  assert.equal(isSkippedDirectoryName('index'), false);
  assert.equal(normalizeDatabasePath('/home/yu/index/'), '/home/yu/index');
  assert.equal(normalizeDatabasePath('/'), '/');
  assert.equal(normalizeDatabasePath('  '), '');
});

test('classifyDatabaseEntry marks failed and empty entries', () => {
  assert.equal(classifyDatabaseEntry({ kind: 'db', name: 'ca.db', dbCount: 1 }), 'ok');
  assert.equal(classifyDatabaseEntry({ kind: 'dbDir', name: 'index', dbCount: 3 }), 'ok');
  assert.equal(classifyDatabaseEntry({ kind: 'dbDir', name: 'empty', dbCount: 0 }), 'empty');
  assert.equal(classifyDatabaseEntry({ kind: 'dbDir', name: 'failed_run', dbCount: 2 }), 'failed');
  assert.equal(classifyDatabaseEntry({ kind: 'db', name: 'broken.db', dbCount: 1 }), 'failed');
});

test('scanLocalDirectory returns db files, root and subdirectory counts', () => {
  const root = makeTempDir();
  try {
    const index = makeIndexTree(root);
    const entries = scanLocalDirectory(index);
    const names = entries.map((entry) => `${entry.kind}:${path.basename(entry.path)}`);
    assert.ok(names.includes('dbDir:index'));
    assert.ok(names.includes('db:oh_S1.db'));
    assert.ok(names.includes('dbDir:functional'));
    assert.ok(names.includes('dbDir:failed_run'));
    assert.ok(!entries.some((entry) => entry.path.endsWith('-wal')));
    assert.ok(!entries.some((entry) => entry.path.includes('.cache')));
    assert.ok(!entries.some((entry) => entry.path.includes('.ca')));
    assert.ok(!entries.some((entry) => entry.path.includes('notes.txt')));
    const rootEntry = entries.find((entry) => entry.kind === 'dbDir' && entry.path === index);
    assert.equal(rootEntry.dbCount, 3);
    const functional = entries.find((entry) => entry.path === path.join(index, 'functional'));
    assert.equal(functional.dbCount, 2);
    const dbEntry = entries.find((entry) => entry.kind === 'db');
    assert.ok(dbEntry.sizeBytes > 0);
    assert.ok(dbEntry.mtime > 0);
  } finally {
    cleanupDir(root);
  }
});

test('scanLocalDatabaseCatalog puts the configured target first and sorts failed last', () => {
  const root = makeTempDir();
  try {
    const index = makeIndexTree(root);
    const report = scanLocalDatabaseCatalog({ dbDir: index });
    assert.equal(report.ok, true);
    assert.equal(report.mode, 'local');
    assert.deepEqual(report.current, { db: null, dbDir: index });
    assert.equal(report.candidates[0].path, index);
    assert.equal(report.candidates[0].kind, 'dbDir');
    assert.equal(report.candidates[0].current, true);
    assert.equal(report.candidates[0].source, 'configured');
    assert.equal(report.candidates[0].dbCount, 3);
    assert.equal(report.roots[0], index);
    const failedIndex = report.candidates.findIndex((entry) => /failed_run/.test(entry.path));
    const okIndex = report.candidates.findIndex((entry) => /oh_S1\.db/.test(entry.path));
    assert.ok(failedIndex > okIndex, 'failed run sorted after healthy databases');
    assert.equal(report.candidates[failedIndex].status, 'failed');
    // No duplicate entries for the same path.
    const paths = report.candidates.map((entry) => entry.path);
    assert.equal(new Set(paths).size, paths.length);
  } finally {
    cleanupDir(root);
  }
});

test('scanLocalDatabaseCatalog respects the candidate limit', () => {
  const root = makeTempDir();
  try {
    const index = makeIndexTree(root);
    const report = scanLocalDatabaseCatalog({ dbDir: index, maxItems: 2 });
    assert.equal(report.maxItems, 2);
    assert.equal(report.candidates.length, 2);
    assert.equal(report.truncated, true);
    assert.equal(report.candidates[0].path, index, 'current stays first even when truncating');
    assert.ok(report.total > 2);
  } finally {
    cleanupDir(root);
  }
});

test('scanLocalDatabaseCatalog scans the repo root for repo-local databases', () => {
  const root = makeTempDir();
  try {
    const repo = path.join(root, 'CA');
    touch(path.join(repo, 'ca.db'));
    touch(path.join(repo, 'dsh', 'ignored.db'));
    const report = scanLocalDatabaseCatalog({ db: path.join(repo, 'ca.db'), repoRoot: repo });
    assert.equal(report.ok, true);
    assert.deepEqual(report.current, { db: path.join(repo, 'ca.db'), dbDir: null });
    assert.equal(report.candidates[0].path, path.join(repo, 'ca.db'));
    assert.equal(report.candidates[0].kind, 'db');
    assert.equal(report.candidates[0].current, true);
    assert.ok(report.roots.includes(repo));
    const repoEntry = report.candidates.find((entry) => entry.kind === 'dbDir' && entry.path === repo);
    assert.equal(repoEntry.dbCount, 1);
  } finally {
    cleanupDir(root);
  }
});

test('scanLocalDatabaseCatalog reports missing configuration/unreadable roots', () => {
  const noConfig = scanLocalDatabaseCatalog({});
  assert.equal(noConfig.ok, false);
  assert.equal(noConfig.error.code, 'BACKEND_NOT_CONFIGURED');

  const root = makeTempDir();
  try {
    const missing = scanLocalDatabaseCatalog({ dbDir: path.join(root, 'nope') });
    assert.equal(missing.ok, false);
    assert.equal(missing.error.code, 'DB_NOT_FOUND');
    assert.equal(missing.candidates.length, 0);
  } finally {
    cleanupDir(root);
  }
});

test('validateLocalDatabaseTarget accepts files and shard dirs, rejects bad input', () => {
  const root = makeTempDir();
  try {
    const index = makeIndexTree(root);
    const dbFile = path.join(root, 'ca.db');
    touch(dbFile);

    const dbResult = validateLocalDatabaseTarget({ kind: 'db', path: dbFile });
    assert.equal(dbResult.ok, true);
    assert.equal(dbResult.dbCount, 1);
    assert.ok(dbResult.sizeBytes > 0);

    const dirResult = validateLocalDatabaseTarget({ kind: 'dbDir', path: index });
    assert.equal(dirResult.ok, true);
    assert.equal(dirResult.dbCount, 3);

    assert.throws(
      () => validateLocalDatabaseTarget({ kind: 'db', path: path.join(root, 'missing.db') }),
      (error) => error instanceof BackendError && error.code === 'DB_NOT_FOUND',
    );
    assert.throws(
      () => validateLocalDatabaseTarget({ kind: 'db', path: path.join(root, 'ca.txt') }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    assert.throws(
      () => validateLocalDatabaseTarget({ kind: 'dbDir', path: path.join(index, 'empty_dir') }),
      (error) => error.code === 'DB_NOT_FOUND',
    );
    assert.throws(
      () => validateLocalDatabaseTarget({ kind: 'db', path: 'relative/ca.db' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    assert.throws(
      () => validateLocalDatabaseTarget({ kind: 'nope', path: '/x' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
  } finally {
    cleanupDir(root);
  }
});

test('buildDatabaseCatalog synthesizes a current entry for ~ and dedupes', () => {
  const report = buildDatabaseCatalog({
    mode: 'remote',
    dbDir: '~/CA_OH/index',
    items: [
      { kind: 'db', path: '/home/yu/CA_OH/index/oh_S1.db', sizeBytes: 10, mtime: 2 },
      { kind: 'db', path: '/home/yu/CA_OH/index/oh_S1.db', sizeBytes: 10, mtime: 2 },
      { kind: 'dbDir', path: '/home/yu/CA_OH/index', dbCount: 1, mtime: 5 },
      { kind: 'dbDir', path: '/home/yu/CA_OH/index/failed', dbCount: 2, mtime: 9 },
    ],
    roots: ['/home/yu/CA_OH/index'],
    maxItems: 10,
  });
  assert.equal(report.ok, true);
  assert.equal(report.candidates[0].path, '/home/yu/CA_OH/index');
  assert.equal(report.candidates[0].current, true, '~/ configured path matches scanned absolute path');
  assert.equal(report.candidates[0].source, 'configured');
  const failedEntry = report.candidates.find((entry) => /failed/.test(entry.path));
  assert.equal(failedEntry.status, 'failed');
  assert.equal(report.candidates.filter((entry) => /oh_S1\.db/.test(entry.path)).length, 1);
  assert.equal(report.maxItems, 10);
  assert.equal(report.truncated, false);
});

test('createDatabaseErrorReport is a safe empty catalog', () => {
  const report = createDatabaseErrorReport({
    mode: 'remote',
    db: '/x.db',
    error: { code: 'SSH_UNREACHABLE', message: 'down', details: { hint: 'check ssh' } },
  });
  assert.equal(report.ok, false);
  assert.deepEqual(report.candidates, []);
  assert.deepEqual(report.current, { db: '/x.db', dbDir: null });
  assert.equal(report.error.code, 'SSH_UNREACHABLE');
  assert.equal(report.error.details.hint, 'check ssh');
  assert.equal(report.maxItems, MAX_DATABASE_ITEMS);
});

test('parseRemoteDatabaseScanOutput parses entries, errors and completion', () => {
  const stdout = [
    'CA_DB_SCAN_ROOT=/home/yu/CA_OH/index',
    'CA_DB_ENTRY:{"kind":"db","path":"/home/yu/CA_OH/index/oh_S1.db","name":"oh_S1.db","sizeBytes":123,"mtime":1700000000000,"source":"scan"}',
    'CA_DB_ENTRY:{"kind":"dbDir","path":"/home/yu/CA_OH/index","name":"index","sizeBytes":null,"mtime":1700000000000,"dbCount":15,"source":"scan"}',
    'CA_DB_DONE=1',
  ].join('\n');
  const parsed = parseRemoteDatabaseScanOutput(stdout);
  assert.equal(parsed.done, true);
  assert.equal(parsed.error, null);
  assert.equal(parsed.entries.length, 2);
  assert.equal(parsed.entries[0].kind, 'db');
  assert.equal(parsed.entries[1].dbCount, 15);

  const failed = parseRemoteDatabaseScanOutput('CA_DB_ERROR=root_missing');
  assert.equal(failed.error, 'root_missing');
  assert.equal(failed.done, false);

  const malformed = parseRemoteDatabaseScanOutput('CA_DB_ENTRY:{not json}\nCA_DB_DONE=1');
  assert.equal(malformed.entries.length, 0);
  assert.equal(malformed.done, true);
});

test('parseRemoteDatabaseValidationOutput handles success and failure shapes', () => {
  assert.deepEqual(parseRemoteDatabaseValidationOutput('CA_DB_VALID=1:OK:3'), {
    ok: true,
    reason: 'OK',
    numbers: [3],
    parsed: true,
  });
  assert.deepEqual(parseRemoteDatabaseValidationOutput('CA_DB_VALID=1:OK:1:4096'), {
    ok: true,
    reason: 'OK',
    numbers: [1, 4096],
    parsed: true,
  });
  assert.deepEqual(parseRemoteDatabaseValidationOutput('CA_DB_VALID=0:NO_DB'), {
    ok: false,
    reason: 'NO_DB',
    numbers: [],
    parsed: true,
  });
  assert.equal(parseRemoteDatabaseValidationOutput('nothing').parsed, false);
});
