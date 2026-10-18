import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { BackendError } from '../../lib/host/backend/errors.js';
import {
  applyConfigPatch,
  DEFAULT_CONFIG,
  describeConfigurationHint,
  loadStoredSettings,
  publicConfig,
  resolveConfig,
  resolveConfigPaths,
  resolveConfiguredMode,
  saveStoredSettings,
  validateConfig,
} from '../../lib/host/config.js';
import { cleanupDir, makePaths, makeTempDir } from './helpers/fakes.mjs';

test('resolveConfigPaths honours DSH_HOME and the state-dir override', () => {
  const defaults = resolveConfigPaths({ env: {}, homedir: '/home/tester' });
  assert.deepEqual(defaults, {
    dshHome: '/home/tester/.dsh',
    stateDir: '/home/tester/.dsh/plugin-data/dsh-ca',
    logDir: '/home/tester/.dsh/plugin-data/dsh-ca/logs',
    settingsPath: '/home/tester/.dsh/plugin-data/dsh-ca/settings.json',
  });

  const custom = resolveConfigPaths({
    env: { DSH_HOME: '/opt/dsh', DSH_CA_STATE_DIR: '/var/lib/dsh-ca' },
    homedir: '/home/tester',
  });
  assert.equal(custom.dshHome, '/opt/dsh');
  assert.equal(custom.stateDir, '/var/lib/dsh-ca');
  assert.equal(custom.settingsPath, '/var/lib/dsh-ca/settings.json');
});

test('resolveConfig merges defaults, deployment config and user patch', () => {
  const config = resolveConfig({
    explicit: { backend: 'local', local: { port: 9876 } },
    stored: { local: { db: '/data/ca.db' } },
    env: {},
    homedir: '/home/tester',
    cwd: '/work',
  });
  assert.equal(config.backend, 'local');
  assert.equal(config.local.port, 9876);
  assert.equal(config.local.db, '/data/ca.db');
  // defaults survive
  assert.equal(config.local.host, '127.0.0.1');
  assert.equal(config.remote.serveRemotePort, 0);
  assert.equal(config.autoStart, true);
});

test('validateConfig rejects invalid values with actionable messages', () => {
  const invalidCases = [
    [{ backend: 'server' }, /backend/],
    [{ local: { port: 70000 } }, /local\.port/],
    [{ local: { maxRestarts: -1 } }, /local\.maxRestarts/],
    [{ local: { db: '/a.db', dbDir: '/b' } }, /互斥/],
    [{ remote: { ssh: { port: 0 } } }, /remote\.ssh\.port/],
    [{ remote: { db: 'relative/path.db' } }, /remote\.db/],
    [{ remote: { db: '/a.db', dbDir: '/b' } }, /互斥/],
    [{ readyTimeoutMs: 10 }, /readyTimeoutMs/],
    [{ log: { keepFiles: 0 } }, /log\.keepFiles/],
  ];
  for (const [patch, matcher] of invalidCases) {
    const merged = { ...JSON.parse(JSON.stringify(DEFAULT_CONFIG)), ...patch };
    assert.throws(
      () => validateConfig(merged, { cwd: '/work', homedir: '/home/tester' }),
      (error) => {
        assert.ok(error instanceof BackendError);
        assert.equal(error.code, 'CONFIG_INVALID');
        assert.match(error.message, matcher);
        return true;
      },
      `expected ${JSON.stringify(patch)} to be rejected`,
    );
  }
});

test('validateConfig expands ~ and relative local paths against cwd', () => {
  const previousHome = process.env.HOME;
  process.env.HOME = '/home/tester';
  try {
    const config = resolveConfig({
      explicit: { local: { db: '~/index/ca.db', repoRoot: './CA' } },
      env: {},
      homedir: '/home/tester',
      cwd: '/work/repo',
    });
    assert.equal(config.local.db, '/home/tester/index/ca.db');
    assert.equal(config.local.repoRoot, '/work/repo/CA');
  } finally {
    process.env.HOME = previousHome;
  }
});

test('resolveConfiguredMode implements the auto preference order', () => {
  const base = resolveConfig({ explicit: {}, env: {}, homedir: '/home/t', cwd: '/work' });
  assert.equal(resolveConfiguredMode(base), 'unconfigured');
  assert.equal(
    resolveConfiguredMode(
      resolveConfig({ explicit: { backend: 'local' }, env: {}, homedir: '/home/t', cwd: '/work' }),
    ),
    'local',
  );
  assert.equal(
    resolveConfiguredMode(
      resolveConfig({ explicit: { backend: 'remote' }, env: {}, homedir: '/home/t', cwd: '/work' }),
    ),
    'remote',
  );
  const localDb = resolveConfig({
    explicit: { local: { db: '/data/ca.db' } },
    env: {},
    homedir: '/home/t',
    cwd: '/work',
  });
  assert.equal(resolveConfiguredMode(localDb), 'local');
  const both = resolveConfig({
    explicit: { local: { db: '/data/ca.db' }, remote: { ssh: { host: 'source-server' } } },
    env: {},
    homedir: '/home/t',
    cwd: '/work',
  });
  assert.equal(resolveConfiguredMode(both), 'remote', 'auto prefers a configured remote host');
  assert.match(describeConfigurationHint(base), /尚未配置/);
  assert.equal(describeConfigurationHint(localDb), null);
});

test('stored settings roundtrip is sparse, atomic and versioned', () => {
  const root = makeTempDir();
  try {
    const paths = makePaths(root);
    const missing = loadStoredSettings(paths);
    assert.equal(missing.exists, false);
    assert.deepEqual(missing.patch, {});

    saveStoredSettings(paths, { backend: 'local', local: { db: '/data/ca.db' } });
    const stored = JSON.parse(fs.readFileSync(paths.settingsPath, 'utf8'));
    assert.equal(stored.version, 1);
    assert.deepEqual(stored.config, { backend: 'local', local: { db: '/data/ca.db' } });
    assert.equal(fs.statSync(paths.settingsPath).mode & 0o777, 0o600);
    assert.deepEqual(loadStoredSettings(paths).patch, stored.config);

    fs.writeFileSync(paths.settingsPath, '{broken');
    const corrupt = loadStoredSettings(paths);
    assert.equal(corrupt.exists, true);
    assert.ok(corrupt.error instanceof Error);
    assert.deepEqual(corrupt.patch, {});
  } finally {
    cleanupDir(root);
  }
});

test('applyConfigPatch validates before returning and supports resets', () => {
  const result = applyConfigPatch({
    currentPatch: { backend: 'local', local: { db: '/a.db' } },
    patch: { backend: 'remote', remote: { ssh: { host: 'linux-box' } } },
    env: {},
    homedir: '/home/t',
    cwd: '/work',
  });
  assert.equal(result.config.backend, 'remote');
  assert.equal(result.config.remote.ssh.host, 'linux-box');
  assert.deepEqual(result.patch.backend, 'remote');
  assert.equal(result.patch.local.db, '/a.db', 'previous patch keys are preserved');

  const reset = applyConfigPatch({
    currentPatch: { backend: 'local', local: { db: '/a.db' } },
    patch: { local: { db: null } },
    env: {},
    homedir: '/home/t',
    cwd: '/work',
  });
  assert.equal(reset.config.local.db, null);

  assert.throws(
    () =>
      applyConfigPatch({
        currentPatch: {},
        patch: { backend: 'nope' },
        env: {},
        homedir: '/home/t',
        cwd: '/work',
      }),
    (error) => error instanceof BackendError && error.code === 'CONFIG_INVALID',
  );
});

test('publicConfig exposes resolved settings plus state paths', () => {
  const config = resolveConfig({ explicit: {}, env: {}, homedir: '/home/t', cwd: '/work' });
  const paths = resolveConfigPaths({ env: {}, homedir: '/home/t' });
  const snapshot = publicConfig(config, paths);
  assert.equal(snapshot.backend, 'auto');
  assert.deepEqual(snapshot.paths, paths);
  assert.notEqual(snapshot.local, config.local, 'snapshot is a copy');
});
