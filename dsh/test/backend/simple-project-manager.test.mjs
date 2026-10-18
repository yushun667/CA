import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import { createBackendManager } from '../../lib/host/backend/manager.js';
import {
  cleanupDir,
  createFakeFetch,
  createFakeSpawn,
  healthResponse,
  makePaths,
  makeTempDir,
  waitFor,
  writeExecutable,
} from './helpers/fakes.mjs';
import { createFakeRunner, probeStdout } from './remote/helpers.mjs';

const LOCAL_READY_LINE =
  '[serve] CA backend listening on http://127.0.0.1:48123 (db=/tmp/ca.db; federated=no)\n';
const REMOTE_READY_LINE =
  '[serve] CA backend listening on http://127.0.0.1:39001 (db=/home/yu/x.db; federated=no)\n';

function touch(file, content = 'data') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function makeWheelDir(root) {
  const dir = path.join(root, 'wheels');
  touch(path.join(dir, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'WHEEL');
  return dir;
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

function makeInstallerRunner({ pythonVersion = '3.12.4' } = {}) {
  const calls = [];
  const run = async (input) => {
    calls.push({ command: input.command, args: input.args.slice(), env: input.env });
    const args = input.args;
    if (args.at(-1) === '-V') {
      if (input.command === 'python3.13') return { code: 1, stdout: '', stderr: 'not found' };
      return { code: 0, stdout: `Python ${pythonVersion}`, stderr: '' };
    }
    if (args.includes('venv')) {
      const venvDir = args.at(-1);
      const bin = path.join(venvDir, 'bin');
      writeExecutable(path.join(bin, 'python'), '#!/bin/sh\nexit 0\n');
      writeExecutable(path.join(bin, 'ca'), '#!/bin/sh\nexit 0\n');
      return { code: 0, stdout: 'venv ok', stderr: '' };
    }
    if (args.includes('pip')) return { code: 0, stdout: 'installed', stderr: '' };
    if (args.includes('version')) {
      return {
        code: 0,
        stdout: JSON.stringify({ version: '0.1.0', schema_version: '2' }),
        stderr: '',
      };
    }
    return { code: 0, stdout: '', stderr: '' };
  };
  run.calls = calls;
  return run;
}

function makeRemoteRunner({ probeOverrides = {} } = {}) {
  return createFakeRunner((call) => {
    const script = call.script || '';
    if (script.includes('CA_PROBE:begin')) {
      return {
        code: 0,
        stdout: probeStdout({
          os: 'Linux',
          arch: 'x86_64',
          python3: '/usr/bin/python3',
          python3_version: 'Python 3.11.17',
          pip: '/usr/bin/pip3',
          ...probeOverrides,
        }),
        stderr: '',
      };
    }
    if (script.includes('CA_DIRS_OK')) return { code: 0, stdout: 'CA_DIRS_OK=1', stderr: '' };
    if (script.includes('CA_PIP_DONE')) return { code: 0, stdout: 'CA_PIP_DONE=1', stderr: '' };
    if (script.includes('CA_VERIFY_DONE')) {
      return {
        code: 0,
        stdout: [
          JSON.stringify({ version: '0.1.0', schema_version: '2', python: '3.11.17' }),
          'CA_CONSOLE_OK=1',
          'CA_VERIFY_DONE=1',
        ].join('\n'),
        stderr: '',
      };
    }
    if (script.includes('CA_VENV_OK')) return { code: 0, stdout: 'CA_VENV_OK=1', stderr: '' };
    return { code: 0, stdout: 'CA_DIR=/home/yu/dsh-ca/backend', stderr: '' };
  });
}

function makeTransfer() {
  const uploads = [];
  return {
    scpAvailable: true,
    tarAvailable: true,
    uploads,
    async uploadFile(input) {
      uploads.push({ type: 'file', ...input });
      return { strategy: 'scp' };
    },
    async uploadDirectory(input) {
      uploads.push({ type: 'dir', ...input });
      return { strategy: 'scp-recursive' };
    },
  };
}

function setupManager({ explicit = {}, spawn, fetchImpl, runProcess, transfer } = {}) {
  const root = makeTempDir('dshca-simple-mgr-');
  const paths = makePaths(root);
  const wheelDir = makeWheelDir(root);
  const fakeSpawn =
    spawn ||
    createFakeSpawn({
      onSpawn(call) {
        call.child.emitData('stderr', call.command === 'ssh' ? REMOTE_READY_LINE : LOCAL_READY_LINE);
      },
    });
  const fetchState = createFakeFetch((call) =>
    call.urlObject.pathname === '/api/health'
      ? healthResponse()
      : { ok: true, status: 200, headers: new Headers(), text: async () => JSON.stringify({ ok: true, data: {} }) },
  );
  const manager = createBackendManager(null, explicit, {
    paths,
    env: {},
    cwd: '/nonexistent-local-cwd',
    homedir: '/tmp',
    hostPlatform: 'darwin',
    hostArch: 'arm64',
    bundledWheelDir: wheelDir,
    spawn: fakeSpawn.spawn,
    fetch: fetchImpl || fetchState,
    findFreePort: async () => 43210,
    resolveLocalTarget: async ({ config }) =>
      makeLocalTarget({
        db: config.local.db,
        dbDir: config.local.dbDir,
        repo: config.local.repo,
        repoRoot: config.local.repoRoot,
      }),
    resolveRuntime: makeRuntime,
    runProcess: runProcess || makeInstallerRunner(),
    ...(transfer ? { transfer } : {}),
  });
  return { manager, root, paths, wheelDir, fakeSpawn, fetchImpl: fetchImpl || fetchState };
}

async function teardown(context) {
  try {
    await context.manager.dispose();
  } finally {
    cleanupDir(context.root);
  }
}

test('manager.testRemoteConnection + listRemoteDirs reuse an in-memory password', async () => {
  const runner = createFakeRunner((call) => {
    const script = call.script || '';
    if (script.includes('CA_CONN_DONE')) {
      return {
        code: 0,
        stdout: [
          'CA_CONN_HOME=/home/yu',
          'CA_CONN_OS=Linux',
          'CA_CONN_ARCH=x86_64',
          'CA_CONN_PYTHON=/usr/bin/python3',
          'CA_CONN_PY_VERSION=Python 3.11.17',
          'CA_CONN_CA=',
          'CA_CONN_DONE=1',
        ].join('\n'),
        stderr: '',
      };
    }
    if (script.includes('CA_DIR_DONE')) {
      return {
        code: 0,
        stdout: [
          'CA_DIR_HOME=/home/yu',
          'CA_DIR_PATH=/home/yu',
          'CA_DIR_PARENT=/home',
          'CA_DIR_IS_HOME=1',
          'CA_DIR_ENTRY=CA_OH',
          'CA_DIR_DONE=1',
        ].join('\n'),
        stderr: '',
      };
    }
    return { code: 0, stdout: '', stderr: '' };
  });
  const context = setupManager({ runProcess: runner });
  try {
    const connection = await context.manager.testRemoteConnection({
      host: 'linux-box',
      user: 'yu',
      password: 's3cret',
    });
    assert.equal(connection.ok, true);
    assert.equal(connection.home, '/home/yu');
    assert.equal(connection.python.supported, true);
    assert.equal(connection.ca.installed, false);
    assert.ok(!JSON.stringify(connection).includes('s3cret'));
    const connectCall = runner.calls[0];
    assert.equal(connectCall.options.env.SSH_ASKPASS_REQUIRE, 'force');
    assert.equal(connectCall.options.env.DSH_CA_SSH_ASKPASS_PASSWORD, 's3cret');

    // Later requests without repeating the password reuse the in-memory secret.
    const listing = await context.manager.listRemoteDirs('yu@linux-box', {});
    assert.equal(listing.path, '/home/yu');
    assert.deepEqual(listing.entries, [{ name: 'CA_OH', path: '/home/yu/CA_OH' }]);
    assert.ok(!JSON.stringify(listing).includes('s3cret'));
  } finally {
    await teardown(context);
  }
});

test('manager.createSimpleProject stores a local project without activating it', async () => {
  const context = setupManager();
  try {
    const source = path.join(context.root, 'src-proj');
    fs.mkdirSync(source, { recursive: true });
    const created = await context.manager.createSimpleProject({
      kind: 'local',
      directory: source,
      name: 'demo',
    });
    assert.equal(created.project.backend, 'local');
    assert.equal(created.project.name, 'demo');
    assert.equal(created.project.sourceRoot, source);
    assert.equal(created.project.db, path.join(source, '.ca', 'index.db'));
    assert.equal(created.project.python, null);
    assert.equal(created.activeProjectId, null, 'simple project is not activated until prepared');
    assert.equal(context.manager.getActiveProject(), null);
    const stored = JSON.parse(fs.readFileSync(context.paths.settingsPath, 'utf8'));
    assert.equal(stored.projects.length, 1);
    assert.equal(stored.projects[0].db, created.project.db);

    // Same directory again: one source tree owns one database, so the
    // existing project is returned instead of creating a duplicate.
    const duplicate = await context.manager.createSimpleProject({
      kind: 'local',
      directory: source,
      name: 'demo',
    });
    assert.equal(duplicate.project.id, created.project.id);
    assert.equal(duplicate.existing, true);
    assert.equal(duplicate.project.db, created.project.db);
    assert.equal(duplicate.projects.length, 1);
  } finally {
    await teardown(context);
  }
});

test('manager.createSimpleProject remote resolves home from a connection and strips password', async () => {
  const runner = createFakeRunner(() => ({
    code: 0,
    stdout: [
      'CA_CONN_HOME=/home/yu',
      'CA_CONN_OS=Linux',
      'CA_CONN_ARCH=x86_64',
      'CA_CONN_PYTHON=/usr/bin/python3',
      'CA_CONN_PY_VERSION=Python 3.11.17',
      'CA_CONN_DONE=1',
    ].join('\n'),
    stderr: '',
  }));
  const context = setupManager({ runProcess: runner });
  try {
    const created = await context.manager.createSimpleProject({
      kind: 'remote',
      directory: '/home/yu/CA_OH/openharmony',
      ssh: { host: 'linux-box', user: 'yu', password: 's3cret' },
    });
    assert.equal(created.project.backend, 'remote');
    assert.equal(created.project.db, '/home/yu/CA_OH/openharmony/.ca/index.db');
    assert.equal(created.project.ssh.password, undefined);
    const stored = fs.readFileSync(context.paths.settingsPath, 'utf8');
    assert.equal(stored.includes('s3cret'), false, 'password is never persisted');
  } finally {
    await teardown(context);
  }
});

test('manager.prepareProject local installs, fills venv python, creates DB and starts', async () => {
  const context = setupManager();
  try {
    const source = path.join(context.root, 'project-src');
    fs.mkdirSync(source, { recursive: true });
    const created = await context.manager.createSimpleProject({ kind: 'local', directory: source, name: 'demo' });
    const started = context.manager.startPrepareProject(created.project.id);
    assert.equal(started.started, true);
    assert.equal(started.scope, 'prepare');
    assert.equal(started.installScope, 'local');
    await waitFor(() => context.manager.getPrepareStatus().state !== 'running', {
      label: 'local prepare',
    });
    const prepare = context.manager.getPrepareStatus();
    assert.equal(prepare.state, 'succeeded', JSON.stringify(prepare.error));
    assert.equal(prepare.installScope, 'local');
    assert.equal(prepare.result.installed, true);

    const project = context.manager.getActiveProject();
    assert.equal(project.id, created.project.id);
    assert.ok(project.python.endsWith('bin/python'));
    assert.equal(fs.existsSync(project.db), true);
    assert.equal(fs.statSync(project.db).size, 0);
    assert.equal(context.manager.getStatus().state, 'running');
    assert.equal(context.manager.getConfig().local.db, project.db);
    assert.equal(context.manager.getConfig().local.python, project.python);
    assert.equal(context.manager.getInstallStatus().local.state, 'succeeded');

    const stored = JSON.parse(fs.readFileSync(context.paths.settingsPath, 'utf8'));
    assert.equal(stored.activeProjectId, project.id);
    assert.equal(stored.projects[0].python, project.python);
  } finally {
    await teardown(context);
  }
});

test('manager.prepareProject remote installs wheel, creates remote dirs and starts', async () => {
  const runner = makeRemoteRunner();
  const transfer = makeTransfer();
  const context = setupManager({
    explicit: {
      backend: 'remote',
      remote: {
        ssh: { host: 'linux-box' },
        repoPath: '/home/yu/CA',
        db: null,
        dbDir: null,
        serveRemotePort: 0,
        localPort: 0,
        readyTimeoutMs: 2000,
        maxRestarts: 0,
        restartBackoffMs: 10,
      },
    },
    runProcess: runner,
    transfer,
  });
  try {
    const created = await context.manager.createSimpleProject({
      kind: 'remote',
      directory: '/home/yu/CA_OH/openharmony',
      ssh: { host: 'linux-box', user: 'yu' },
      home: '/home/yu',
    });
    const started = context.manager.startPrepareProject(created.project.id);
    assert.equal(started.installScope, 'remote');
    await waitFor(() => context.manager.getPrepareStatus().state !== 'running', {
      label: 'remote prepare',
    });
    const prepare = context.manager.getPrepareStatus();
    assert.equal(prepare.state, 'succeeded', JSON.stringify(prepare.error));
    assert.equal(prepare.result.installed, true);

    const project = context.manager.getActiveProject();
    assert.equal(project.python, '~/dsh-ca/backend/venv/bin/python');
    assert.equal(project.caRepoPath, '~/dsh-ca/backend');
    assert.equal(project.db, '/home/yu/CA_OH/openharmony/.ca/index.db');
    assert.equal(context.manager.getStatus().state, 'running');
    assert.equal(context.manager.getStatus().mode, 'remote');
    // The backend was recreated when the project got activated, so the live
    // install status is back to idle; the persisted install summary still says
    // installed and prepare.result records the install.
    assert.equal(context.manager.getInstallSummary().remote.installed, true);
    assert.equal(prepare.result.installed, true);
    assert.equal(transfer.uploads.filter((upload) => upload.type === 'file').length, 1);
    const dirsScript = runner.calls.map((call) => call.script || '').find((script) => script.includes('CA_DIRS_OK'));
    assert.ok(dirsScript, 'remote DB dirs script ran');
    assert.match(dirsScript, /openharmony\/\.ca\/index\.db/);

    const stored = JSON.parse(fs.readFileSync(context.paths.settingsPath, 'utf8'));
    assert.equal(stored.projects[0].python, project.python);
    assert.equal(stored.projects[0].caRepoPath, project.caRepoPath);
  } finally {
    await teardown(context);
  }
});

test('manager.prepareProject failures are recorded and leave the project unactivated', async () => {
  const context = setupManager({ runProcess: makeInstallerRunner({ pythonVersion: '3.9.18' }) });
  try {
    const source = path.join(context.root, 'bad-project');
    fs.mkdirSync(source, { recursive: true });
    const created = await context.manager.createSimpleProject({ kind: 'local', directory: source, name: 'bad' });
    context.manager.startPrepareProject(created.project.id);
    await waitFor(() => context.manager.getPrepareStatus().state !== 'running', {
      label: 'failed prepare',
    });
    const prepare = context.manager.getPrepareStatus();
    assert.equal(prepare.state, 'failed');
    assert.equal(prepare.error.code, 'PYTHON_NOT_FOUND');
    assert.equal(context.manager.getActiveProject(), null);
    assert.equal(context.manager.getStatus().state, 'stopped');
    const project = context.manager.listProjects().projects[0];
    assert.equal(project.python, null);
    assert.equal(fs.existsSync(path.join(source, '.ca', 'index.db')), false);
  } finally {
    await teardown(context);
  }
});

test('startPrepareProject validates ids/options synchronously', async () => {
  const context = setupManager();
  try {
    assert.throws(
      () => context.manager.startPrepareProject('proj-missing'),
      (error) => error instanceof BackendError && error.code === 'PROJECT_NOT_FOUND',
    );
    const source = path.join(context.root, 'opts-src');
    fs.mkdirSync(source, { recursive: true });
    const created = await context.manager.createSimpleProject({ kind: 'local', directory: source });
    assert.throws(
      () => context.manager.startPrepareProject(created.project.id, { offline: 'yes' }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    assert.throws(
      () => context.manager.startPrepareProject(created.project.id, { wheelhousePath: 42 }),
      (error) => error.code === 'INVALID_ARGUMENT',
    );
    assert.equal(context.manager.getPrepareStatus().state, 'idle');
  } finally {
    await teardown(context);
  }
});
