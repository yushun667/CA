import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import { createBackendManager } from '../../lib/host/backend/manager.js';
import { createProjectId, buildProjectFromLegacyConfig, defaultProjectNameFromTarget, findProjectById, findProjectByName, normalizeProject, projectNameKey, projectToBackendPatch, sanitizeProjectName, sanitizeStoredProject, uniqueProjectName } from '../../lib/host/backend/projects.js';
import {
  cleanupDir,
  createFakeFetch,
  createFakeSpawn,
  healthResponse,
  makePaths,
  makeTempDir,
  writeExecutable,
} from './helpers/fakes.mjs';

const LOCAL_READY_LINE =
  '[serve] CA backend listening on http://127.0.0.1:48123 (db=/tmp/ca.db; federated=no)\n';

function touch(file, content = 'sqlite') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function makeSourceTree(root, name = 'src') {
  const source = path.join(root, name);
  fs.mkdirSync(source, { recursive: true });
  fs.writeFileSync(path.join(source, 'main.c'), 'int main(void){return 0;}');
  touch(path.join(source, 'compdb.json'), '[]');
  return source;
}

function makeLocalTarget({ db, dbDir, repo, repoRoot }) {
  return {
    db: db || null,
    dbDir: dbDir || null,
    shards: 0,
    repoRoot: repoRoot || '/repo',
    repoSource: 'configured',
    python: { path: '/usr/bin/python3', source: 'configured' },
    cli: { kind: null, path: null, source: null },
    cwd: repoRoot || '/repo',
    env: {},
    stateDb: null,
    repo: repo || null,
    compdb: null,
    warnings: [],
  };
}

function makeRuntime() {
  return {
    repoRoot: '/repo',
    repoSource: 'configured',
    python: { path: '/usr/bin/python3', source: 'configured' },
    cli: { kind: null, path: null, source: null },
    cwd: '/repo',
    warnings: [],
  };
}

/**
 * Create a manager over a temp state dir, optionally pre-seeding a legacy
 * settings.json (no `projects` key) to exercise migration.
 */
function setupManager({ root = makeTempDir('dshca-projects-'), explicit = {}, settings = null, fakeSpawn } = {}) {
  const paths = makePaths(root);
  if (settings) {
    fs.mkdirSync(path.dirname(paths.settingsPath), { recursive: true });
    fs.writeFileSync(paths.settingsPath, JSON.stringify(settings, null, 2));
  }
  let idCounter = 0;
  const spawn =
    fakeSpawn ||
    createFakeSpawn({
      onSpawn(call) {
        call.child.emitData('stderr', LOCAL_READY_LINE);
      },
    });
  const fetchImpl = createFakeFetch((call) =>
    call.urlObject.pathname === '/api/health'
      ? healthResponse()
      : { ok: true, status: 200, headers: new Headers(), text: async () => JSON.stringify({ ok: true, data: {} }) },
  );
  const manager = createBackendManager(null, explicit, {
    paths,
    env: {},
    cwd: '/nonexistent-local-cwd',
    homedir: '/tmp',
    now: () => 1700000000000 + idCounter,
    spawn: spawn.spawn,
    fetch: fetchImpl,
    runProcess: async () => ({
      command: 'ssh',
      args: [],
      code: 0,
      signal: null,
      stdout: '',
      stderr: '',
      spawnError: null,
      timedOut: false,
      cancelled: false,
    }),
    findFreePort: async () => 43210,
    resolveLocalTarget: async ({ config }) =>
      makeLocalTarget({
        db: config.local.db,
        dbDir: config.local.dbDir,
        repo: config.local.repo,
        repoRoot: config.local.repoRoot,
      }),
    resolveRuntime: makeRuntime,
    generateProjectId: () => {
      idCounter += 1;
      return `proj-test${String(idCounter).padStart(4, '0')}`;
    },
  });
  return { manager, root, paths, spawn, fetchImpl };
}

async function teardown(context) {
  try {
    await context.manager.dispose();
  } finally {
    cleanupDir(context.root);
  }
}

// ------------------------------------------------------------------------ //
// unit tests: normalization / validation / mapping / migration
// ------------------------------------------------------------------------ //

test('createProjectId generates proj- prefixed ids', () => {
  assert.match(createProjectId(), /^proj-[0-9a-f]{8}$/);
  assert.equal(
    createProjectId({ randomBytes: () => Buffer.from('a1b2c3d4', 'hex') }),
    'proj-a1b2c3d4',
  );
});

test('project names are trimmed, bounded and compared case-insensitively', () => {
  assert.equal(sanitizeProjectName('  OpenHarmony Index  '), 'OpenHarmony Index');
  assert.equal(projectNameKey('  OH Index '), 'oh index');
  assert.throws(() => sanitizeProjectName('   '), (error) => error.code === 'INVALID_ARGUMENT');
  assert.throws(() => sanitizeProjectName('x'.repeat(121)), /不能超过/);
  assert.equal(sanitizeProjectName('a\u0000b'), 'ab', 'control characters are stripped');
});

test('uniqueProjectName appends a suffix for case-insensitive collisions', () => {
  const projects = [
    { id: 'p1', name: 'Index' },
    { id: 'p2', name: 'Index (2)' },
  ];
  assert.equal(uniqueProjectName(projects, 'Fresh'), 'Fresh');
  assert.equal(uniqueProjectName(projects, 'index'), 'index (3)');
  assert.equal(uniqueProjectName(projects, 'INDEX', 'p1'), 'INDEX', 'own name is not a collision');
  assert.equal(uniqueProjectName(projects, 'Index (2)'), 'Index (2) (2)');
});

test('normalizeProject accepts remote projects without touching the remote fs', () => {
  const project = normalizeProject(
    {
      name: 'OH Index',
      backend: 'remote',
      sourceRoot: '/home/yu/src/openharmony',
      dbDir: '/home/yu/CA_OH/index',
      ssh: { host: 'yu@linux-box', port: '2222', keyPath: '~/.ssh/id_ed25519' },
      python: '/home/yu/CA_OH/venv/bin/python',
      caRepoPath: '~/CA_OH/ca',
    },
    { now: () => 111, generateId: () => 'proj-aaaabbbb' },
  );
  assert.equal(project.id, 'proj-aaaabbbb');
  assert.equal(project.name, 'OH Index');
  assert.equal(project.backend, 'remote');
  assert.equal(project.db, null);
  assert.equal(project.dbDir, '/home/yu/CA_OH/index');
  assert.deepEqual(project.ssh.host, 'yu@linux-box');
  assert.equal(project.ssh.port, 2222);
  assert.ok(project.ssh.keyPath.endsWith('/.ssh/id_ed25519'));
  assert.equal(project.python, '/home/yu/CA_OH/venv/bin/python');
  assert.equal(project.caRepoPath, '~/CA_OH/ca');
  assert.equal(project.createdAt, 111);
  assert.equal(project.updatedAt, 111);

  const single = normalizeProject({
    name: 'Layer',
    backend: 'remote',
    sourceRoot: '~/src',
    db: '/home/yu/layered-oh.db',
    ssh: { host: 'box' },
  });
  assert.equal(single.db, '/home/yu/layered-oh.db');
  assert.equal(single.dbDir, null);
  assert.equal(single.ssh.port, null);
  assert.equal(single.missing, undefined);
});

test('normalizeProject rejects invalid project definitions', () => {
  const base = {
    name: 'proj',
    backend: 'remote',
    sourceRoot: '/remote/src',
    dbDir: '/remote/index',
    ssh: { host: 'box' },
  };
  const cases = [
    [{ ...base, backend: 'cloud' }, /backend/],
    [{ ...base, name: '' }, /项目名|name/],
    [{ ...base, sourceRoot: null }, /sourceRoot/],
    [{ ...base, sourceRoot: 'relative/src' }, /sourceRoot/],
    [{ ...base, db: '/remote/x.db' }, /只能设置一项/],
    [{ ...base, db: null, dbDir: null }, /只能设置一项/],
    [{ ...base, ssh: {} }, /ssh\.host/],
    [{ ...base, ssh: { host: 'box', port: 0 } }, /ssh\.port/],
    [{ ...base, python: 'relative/python' }, /python/],
  ];
  for (const [input, matcher] of cases) {
    assert.throws(
      () => normalizeProject(input),
      (error) => {
        assert.ok(error instanceof BackendError);
        assert.equal(error.code, 'INVALID_ARGUMENT');
        assert.match(error.message, matcher);
        return true;
      },
    );
  }
  assert.throws(
    () => normalizeProject({ ...base, id: 'proj-unknown' }),
    (error) => error.code === 'PROJECT_NOT_FOUND',
  );
});

test('normalizeProject validates local paths for existence', () => {
  const root = makeTempDir();
  try {
    const source = makeSourceTree(root);
    const db = path.join(root, 'index.db');
    fs.writeFileSync(db, 'sqlite');
    const dbDir = path.join(root, 'shards');
    touch(path.join(dbDir, 'oh_S1.db'));
    const python = path.join(root, 'bin', 'python3');
    writeExecutable(python);
    const caRepo = path.join(root, 'CA');
    fs.mkdirSync(caRepo, { recursive: true });

    const project = normalizeProject({
      name: 'local',
      backend: 'local',
      sourceRoot: source,
      db,
      compdb: path.join(source, 'compdb.json'),
      python,
      caRepoPath: caRepo,
    });
    assert.equal(project.backend, 'local');
    assert.equal(project.ssh, null);
    assert.equal(project.db, db);
    assert.equal(project.dbDir, null);
    assert.equal(project.sourceRoot, source);

    const shards = normalizeProject({
      name: 'shards',
      backend: 'local',
      sourceRoot: source,
      dbDir,
    });
    assert.equal(shards.dbDir, dbDir);

    assert.throws(
      () => normalizeProject({ ...project, db: path.join(root, 'missing.db') }),
      (error) => error.code === 'DB_NOT_FOUND',
    );
    assert.throws(
      () => normalizeProject({ name: 'x', backend: 'local', sourceRoot: path.join(root, 'missing'), db }),
      (error) => error.code === 'INVALID_ARGUMENT' && /源码根目录/.test(error.message),
    );
    assert.throws(
      () => normalizeProject({ name: 'x', backend: 'local', sourceRoot: source, dbDir: path.join(root, 'empty') }),
      (error) => error.code === 'DB_NOT_FOUND',
    );
    assert.throws(
      () => normalizeProject({ name: 'x', backend: 'local', sourceRoot: source, db, python: path.join(root, 'nope') }),
      (error) => error.code === 'INVALID_ARGUMENT' && /python/.test(error.message),
    );
  } finally {
    cleanupDir(root);
  }
});

test('normalizeProject updates keep id/createdAt and reject unknown ids', () => {
  const previous = {
    id: 'proj-abc',
    name: 'old',
    backend: 'remote',
    sourceRoot: '/src',
    db: null,
    dbDir: '/index',
    compdb: null,
    ssh: { host: 'box', user: null, port: null, keyPath: null },
    python: null,
    caRepoPath: null,
    createdAt: 100,
    updatedAt: 100,
  };
  const updated = normalizeProject(
    { ...previous, name: 'new', dbDir: '/index2' },
    { now: () => 200, previous, generateId: () => 'proj-generated' },
  );
  assert.equal(updated.id, 'proj-abc');
  assert.equal(updated.createdAt, 100);
  assert.equal(updated.updatedAt, 200);
  assert.equal(updated.name, 'new');
  assert.equal(updated.dbDir, '/index2');
});

test('projectToBackendPatch maps local and remote projects onto config', () => {
  const local = projectToBackendPatch({
    backend: 'local',
    sourceRoot: '/src/oh',
    db: '/data/ca.db',
    dbDir: null,
    compdb: '/src/compdb.json',
    caRepoPath: '/tools/CA',
    python: '/tools/CA/.venv/bin/python',
    ssh: null,
  });
  assert.equal(local.backend, 'local');
  assert.equal(local.local.db, '/data/ca.db');
  assert.equal(local.local.dbDir, null);
  assert.equal(local.local.repo, '/src/oh');
  assert.equal(local.local.repoRoot, '/tools/CA');
  assert.equal(local.local.compdb, '/src/compdb.json');

  const remote = projectToBackendPatch({
    backend: 'remote',
    sourceRoot: '/home/yu/oh',
    db: null,
    dbDir: '/home/yu/index',
    compdb: null,
    caRepoPath: '/home/yu/CA',
    python: '/home/yu/venv/bin/python',
    ssh: { host: 'linux-box', user: 'yu', port: 2222, keyPath: '/k' },
  });
  assert.equal(remote.backend, 'remote');
  assert.equal(remote.remote.dbDir, '/home/yu/index');
  assert.equal(remote.remote.repo, '/home/yu/oh');
  assert.equal(remote.remote.repoPath, '/home/yu/CA');
  assert.deepEqual(remote.remote.ssh, { host: 'linux-box', user: 'yu', port: 2222, keyPath: '/k' });
});

test('defaultProjectNameFromTarget derives a readable name', () => {
  assert.equal(defaultProjectNameFromTarget({ dbDir: '/home/yu/CA_OH/index/' }), 'index');
  assert.equal(defaultProjectNameFromTarget({ db: '/data/openharmony.db' }), 'openharmony');
  assert.equal(defaultProjectNameFromTarget({}), 'default');
});

test('buildProjectFromLegacyConfig migrates remote and local settings', () => {
  const remote = buildProjectFromLegacyConfig({
    config: {
      backend: 'remote',
      remote: {
        ssh: { host: 'yu@linux-box', user: 'yu', port: 2222, keyPath: '/k' },
        repoPath: '/home/yu/CA_OH/ca',
        installDir: null,
        db: null,
        dbDir: '/home/yu/CA_OH/index',
        repo: '/home/yu/CA_OH/openharmony',
        compdb: '/home/yu/corpus/compdb.json',
        python: '/home/yu/CA_OH/venv/bin/python',
      },
    },
    mode: 'remote',
    now: () => 500,
    generateId: () => 'proj-legacy1',
  });
  assert.equal(remote.id, 'proj-legacy1');
  assert.equal(remote.name, 'index');
  assert.equal(remote.backend, 'remote');
  assert.equal(remote.dbDir, '/home/yu/CA_OH/index');
  assert.equal(remote.db, null);
  assert.equal(remote.sourceRoot, '/home/yu/CA_OH/openharmony');
  assert.equal(remote.caRepoPath, '/home/yu/CA_OH/ca');
  assert.equal(remote.python, '/home/yu/CA_OH/venv/bin/python');
  assert.equal(remote.compdb, '/home/yu/corpus/compdb.json');
  assert.equal(remote.ssh.host, 'yu@linux-box');
  assert.equal(remote.createdAt, 500);

  const local = buildProjectFromLegacyConfig({
    config: {
      backend: 'local',
      local: { db: '/data/openharmony.db', dbDir: null, repo: '/src/oh', repoRoot: '/tools/CA', compdb: null, python: null },
    },
    mode: 'local',
  });
  assert.equal(local.name, 'openharmony');
  assert.equal(local.backend, 'local');
  assert.equal(local.db, '/data/openharmony.db');
  assert.equal(local.sourceRoot, '/src/oh');
  assert.equal(local.caRepoPath, '/tools/CA');
  assert.equal(local.ssh, null);

  assert.equal(
    buildProjectFromLegacyConfig({ config: { backend: 'auto' }, mode: 'unconfigured' }),
    null,
  );
  assert.equal(
    buildProjectFromLegacyConfig({
      config: { backend: 'remote', remote: { dbDir: '/x', ssh: { host: null } } },
      mode: 'remote',
    }),
    null,
  );
});

test('sanitizeStoredProject is lenient but drops structurally broken entries', () => {
  const valid = sanitizeStoredProject({
    id: 'proj-1',
    name: ' Index ',
    backend: 'remote',
    sourceRoot: '/remote/src',
    db: null,
    dbDir: '/remote/index',
    ssh: { host: 'box' },
    createdAt: 10,
    updatedAt: 20,
  });
  assert.equal(valid.name, 'Index');
  assert.equal(valid.ssh.port, null);
  assert.equal(valid.createdAt, 10);

  assert.equal(sanitizeStoredProject(null), null);
  assert.equal(sanitizeStoredProject({ id: 'x' }), null);
  assert.equal(
    sanitizeStoredProject({
      id: 'p',
      name: 'x',
      backend: 'remote',
      sourceRoot: '/s',
      db: '/a.db',
      dbDir: '/a',
      ssh: { host: 'box' },
    }),
    null,
  );
  assert.equal(
    sanitizeStoredProject({ id: 'p', name: 'x', backend: 'remote', sourceRoot: '/s', dbDir: '/a' }),
    null,
    'remote project without ssh.host is unusable',
  );
});

// ------------------------------------------------------------------------ //
// manager tests: migration / CRUD / activation / delete-active
// ------------------------------------------------------------------------ //

test('manager migrates a legacy settings.json into a default project', async () => {
  const settings = {
    version: 1,
    config: {
      backend: 'remote',
      remote: {
        ssh: { host: 'linux-box', user: 'yu' },
        repoPath: '/home/yu/CA',
        db: null,
        dbDir: '/home/yu/CA_OH/index',
        repo: '/home/yu/CA_OH/openharmony',
      },
    },
  };
  const context = setupManager({ explicit: {}, settings });
  try {
    const listed = context.manager.listProjects();
    assert.equal(listed.projects.length, 1);
    assert.equal(listed.projects[0].name, 'index');
    assert.equal(listed.projects[0].backend, 'remote');
    assert.equal(listed.projects[0].active, true);
    assert.equal(listed.activeProjectId, listed.projects[0].id);
    const active = context.manager.getActiveProject();
    assert.ok(active);
    assert.equal(active.id, listed.projects[0].id);
    assert.equal(active.name, 'index');
    assert.equal(active.active, undefined, 'getActiveProject returns the raw definition');
    const stored = JSON.parse(fs.readFileSync(context.paths.settingsPath, 'utf8'));
    assert.equal(stored.projects.length, 1);
    assert.equal(stored.projects[0].name, 'index');
    assert.equal(stored.activeProjectId, stored.projects[0].id);
    assert.equal(stored.config.remote.dbDir, '/home/yu/CA_OH/index', 'legacy config patch preserved');
  } finally {
    await teardown(context);
  }
});

test('legacy migration is idempotent across manager restarts', async () => {
  const root = makeTempDir('dshca-migrate-idem-');
  const settings = {
    version: 1,
    config: {
      backend: 'remote',
      remote: { ssh: { host: 'linux-box' }, dbDir: '/home/yu/CA_OH/index' },
    },
  };
  const first = setupManager({ root, explicit: {}, settings });
  const projectId = first.manager.getActiveProject().id;
  await first.manager.dispose();
  const second = setupManager({ root, explicit: {} });
  try {
    const listed = second.manager.listProjects();
    assert.equal(listed.projects.length, 1, 'no duplicate project on reload');
    assert.equal(listed.projects[0].id, projectId, 'project id is stable');
    assert.equal(listed.activeProjectId, projectId);
    const persisted = JSON.parse(fs.readFileSync(second.paths.settingsPath, 'utf8'));
    assert.equal(persisted.projects.length, 1);
  } finally {
    await teardown(second);
  }
});

test('manager does not create projects on a fresh install or when projects are empty', async () => {
  const fresh = setupManager({ explicit: { backend: 'local', local: { db: '/tmp/ca.db' } } });
  try {
    assert.deepEqual(fresh.manager.listProjects(), { projects: [], activeProjectId: null });
    assert.equal(fs.existsSync(fresh.paths.settingsPath), false);
  } finally {
    await teardown(fresh);
  }

  const explicitEmpty = setupManager({
    explicit: {},
    settings: {
      version: 1,
      config: { backend: 'local', local: { db: '/tmp/ca.db' } },
      projects: [],
      activeProjectId: null,
    },
  });
  try {
    assert.deepEqual(explicitEmpty.manager.listProjects(), { projects: [], activeProjectId: null });
  } finally {
    await teardown(explicitEmpty);
  }
});

test('saveProject creates, enforces unique names and updates definitions', async () => {
  const context = setupManager({ explicit: {} });
  try {
    const created = context.manager.saveProject({
      name: 'OH Index',
      backend: 'remote',
      sourceRoot: '/home/yu/CA_OH/openharmony',
      dbDir: '/home/yu/CA_OH/index',
      ssh: { host: 'linux-box' },
    });
    assert.equal(created.projects.length, 1);
    assert.equal(created.activeProjectId, null);
    assert.match(created.project.id, /^proj-/);
    const stored = JSON.parse(fs.readFileSync(context.paths.settingsPath, 'utf8'));
    assert.equal(stored.projects.length, 1);
    assert.equal(stored.projects[0].name, 'OH Index');

    assert.throws(
      () =>
        context.manager.saveProject({
          name: 'oh index',
          backend: 'remote',
          sourceRoot: '/remote/src',
          db: '/remote/x.db',
          ssh: { host: 'box' },
        }),
      (error) => {
        assert.equal(error.code, 'INVALID_ARGUMENT');
        assert.equal(error.details.reason, 'PROJECT_NAME_CONFLICT');
        assert.match(error.message, /已存在/);
        return true;
      },
    );

    const updated = context.manager.saveProject({
      ...created.project,
      name: 'OpenHarmony',
      dbDir: '/home/yu/CA_OH/index2',
    });
    assert.equal(updated.project.id, created.project.id);
    assert.equal(updated.project.name, 'OpenHarmony');
    assert.equal(updated.project.createdAt, created.project.createdAt);
    assert.equal(updated.projects.length, 1);

    assert.throws(
      () => context.manager.saveProject({ ...created.project, id: 'proj-unknown' }),
      (error) => error.code === 'PROJECT_NOT_FOUND',
    );
  } finally {
    await teardown(context);
  }
});

test('saveProject validates local existence and allows remote-only paths', async () => {
  const root = makeTempDir('dshca-project-save-');
  const context = setupManager({ root, explicit: {} });
  try {
    const source = makeSourceTree(root);
    const db = path.join(root, 'ca.db');
    fs.writeFileSync(db, 'sqlite');
    const local = context.manager.saveProject({
      name: 'local',
      backend: 'local',
      sourceRoot: source,
      db,
    });
    assert.equal(local.project.backend, 'local');
    assert.equal(local.project.db, db);

    assert.throws(
      () =>
        context.manager.saveProject({
          name: 'local-missing-db',
          backend: 'local',
          sourceRoot: source,
          db: path.join(root, 'missing.db'),
        }),
      (error) => error.code === 'DB_NOT_FOUND',
    );
    assert.throws(
      () =>
        context.manager.saveProject({
          name: 'local-missing-source',
          backend: 'local',
          sourceRoot: path.join(root, 'missing-src'),
          db,
        }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );

    const remote = context.manager.saveProject({
      name: 'remote',
      backend: 'remote',
      sourceRoot: '/remote/not-on-this-mac',
      dbDir: '/remote/not-on-this-mac/index',
      ssh: { host: 'linux-box' },
    });
    assert.equal(remote.project.sourceRoot, '/remote/not-on-this-mac');
    assert.equal(remote.project.dbDir, '/remote/not-on-this-mac/index');
  } finally {
    await teardown(context);
  }
});

test('activateProject applies local config, persists the selection and reports status', async () => {
  const root = makeTempDir('dshca-project-activate-');
  const context = setupManager({ root, explicit: {} });
  try {
    const source1 = makeSourceTree(root, 'src1');
    const source2 = makeSourceTree(root, 'src2');
    const db1 = path.join(root, 'one.db');
    const db2 = path.join(root, 'two.db');
    fs.writeFileSync(db1, 'sqlite');
    fs.writeFileSync(db2, 'sqlite');
    const p1 = context.manager.saveProject({ name: 'one', backend: 'local', sourceRoot: source1, db: db1 }).project;
    const p2 = context.manager.saveProject({ name: 'two', backend: 'local', sourceRoot: source2, db: db2 }).project;

    const activated = await context.manager.activateProject(p2.id);
    assert.equal(activated.project.id, p2.id);
    assert.equal(activated.status.state, 'running');
    assert.equal(activated.config.backend, 'local');
    assert.equal(activated.config.local.db, db2);
    assert.equal(activated.config.local.repo, source2);
    assert.equal(context.manager.getActiveProject().id, p2.id);

    const listed = context.manager.listProjects();
    assert.equal(listed.activeProjectId, p2.id);
    assert.equal(listed.projects.find((project) => project.id === p2.id).active, true);
    assert.equal(listed.projects.find((project) => project.id === p1.id).active, false);
    assert.equal(listed.projects.find((project) => project.id === p2.id).status.state, 'running');

    const stored = JSON.parse(fs.readFileSync(context.paths.settingsPath, 'utf8'));
    assert.equal(stored.activeProjectId, p2.id);
    assert.equal(stored.config.local.db, db2);
  } finally {
    await teardown(context);
  }
});

test('activateProject rolls back the previous config when restart fails', async () => {
  const root = makeTempDir('dshca-project-rollback-');
  const oldSource = makeSourceTree(root, 'old-src');
  const oldDb = path.join(root, 'old.db');
  fs.writeFileSync(oldDb, 'sqlite');
  // ssh spawns fail; local python spawns come up so rollback can restore service.
  const spawn = createFakeSpawn({
    onSpawn(call) {
      if (call.command === 'ssh') {
        call.child.emitData('stderr', 'error: cannot bind\n');
        call.child.exit(1, null);
      } else {
        call.child.emitData('stderr', LOCAL_READY_LINE);
      }
    },
  });
  const context = setupManager({
    root,
    explicit: { backend: 'local', local: { db: oldDb, repo: oldSource } },
    fakeSpawn: spawn,
  });
  try {
    await context.manager.start();
    assert.equal(context.manager.getStatus().state, 'running');
    const saved = context.manager.saveProject({
      name: 'remote-broken',
      backend: 'remote',
      sourceRoot: '/remote/src',
      dbDir: '/remote/index',
      ssh: { host: 'linux-box' },
    }).project;

    await assert.rejects(
      context.manager.activateProject(saved.id),
      (error) => {
        assert.equal(error.code, 'PROJECT_ACTIVATION_FAILED');
        assert.equal(error.details.project.id, saved.id);
        assert.equal(error.details.restored, true, 'old local backend was restarted');
        return true;
      },
    );

    assert.equal(context.manager.getStatus().state, 'running');
    assert.equal(context.manager.getStatus().mode, 'local');
    assert.equal(context.manager.config.local.db, oldDb, 'previous config restored');
    assert.equal(context.manager.getActiveProject(), null, 'active project unchanged');
    const stored = JSON.parse(fs.readFileSync(context.paths.settingsPath, 'utf8'));
    assert.equal(stored.config.remote, undefined, 'failed project patch was rolled back');
    assert.equal(stored.activeProjectId, null);
  } finally {
    await teardown(context);
  }
});

test('deleteProject clears the active id but keeps the backend running', async () => {
  const root = makeTempDir('dshca-project-delete-');
  const context = setupManager({ root, explicit: {} });
  try {
    const source = makeSourceTree(root);
    const db = path.join(root, 'ca.db');
    fs.writeFileSync(db, 'sqlite');
    const project = context.manager.saveProject({ name: 'p', backend: 'local', sourceRoot: source, db }).project;
    const other = context.manager.saveProject({
      name: 'other',
      backend: 'remote',
      sourceRoot: '/remote/src',
      db: '/remote/x.db',
      ssh: { host: 'box' },
    }).project;
    await context.manager.activateProject(project.id);
    assert.equal(context.manager.getStatus().state, 'running');

    const deleted = context.manager.deleteProject(project.id);
    assert.equal(deleted.deleted.id, project.id);
    assert.equal(deleted.activeProjectId, null);
    assert.equal(deleted.activeCleared, true);
    assert.equal(context.manager.getActiveProject(), null);
    assert.equal(context.manager.listProjects().projects.length, 1);
    assert.equal(context.manager.getStatus().state, 'running', 'backend keeps running on old config');

    const removedOther = context.manager.deleteProject(other.id);
    assert.equal(removedOther.activeCleared, false);
    assert.equal(context.manager.listProjects().projects.length, 0);
    assert.throws(
      () => context.manager.deleteProject('proj-unknown'),
      (error) => error.code === 'PROJECT_NOT_FOUND',
    );
  } finally {
    await teardown(context);
  }
});

test('activateProject rejects unknown projects and reports no active project', async () => {
  const context = setupManager({ explicit: {} });
  try {
    assert.equal(context.manager.getActiveProject(), null);
    await assert.rejects(
      context.manager.activateProject('proj-missing'),
      (error) => error.code === 'PROJECT_NOT_FOUND',
    );
    assert.equal(findProjectById(context.manager.listProjects().projects, 'x'), null);
    assert.equal(findProjectByName(context.manager.listProjects().projects, 'x'), null);
  } finally {
    await teardown(context);
  }
});
