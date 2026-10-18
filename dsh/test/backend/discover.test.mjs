import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import {
  buildCaArgv,
  countDbFiles,
  findRepoRoot,
  healthFields,
  looksLikeCaRepo,
  probeLocalEnvironment,
  resolveExecutableOnPath,
  resolveLocalDatabase,
  resolveLocalTarget,
  resolveRuntime,
  scanDatabaseCandidates,
} from '../../lib/host/backend/discover.js';
import { cleanupDir, makeTempDir, testConfig, writeExecutable } from './helpers/fakes.mjs';

function makeCaRepo(root, {withVenv = true} = {}) {
  fs.mkdirSync(path.join(root, 'src', 'ca'), { recursive: true });
  fs.writeFileSync(
    path.join(root, 'pyproject.toml'),
    '[project]\nname = "ca-analyzer"\nversion = "0.1.0"\n',
  );
  fs.writeFileSync(path.join(root, 'src', 'ca', '__init__.py'), '');
  if (withVenv) {
    writeExecutable(path.join(root, '.venv', 'bin', 'python'));
    writeExecutable(path.join(root, '.venv', 'bin', 'ca'));
  }
  return root;
}

test('looksLikeCaRepo detects pyproject name and src/ca layout', () => {
  const root = makeTempDir();
  try {
    assert.equal(looksLikeCaRepo(root), false);
    makeCaRepo(root, { withVenv: false });
    assert.equal(looksLikeCaRepo(root), true);
    const other = path.join(root, 'other');
    fs.mkdirSync(other);
    fs.writeFileSync(path.join(other, 'pyproject.toml'), '[project]\nname = "something-else"\n');
    assert.equal(looksLikeCaRepo(other), false);
  } finally {
    cleanupDir(root);
  }
});

test('findRepoRoot walks up from a nested cwd', () => {
  const root = makeTempDir();
  try {
    makeCaRepo(root, { withVenv: false });
    const nested = path.join(root, 'dsh', 'lib');
    fs.mkdirSync(nested, { recursive: true });
    assert.equal(findRepoRoot({ startDirs: [nested] }), root);
    const empty = path.join(root, 'plain', 'dir');
    fs.mkdirSync(empty, { recursive: true });
    assert.equal(findRepoRoot({ startDirs: [empty] }), root, 'walks up to the CA checkout');
    const isolated = makeTempDir('dshca-no-ca-');
    try {
      assert.equal(findRepoRoot({ startDirs: [isolated] }), null);
    } finally {
      cleanupDir(isolated);
    }
  } finally {
    cleanupDir(root);
  }
});

test('resolveExecutableOnPath returns absolute executables only', () => {
  const root = makeTempDir();
  try {
    const bin = path.join(root, 'bin');
    writeExecutable(path.join(bin, 'python3'));
    assert.equal(resolveExecutableOnPath('python3', { env: { PATH: bin } }), path.join(bin, 'python3'));
    assert.equal(resolveExecutableOnPath('python3', { env: { PATH: '/nowhere' } }), null);
  } finally {
    cleanupDir(root);
  }
});

test('resolveRuntime prefers configured values then the repo venv', () => {
  const root = makeTempDir();
  try {
    const repo = makeCaRepo(root);
    const config = testConfig({ local: { repoRoot: repo } });

    const runtime = resolveRuntime({ config, cwd: root, env: {} });
    assert.equal(runtime.repoRoot, repo);
    assert.equal(runtime.repoSource, 'configured');
    assert.equal(runtime.python.path, path.join(repo, '.venv', 'bin', 'python'));
    assert.equal(runtime.python.source, 'repo-venv');
    assert.equal(runtime.cli.kind, 'console');
    assert.equal(runtime.cli.path, path.join(repo, '.venv', 'bin', 'ca'));

    // configured python wins
    const customPython = path.join(root, 'custom-python');
    writeExecutable(customPython);
    const configured = resolveRuntime({
      config: testConfig({ local: { repoRoot: repo, python: customPython } }),
      cwd: root,
      env: {},
    });
    assert.equal(configured.python.path, customPython);
    assert.equal(configured.python.source, 'configured');

    // invalid configured repo fails fast
    assert.throws(
      () =>
        resolveRuntime({
          config: testConfig({ local: { repoRoot: path.join(root, 'missing') } }),
          cwd: root,
          env: {},
        }),
      (error) => error instanceof BackendError && error.code === 'REPO_NOT_FOUND',
    );
    assert.throws(
      () =>
        resolveRuntime({
          config: testConfig({ local: { repoRoot: repo, python: path.join(root, 'missing-python') } }),
          cwd: root,
          env: {},
        }),
      (error) => error.code === 'PYTHON_NOT_FOUND',
    );
  } finally {
    cleanupDir(root);
  }
});

test('resolveRuntime falls back to PATH python3 and reports missing python', () => {
  const root = makeTempDir();
  try {
    const bin = path.join(root, 'bin');
    writeExecutable(path.join(bin, 'python3'));
    const runtime = resolveRuntime({
      config: testConfig({}),
      cwd: path.join(root, 'work'),
      env: { PATH: bin },
    });
    assert.equal(runtime.repoRoot, null);
    assert.equal(runtime.python.source, 'path');
    assert.match(runtime.warnings.join('\n'), /未找到 CA 仓库/);

    assert.throws(
      () => resolveRuntime({ config: testConfig({}), cwd: root, env: { PATH: '/nowhere' } }),
      (error) => error.code === 'PYTHON_NOT_FOUND',
    );
  } finally {
    cleanupDir(root);
  }
});

test('buildCaArgv uses console script when present, python -m ca otherwise', () => {
  assert.deepEqual(
    buildCaArgv({ cli: { kind: 'console', path: '/repo/.venv/bin/ca' }, python: { path: '/py' } }, [
      'serve',
      '--port',
      '0',
    ]),
    { command: '/repo/.venv/bin/ca', args: ['serve', '--port', '0'] },
  );
  assert.deepEqual(
    buildCaArgv({ cli: { kind: null, path: null }, python: { path: '/py' } }, ['version']),
    { command: '/py', args: ['-m', 'ca', 'version'] },
  );
});

test('resolveLocalDatabase validates existence and federation shards', () => {
  const root = makeTempDir();
  try {
    const db = path.join(root, 'ca.db');
    fs.writeFileSync(db, 'sqlite');
    assert.deepEqual(resolveLocalDatabase({ config: testConfig({ local: { db } }) }), {
      db,
      dbDir: null,
      shards: 0,
    });

    assert.throws(
      () => resolveLocalDatabase({ config: testConfig({ local: { db: path.join(root, 'missing.db') } }) }),
      (error) => error.code === 'DB_NOT_FOUND',
    );

    const dbDir = path.join(root, 'index');
    fs.mkdirSync(dbDir);
    fs.writeFileSync(path.join(dbDir, 'a.db'), 'x');
    fs.writeFileSync(path.join(dbDir, 'b.db'), 'x');
    fs.writeFileSync(path.join(dbDir, 'notes.txt'), 'x');
    assert.equal(countDbFiles(dbDir), 2);
    assert.deepEqual(resolveLocalDatabase({ config: testConfig({ local: { dbDir } }) }), {
      db: null,
      dbDir,
      shards: 2,
    });

    const emptyDir = path.join(root, 'empty');
    fs.mkdirSync(emptyDir);
    assert.throws(
      () => resolveLocalDatabase({ config: testConfig({ local: { dbDir: emptyDir } }) }),
      (error) => error.code === 'DB_NOT_FOUND',
    );
    assert.throws(
      () => resolveLocalDatabase({ config: testConfig({}) }),
      (error) => error.code === 'BACKEND_NOT_CONFIGURED',
    );
    assert.throws(
      () => resolveLocalDatabase({ config: testConfig({ local: { db, dbDir } }) }),
      (error) => error.code === 'CONFIG_INVALID',
    );
  } finally {
    cleanupDir(root);
  }
});

test('resolveLocalTarget adds warnings for missing deep-tool paths', () => {
  const root = makeTempDir();
  try {
    const repo = makeCaRepo(root);
    const db = path.join(root, 'ca.db');
    fs.writeFileSync(db, 'sqlite');
    const target = resolveLocalTarget({
      config: testConfig({
        local: { repoRoot: repo, db, repo: path.join(root, 'no-repo'), compdb: path.join(root, 'no.json') },
      }),
      cwd: root,
      env: {},
    });
    assert.equal(target.db, db);
    assert.equal(target.cwd, repo);
    assert.match(target.warnings.join('\n'), /local\.repo/);
    assert.match(target.warnings.join('\n'), /local\.compdb/);
  } finally {
    cleanupDir(root);
  }
});

test('probeLocalEnvironment reports a runnable environment', async () => {
  const root = makeTempDir();
  try {
    const repo = makeCaRepo(root);
    const db = path.join(root, 'ca.db');
    fs.writeFileSync(db, 'sqlite');
    const calls = [];
    const report = await probeLocalEnvironment({
      config: testConfig({ local: { repoRoot: repo, db } }),
      cwd: repo,
      homedir: root,
      env: {},
      execCa: async (argv, options) => {
        calls.push({ argv, options });
        return {
          code: 0,
          stdout: JSON.stringify({ tool: 'ca-analyzer', version: '0.1.0', schema_version: '2', python: '3.11.7' }),
          stderr: '',
          spawnError: null,
        };
      },
    });
    assert.equal(report.ok, true);
    assert.equal(report.target, 'local');
    assert.equal(report.ca.version, '0.1.0');
    assert.equal(report.ca.schemaVersion, '2');
    assert.equal(report.database.db, db);
    assert.equal(report.checks.find((check) => check.name === 'database').ok, true);
    assert.equal(report.checks.find((check) => check.name === 'ca-version').ok, true);
    assert.ok(calls.length === 1);
    assert.deepEqual(calls[0].argv.args.slice(-2), ['version', '--json']);
    assert.ok(Array.isArray(report.candidates));
  } finally {
    cleanupDir(root);
  }
});

test('probeLocalEnvironment reports missing DB and broken CLI instead of throwing', async () => {
  const root = makeTempDir();
  try {
    const repo = makeCaRepo(root);
    const report = await probeLocalEnvironment({
      config: testConfig({ local: { repoRoot: repo, db: path.join(root, 'missing.db') } }),
      cwd: repo,
      homedir: root,
      env: {},
      execCa: async () => ({
        code: 1,
        stdout: '',
        stderr: 'ModuleNotFoundError: No module named ca',
        spawnError: null,
      }),
    });
    assert.equal(report.ok, false);
    assert.equal(report.checks.find((check) => check.name === 'database').ok, false);
    assert.equal(report.checks.find((check) => check.name === 'ca-version').ok, false);
    assert.ok(report.hints.length > 0);
  } finally {
    cleanupDir(root);
  }
});

test('scanDatabaseCandidates finds top-level DBs and shard dirs only', () => {
  const root = makeTempDir();
  try {
    const home = path.join(root, 'home');
    const caOh = path.join(home, 'CA_OH');
    fs.mkdirSync(path.join(caOh, 'index'), { recursive: true });
    fs.writeFileSync(path.join(caOh, 'openharmony.db'), 'sqlite');
    fs.writeFileSync(path.join(caOh, 'openharmony.serve-state.db'), 'state');
    fs.writeFileSync(path.join(caOh, 'index', 'oh_S1.db'), 'sqlite');
    fs.writeFileSync(path.join(caOh, 'index', 'oh_S2.db'), 'sqlite');
    fs.mkdirSync(path.join(caOh, 'openharmony', 'deep'), { recursive: true });

    const cwd = path.join(root, 'work');
    fs.mkdirSync(cwd);
    fs.writeFileSync(path.join(cwd, 'local.db'), 'sqlite');

    const { candidates } = scanDatabaseCandidates({ homedir: home, cwd });
    const kinds = candidates.map((candidate) => `${candidate.kind}:${path.basename(candidate.path)}`);
    assert.ok(kinds.includes('db:openharmony.db'));
    assert.ok(kinds.includes('dbDir:index'));
    assert.ok(kinds.includes('db:local.db'));
    assert.ok(!candidates.some((candidate) => candidate.path.includes('serve-state')));
    const shardDir = candidates.find((candidate) => candidate.kind === 'dbDir');
    assert.equal(shardDir.shards, 2);
  } finally {
    cleanupDir(root);
  }
});

test('healthFields projects CA health payload and derives features', () => {
  const fields = healthFields({
    version: '0.1.0',
    server: 'ca-serve',
    status: 'ok',
    database: { paths: ['/x.db'], federated: true },
    jobs: { queued: 1 },
    events_endpoint: '/events',
    mcp_endpoint: '/mcp',
  });
  assert.equal(fields.version, '0.1.0');
  assert.deepEqual(fields.features, { jobs: true, sse: true, mcp: true, federated: true });
  assert.equal(fields.database.federated, true);

  const explicit = healthFields({ features: { custom: true }, version: '1' });
  assert.deepEqual(explicit.features, { custom: true });
  assert.deepEqual(healthFields(null), {
    version: null,
    features: null,
    database: null,
    server: null,
    status: null,
  });
});
