import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../../lib/host/backend/errors.js';
import { createLogger } from '../../../lib/host/backend/log.js';
import {
  createInstallSteps,
  performRemoteInstall,
  resolveLocalSource,
} from '../../../lib/host/backend/remote/install.js';
import { createRemoteExecutor } from '../../../lib/host/backend/remote/exec.js';
import {
  cleanupDir,
  makeTempDir,
  writeExecutable,
} from '../helpers/fakes.mjs';
import {
  createFakeRunner,
  installSuccessRunner,
  probeExistingInstall,
  probeNeedInstall,
  probeStdout,
  remoteTestConfig,
  verifyStdout,
} from './helpers.mjs';

const logger = createLogger({ dir: null, ringSize: 200 });

function makeCaCheckout(dir) {
  fs.mkdirSync(path.join(dir, 'src', 'ca'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'src', 'ca', '__init__.py'), '');
  fs.writeFileSync(path.join(dir, 'pyproject.toml'), '[project]\nname = "ca-analyzer"\n');
  fs.writeFileSync(path.join(dir, 'requirements.txt'), 'clang==18.1.8\n');
  writeExecutable(path.join(dir, '.venv', 'bin', 'python'));
  writeExecutable(path.join(dir, '.venv', 'bin', 'ca'));
  return dir;
}

function installOptions(localRepo) {
  return {
    localRepo,
    timeoutMs: 5000,
  };
}

async function runInstall({ config, runner, options = {}, onEvent }) {
  const { executor } = (() => {
    const exec = createRemoteExecutor({ deps: { runProcess: runner }, logger });
    return { executor: exec };
  })();
  return performRemoteInstall({
    config,
    executor,
    logger,
    installOptions: options,
    onEvent,
    localCwd: '/nonexistent-local-cwd',
  });
}

test('createInstallSteps returns every step pending', () => {
  const steps = createInstallSteps();
  assert.deepEqual(
    steps.map((step) => step.id),
    ['probe', 'source', 'rsync', 'venv', 'dependencies', 'editable', 'verify'],
  );
  assert.ok(steps.every((step) => step.status === 'pending'));
});

test('performRemoteInstall rsyncs a fresh checkout, installs editable and verifies', async () => {
  const local = makeCaCheckout(makeTempDir('dshca-local-ca-'));
  try {
    const runner = installSuccessRunner();
    const events = [];
    const report = await runInstall({
      config: remoteTestConfig({
        remote: {
          ssh: { host: 'linux-box', user: 'yu' },
          repoPath: null,
          installDir: '/home/yu/ca-install',
          db: '/home/yu/CA_OH/openharmony.db',
          serveRemotePort: 0,
          localPort: 0,
          readyTimeoutMs: 3000,
          maxRestarts: 0,
          restartBackoffMs: 10,
          env: {},
        },
      }),
      runner,
      options: installOptions(local),
      onEvent: (event) => events.push(event),
    });
    assert.equal(report.ok, true, JSON.stringify(report.error));
    assert.equal(report.mode, 'rsync');
    assert.equal(report.repoPath, '/home/yu/ca-install');
    assert.equal(report.version.version, '0.1.0');
    assert.equal(report.version.schemaVersion, '2');
    assert.deepEqual(
      report.steps.map((step) => `${step.id}:${step.status}`),
      [
        'probe:succeeded',
        'source:succeeded',
        'rsync:succeeded',
        'venv:succeeded',
        'dependencies:succeeded',
        'editable:succeeded',
        'verify:succeeded',
      ],
    );
    const rsyncCall = runner.calls.find((call) => call.command === 'rsync');
    assert.ok(rsyncCall, 'rsync ran');
    assert.ok(rsyncCall.args.some((arg) => arg === '--exclude=.git'));
    assert.ok(rsyncCall.args[rsyncCall.args.length - 1].includes('linux-box:/home/yu/ca-install'));
    const sshScripts = runner.calls.filter((call) => call.command === 'ssh').map((call) => call.script);
    assert.ok(sshScripts.some((script) => script.includes('CA_PROBE:begin')));
    assert.ok(sshScripts.some((script) => script.includes('mkdir -p /home/yu/ca-install')));
    assert.ok(sshScripts.some((script) => script.includes('-m venv')));
    assert.ok(sshScripts.some((script) => script.includes('pip install') && script.includes('requirements.txt')));
    assert.ok(sshScripts.some((script) => script.includes('pip install') && script.includes('-e /home/yu/ca-install')));
    assert.ok(sshScripts.some((script) => script.includes('CA_VERIFY_DONE')));
    assert.ok(events.some((event) => event.type === 'step' && event.step.id === 'rsync'));
    assert.ok(events.some((event) => event.type === 'log'));
  } finally {
    cleanupDir(local);
  }
});

test('performRemoteInstall is idempotent for an existing working install', async () => {
  const runner = createFakeRunner((call) => {
    if (call.script?.includes('CA_PROBE:begin')) return { code: 0, stdout: probeExistingInstall(), stderr: '' };
    return { code: 0, stdout: '', stderr: '' };
  });
  const report = await runInstall({ config: remoteTestConfig(), runner });
  assert.equal(report.ok, true);
  assert.equal(report.skipped, true);
  assert.equal(report.mode, 'existing');
  assert.equal(report.version.version, '0.1.0');
  for (const stepId of ['rsync', 'venv', 'dependencies', 'editable']) {
    assert.equal(report.steps.find((step) => step.id === stepId).status, 'skipped');
  }
  assert.equal(report.steps.find((step) => step.id === 'verify').status, 'succeeded');
  assert.equal(runner.calls.length, 1, 'only the probe ssh call happened');
});

test('performRemoteInstall repairs a broken venv in an existing checkout', async () => {
  const runner = createFakeRunner((call) => {
    if (call.script?.includes('CA_PROBE:begin')) {
      return {
        code: 0,
        stdout: probeExistingInstall({
          venv_python: '0',
          venv_ca: '0',
          ca_version_json: '',
          python_used: '',
        }),
        stderr: '',
      };
    }
    if (call.script?.includes('CA_VERIFY_DONE')) return { code: 0, stdout: verifyStdout(), stderr: '' };
    return { code: 0, stdout: '', stderr: '' };
  });
  const report = await runInstall({ config: remoteTestConfig(), runner });
  assert.equal(report.ok, true, JSON.stringify(report.error));
  assert.equal(report.skipped, false);
  assert.equal(report.steps.find((step) => step.id === 'rsync').status, 'skipped');
  assert.equal(report.steps.find((step) => step.id === 'venv').status, 'succeeded');
  assert.equal(report.steps.find((step) => step.id === 'editable').status, 'succeeded');
  const venvScript = runner.calls.find((call) => call.script?.includes('-m venv'));
  assert.ok(venvScript, 'venv was recreated');
});

test('performRemoteInstall reports the failing step with diagnostics', async () => {
  const local = makeCaCheckout(makeTempDir('dshca-local-ca-'));
  try {
    const runner = createFakeRunner((call) => {
      if (call.command === 'rsync') return { code: 0, stdout: '', stderr: '' };
      if (call.script?.includes('CA_PROBE:begin')) return { code: 0, stdout: probeNeedInstall(), stderr: '' };
      if (call.script?.includes('requirements.txt')) {
        return { code: 1, stdout: 'Collecting clang', stderr: 'ERROR: Could not find a version that satisfies clang' };
      }
      return { code: 0, stdout: '', stderr: '' };
    });
    const report = await runInstall({
      config: remoteTestConfig({
        remote: {
          ssh: { host: 'linux-box', user: 'yu' },
          repoPath: null,
          installDir: '/home/yu/ca-install',
          db: '/home/yu/CA_OH/x.db',
          serveRemotePort: 0,
          localPort: 0,
          readyTimeoutMs: 3000,
          maxRestarts: 0,
          restartBackoffMs: 10,
          env: {},
        },
      }),
      runner,
      options: installOptions(local),
    });
    assert.equal(report.ok, false);
    assert.equal(report.error.code, 'INSTALL_STEP_FAILED');
    assert.equal(report.error.message.includes('exitCode=1'), true);
    assert.equal(report.steps.find((step) => step.id === 'dependencies').status, 'failed');
    assert.equal(report.steps.find((step) => step.id === 'editable').status, 'pending');
    assert.equal(report.steps.find((step) => step.id === 'verify').status, 'pending');
  } finally {
    cleanupDir(local);
  }
});

test('performRemoteInstall fails early when SSH is unreachable', async () => {
  const runner = createFakeRunner(() => ({
    code: 255,
    stdout: '',
    stderr: 'ssh: connect to host linux-box port 2222: Connection refused',
  }));
  const report = await runInstall({ config: remoteTestConfig(), runner });
  assert.equal(report.ok, false);
  assert.equal(report.error.code, 'SSH_UNREACHABLE');
  assert.equal(report.steps.find((step) => step.id === 'probe').status, 'failed');
  assert.equal(runner.calls.length, 1);
});

test('performRemoteInstall refuses a non-CA directory at repoPath', async () => {
  const runner = createFakeRunner(() => ({
    code: 0,
    stdout: probeStdout({ repo_dir: '1', repo_ca: '0', repo_manifest: '1' }),
    stderr: '',
  }));
  const report = await runInstall({ config: remoteTestConfig(), runner });
  assert.equal(report.ok, false);
  assert.equal(report.steps.find((step) => step.id === 'source').status, 'failed');
  assert.match(report.error.message, /不是 CA 检出/);
});

test('performRemoteInstall reports a missing source checkout step', async () => {
  const local = makeTempDir('dshca-empty-');
  try {
    // config repoPath points at a missing remote dir; source is local cwd only.
    const runner = createFakeRunner((call) => {
      if (call.command === 'rsync') return { code: 0, stdout: '', stderr: '' };
      if (call.script?.includes('CA_PROBE:begin')) {
        return { code: 0, stdout: probeNeedInstall(), stderr: '' };
      }
      return { code: 0, stdout: '', stderr: '' };
    });
    const report = await runInstall({
      config: remoteTestConfig({
        remote: {
          ssh: { host: 'linux-box' },
          repoPath: '/remote/missing',
          installDir: null,
          db: '/home/yu/x.db',
          serveRemotePort: 0,
          localPort: 0,
          readyTimeoutMs: 3000,
          maxRestarts: 0,
          restartBackoffMs: 10,
          env: {},
        },
        local: { repoRoot: '/definitely/not/a/repo' },
      }),
      runner,
      options: { localRepo: '/definitely/not/a/repo', timeoutMs: 5000 },
    });
    assert.equal(report.ok, false);
    assert.equal(report.steps.find((step) => step.id === 'source').status, 'failed');
    assert.match(report.error.message, /本机 CA 源码|rsync/);
  } finally {
    cleanupDir(local);
  }
});

test('resolveLocalSource finds the enclosing CA checkout', () => {
  const dir = makeCaCheckout(makeTempDir('dshca-source-'));
  try {
    assert.equal(resolveLocalSource({ localCwd: dir }), dir);
    const nested = path.join(dir, 'dsh');
    fs.mkdirSync(nested, { recursive: true });
    assert.equal(resolveLocalSource({ localCwd: nested }), dir);
    assert.equal(resolveLocalSource({ localCwd: '/definitely/not/ca', effective: {} }), null);
  } finally {
    cleanupDir(dir);
  }
});

test('performRemoteInstall validates offline wheel configuration', async () => {
  const local = makeCaCheckout(makeTempDir('dshca-local-ca-'));
  try {
    const runner = createFakeRunner((call) => {
      if (call.command === 'rsync') return { code: 0, stdout: '', stderr: '' };
      if (call.script?.includes('CA_PROBE:begin')) return { code: 0, stdout: probeNeedInstall(), stderr: '' };
      return { code: 0, stdout: '', stderr: '' };
    });
    const report = await runInstall({
      config: remoteTestConfig({
        remote: {
          ssh: { host: 'linux-box' },
          repoPath: null,
          installDir: '/home/yu/ca-install',
          db: '/home/yu/x.db',
          serveRemotePort: 0,
          localPort: 0,
          readyTimeoutMs: 3000,
          maxRestarts: 0,
          restartBackoffMs: 10,
          env: {},
        },
      }),
      runner,
      options: { ...installOptions(local), offline: true },
    });
    assert.equal(report.ok, false);
    assert.equal(report.steps.find((step) => step.id === 'dependencies').status, 'failed');
    assert.match(report.error.message, /wheelDir|离线/);
  } finally {
    cleanupDir(local);
  }
});
