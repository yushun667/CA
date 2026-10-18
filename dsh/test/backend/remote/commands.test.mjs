import assert from 'node:assert/strict';
import test from 'node:test';

import { BackendError } from '../../../lib/host/backend/errors.js';
import { parseCaVersionJson, parseReadyLine } from '../../../lib/host/backend/ca-protocol.js';
import {
  buildProbeScript,
  buildRemoteCaScript,
  buildRemoteEditableInstallScript,
  buildRemotePipScript,
  buildRemoteServeScript,
  buildRemoteStopScript,
  buildRemoteVenvScript,
  buildRemoteVerifyScript,
  buildRsyncArgv,
  buildSshArgv,
  buildSshTunnelArgv,
  classifySshFailure,
  normalizeSshConfig,
  parseProbeOutput,
  remotePathExpression,
  shellQuote,
} from '../../../lib/host/backend/remote/commands.js';
import { probeStdout } from './helpers.mjs';

test('shellQuote keeps safe tokens and quotes everything else', () => {
  assert.equal(shellQuote('index'), 'index');
  assert.equal(shellQuote('/home/yu/CA_OH/openharmony.db'), '/home/yu/CA_OH/openharmony.db');
  assert.equal(shellQuote('user@host'), 'user@host');
  assert.equal(shellQuote('a b'), "'a b'");
  assert.equal(shellQuote("it's"), "'it'\\''s'");
  assert.equal(shellQuote(''), "''");
  assert.equal(shellQuote('$(rm -rf /)'), "'$(rm -rf /)'");
});

test('remotePathExpression expands ~ but never lets the shell expand other paths', () => {
  assert.equal(remotePathExpression('/home/yu/CA'), '/home/yu/CA');
  assert.equal(remotePathExpression('~/CA_OH'), '"$HOME/CA_OH"');
  assert.equal(remotePathExpression('~'), '"$HOME"');
  assert.equal(remotePathExpression('/tmp/a b'), "'/tmp/a b'");
  assert.equal(remotePathExpression('~/a b'), '"$HOME/a b"');
});

test('normalizeSshConfig accepts alias, user@host and full object', () => {
  assert.deepEqual(normalizeSshConfig('linux-box'), {
    host: 'linux-box',
    user: null,
    port: null,
    keyPath: null,
    target: 'linux-box',
  });
  assert.deepEqual(normalizeSshConfig('yu@linux-box'), {
    host: 'linux-box',
    user: 'yu',
    port: null,
    keyPath: null,
    target: 'yu@linux-box',
  });
  assert.deepEqual(normalizeSshConfig({ host: 'box', user: 'yu', port: '2200', keyPath: '/k' }, null), {
    host: 'box',
    user: 'yu',
    port: 2200,
    keyPath: '/k',
    target: 'yu@box',
  });
  // object fields fall back to the configured remote.ssh
  assert.deepEqual(
    normalizeSshConfig({ host: 'override' }, { host: 'base', user: 'yu', port: 22, keyPath: '/k' }),
    { host: 'override', user: 'yu', port: 22, keyPath: '/k', target: 'yu@override' },
  );
  assert.throws(
    () => normalizeSshConfig(null, null),
    (error) => error instanceof BackendError && error.code === 'INVALID_ARGUMENT',
  );
  assert.throws(() => normalizeSshConfig({ host: 'x', port: 70000 }), /端口无效/);
});

test('buildSshArgv runs BatchMode, key/port options, target and script', () => {
  const built = buildSshArgv(
    { host: 'box', user: 'yu', port: 2222, keyPath: '/home/me/.ssh/key' },
    'echo hi',
  );
  assert.equal(built.command, 'ssh');
  assert.ok(built.args.includes('-T'));
  assert.ok(built.args.includes('BatchMode=yes'));
  assert.deepEqual(built.args.slice(0, 4), ['-p', '2222', '-i', '/home/me/.ssh/key']);
  assert.equal(built.args[built.args.length - 2], 'yu@box');
  assert.equal(built.args[built.args.length - 1], 'echo hi');
});

test('buildSshTunnelArgv builds -N -L local:remote mapping', () => {
  const built = buildSshTunnelArgv('yu@box', { localPort: 45678, remotePort: 39001 });
  assert.ok(built.args.includes('-N'));
  const forwardIndex = built.args.indexOf('-L');
  assert.equal(built.args[forwardIndex + 1], '45678:127.0.0.1:39001');
  assert.equal(built.args[built.args.length - 1], 'yu@box');
  assert.ok(built.args.includes('ExitOnForwardFailure=yes'));
  assert.throws(() => buildSshTunnelArgv('box', { localPort: 0, remotePort: 1 }), /本地隧道端口无效/);
  assert.throws(() => buildSshTunnelArgv('box', { localPort: 1, remotePort: 0 }), /远端服务端口无效/);
});

test('buildRsyncArgv excludes local-only dirs and quotes the remote path', () => {
  const built = buildRsyncArgv({
    ssh: 'yu@box',
    sourceDir: '/Users/yu/Documents/hobby/CA',
    targetDir: '/home/yu/ca install',
  });
  assert.equal(built.command, 'rsync');
  const args = built.args;
  assert.equal(args[0], '-az');
  assert.ok(args.includes('--delete'));
  assert.ok(args.includes('--exclude=.git'));
  assert.ok(args.includes('--exclude=.venv'));
  assert.ok(args.includes('--exclude=.tmp'));
  assert.ok(args.includes('--exclude=__pycache__'));
  const eIndex = args.indexOf('-e');
  assert.match(args[eIndex + 1], /ssh/);
  assert.match(args[eIndex + 1], /BatchMode=yes/);
  assert.ok(args[eIndex + 2].endsWith('/'), 'source must end with a slash to copy contents');
  assert.equal(args[eIndex + 2], '/Users/yu/Documents/hobby/CA/');
  assert.equal(args[args.length - 1], "yu@box:'/home/yu/ca install'");
  assert.throws(() => buildRsyncArgv({ ssh: 'box', sourceDir: '/x', targetDir: '' }), /安装目录未配置/);
});

test('buildRemoteServeScript starts ca serve with a pid file and DB flags', () => {
  const script = buildRemoteServeScript({
    repoPath: '/home/yu/CA',
    python: '/home/yu/CA/.venv/bin/python',
    db: '/home/yu/CA_OH/openharmony.db',
    host: '127.0.0.1',
    port: 0,
    env: { CA_LIBCLANG_LIBRARY: '/opt/libclang.so' },
  });
  assert.match(script, /^set -e/);
  assert.match(script, /export CA_LIBCLANG_LIBRARY=\/opt\/libclang\.so/);
  assert.match(script, /RUN_DIR="\$HOME\/CA\/\.dsh-ca"|RUN_DIR=\/home\/yu\/CA\/\.dsh-ca/);
  assert.match(script, /mkdir -p "\$RUN_DIR"/);
  assert.match(script, /echo \$\$ > "\$RUN_DIR\/serve\.pid"/);
  assert.match(script, /cd \/home\/yu\/CA/);
  assert.match(script, /exec \/home\/yu\/CA\/\.venv\/bin\/python -m ca serve --host 127\.0\.0\.1 --port 0/);
  assert.match(script, /--db \/home\/yu\/CA_OH\/openharmony\.db/);

  const withExtras = buildRemoteServeScript({
    repoPath: '/home/yu/CA',
    dbDir: '/home/yu/CH/index',
    stateDb: '/home/yu/jobs.db',
    repo: '/home/yu/src/openharmony',
    compdb: '/home/yu/compdb.json',
    port: 39001,
  });
  assert.match(withExtras, /--port 39001/);
  assert.match(withExtras, /--db-dir \/home\/yu\/CH\/index/);
  assert.match(withExtras, /--repo \/home\/yu\/src\/openharmony/);
  assert.match(withExtras, /--compdb \/home\/yu\/compdb\.json/);
  assert.throws(() => buildRemoteServeScript({ repoPath: '' }), /检出路径未配置/);
});

test('buildRemoteStopScript stops the pid from the run dir', () => {
  const script = buildRemoteStopScript({ repoPath: '/home/yu/CA' });
  assert.match(script, /RUN_DIR=\/home\/yu\/CA\/\.dsh-ca/);
  assert.match(script, /PID_FILE="\$RUN_DIR\/serve\.pid"/);
  assert.match(script, /kill -TERM "\$PID"/);
  assert.match(script, /kill -KILL "\$PID"/);
  assert.match(script, /rm -f "\$PID_FILE"/);
  assert.match(script, /CA_STOP_DONE/);

  const custom = buildRemoteStopScript({ repoPath: '/home/yu/CA', pidFile: 'task-abc', signal: 'INT' });
  assert.match(custom, /task-abc\.pid/);
  assert.match(custom, /kill -INT "\$PID"/);
});

test('buildRemoteCaScript executes CLI args with ~ expansion and a pid file', () => {
  const script = buildRemoteCaScript({
    repoPath: '/home/yu/CA',
    python: '/home/yu/CA/.venv/bin/python',
    args: ['index', '--repo', '~/src/openharmony', '--db', '~/CA_OH/openharmony.db', '--jobs', '8'],
    pidTag: 'task-1',
    env: { CA_JOBS: '8' },
  });
  assert.match(script, /echo \$\$ > "\$RUN_DIR\/task-1\.pid"/);
  assert.match(script, /exec \/home\/yu\/CA\/\.venv\/bin\/python -m ca index --repo "\$HOME\/src\/openharmony"/);
  assert.match(script, /--db "\$HOME\/CA_OH\/openharmony\.db"/);
  assert.match(script, /--jobs 8/);
  assert.match(script, /export CA_JOBS=8/);
  assert.throws(() => buildRemoteCaScript({ repoPath: '' }), /检出路径未配置/);
});

test('install step scripts use venv, requirements, editable install and verify', () => {
  const venv = buildRemoteVenvScript({ repoPath: '/home/yu/CA', python: '/usr/bin/python3' });
  assert.match(venv, /\/usr\/bin\/python3 -m venv \/home\/yu\/CA\/\.venv/);
  assert.match(venv, /\/home\/yu\/CA\/\.venv\/bin\/python -V/);

  const pip = buildRemotePipScript({ repoPath: '/home/yu/CA' });
  assert.match(pip, /-m pip install --disable-pip-version-check --upgrade pip/);
  assert.match(pip, /-r \/home\/yu\/CA\/requirements\.txt/);

  const offline = buildRemotePipScript({
    repoPath: '/home/yu/CA',
    offline: true,
    wheelDir: '/home/yu/wheels',
  });
  assert.match(offline, /--no-index --find-links \/home\/yu\/wheels/);
  assert.throws(
    () => buildRemotePipScript({ repoPath: '/home/yu/CA', offline: true }),
    (error) => error instanceof BackendError && error.code === 'BACKEND_NOT_CONFIGURED',
  );

  const editable = buildRemoteEditableInstallScript({ repoPath: '/home/yu/CA' });
  assert.match(editable, /-m pip install --disable-pip-version-check -e \/home\/yu\/CA/);

  const verify = buildRemoteVerifyScript({ repoPath: '/home/yu/CA' });
  assert.match(verify, /-m ca version --json/);
  assert.match(verify, /CA_CONSOLE_OK/);
  assert.match(verify, /CA_VERIFY_DONE=1/);
});

test('buildProbeScript covers system, checkout, venv, db and disk markers', () => {
  const script = buildProbeScript({
    repoPath: '/home/yu/CA',
    installDir: '/home/yu/ca-install',
    python: '/usr/bin/python3.11',
    db: '/home/yu/CA_OH/openharmony.db',
    dbDir: null,
    env: { CA_LIBCLANG_LIBRARY: '/opt/libclang.so' },
  });
  for (const marker of [
    'CA_PROBE:os=',
    'CA_PROBE:python3=',
    'CA_PROBE:pip=',
    'CA_PROBE:rsync=',
    'CA_PROBE:repo_dir',
    'CA_PROBE:repo_ca',
    'CA_PROBE:venv_python',
    'CA_PROBE:ca_version_json',
    'CA_PROBE:libclang',
    'CA_PROBE:install_parent_writable',
    'CA_PROBE:db_file',
    'CA_PROBE:db_bytes',
    'CA_PROBE:disk_kb',
    'CA_PROBE:end=1',
  ]) {
    assert.ok(script.includes(marker), `probe script is missing ${marker}`);
  }
  assert.match(script, /export CA_LIBCLANG_LIBRARY=\/opt\/libclang\.so/);
});

test('parseProbeOutput parses markers including values with equals signs', () => {
  const parsed = parseProbeOutput(probeStdout({ some_key: 'a=b' }));
  assert.equal(parsed.complete, true);
  assert.equal(parsed.markers.os, 'Linux');
  assert.equal(parsed.markers.some_key, 'a=b');
  assert.deepEqual(parseProbeOutput('garbage').markers, {});
  assert.equal(parseProbeOutput('garbage').complete, false);
});

test('classifySshFailure maps common ssh errors to actionable codes', () => {
  const auth = classifySshFailure({ code: 255, stderr: 'yu@box: Permission denied (publickey).' });
  assert.equal(auth.code, 'SSH_AUTH_FAILED');
  assert.match(auth.message, /认证失败/);

  const host = classifySshFailure({ code: 255, stderr: 'ssh: Could not resolve hostname box' });
  assert.equal(host.code, 'SSH_HOST_UNKNOWN');

  const refused = classifySshFailure({ code: 255, stderr: 'ssh: connect to host box port 22: Connection refused' });
  assert.equal(refused.code, 'SSH_UNREACHABLE');

  const missing = classifySshFailure({ spawnError: Object.assign(new Error('spawn ssh ENOENT'), { code: 'ENOENT' }) });
  assert.equal(missing.code, 'SSH_UNREACHABLE');

  const generic = classifySshFailure({ code: 1, stderr: 'something odd' });
  assert.equal(generic.code, 'SSH_FAILED');
});

test('ca-protocol parsers handle readiness line and pretty version JSON', () => {
  assert.deepEqual(parseReadyLine('[serve] CA backend listening on http://127.0.0.1:8765 (db=x)'), {
    url: 'http://127.0.0.1:8765',
    port: 8765,
  });
  assert.equal(parseReadyLine('nothing'), null);
  const record = parseCaVersionJson('{\n  "version": "0.1.0",\n  "schema_version": "2"\n}');
  assert.equal(record.version, '0.1.0');
  assert.equal(parseCaVersionJson('no json here'), null);
});
