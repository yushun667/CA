/**
 * Real remote-host tests (opt-in; skipped by default).
 *
 *   DSH_CA_REAL_SSH='yu@192.168.0.100' \
 *   DSH_CA_REAL_REMOTE_REPO='/home/yu/CA_OH/openharmony' \
 *   DSH_CA_REAL_REMOTE_DB='/home/yu/CA_OH/openharmony.db' \
 *   cd dsh && node --test
 *
 * By default only a read-only probe runs. Set DSH_CA_REAL_REMOTE_START=1 to
 * additionally start the remote `ca serve` through the SSH tunnel, query
 * /api/health, then stop it. Installation is never triggered here.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import { createRemoteBackend } from '../../../lib/host/backend/remote/index.js';
import { remoteTestConfig } from './helpers.mjs';

const sshTarget = process.env.DSH_CA_REAL_SSH || '';
const enabled = sshTarget.length > 0;
const repoPath = process.env.DSH_CA_REAL_REMOTE_REPO || '';
const db = process.env.DSH_CA_REAL_REMOTE_DB || '';
const dbDir = process.env.DSH_CA_REAL_REMOTE_DBDIR || '';
const python = process.env.DSH_CA_REAL_REMOTE_PYTHON || '';
const startEnabled = process.env.DSH_CA_REAL_REMOTE_START === '1';

function realConfig() {
  return remoteTestConfig({
    remote: {
      ssh: { host: sshTarget },
      repoPath: repoPath || null,
      installDir: null,
      python: python || null,
      db: db || null,
      dbDir: dbDir || null,
      stateDb: null,
      repo: null,
      compdb: null,
      serveRemotePort: 0,
      localPort: 0,
      readyTimeoutMs: 30000,
      maxRestarts: 0,
      restartBackoffMs: 10,
      env: {},
    },
  });
}

test(
  'real remote host probe over SSH',
  { skip: !enabled ? 'set DSH_CA_REAL_SSH=<user@host> to run' : false },
  async () => {
    const backend = createRemoteBackend({ config: realConfig(), logger: null, deps: {} });
    try {
      const report = await backend.probe();
      assert.equal(report.target, 'remote');
      assert.ok(report.ssh?.target, 'ssh target resolved');
      assert.ok(Array.isArray(report.checks));
      if (!report.ok) {
        console.error('remote probe not ok:', JSON.stringify(report.checks, null, 2));
        console.error('remote probe hints:', report.hints);
      }
      if (repoPath && (db || dbDir)) {
        assert.equal(report.ok, true, `remote env incomplete: ${JSON.stringify(report.hints)}`);
      }
    } finally {
      await backend.dispose();
    }
  },
);

test(
  'real remote ca serve start/health/stop through the SSH tunnel',
  {
    skip: !enabled || !startEnabled || !repoPath || !(db || dbDir)
      ? 'set DSH_CA_REAL_SSH + DSH_CA_REAL_REMOTE_REPO + (DSH_CA_REAL_REMOTE_DB|DSH_CA_REAL_REMOTE_DBDIR) + DSH_CA_REAL_REMOTE_START=1 to run'
      : false,
  },
  async () => {
    const backend = createRemoteBackend({ config: realConfig(), logger: null, deps: {} });
    try {
      const started = await backend.start();
      assert.equal(started.state, 'running');
      assert.match(started.baseUrl, /^http:\/\/127\.0\.0\.1:\d+$/);
      assert.ok(started.remote?.remotePort > 0);
      const stopped = await backend.stop();
      assert.equal(stopped.state, 'stopped');
    } finally {
      await backend.dispose();
    }
  },
);
