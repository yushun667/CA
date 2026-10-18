/**
 * Real macOS -> Linux SSH test for the simplified connect/browse flow.
 *
 * Passwordless auth only (key or ssh-agent); passwords are not stored in CI
 * environment variables. Enable with:
 *
 *   DSH_CA_REAL_SSH='yu@192.168.0.100' \
 *   DSH_CA_REAL_REMOTE_DIR='/home/yu/CA_OH' \
 *   cd dsh && node --test test/backend/remote/auth.real.test.mjs
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  listRemoteDirs,
  testRemoteConnection,
} from '../../../lib/host/backend/remote/auth.js';

const ssh = process.env.DSH_CA_REAL_SSH || '';
const remoteDir = process.env.DSH_CA_REAL_REMOTE_DIR || '';
const enabled = ssh.length > 0;

test(
  'real SSH connection: home/platform/python/ca',
  { skip: !enabled ? 'set DSH_CA_REAL_SSH=<user@host> to run' : false },
  async () => {
    const report = await testRemoteConnection(ssh);
    assert.equal(report.ok, true);
    assert.ok(report.home && report.home.startsWith('/'), `unexpected home: ${report.home}`);
    assert.match(report.platform.os, /Linux/i);
    assert.equal(report.target, ssh);
    assert.equal(typeof report.python.supported, 'boolean');
    assert.equal(typeof report.ca.installed, 'boolean');
    console.log(
      `[auth.real] home=${report.home} platform=${report.platform.label} python=${report.python.version || '?'} supported=${report.python.supported} ca=${report.ca.installed ? report.ca.version : 'not installed'}`,
    );
  },
);

test(
  'real SSH directory browse: home and a project directory',
  { skip: !enabled ? 'set DSH_CA_REAL_SSH=<user@host> to run' : false },
  async () => {
    const homeListing = await listRemoteDirs(ssh, {});
    assert.ok(homeListing.path.startsWith('/'));
    assert.equal(homeListing.isHome, true);
    assert.ok(Array.isArray(homeListing.entries));

    if (remoteDir) {
      const listing = await listRemoteDirs(ssh, { path: remoteDir });
      assert.equal(listing.path, remoteDir);
      assert.ok(listing.parent);
      assert.ok(Array.isArray(listing.entries));
      for (const entry of listing.entries) {
        assert.equal(entry.path, `${listing.path}/${entry.name}`);
      }
      console.log(
        `[auth.real] ${remoteDir}: ${listing.entries.length} dirs (${listing.entries
          .slice(0, 5)
          .map((entry) => entry.name)
          .join(', ')})`,
      );
    }

    const missing = `/tmp/dsh-ca-definitely-missing-${Date.now()}`;
    await assert.rejects(listRemoteDirs(ssh, { path: missing }), (error) => {
      assert.equal(error.code, 'DIR_NOT_FOUND');
      return true;
    });
  },
);
