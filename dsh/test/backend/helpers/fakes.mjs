/**
 * Shared fakes for the backend tests. No real CA process, SSH or network is
 * touched here; tests that need them are gated by environment variables.
 */

import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { DEFAULT_CONFIG } from '../../../lib/host/config.js';
import { deepMerge } from '../../../lib/host/backend/util.js';

/** Config object for module-level tests (skips user-facing validation minima). */
export function testConfig(overrides = {}) {
  return deepMerge(JSON.parse(JSON.stringify(DEFAULT_CONFIG)), overrides);
}

/** Unique temp directory under the OS temp dir. */
export function makeTempDir(prefix = 'dshca-test-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/** State paths rooted in a temp dir. */
export function makePaths(root) {
  const stateDir = path.join(root, 'state');
  return {
    dshHome: root,
    stateDir,
    logDir: path.join(stateDir, 'logs'),
    settingsPath: path.join(stateDir, 'settings.json'),
  };
}

/** Create an executable file (used to fake python/ca binaries). */
export function writeExecutable(file, content = '#!/bin/sh\nexit 0\n') {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content, 'utf8');
  fs.chmodSync(file, 0o755);
}

/** Remove a temp tree; never throws. */
export function cleanupDir(dir) {
  if (!dir) return;
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    // best effort
  }
}

/**
 * Programmable fake `node:child_process.spawn`.
 *
 * Each spawned child is an EventEmitter with stdout/stderr emitters and a kill
 * implementation that records signals and emits `exit` (like a cooperative
 * process). `onSpawn` runs on a macrotask so the production code has attached
 * its listeners first.
 */
export function createFakeSpawn(options = {}) {
  const calls = [];
  const onSpawn = options.onSpawn || null;
  const defaultPid = options.pid || 12000;

  function spawn(command, args = [], spawnOptions = {}) {
    const child = new EventEmitter();
    child.pid = defaultPid + calls.length;
    child.killed = false;
    child.killSignals = [];
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = null;
    child.kill = (signal = 'SIGTERM') => {
      if (child.killed) return false;
      child.killed = true;
      child.killSignals.push(signal);
      setImmediate(() => child.exit(null, signal));
      return true;
    };
    child.emitData = (stream, text) => {
      const emitter = stream === 'stderr' ? child.stderr : child.stdout;
      emitter.emit('data', Buffer.from(text, 'utf8'));
    };
    child.exit = (code = 0, signal = null) => {
      if (child._exited) return;
      child._exited = true;
      child.emit('exit', code, signal);
      child.emit('close', code, signal);
    };
    child._exited = false;
    const call = { command: String(command), args: args.map(String), options: spawnOptions, child };
    calls.push(call);
    setImmediate(() => onSpawn?.(call, calls.length - 1));
    return child;
  }

  return {
    spawn,
    calls,
    get count() {
      return calls.length;
    },
    last() {
      return calls[calls.length - 1] || null;
    },
    all() {
      return calls;
    },
  };
}

/** Fake fetch with a handler `(call, index) => Response-like`. */
export function createFakeFetch(handler) {
  const calls = [];
  async function fetchImpl(url, init = {}) {
    const call = { url: String(url), init, urlObject: new URL(String(url)) };
    calls.push(call);
    return handler(call, calls.length - 1);
  }
  fetchImpl.calls = calls;
  return fetchImpl;
}

/** JSON Response-like object (only fields the client uses). */
export function jsonResponse(payload, status = 200) {
  const text = typeof payload === 'string' ? payload : JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ 'content-type': 'application/json' }),
    async text() {
      return text;
    },
  };
}

/** `/api/health` response with CA's real payload shape. */
export function healthResponse(overrides = {}, status = 200) {
  return jsonResponse(
    {
      ok: true,
      data: {
        status: 'ok',
        server: 'ca-serve',
        version: '0.1.0',
        started_at: '2026-10-18T00:00:00+0800',
        uptime_seconds: 0.1,
        database: { paths: ['/tmp/ca.db'], federated: false, state_db: '/tmp/ca.serve-state.db' },
        jobs: { queued: 0, running: 0 },
        mcp_endpoint: '/mcp',
        events_endpoint: '/events',
        features: { jobs: true, sse: true, mcp: true, federated: false },
        ...overrides,
      },
    },
    status,
  );
}

/** Streaming SSE Response-like object; push/close drive the stream. */
export function createSseResponse() {
  let controller = null;
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(c) {
      controller = c;
    },
  });
  return {
    response: {
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'text/event-stream' }),
      body,
      async text() {
        return '';
      },
    },
    push(text) {
      controller.enqueue(encoder.encode(text));
    },
    close() {
      controller.close();
    },
    abort() {
      const error = new Error('The operation was aborted');
      error.name = 'AbortError';
      try {
        controller.error(error);
      } catch {
        // stream already closed
      }
    },
  };
}

/** Fetch impl that never settles until aborted (timeout tests). */
export function hangingFetch() {
  const calls = [];
  const fetchImpl = (url, init = {}) => {
    calls.push({ url: String(url), init });
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        const error = new Error('The operation was aborted');
        error.name = 'AbortError';
        reject(error);
      };
      if (init.signal?.aborted) onAbort();
      else init.signal?.addEventListener('abort', onAbort, { once: true });
    });
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

/** Fetch impl that always rejects like a refused connection. */
export function unreachableFetch(message = 'connect ECONNREFUSED 127.0.0.1:1') {
  const error = new Error(message);
  error.code = 'ECONNREFUSED';
  const fetchImpl = async () => {
    throw error;
  };
  fetchImpl.calls = [];
  return fetchImpl;
}

/** Resolve on the next macrotask (lets setImmediate-scheduled fakes run). */
export function nextTick() {
  return new Promise((resolve) => setImmediate(resolve));
}

/** Wait until `predicate()` is true (polling, bounded). */
export async function waitFor(predicate, { timeoutMs = 2000, intervalMs = 5, label = 'condition' } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await predicate()) return;
    if (Date.now() > deadline) throw new Error(`waitFor timed out: ${label}`);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
