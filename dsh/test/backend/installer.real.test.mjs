/**
 * Real local offline install test (opt-in; default skip).
 *
 * Requires the offline wheelhouse built by `node dsh/scripts/build-wheelhouse.mjs`
 * for the current host platform. This test creates a throwaway venv under the
 * OS temp dir and performs a real `pip install --no-index --find-links <wh>`
 * of the bundled CA wheel, then verifies `ca version --json`. It never touches
 * the user's DSH profile.
 *
 *   DSH_CA_REAL_INSTALL_TEST=1 \
 *   DSH_CA_REAL_WHEELHOUSE=/path/to/wheelhouse/macos-arm64 \
 *   cd dsh && node --test test/backend/installer.real.test.mjs
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { createBackendManager } from '../../lib/host/backend/manager.js';
import { detectHostPlatform } from '../../lib/host/backend/installer/platform.js';
import { cleanupDir, makePaths, makeTempDir } from './helpers/fakes.mjs';

const enabled = process.env.DSH_CA_REAL_INSTALL_TEST === '1';
const platform = detectHostPlatform();
const defaultWheelhouse = platform.platformId
  ? path.resolve(import.meta.dirname, '..', '..', 'artifacts', 'wheelhouse', platform.platformId)
  : null;
const wheelhousePath = process.env.DSH_CA_REAL_WHEELHOUSE || defaultWheelhouse;
const wheelhouseReady = Boolean(wheelhousePath && fs.existsSync(wheelhousePath));

test(
  'real local offline install from wheelhouse',
  {
    skip: !enabled
      ? 'set DSH_CA_REAL_INSTALL_TEST=1 (and optionally DSH_CA_REAL_WHEELHOUSE) to run'
      : !wheelhouseReady
        ? `wheelhouse not found: ${wheelhousePath || '(platform unsupported)'}`
        : false,
  },
  async () => {
    const root = makeTempDir('dshca-real-install-');
    const paths = makePaths(root);
    const installDir = path.join(root, 'backend-local');
    const manager = createBackendManager(null, {}, { paths, cwd: root });
    try {
      const report = await manager.installLocal({
        installDir,
        offline: true,
        wheelhousePath,
      });
      assert.equal(report.ok, true, JSON.stringify(report.error));
      assert.equal(report.mode, 'offline');
      assert.equal(report.offline, true);
      assert.ok(report.version && typeof report.version.version === 'string');
      assert.ok(fs.existsSync(report.venvPython), `venv python missing: ${report.venvPython}`);
      assert.ok(fs.existsSync(report.caScript), `ca console script missing: ${report.caScript}`);

      const status = manager.getInstallStatus();
      assert.equal(status.local.state, 'succeeded');
      const summary = manager.getInstallSummary();
      assert.equal(summary.local.installed, true);
      assert.equal(summary.local.installDir, installDir);
    } finally {
      await manager.dispose();
      cleanupDir(root);
    }
  },
);
