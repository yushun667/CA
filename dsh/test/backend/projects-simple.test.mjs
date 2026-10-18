import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import {
  buildSimpleProject,
  defaultStateDir,
  ensureDefaultDirs,
  projectSlug,
} from '../../lib/host/backend/projects.js';
import { cleanupDir, makeTempDir } from './helpers/fakes.mjs';

function makeSourceDir(root, name = 'openharmony') {
  const dir = path.join(root, name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'README.md'), 'source');
  return dir;
}

test('projectSlug produces stable directory-safe slugs', () => {
  assert.equal(projectSlug('OpenHarmony Index'), 'openharmony-index');
  assert.equal(projectSlug('CA_OH/index'), 'ca_oh-index');
  assert.equal(projectSlug('  ..///  '), 'project');
  assert.equal(projectSlug('项目'), 'project');
  assert.ok(projectSlug('x'.repeat(200)).length <= 48);
});

test('buildSimpleProject local picks defaults: basename name, managed DB, compdb', () => {
  const root = makeTempDir('dshca-simple-');
  try {
    const source = makeSourceDir(root);
    fs.writeFileSync(path.join(source, 'compile_commands.json'), '[]');
    const stateDir = path.join(root, 'state');
    const project = buildSimpleProject({
      kind: 'local',
      directory: source,
      stateDir,
      now: () => 111,
      generateId: () => 'proj-simple1',
    });
    assert.equal(project.id, 'proj-simple1');
    assert.equal(project.name, 'openharmony');
    assert.equal(project.backend, 'local');
    assert.equal(project.sourceRoot, source);
    assert.equal(project.db, path.join(source, '.ca', 'index.db'));
    assert.equal(project.dbDir, null);
    assert.equal(project.compdb, path.join(source, 'compile_commands.json'));
    assert.equal(project.python, null);
    assert.equal(project.caRepoPath, null);
    assert.equal(project.ssh, null);
    assert.equal(project.createdAt, 111);
  } finally {
    cleanupDir(root);
  }
});

test('buildSimpleProject local uses explicit name, slug suffix and no compdb when absent', () => {
  const root = makeTempDir('dshca-simple-');
  try {
    const source = makeSourceDir(root);
    const stateDir = path.join(root, 'state');
    const project = buildSimpleProject({
      kind: 'local',
      directory: source,
      name: 'My Project',
      stateDir,
      slugSuffix: 2,
    });
    assert.equal(project.name, 'My Project');
    assert.equal(project.db, path.join(source, '.ca', 'index.db'));
    assert.equal(project.compdb, null);

    const rootProject = buildSimpleProject({ kind: 'local', directory: '/', stateDir });
    assert.equal(rootProject.name, 'root');
    assert.equal(rootProject.db, path.join('/', '.ca', 'index.db'));
  } finally {
    cleanupDir(root);
  }
});

test('buildSimpleProject rejects invalid local input', () => {
  const root = makeTempDir('dshca-simple-');
  try {
    assert.throws(
      () => buildSimpleProject({ kind: 'cloud', directory: root }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    assert.throws(
      () => buildSimpleProject({ kind: 'local', directory: '' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    assert.throws(
      () => buildSimpleProject({ kind: 'local', directory: path.join(root, 'missing') }),
      (error) => error.code === 'INVALID_ARGUMENT' && /不存在/.test(error.message),
    );
    assert.throws(
      () => buildSimpleProject({ kind: 'local', directory: 'relative/dir' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
  } finally {
    cleanupDir(root);
  }
});

test('buildSimpleProject remote derives DB under the remote home and strips password', () => {
  const root = makeTempDir('dshca-simple-');
  try {
    const project = buildSimpleProject({
      kind: 'remote',
      directory: '/home/yu/CA_OH/openharmony',
      ssh: { host: 'linux-box', user: 'yu', port: 2222, password: 's3cret' },
      home: '/home/yu',
      now: () => 222,
    });
    assert.equal(project.backend, 'remote');
    assert.equal(project.name, 'openharmony');
    assert.equal(project.sourceRoot, '/home/yu/CA_OH/openharmony');
    assert.equal(project.db, '/home/yu/CA_OH/openharmony/.ca/index.db');
    assert.equal(project.dbDir, null);
    assert.equal(project.python, null);
    assert.equal(project.caRepoPath, null);
    assert.deepEqual(project.ssh, { host: 'linux-box', user: 'yu', port: 2222, keyPath: null });
    assert.equal(JSON.stringify(project).includes('s3cret'), false);

    assert.throws(
      () => buildSimpleProject({ kind: 'remote', directory: '/home/yu/src', ssh: { host: 'box' } }),
      (error) => error.code === 'INVALID_ARGUMENT' && /home/.test(error.message),
    );
    assert.throws(
      () => buildSimpleProject({ kind: 'remote', directory: '/home/yu/src', home: '/home/yu' }),
      (error) => error.code === 'INVALID_ARGUMENT' && /ssh\.host/.test(error.message),
    );
    assert.throws(
      () => buildSimpleProject({ kind: 'remote', directory: 'relative/src', ssh: { host: 'box' }, home: '/home/yu' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
  } finally {
    cleanupDir(root);
  }
});

test('ensureDefaultDirs creates the local DB parent and an empty database file', async () => {
  const root = makeTempDir('dshca-simple-');
  try {
    const source = makeSourceDir(root);
    const project = buildSimpleProject({
      kind: 'local',
      directory: source,
      stateDir: path.join(root, 'state'),
    });
    assert.equal(fs.existsSync(project.db), false);
    const result = await ensureDefaultDirs(project);
    assert.equal(result.kind, 'local');
    assert.equal(result.dir, path.dirname(project.db));
    assert.equal(fs.existsSync(project.db), true);
    assert.equal(fs.statSync(project.db).size, 0);
    const again = await ensureDefaultDirs(project);
    assert.equal(again.created, false, 'existing DB is never truncated');
    assert.equal(fs.statSync(project.db).size, 0);
  } finally {
    cleanupDir(root);
  }
});

test('ensureDefaultDirs creates remote directories via one ssh command', async () => {
  const root = makeTempDir('dshca-simple-');
  try {
    const calls = [];
    const runProcess = async (options) => {
      calls.push(options);
      return {
        code: 0,
        stdout: 'CA_DIRS_OK=1',
        stderr: '',
        spawnError: null,
        timedOut: false,
        cancelled: false,
        command: options.command,
        args: options.args,
      };
    };
    const project = {
      backend: 'remote',
      db: '/home/yu/CA_OH/openharmony/.ca/index.db',
      ssh: { host: 'linux-box', user: 'yu' },
    };
    const result = await ensureDefaultDirs(project, { deps: { runProcess, env: {} } });
    assert.equal(result.kind, 'remote');
    assert.equal(result.dir, '/home/yu/CA_OH/openharmony/.ca');
    assert.equal(calls.length, 1);
    const script = calls[0].args.at(-1);
    assert.match(script, /mkdir -p "\$DIR"/);
    assert.match(script, /: > "\$DB"/);
    assert.match(script, /CA_DIRS_OK=1/);
    assert.ok(!script.includes('s3cret'));
  } finally {
    cleanupDir(root);
  }
});
