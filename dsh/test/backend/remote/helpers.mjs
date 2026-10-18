/**
 * Fakes for the remote backend tests: a programmable `runProcess` that answers
 * ssh/rsync invocations by inspecting the remote script, plus canned probe
 * output. No real SSH, rsync or Linux host is involved.
 */

import { createRemoteExecutor } from '../../../lib/host/backend/remote/exec.js';
import { createLogger } from '../../../lib/host/backend/log.js';
import { testConfig } from '../helpers/fakes.mjs';

/** Resolve a missing/invalid flag helper. */
function emptyResult(overrides = {}) {
  return {
    command: 'ssh',
    args: [],
    code: 0,
    signal: null,
    spawnError: null,
    timedOut: false,
    cancelled: false,
    outputTruncated: false,
    stdout: '',
    stderr: '',
    ...overrides,
  };
}

/**
 * Fake `runProcess({command,args,...})` that records calls and dispatches to
 * `handler(call, index)`. A sync/async return (or `undefined`) becomes a
 * successful empty result.
 */
export function createFakeRunner(handler = () => emptyResult()) {
  const calls = [];
  async function runProcess(options) {
    const script = options.command === 'ssh' ? options.args[options.args.length - 1] : undefined;
    const call = {
      command: options.command,
      args: options.args,
      script,
      options,
      timeoutMs: options.timeoutMs,
    };
    calls.push(call);
    const result = await handler(call, calls.length - 1);
    const finalResult =
      result === undefined ? emptyResult({ command: options.command, args: options.args }) : result;
    // Emulate runProcess line streaming so onStdoutLine/onStderrLine consumers
    // (install progress, task logs) see the data.
    const emit = (text, callback) => {
      if (typeof callback !== 'function') return;
      for (const line of String(text || '').split(/\r?\n/)) {
        if (line.length > 0) callback(line, { truncated: false });
      }
    };
    emit(finalResult.stdout, options.onStdoutLine);
    emit(finalResult.stderr, options.onStderrLine);
    return finalResult;
  }
  runProcess.calls = calls;
  return runProcess;
}

/** Canned `CA_PROBE:` stdout matching probe.js expectations. */
export function probeStdout(overrides = {}) {
  const markers = {
    begin: '1',
    os: 'Linux',
    arch: 'aarch64',
    nproc: '8',
    home: '/home/yu',
    python3: '/usr/bin/python3',
    python3_version: 'Python 3.11.7',
    pip: '/usr/bin/pip3',
    rsync: '/usr/bin/rsync',
    ca_on_path: '',
    python_used: '',
    repo_dir: '0',
    repo_manifest: '0',
    repo_ca: '0',
    repo_writable: '0',
    venv_python: '0',
    venv_ca: '0',
    ca_version_json: '',
    libclang: '',
    install_dir: '0',
    install_parent_writable: '1',
    db_file: '0',
    db_bytes: '',
    db_dir: '0',
    db_shards: '0',
    disk_kb: '52428800',
    end: '1',
    ssh_ok: '1',
    ...overrides,
  };
  return Object.entries(markers)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([key, value]) => `CA_PROBE:${key}=${value}`)
    .join('\n');
}

/** Canned probe output for an existing, working remote CA install. */
export function probeExistingInstall(overrides = {}) {
  return probeStdout({
    repo_dir: '1',
    repo_manifest: '1',
    repo_ca: '1',
    repo_writable: '1',
    venv_python: '1',
    venv_ca: '1',
    ca_version_json: JSON.stringify({
      tool: 'ca-analyzer',
      version: '0.1.0',
      schema_version: '2',
      python: '3.11.7',
      platform: 'Linux-6.1-aarch64',
    }),
    libclang: 'ok',
    python_used: '/home/yu/CA_OH/venv/bin/python',
    db_file: '1',
    db_bytes: '12345',
    ...overrides,
  });
}

/** Canned probe output for a host that can install CA (empty target dir). */
export function probeNeedInstall(overrides = {}) {
  return probeStdout({
    repo_dir: '0',
    repo_manifest: '0',
    repo_ca: '0',
    install_dir: '0',
    install_parent_writable: '1',
    ...overrides,
  });
}

/** Canned verify output from `buildRemoteVerifyScript`. */
export function verifyStdout(version = '0.1.0', schema = '2') {
  return [
    'CA_PY_VERSION=Python 3.11.7',
    JSON.stringify(
      { tool: 'ca-analyzer', version, schema_version: schema, python: '3.11.7', platform: 'Linux-6.1-aarch64' },
      null,
      2,
    ),
    'CA_CONSOLE_OK=1',
    'CA_VERIFY_DONE=1',
  ].join('\n');
}

/** Standard config for remote tests (ssh host, existing checkout, db). */
export function remoteTestConfig(overrides = {}) {
  return testConfig({
    backend: 'remote',
    remote: {
      ssh: { host: 'linux-box', user: 'yu', port: 2222, keyPath: '/home/me/.ssh/id_ed25519' },
      repoPath: '/home/yu/CA',
      installDir: null,
      python: null,
      db: '/home/yu/CA_OH/openharmony.db',
      dbDir: null,
      stateDb: null,
      repo: null,
      compdb: null,
      serveRemotePort: 0,
      localPort: 0,
      readyTimeoutMs: 3000,
      maxRestarts: 0,
      restartBackoffMs: 10,
      env: {},
    },
    requestTimeoutMs: 500,
    healthIntervalMs: 10,
    ...overrides,
  });
}

/** Executor + logger bundle wired to a fake runner/spawn. */
export function makeExecutor({ runProcess, spawn } = {}) {
  const logger = createLogger({ dir: null, ringSize: 200 });
  const executor = createRemoteExecutor({ deps: { runProcess, spawn }, logger });
  return { logger, executor };
}

/** Runner that succeeds every step of a fresh install (rsync mode). */
export function installSuccessRunner({ probe = probeNeedInstall(), verify = verifyStdout() } = {}) {
  return createFakeRunner((call) => {
    if (call.command === 'rsync') return emptyResult({ command: 'rsync', code: 0, stderr: 'sent 1 file' });
    if (call.script?.includes('CA_PROBE:begin')) return emptyResult({ stdout: probe });
    if (call.script?.includes('CA_VERIFY_DONE')) return emptyResult({ stdout: verify });
    if (call.script?.includes('venv')) return emptyResult({ stdout: 'Python 3.11.7' });
    return emptyResult({ stdout: 'ok' });
  });
}
