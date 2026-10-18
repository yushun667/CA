import assert from 'node:assert/strict';
import test from 'node:test';

import { probeRemote } from '../../../lib/host/backend/remote/probe.js';
import { createLogger } from '../../../lib/host/backend/log.js';
import { createFakeRunner, makeExecutor, probeExistingInstall, probeStdout, remoteTestConfig } from './helpers.mjs';

const logger = createLogger({ dir: null, ringSize: 100 });

function makeProbe({ config = remoteTestConfig(), runner }) {
  const { executor } = makeExecutor({ runProcess: runner });
  return probeRemote({ config, executor, logger });
}

test('probeRemote reports a healthy Linux host and CA install', async () => {
  const runner = createFakeRunner(() => ({ code: 0, stdout: probeExistingInstall(), stderr: '' }));
  const report = await makeProbe({ runner });
  assert.equal(report.ok, true);
  assert.equal(report.target, 'remote');
  assert.equal(report.platform, 'linux');
  assert.equal(report.ssh.target, 'yu@linux-box');
  assert.equal(report.os.arch, 'aarch64');
  assert.equal(report.os.cpus, 8);
  assert.equal(report.python.system, '/usr/bin/python3');
  assert.equal(report.pip, '/usr/bin/pip3');
  assert.equal(report.rsync, '/usr/bin/rsync');
  assert.equal(report.ca.version, '0.1.0');
  assert.equal(report.paths.repoPath.exists, true);
  assert.equal(report.paths.repoPath.isCaRepo, true);
  assert.equal(report.paths.db.exists, true);
  assert.equal(report.paths.db.bytes, 12345);
  assert.equal(report.libclang.available, true);
  assert.equal(report.disk.availableText, '50.0 GB');
  assert.equal(report.checks.find((check) => check.name === 'database').ok, true);
  assert.equal(runner.calls.length, 1, 'one ssh round trip');
  assert.match(runner.calls[0].script, /CA_PROBE:begin=1/);
});

test('probeRemote returns a failure report instead of throwing on SSH errors', async () => {
  const runner = createFakeRunner(() => ({
    code: 255,
    stdout: '',
    stderr: 'yu@linux-box: Permission denied (publickey).',
  }));
  const report = await makeProbe({ runner });
  assert.equal(report.ok, false);
  assert.equal(report.error.code, 'SSH_AUTH_FAILED');
  assert.equal(report.checks[0].name, 'ssh');
  assert.equal(report.checks[0].ok, false);
  assert.ok(report.hints.length > 0);
});

test('probeRemote returns a failure report when SSH is not configured', async () => {
  const config = remoteTestConfig({ remote: { ssh: { host: null } } });
  const runner = createFakeRunner();
  const report = await makeProbe({ config, runner });
  assert.equal(report.ok, false);
  assert.equal(report.error.code, 'BACKEND_NOT_CONFIGURED');
  assert.match(report.hints.join(' '), /remote\.ssh\.host/);
  assert.equal(runner.calls.length, 0);
});

test('probeRemote flags non-Linux hosts and missing databases', async () => {
  const darwin = await makeProbe({
    runner: createFakeRunner(() => ({ code: 0, stdout: probeStdout({ os: 'Darwin', db_file: '1' }) })),
  });
  assert.equal(darwin.ok, false);
  assert.equal(darwin.checks.find((check) => check.name === 'os').ok, false);
  assert.match(darwin.hints.join(' '), /Linux/);

  const noDb = await makeProbe({
    runner: createFakeRunner(() => ({ code: 0, stdout: probeStdout({ os: 'Linux' }) })),
  });
  assert.equal(noDb.ok, false);
  assert.equal(noDb.checks.find((check) => check.name === 'database').ok, false);
  assert.match(noDb.hints.join(' '), /数据库/);
});

test('probeRemote reports missing checkout and suggests installDir', async () => {
  const runner = createFakeRunner(() => ({
    code: 0,
    stdout: probeStdout({ repo_dir: '0', repo_ca: '0' }),
  }));
  const report = await makeProbe({ runner });
  const repoCheck = report.checks.find((check) => check.name === 'repo');
  assert.equal(repoCheck.ok, false);
  assert.match(report.hints.join(' '), /remote\.repoPath/);
});

test('probeRemote accepts install-dir-only configuration', async () => {
  const config = remoteTestConfig({
    remote: {
      ssh: { host: 'linux-box', user: 'yu' },
      repoPath: null,
      installDir: '/home/yu/ca-install',
      db: '/home/yu/CA_OH/openharmony.db',
    },
  });
  const runner = createFakeRunner(() => ({
    code: 0,
    stdout: probeStdout({ install_dir: '0', install_parent_writable: '1', db_file: '1' }),
  }));
  const report = await makeProbe({ config, runner });
  const installCheck = report.checks.find((check) => check.name === 'install-dir');
  assert.equal(installCheck.ok, true, 'parent writable is enough to install');
  assert.equal(report.ok, true);
  assert.match(runner.calls[0].script, /install_parent_writable/);
});

test('probeRemote treats a corrupted script reply as PROBE_FAILED', async () => {
  const runner = createFakeRunner(() => ({ code: 0, stdout: 'login banner only', stderr: '' }));
  const report = await makeProbe({ runner });
  assert.equal(report.ok, false);
  assert.equal(report.error.code, 'PROBE_FAILED');
  assert.match(report.error.message, /标记/);
});

test('probeRemote warns when libclang is missing but queries still work', async () => {
  const runner = createFakeRunner(() => ({
    code: 0,
    stdout: probeExistingInstall({ libclang: '' }),
  }));
  const report = await makeProbe({ runner });
  assert.equal(report.ok, true, 'libclang is not required for querying');
  assert.equal(report.libclang.available, false);
  assert.ok(report.warnings.some((warning) => /libclang/.test(warning)));
});

test('probeRemote accepts ssh override strings without touching config', async () => {
  const runner = createFakeRunner(() => ({ code: 0, stdout: probeExistingInstall() }));
  const { executor } = makeExecutor({ runProcess: runner });
  const report = await probeRemote({
    config: remoteTestConfig(),
    sshOverride: 'other@other-box',
    executor,
    logger,
  });
  assert.equal(report.ssh.target, 'other@other-box');
  assert.match(runner.calls[0].args.join(' '), /other@other-box/);
});
