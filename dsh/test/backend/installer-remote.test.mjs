import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { createLogger } from '../../lib/host/backend/log.js';
import { createRemoteExecutor } from '../../lib/host/backend/remote/exec.js';
import {
  adaptSourceReport,
  buildRemotePipInstallScript,
  buildRemotePrepareScript,
  buildRemotePythonCheckScript,
  buildRemoteVenvCreateScript,
  buildRemoteVerifyScript,
  buildScpArgv,
  buildStdinWriteScript,
  buildTarExtractScript,
  createRemoteWheelInstallSteps,
  performRemoteWheelInstall,
  planUploadStrategy,
  remoteInstallLayout,
} from '../../lib/host/backend/installer/remote-linux.js';
import { cleanupDir, makeTempDir } from './helpers/fakes.mjs';
import { createFakeRunner, probeStdout, remoteTestConfig } from './remote/helpers.mjs';

const logger = createLogger({ dir: null, ringSize: 300 });

function touch(file, content = 'data') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content);
}

function makeWheelDir(root) {
  const dir = path.join(root, 'wheels');
  touch(path.join(dir, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'WHEEL');
  return dir;
}

function makeWheelhouse(root) {
  const dir = path.join(root, 'wheelhouse', 'linux-x64');
  touch(path.join(dir, 'ca_analyzer-0.1.0-py3-none-any.whl'), 'WHEEL');
  touch(path.join(dir, 'duckdb-1.5.6-cp311-cp311-manylinux.whl'), 'WHEEL');
  touch(path.join(dir, 'manifest.json'), JSON.stringify({ platform: 'linux-x64' }));
  return dir;
}

function makeRunner({ probeOverrides = {}, pipFail = false, verifyInvalid = false, pythonCheck = 'Python 3.11.17' } = {}) {
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
          db_file: '1',
          ...probeOverrides,
        }),
        stderr: '',
      };
    }
    if (script.includes('CA_PY_CHECK_DONE')) {
      return { code: 0, stdout: `${pythonCheck}\nCA_PY_CHECK_DONE=1`, stderr: '' };
    }
    if (script.includes('CA_PIP_DONE')) {
      if (pipFail) {
        return { code: 1, stdout: '', stderr: 'ERROR: Could not find a version that satisfies the requirement clang' };
      }
      return { code: 0, stdout: 'CA_PIP_DONE=1', stderr: '' };
    }
    if (script.includes('CA_VERIFY_DONE')) {
      if (verifyInvalid) return { code: 0, stdout: 'not json', stderr: '' };
      return {
        code: 0,
        stdout: [
          JSON.stringify({ tool: 'ca-analyzer', version: '0.1.0', schema_version: '2', python: '3.11.17' }, null, 2),
          'CA_PY_VERSION=Python 3.11.17',
          'CA_CONSOLE_OK=1',
          'CA_VERIFY_DONE=1',
        ].join('\n'),
        stderr: '',
      };
    }
    if (script.includes('CA_VENV_OK')) return { code: 0, stdout: 'CA_VENV_OK=1', stderr: '' };
    if (script.includes('CA_DIR=')) return { code: 0, stdout: 'CA_DIR=/home/yu/ca-install', stderr: '' };
    return { code: 0, stdout: '', stderr: '' };
  });
}

function makeTransfer({ fileError = null, dirError = null } = {}) {
  const uploads = [];
  return {
    scpAvailable: true,
    tarAvailable: true,
    uploads,
    async uploadFile(input) {
      uploads.push({ type: 'file', ...input });
      if (fileError) throw fileError;
      return { strategy: 'scp' };
    },
    async uploadDirectory(input) {
      uploads.push({ type: 'dir', ...input });
      if (dirError) throw dirError;
      return { strategy: 'scp-recursive' };
    },
  };
}

function setupInstall({ root, installOptions = {}, runner = makeRunner(), transfer = makeTransfer() } = {}) {
  const stateDir = path.join(root, 'state');
  const wheelDir = makeWheelDir(root);
  const config = remoteTestConfig();
  const events = [];
  return {
    root,
    wheelDir,
    transfer,
    runner,
    events,
    config,
    options: {
      config,
      executor: createRemoteExecutor({ deps: { runProcess: runner }, logger }),
      deps: {
        transfer,
        bundledWheelDir: wheelDir,
        paths: { stateDir },
      },
      logger,
      installOptions: { installDir: '/home/yu/ca-install', ...installOptions },
      onEvent: (event) => events.push(event),
    },
  };
}

test('remote wheel install steps are the wheel-first flow', () => {
  const steps = createRemoteWheelInstallSteps();
  assert.deepEqual(
    steps.map((step) => step.id),
    ['probe', 'python', 'upload-wheel', 'upload-wheelhouse', 'venv', 'pip', 'verify'],
  );
});

test('planUploadStrategy prefers scp and falls back to ssh/tar', () => {
  assert.deepEqual(planUploadStrategy({ kind: 'file', scpAvailable: true, tarAvailable: false }), {
    strategy: 'scp',
    reason: 'scp available',
  });
  assert.equal(planUploadStrategy({ kind: 'file', scpAvailable: false, tarAvailable: false }).strategy, 'ssh-cat');
  assert.equal(planUploadStrategy({ kind: 'dir', scpAvailable: true, tarAvailable: false }).strategy, 'scp-recursive');
  assert.equal(planUploadStrategy({ kind: 'dir', scpAvailable: false, tarAvailable: true }).strategy, 'tar-stream');
  assert.equal(planUploadStrategy({ kind: 'dir', scpAvailable: false, tarAvailable: false }).strategy, 'unsupported');
});

test('upload/tar/ssh command builders are shell-safe', () => {
  const scp = buildScpArgv({ host: 'box', user: 'yu', port: 2222, keyPath: '/home/me/.ssh/key' }, {
    sources: ['/tmp/ca_analyzer-0.1.0-py3-none-any.whl'],
    remoteTarget: '/home/yu/ca-install/wheels',
  });
  assert.equal(scp.command, 'scp');
  assert.deepEqual(scp.args.slice(0, 4), ['-P', '2222', '-i', '/home/me/.ssh/key']);
  assert.ok(scp.args.includes('BatchMode=yes'));
  assert.equal(scp.args.at(-2), '/tmp/ca_analyzer-0.1.0-py3-none-any.whl');
  assert.equal(scp.args.at(-1), 'yu@box:/home/yu/ca-install/wheels');
  const recursive = buildScpArgv('yu@box', { sources: ['/tmp/wh'], remoteTarget: '/home/yu/wh', recursive: true });
  assert.ok(recursive.args.includes('-r'));

  assert.match(buildStdinWriteScript('/home/yu/ca-install/wheels/ca.whl'), /cat > \/home\/yu\/ca-install\/wheels\/ca\.whl/);
  assert.match(buildStdinWriteScript('~/ca/wheels/ca.whl'), /cat > "\$HOME\/ca\/wheels\/ca\.whl"/);
  assert.match(buildStdinWriteScript('~/ca/wheels/ca.whl'), /CA_UPLOAD_DONE=1/);
  assert.match(buildTarExtractScript('/home/yu/wheelhouse'), /tar -xzf - -C \/home\/yu\/wheelhouse/);
  assert.match(buildTarExtractScript('/home/yu/wheelhouse'), /CA_EXTRACT_DONE=1/);

  const prepare = buildRemotePrepareScript({ installDir: '/home/yu/ca-install' });
  assert.match(prepare, /mkdir -p "\$CA_DIR\/wheels" "\$CA_DIR\/wheelhouse"/);
  const winPaths = buildRemotePrepareScript({ installDir: '~/CA install' });
  assert.match(winPaths, /CA_DIR="\$HOME\/CA install"/, 'remote ~ expansion keeps spaces quoted');

  const pyCheck = buildRemotePythonCheckScript({ python: '/usr/bin/python3.11' });
  assert.match(pyCheck, /\/usr\/bin\/python3\.11 -V|PY=\/usr\/bin\/python3\.11/);
  assert.match(pyCheck, /CA_PY_CHECK_DONE=1/);

  const venv = buildRemoteVenvCreateScript({ installDir: '/home/yu/ca-install', python: '/usr/bin/python3' });
  assert.match(venv, /-m venv "\$CA_DIR\/venv"/);
  assert.match(venv, /CA_VENV_OK=1/);

  const online = buildRemotePipInstallScript({ installDir: '/home/yu/ca-install', wheelName: 'ca_analyzer-0.1.0-py3-none-any.whl' });
  assert.match(online, /pip install/);
  assert.ok(!online.includes('--no-index'), 'online pip does not use --no-index');
  assert.match(online, /CA_PIP_DONE=1/);
  const offline = buildRemotePipInstallScript({
    installDir: '/home/yu/ca-install',
    wheelName: 'ca_analyzer-0.1.0-py3-none-any.whl',
    offline: true,
  });
  assert.match(offline, /--no-index --find-links "\$CA_DIR\/wheelhouse"/);

  const verify = buildRemoteVerifyScript({ installDir: '/home/yu/ca-install' });
  assert.match(verify, /ca version --json/);
  assert.match(verify, /CA_CONSOLE_OK/);
  assert.match(verify, /CA_VERIFY_DONE=1/);

  assert.deepEqual(remoteInstallLayout('/home/yu/ca'), {
    installDir: '/home/yu/ca',
    wheelDir: '/home/yu/ca/wheels',
    wheelhouseDir: '/home/yu/ca/wheelhouse',
    venvDir: '/home/yu/ca/venv',
    venvPython: '/home/yu/ca/venv/bin/python',
    caScript: '/home/yu/ca/venv/bin/ca',
    runDir: '/home/yu/ca/.dsh-ca',
  });
});

test('remote online wheel install uploads only the wheel and runs pip online', async () => {
  const root = makeTempDir('dshca-installer-remote-');
  try {
    const context = setupInstall({ root });
    const report = await performRemoteWheelInstall(context.options);
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.equal(report.mode, 'wheel');
    assert.equal(report.offline, false);
    assert.equal(report.installDir, '/home/yu/ca-install');
    assert.equal(report.venvPython, '/home/yu/ca-install/venv/bin/python');
    assert.equal(report.version.version, '0.1.0');
    assert.deepEqual(
      report.steps.map((step) => `${step.id}:${step.status}`),
      [
        'probe:succeeded',
        'python:succeeded',
        'upload-wheel:succeeded',
        'upload-wheelhouse:skipped',
        'venv:succeeded',
        'pip:succeeded',
        'verify:succeeded',
      ],
    );
    assert.equal(context.transfer.uploads.filter((upload) => upload.type === 'dir').length, 0);
    const fileUpload = context.transfer.uploads.find((upload) => upload.type === 'file');
    assert.ok(fileUpload);
    assert.equal(fileUpload.remotePath, '/home/yu/ca-install/wheels/ca_analyzer-0.1.0-py3-none-any.whl');
    const pipScripts = context.runner.calls
      .map((call) => call.script || '')
      .filter((script) => script.includes('CA_PIP_DONE'));
    assert.equal(pipScripts.length, 1);
    assert.ok(!pipScripts[0].includes('--no-index'));
    assert.ok(context.events.some((event) => event.type === 'step'));
    assert.ok(context.events.some((event) => event.type === 'log'));
  } finally {
    cleanupDir(root);
  }
});

test('remote offline wheel install uploads a wheelhouse and uses --no-index', async () => {
  const root = makeTempDir('dshca-installer-remote-');
  try {
    const wheelhouse = makeWheelhouse(root);
    const context = setupInstall({
      root,
      installOptions: { offline: true, wheelhousePath: wheelhouse },
    });
    const report = await performRemoteWheelInstall(context.options);
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.equal(report.offline, true);
    assert.equal(report.wheelhouse.wheelCount, 2);
    const dirUpload = context.transfer.uploads.find((upload) => upload.type === 'dir');
    assert.ok(dirUpload);
    assert.equal(dirUpload.remoteDir, '/home/yu/ca-install/wheelhouse');
    assert.equal(dirUpload.localDir, wheelhouse);
    const pipScript = context.runner.calls
      .map((call) => call.script || '')
      .find((script) => script.includes('CA_PIP_DONE'));
    assert.ok(pipScript.includes('--no-index'));
    assert.ok(pipScript.includes('--find-links'));
  } finally {
    cleanupDir(root);
  }
});

test('remote wheel install fails early on a Python older than 3.11', async () => {
  const root = makeTempDir('dshca-installer-remote-');
  try {
    const context = setupInstall({ root, runner: makeRunner({ probeOverrides: { python3_version: 'Python 3.9.18' } }) });
    const report = await performRemoteWheelInstall(context.options);
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'PYTHON_VERSION_UNSUPPORTED');
    assert.equal(report.steps.find((step) => step.id === 'python').status, 'failed');
    assert.equal(context.transfer.uploads.length, 0, 'nothing is uploaded before Python is validated');
    assert.ok(report.error.details.hints.some((hint) => /apt install python3/.test(hint)));
  } finally {
    cleanupDir(root);
  }
});

test('remote wheel install validates a configured Python interpreter on the host', async () => {
  const root = makeTempDir('dshca-installer-remote-');
  try {
    const context = setupInstall({
      root,
      installOptions: { python: '/opt/python3.10' },
      runner: makeRunner({ pythonCheck: 'Python 3.10.14' }),
    });
    const report = await performRemoteWheelInstall(context.options);
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'PYTHON_VERSION_UNSUPPORTED');
    assert.equal(report.steps.find((step) => step.id === 'python').status, 'failed');
  } finally {
    cleanupDir(root);
  }
});

test('remote wheel install surfaces upload/pip/verify failures per step', async () => {
  const root = makeTempDir('dshca-installer-remote-');
  try {
    const uploadFailure = setupInstall({
      root,
      installOptions: { installDir: '/home/yu/ca-install-1' },
      transfer: makeTransfer({ fileError: new Error('scp exploded') }),
    });
    const uploadReport = await performRemoteWheelInstall(uploadFailure.options);
    assert.equal(uploadReport.ok, false);
    assert.equal(uploadReport.steps.find((step) => step.id === 'upload-wheel').status, 'failed');

    const pipFailure = setupInstall({
      root,
      installOptions: { installDir: '/home/yu/ca-install-2' },
      runner: makeRunner({ pipFail: true }),
    });
    const pipReport = await performRemoteWheelInstall(pipFailure.options);
    assert.equal(pipReport.ok, false);
    assert.equal(pipReport.error.code, 'INSTALL_STEP_FAILED');
    assert.equal(pipReport.steps.find((step) => step.id === 'pip').status, 'failed');
    assert.match(pipReport.error.message, /Could not find a version/);

    const verifyFailure = setupInstall({
      root,
      installOptions: { installDir: '/home/yu/ca-install-3' },
      runner: makeRunner({ verifyInvalid: true }),
    });
    const verifyReport = await performRemoteWheelInstall(verifyFailure.options);
    assert.equal(verifyReport.ok, false);
    assert.equal(verifyReport.error.code, 'VERIFY_FAILED');
    assert.equal(verifyReport.steps.find((step) => step.id === 'verify').status, 'failed');
  } finally {
    cleanupDir(root);
  }
});

test('remote offline install fails clearly without a wheelhouse or bundled wheel', async () => {
  const root = makeTempDir('dshca-installer-remote-');
  try {
    const noWheelhouse = setupInstall({
      root,
      installOptions: { offline: true, wheelhousePath: path.join(root, 'missing') },
    });
    const report = await performRemoteWheelInstall(noWheelhouse.options);
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'WHEELHOUSE_NOT_FOUND');
    assert.ok(report.error.details.hints.some((hint) => /build-wheelhouse\.mjs/.test(hint)));

    const noWheel = setupInstall({ root });
    const wheelReport = await performRemoteWheelInstall({
      ...noWheel.options,
      deps: { ...noWheel.options.deps, bundledWheelDir: path.join(root, 'no-wheels') },
    });
    assert.equal(wheelReport.ok, false);
    assert.equal(wheelReport.error.code, 'WHEEL_NOT_FOUND');
  } finally {
    cleanupDir(root);
  }
});

test('adaptSourceReport maps the legacy rsync install into the unified shape', () => {
  const adapted = adaptSourceReport({
    ok: true,
    mode: 'rsync',
    repoPath: '/home/yu/CA',
    venvPath: '/home/yu/CA/.venv',
    version: { version: '0.1.0', schemaVersion: '2' },
    steps: [{ id: 'probe', status: 'succeeded' }],
    startedAt: 1,
    finishedAt: 2,
    skipped: false,
  });
  assert.equal(adapted.ok, true);
  assert.equal(adapted.mode, 'source');
  assert.equal(adapted.scope, 'remote');
  assert.equal(adapted.installDir, '/home/yu/CA');
  assert.equal(adapted.venvPython, '/home/yu/CA/.venv/bin/python');
  assert.equal(adapted.caScript, '/home/yu/CA/.venv/bin/ca');
  assert.equal(adapted.version.version, '0.1.0');
});
