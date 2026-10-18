import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../../lib/host/backend/errors.js';
import {
  ASKPASS_HELPER_CONTENT,
  ASKPASS_PASSWORD_ENV,
  buildConnectionTestScript,
  buildListDirsScript,
  buildSshArgv,
  createAskpassHelper,
  createSshAuthSession,
  listRemoteDirs,
  normalizeSshAuth,
  publicSshConfig,
  runSshWithAuth,
  sshTargetKey,
  testRemoteConnection,
} from '../../../lib/host/backend/remote/auth.js';
import { cleanupDir, makeTempDir } from '../helpers/fakes.mjs';

function makeRunner(handler) {
  const calls = [];
  const run = async (options) => {
    calls.push(options);
    const result = await handler(options, calls.length - 1);
    return {
      command: options.command,
      args: options.args,
      code: 0,
      signal: null,
      stdout: '',
      stderr: '',
      spawnError: null,
      timedOut: false,
      cancelled: false,
      ...(result || {}),
    };
  };
  run.calls = calls;
  return run;
}

test('normalizeSshAuth parses string/object forms and strips secrets publicly', () => {
  assert.deepEqual(normalizeSshAuth('yu@linux-box'), {
    host: 'linux-box',
    user: 'yu',
    port: null,
    keyPath: null,
    target: 'yu@linux-box',
    password: null,
    agent: false,
  });
  const withPassword = normalizeSshAuth({ host: 'box', user: 'yu', port: 2222, password: 's3cret' });
  assert.equal(withPassword.password, 's3cret');
  assert.equal(withPassword.port, 2222);
  assert.deepEqual(publicSshConfig(withPassword), {
    host: 'box',
    user: 'yu',
    port: 2222,
    keyPath: null,
  });
  assert.equal(publicSshConfig(withPassword).password, undefined);
  assert.equal(sshTargetKey(withPassword), 'yu@box:2222');
  assert.equal(normalizeSshAuth({ host: 'box', agent: true }).agent, true);
});

test('askpass helper is a mode-0600 script with no embedded password and is deleted', () => {
  const root = makeTempDir('dshca-askpass-test-');
  try {
    const helper = createAskpassHelper({ tmpDir: root });
    assert.ok(fs.existsSync(helper.path));
    assert.equal(fs.statSync(helper.path).mode & 0o777, 0o600);
    const content = fs.readFileSync(helper.path, 'utf8');
    assert.equal(content, ASKPASS_HELPER_CONTENT);
    assert.match(content, new RegExp(ASKPASS_PASSWORD_ENV));
    assert.ok(!content.includes('s3cret'));
    helper.cleanup();
    assert.equal(fs.existsSync(helper.dir), false);
    helper.cleanup(); // idempotent
  } finally {
    cleanupDir(root);
  }
});

test('passwordless sessions keep BatchMode and expose no askpass state', () => {
  const keySession = createSshAuthSession({ ssh: { host: 'box', keyPath: '/home/me/.ssh/key' } });
  assert.equal(keySession.batchMode, true);
  assert.equal(keySession.method, 'key');
  assert.deepEqual(keySession.extraOptions, []);
  assert.deepEqual(keySession.env, {});
  assert.equal(keySession.askpass, null);
  keySession.cleanup();

  const agentSession = createSshAuthSession({ ssh: 'yu@box' });
  assert.equal(agentSession.method, 'agent');
  assert.equal(agentSession.batchMode, true);
  agentSession.cleanup();
});

test('password sessions drop BatchMode and configure SSH_ASKPASS fully', () => {
  const session = createSshAuthSession({
    ssh: { host: 'box', user: 'yu', password: 's3cret' },
    deps: { platform: 'darwin', display: ':7' },
  });
  try {
    assert.equal(session.batchMode, false);
    assert.equal(session.method, 'password');
    assert.equal(session.ssh.password, undefined, 'session ssh object never carries the password');
    assert.ok(session.extraOptions.includes('PreferredAuthentications=publickey,password,keyboard-interactive'));
    assert.ok(session.extraOptions.includes('PubkeyAuthentication=yes'));
    assert.ok(session.extraOptions.includes('NumberOfPasswordPrompts=1'));
    assert.equal(session.env.SSH_ASKPASS, session.askpass.path);
    assert.equal(session.env.SSH_ASKPASS_REQUIRE, 'force');
    assert.equal(session.env.DISPLAY, ':7');
    assert.equal(session.env[ASKPASS_PASSWORD_ENV], 's3cret');
    assert.ok(fs.existsSync(session.askpass.path));
    const built = buildSshArgv({ host: 'box', user: 'yu' }, 'echo hi', { session });
    assert.ok(!built.args.includes('BatchMode=yes'), 'password auth must allow prompts');
    assert.ok(built.args.includes('PreferredAuthentications=publickey,password,keyboard-interactive'));
    assert.ok(!built.args.includes('s3cret'), 'password never appears in argv');
  } finally {
    session.cleanup();
  }
  assert.equal(fs.existsSync(session.askpass.path), false);
});

test('password auth on Windows fails with a clear unsupported error', () => {
  assert.throws(
    () =>
      createSshAuthSession({
        ssh: { host: 'box', user: 'yu', password: 's3cret' },
        deps: { platform: 'win32' },
      }),
    (error) => error instanceof BackendError && error.code === 'PASSWORD_AUTH_UNSUPPORTED_ON_WINDOWS',
  );
});

test('runSshWithAuth passes askpass env, cleans the helper and never leaks the password', async () => {
  const runner = makeRunner(() => ({ stdout: 'ok' }));
  const result = await runSshWithAuth({
    ssh: { host: 'box', user: 'yu', password: 's3cret' },
    script: 'echo hi',
    deps: { runProcess: runner, platform: 'darwin', env: {} },
    label: 'unit',
  });
  assert.equal(result.authMethod, 'password');
  const call = runner.calls[0];
  assert.ok(call.env.SSH_ASKPASS);
  assert.equal(call.env.SSH_ASKPASS_REQUIRE, 'force');
  assert.equal(call.env[ASKPASS_PASSWORD_ENV], 's3cret');
  assert.ok(!call.args.join(' ').includes('s3cret'));
  assert.ok(!String(result.stdout).includes('s3cret'));
  const helperPath = call.env.SSH_ASKPASS;
  assert.equal(fs.existsSync(helperPath), false, 'askpass helper removed after the command');
});

test('testRemoteConnection parses one ssh round trip into a connection report', async () => {
  const runner = makeRunner(() => ({
    stdout: [
      'CA_CONN_HOME=/home/yu',
      'CA_CONN_OS=Linux',
      'CA_CONN_ARCH=x86_64',
      'CA_CONN_PYTHON=/usr/bin/python3',
      'CA_CONN_PY_VERSION=Python 3.11.17',
      'CA_CONN_CA=/home/yu/CA_OH/venv/bin/ca',
      `CA_CONN_CA_VERSION=${JSON.stringify({ version: '0.1.0', schema_version: '2' }).replace(/\n/g, ' ')}`,
      'CA_CONN_DONE=1',
    ].join('\n'),
  }));
  const report = await testRemoteConnection('yu@linux-box', {
    deps: { runProcess: runner, env: {} },
  });
  assert.equal(report.ok, true);
  assert.equal(report.authMethod, 'agent');
  assert.equal(report.target, 'yu@linux-box');
  assert.equal(report.home, '/home/yu');
  assert.equal(report.platform.os, 'Linux');
  assert.equal(report.platform.arch, 'x86_64');
  assert.equal(report.python.path, '/usr/bin/python3');
  assert.equal(report.python.version, '3.11.17');
  assert.equal(report.python.supported, true);
  assert.equal(report.ca.installed, true);
  assert.equal(report.ca.version, '0.1.0');
  assert.equal(runner.calls.length, 1);
});

test('testRemoteConnection prefers an existing >=3.11 venv when system python is old', async () => {
  const runner = makeRunner(() => ({
    stdout: [
      'CA_CONN_HOME=/home/yu',
      'CA_CONN_OS=Linux',
      'CA_CONN_ARCH=x86_64',
      'CA_CONN_PYTHON=/usr/bin/python3',
      'CA_CONN_PY_VERSION=Python 3.10.12',
      'CA_CONN_PY_CANDIDATE=/home/yu/CA_OH/venv/bin/python|Python 3.11.17',
      'CA_CONN_CA=',
      'CA_CONN_DONE=1',
    ].join('\n'),
  }));
  const report = await testRemoteConnection('yu@lifecycle-box', { deps: { runProcess: runner, env: {} } });
  assert.equal(report.python.supported, true);
  assert.equal(report.python.path, '/home/yu/CA_OH/venv/bin/python');
  assert.equal(report.python.version, '3.11.17');
  assert.equal(report.python.system, '/usr/bin/python3');
  assert.equal(report.python.candidates.length, 1);
  assert.equal(report.python.candidates[0].supported, true);
});

test('testRemoteConnection maps auth failures and hides the password', async () => {
  const runner = makeRunner(() => ({
    code: 255,
    stderr: 'yu@linux-box: Permission denied (publickey,password).',
  }));
  await assert.rejects(
    testRemoteConnection(
      { host: 'linux-box', user: 'yu', password: 's3cret' },
      { deps: { runProcess: runner, env: {}, platform: 'darwin' } },
    ),
    (error) => {
      assert.ok(error instanceof BackendError);
      assert.equal(error.code, 'SSH_AUTH_FAILED');
      assert.ok(!error.message.includes('s3cret'));
      assert.ok(!JSON.stringify(error.details || {}).includes('s3cret'));
      return true;
    },
  );
});

test('connection test script/version parsing handles missing CA and old Python', async () => {
  const runner = makeRunner(() => ({
    stdout: [
      'CA_CONN_HOME=/root',
      'CA_CONN_OS=Linux',
      'CA_CONN_ARCH=aarch64',
      'CA_CONN_PYTHON=',
      'CA_CONN_CA=',
      'CA_CONN_DONE=1',
    ].join('\n'),
  }));
  const report = await testRemoteConnection('root@box', { deps: { runProcess: runner, env: {} } });
  assert.equal(report.home, '/root');
  assert.equal(report.python.path, null);
  assert.equal(report.python.supported, false);
  assert.equal(report.ca.installed, false);
  assert.match(buildConnectionTestScript(), /CA_CONN_HOME=/);
});

test('buildListDirsScript quotes paths and skips hidden directories', () => {
  const script = buildListDirsScript({ path: '/home/yu/CA OH' });
  assert.match(script, /TARGET='\/home\/yu\/CA OH'/);
  assert.match(script, /case "\$NAME" in \.\*\) continue/);
  assert.match(script, /CA_DIR_ENTRY=/);
  assert.match(script, /pwd -P/);
  const homeScript = buildListDirsScript({});
  assert.match(homeScript, /TARGET="\$HOME"/);
});

test('listRemoteDirs returns path/parent/home/entries from markers', async () => {
  const runner = makeRunner(() => ({
    stdout: [
      'CA_DIR_HOME=/home/yu',
      'CA_DIR_PATH=/home/yu/CA_OH',
      'CA_DIR_PARENT=/home/yu',
      'CA_DIR_IS_HOME=0',
      'CA_DIR_ENTRY=openharmony',
      'CA_DIR_ENTRY=index',
      'CA_DIR_DONE=1',
    ].join('\n'),
  }));
  const listing = await listRemoteDirs('yu@box', { path: '/home/yu/CA_OH' }, { deps: { runProcess: runner, env: {} } });
  assert.equal(listing.path, '/home/yu/CA_OH');
  assert.equal(listing.parent, '/home/yu');
  assert.equal(listing.home, '/home/yu');
  assert.equal(listing.isHome, false);
  assert.deepEqual(listing.entries, [
    { name: 'openharmony', path: '/home/yu/CA_OH/openharmony' },
    { name: 'index', path: '/home/yu/CA_OH/index' },
  ]);
  assert.match(runner.calls[0].args.at(-1), /CA_DIR_PATH/);
});

test('listRemoteDirs starts at home and maps path errors', async () => {
  const homeRunner = makeRunner(() => ({
    stdout: ['CA_DIR_HOME=/home/yu', 'CA_DIR_PATH=/home/yu', 'CA_DIR_IS_HOME=1', 'CA_DIR_DONE=1'].join('\n'),
  }));
  const listing = await listRemoteDirs('yu@box', {}, { deps: { runProcess: homeRunner, env: {} } });
  assert.equal(listing.path, '/home/yu');
  assert.equal(listing.isHome, true);
  assert.deepEqual(listing.entries, []);

  const cases = [
    ['NOT_FOUND', 'DIR_NOT_FOUND'],
    ['NOT_READABLE', 'DIR_NOT_READABLE'],
    ['NOT_DIRECTORY', 'DIR_NOT_A_DIRECTORY'],
  ];
  for (const [reason, code] of cases) {
    const runner = makeRunner(() => ({ stdout: `CA_DIR_ERROR=${reason}` }));
    await assert.rejects(
      listRemoteDirs('yu@box', { path: '/nope' }, { deps: { runProcess: runner, env: {} } }),
      (error) => {
        assert.equal(error.code, code);
        assert.equal(error.details.reason, reason);
        return true;
      },
    );
  }
});
