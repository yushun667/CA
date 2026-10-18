/**
 * Real-CA integration test for the local backend lifecycle.
 *
 * Skipped by default. Enable with:
 *   DSH_CA_REAL_TEST=1 DSH_CA_REAL_DB=/abs/path/index.db \
 *     DSH_CA_REAL_REPO=/Users/yu/Documents/hobby/CA cd dsh && node --test
 *
 * The test starts the real `ca serve` process on an OS-assigned port, checks
 * /api/health, stops it again and asserts the process is gone.
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { createBackendManager } from '../../lib/host/backend/manager.js';
import { cleanupDir, makePaths, makeTempDir } from './helpers/fakes.mjs';

const enabled = process.env.DSH_CA_REAL_TEST === '1';
const repo = process.env.DSH_CA_REAL_REPO || '';
const db = process.env.DSH_CA_REAL_DB || '';

test(
  'real ca serve lifecycle through the backend manager',
  { skip: !enabled ? 'set DSH_CA_REAL_TEST=1 (and DSH_CA_REAL_REPO/DSH_CA_REAL_DB) to run' : false },
  async () => {
    assert.ok(repo, 'DSH_CA_REAL_REPO must point at a CA checkout');
    assert.ok(db, 'DSH_CA_REAL_DB must point at an indexed CA database');
    assert.ok(fs.existsSync(path.join(repo, 'pyproject.toml')), 'repo has pyproject.toml');
    assert.ok(fs.existsSync(db), 'database exists');

    const root = makeTempDir('dshca-real-');
    const paths = makePaths(root);
    const manager = createBackendManager(
      null,
      { backend: 'local', local: { repoRoot: repo, db } },
      { paths, cwd: repo, env: process.env },
    );
    try {
      const started = await manager.start();
      assert.equal(started.state, 'running');
      assert.match(started.baseUrl, /^http:\/\/127\.0\.0\.1:\d+$/);
      assert.equal(typeof started.version, 'string');

      const health = await manager.request('/api/health');
      assert.equal(health.ok, true);
      assert.equal(health.data.status, 'ok');

      const stopped = await manager.stop();
      assert.equal(stopped.state, 'stopped');
      assert.equal(stopped.baseUrl, null);
    } finally {
      await manager.dispose();
      cleanupDir(root);
    }
  },
);
