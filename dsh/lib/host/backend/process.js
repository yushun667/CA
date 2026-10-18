/**
 * Child-process helpers for the backend modules.
 *
 * `ca serve`, one-shot probes and long-running CA CLI tasks all go through
 * here so lifecycle details live in one place:
 *   - POSIX children are spawned detached so they own a process group; killing
 *     the group stops CA's worker subprocesses instead of orphaning them.
 *   - stdout/stderr are decoded into lines with a bounded line length, and
 *     every line is recorded in the bounded logger.
 *   - `runProcess` is the one-shot variant (probe/version/ssh) with timeout and
 *     cancellation; it never throws for a non-zero exit code, only for spawn
 *     failures, so callers can inspect exit codes.
 */

import net from 'node:net';

import { BackendError, describeCause } from './errors.js';
import { createDeferred, createLineDecoder, tailText } from './util.js';

const DEFAULT_GRACE_MS = 5000;

/**
 * Kill a child and its process group (POSIX). Falls back to `child.kill` when
 * the group cannot be signalled (fake children in tests, Windows, already
 * reaped). Never throws.
 * @param {import('node:child_process').ChildProcess|any} child
 * @param {NodeJS.Signals} [signal]
 */
export function killProcessTree(child, signal = 'SIGTERM') {
  if (!child) return;
  const pid = child.pid;
  if (typeof pid === 'number' && pid > 0 && process.platform !== 'win32') {
    try {
      process.kill(-pid, signal);
      return;
    } catch {
      // fall through to child.kill (process may have exited already)
    }
  }
  try {
    child.kill(signal);
  } catch {
    // already gone
  }
}

/**
 * Spawn a managed child process.
 *
 * @param {object} options
 * @param {Function} options.spawn node:child_process spawn compatible
 * @param {string} options.command
 * @param {string[]} options.args
 * @param {string} [options.cwd]
 * @param {Record<string,string>} [options.env]
 * @param {boolean} [options.detached] default true on POSIX
 * @param {import('./log.js').createLogger extends Function ? any : any} [options.logger]
 * @param {(line: string, info: {truncated: boolean}) => void} [options.onStdoutLine]
 * @param {(line: string, info: {truncated: boolean}) => void} [options.onStderrLine]
 * @param {number} [options.maxLineLength]
 */
export function spawnManaged(options) {
  const spawn = options.spawn;
  if (typeof spawn !== 'function') {
    throw new BackendError('SPAWN_FAILED', '进程启动失败：未注入 spawn 实现');
  }
  const command = options.command;
  const args = Array.isArray(options.args) ? options.args.map(String) : [];
  if (typeof command !== 'string' || command.trim().length === 0) {
    throw new BackendError('SPAWN_FAILED', '进程启动失败：命令为空');
  }
  const detached = options.detached !== false && process.platform !== 'win32';
  const maxLineLength = options.maxLineLength || 16384;

  let child;
  try {
    child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env,
      detached,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch (error) {
    throw new BackendError('SPAWN_FAILED', `无法启动进程 ${command}：${describeCause(error)}`, {
      command,
      cause: describeCause(error),
    });
  }

  const exited = createDeferred();
  let settled = false;
  let spawnError = null;
  let killSignal = null;
  const settle = (info) => {
    if (settled) return;
    settled = true;
    exited.resolve({ ...info, spawnError });
  };

  const handleLine = (stream, line, info) => {
    try {
      options.logger?.record?.(stream, info?.truncated ? `[truncated] ${line}` : line);
    } catch {
      // logging is advisory
    }
    try {
      const callback = stream === 'stdout' ? options.onStdoutLine : options.onStderrLine;
      callback?.(line, info || { truncated: false });
    } catch {
      // a bad callback must not crash the process tree
    }
  };
  const stdoutDecoder = createLineDecoder((line, info) => handleLine('stdout', line, info), {
    maxLineLength,
  });
  const stderrDecoder = createLineDecoder((line, info) => handleLine('stderr', line, info), {
    maxLineLength,
  });
  const flushDecoders = () => {
    try {
      stdoutDecoder.flush();
    } catch {
      // ignore
    }
    try {
      stderrDecoder.flush();
    } catch {
      // ignore
    }
  };

  child.on('error', (error) => {
    spawnError = error;
    settle({ code: null, signal: null, error });
  });
  child.on('exit', (code, signal) => {
    flushDecoders();
    settle({ code, signal: signal || null, error: null });
  });
  child.on('close', () => {
    flushDecoders();
  });
  if (child.stdout && typeof child.stdout.on === 'function') {
    child.stdout.on('data', (chunk) => stdoutDecoder.push(chunk));
  }
  if (child.stderr && typeof child.stderr.on === 'function') {
    child.stderr.on('data', (chunk) => stderrDecoder.push(chunk));
  }

  return {
    command,
    args,
    child,
    pid: child.pid,
    exited: exited.promise,
    get settled() {
      return settled;
    },
    get spawnError() {
      return spawnError;
    },
    get killSignal() {
      return killSignal;
    },
    /** Kill the process group (default SIGTERM). Safe to call repeatedly. */
    kill(signal = 'SIGTERM') {
      if (settled) return false;
      killSignal = signal;
      killProcessTree(child, signal);
      return true;
    },
    /** SIGTERM, wait `graceMs`, then SIGKILL. Resolves with exit info. */
    async terminate(graceMs = DEFAULT_GRACE_MS) {
      if (settled) return exited.promise;
      killProcessTree(child, 'SIGTERM');
      killSignal = 'SIGTERM';
      const timer = setTimeout(() => {
        if (!settled) {
          killProcessTree(child, 'SIGKILL');
          killSignal = 'SIGKILL';
        }
      }, Math.max(0, graceMs));
      try {
        return await exited.promise;
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

/**
 * One-shot process execution with timeout/cancellation.
 * Resolves `{code,signal,stdout,stderr,timedOut,cancelled,error,spawnError}`.
 *
 * @param {object} options
 * @param {Function} options.spawn
 * @param {string} options.command
 * @param {string[]} [options.args]
 * @param {string} [options.cwd]
 * @param {Record<string,string>} [options.env]
 * @param {number} [options.timeoutMs] 0/undefined = no timeout
 * @param {AbortSignal} [options.signal]
 * @param {number} [options.maxOutputBytes] per-stream cap
 * @param {any} [options.logger]
 * @param {number} [options.graceMs]
 */
export async function runProcess(options) {
  const maxOutputBytes = Math.max(1024, Number(options.maxOutputBytes) || 256 * 1024);
  const graceMs = Math.max(100, Number(options.graceMs) || 1000);
  const handle = spawnManaged({
    spawn: options.spawn,
    command: options.command,
    args: options.args,
    cwd: options.cwd,
    env: options.env,
    logger: options.logger,
    maxLineLength: 16384,
    onStdoutLine: options.onStdoutLine,
    onStderrLine: options.onStderrLine,
  });

  const stdoutChunks = [];
  const stderrChunks = [];
  let stdoutBytes = 0;
  let stderrBytes = 0;
  let outputTruncated = false;
  const capture = (chunks, chunk, stream) => {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    const used = stream === 'stdout' ? stdoutBytes : stderrBytes;
    if (used >= maxOutputBytes) {
      outputTruncated = true;
      return;
    }
    const remaining = maxOutputBytes - used;
    const slice = buffer.length <= remaining ? buffer : buffer.subarray(0, remaining);
    if (slice.length < buffer.length) outputTruncated = true;
    chunks.push(slice);
    if (stream === 'stdout') stdoutBytes += slice.length;
    else stderrBytes += slice.length;
  };
  if (handle.child?.stdout && typeof handle.child.stdout.on === 'function') {
    handle.child.stdout.on('data', (chunk) => capture(stdoutChunks, chunk, 'stdout'));
  }
  if (handle.child?.stderr && typeof handle.child.stderr.on === 'function') {
    handle.child.stderr.on('data', (chunk) => capture(stderrChunks, chunk, 'stderr'));
  }

  let timedOut = false;
  let cancelled = false;
  let timer = null;
  let forceTimer = null;
  const timeoutMs = Math.max(0, Number(options.timeoutMs) || 0);
  if (timeoutMs > 0) {
    timer = setTimeout(() => {
      timedOut = true;
      handle.kill('SIGTERM');
      forceTimer = setTimeout(() => {
        if (!handle.settled) handle.kill('SIGKILL');
      }, graceMs);
    }, timeoutMs);
  }
  const callerSignal = options.signal;
  const onAbort = () => {
    cancelled = true;
    handle.kill('SIGTERM');
    forceTimer = setTimeout(() => {
      if (!handle.settled) handle.kill('SIGKILL');
    }, graceMs);
  };
  if (callerSignal) {
    if (callerSignal.aborted) onAbort();
    else if (typeof callerSignal.addEventListener === 'function') {
      callerSignal.addEventListener('abort', onAbort, { once: true });
    }
  }

  let info;
  try {
    info = await handle.exited;
  } finally {
    if (timer) clearTimeout(timer);
    if (forceTimer) clearTimeout(forceTimer);
    if (callerSignal && typeof callerSignal.removeEventListener === 'function') {
      callerSignal.removeEventListener('abort', onAbort);
    }
  }

  return {
    command: handle.command,
    args: handle.args,
    code: info.code,
    signal: info.signal,
    spawnError: info.error || null,
    timedOut,
    cancelled,
    outputTruncated,
    stdout: Buffer.concat(stdoutChunks).toString('utf8'),
    stderr: Buffer.concat(stderrChunks).toString('utf8'),
  };
}

/**
 * Build a child environment: inherit the plugin process env, add configured
 * overrides, and force unbuffered Python output so readiness lines appear
 * immediately. Values that are not strings are dropped (spawn rejects them).
 *
 * @param {Record<string,string>} [overrides]
 * @param {Record<string,string|undefined>} [base]
 * @returns {Record<string,string>}
 */
export function buildChildEnv(overrides, base = process.env) {
  /** @type {Record<string,string>} */
  const env = {};
  for (const [key, value] of Object.entries(base || {})) {
    if (typeof value === 'string') env[key] = value;
  }
  for (const [key, value] of Object.entries(overrides || {})) {
    if (value === undefined || value === null) continue;
    env[key] = String(value);
  }
  env.PYTHONUNBUFFERED = env.PYTHONUNBUFFERED || '1';
  env.PYTHONIOENCODING = env.PYTHONIOENCODING || 'utf-8';
  return env;
}

/**
 * Ask the OS for an unused TCP port (fallback path when `ca serve --port 0`
 * readiness cannot be parsed).
 * @param {{host?: string, netImpl?: typeof import('node:net')}} [options]
 * @returns {Promise<number>}
 */
export function findFreePort(options = {}) {
  const host = options.host || '127.0.0.1';
  const netImpl = options.netImpl || net;
  return new Promise((resolve, reject) => {
    const server = netImpl.createServer();
    server.unref();
    server.on('error', (error) => {
      reject(
        new BackendError('PORT_UNAVAILABLE', `无法获取空闲端口（${host}）：${describeCause(error)}`, {
          host,
          cause: describeCause(error),
        }),
      );
    });
    server.listen(0, host, () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : null;
      server.close(() => {
        if (typeof port === 'number' && port > 0) resolve(port);
        else reject(new BackendError('PORT_UNAVAILABLE', '无法获取空闲端口：操作系统未返回端口'));
      });
    });
  });
}

/** Last `n` lines of captured stderr/stdout, joined for error details. */
export function outputTail(result, lines = 8) {
  const text = [result?.stderr, result?.stdout]
    .filter((part) => part && String(part).trim().length > 0)
    .map((part) => String(part).trimEnd())
    .join('\n');
  if (!text) return '';
  const parts = text.split('\n');
  return tailText(parts.slice(-lines).join('\n'), 4000);
}
