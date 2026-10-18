/**
 * Small dependency-free helpers shared by the backend modules.
 *
 * Everything here is pure (except the two filesystem helpers) and covered by
 * tests; no DSH / Cordis imports so the modules stay loadable in plain Node.
 */

import fs from 'node:fs';
import path from 'node:path';

/** @param {number} ms @returns {string} */
export function nowIso(ms) {
  return new Date(ms).toISOString();
}

/**
 * Clamp an integer into [min,max] with a fallback for invalid input.
 * @param {unknown} value
 * @param {number} min
 * @param {number} max
 * @param {number} fallback
 * @returns {number}
 */
export function clampInt(value, min, max, fallback) {
  const number = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(number)) return fallback;
  const integer = Math.trunc(number);
  if (integer < min) return min;
  if (integer > max) return max;
  return integer;
}

/** True for non-array, non-null objects. */
export function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Non-empty trimmed string or null. */
export function nonEmptyString(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** `~` expansion and path normalization against `baseDir`. */
export function expandPath(value, baseDir) {
  const text = nonEmptyString(value);
  if (!text) return null;
  let expanded = text;
  if (expanded === '~') expanded = process.env.HOME || '';
  else if (expanded.startsWith('~/')) {
    expanded = path.join(process.env.HOME || '', expanded.slice(2));
  }
  if (path.isAbsolute(expanded)) return path.normalize(expanded);
  return path.resolve(baseDir || process.cwd(), expanded);
}

/** Human byte size, e.g. "1.5 MB". */
export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '0 B';
  if (value < 1024) return `${Math.round(value)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let scaled = value / 1024;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  return `${scaled >= 100 ? Math.round(scaled) : scaled.toFixed(1)} ${units[unit]}`;
}

/** UTF-8 byte length without allocating when possible. */
export function byteLength(text) {
  return Buffer.byteLength(String(text), 'utf8');
}

/**
 * Fixed-capacity ring buffer. Old items are overwritten; iteration order is
 * oldest-first. Used for bounded in-memory process/log history.
 * @template T
 * @param {number} capacity
 */
export function createRingBuffer(capacity) {
  const size = Math.max(1, Math.trunc(Number(capacity) || 1));
  /** @type {T[]} */
  const items = [];
  let start = 0;
  const api = {
    push(item) {
      if (items.length < size) items.push(item);
      else {
        items[start] = item;
        start = (start + 1) % size;
      }
    },
    toArray() {
      if (items.length < size) return items.slice();
      return items.slice(start).concat(items.slice(0, start));
    },
    last(count) {
      const all = api.toArray();
      const n = Math.max(0, Math.trunc(Number(count) || 0));
      return n >= all.length ? all : all.slice(all.length - n);
    },
    clear() {
      items.length = 0;
      start = 0;
    },
    get size() {
      return items.length;
    },
    get capacity() {
      return size;
    },
  };
  return api;
}

/** AbortError compatible with `AbortSignal.reason` consumers. */
export function createAbortError(message = '操作已取消') {
  const error = new Error(message);
  error.name = 'AbortError';
  error.code = 'ABORT_ERR';
  return error;
}

/** Throws AbortError when the signal is already aborted. */
export function throwIfAborted(signal) {
  if (signal?.aborted) throw createAbortError();
}

/**
 * setTimeout-based sleep that rejects with AbortError when `signal` aborts.
 * @param {number} ms
 * @param {AbortSignal} [signal]
 * @returns {Promise<void>}
 */
export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(createAbortError());
      return;
    }
    const timer = setTimeout(() => {
      cleanup();
      resolve();
    }, Math.max(0, ms));
    const onAbort = () => {
      clearTimeout(timer);
      cleanup();
      reject(createAbortError());
    };
    const cleanup = () => {
      if (typeof signal?.removeEventListener === 'function') {
        signal.removeEventListener('abort', onAbort);
      }
    };
    if (typeof signal?.addEventListener === 'function') {
      signal.addEventListener('abort', onAbort, { once: true });
    }
  });
}

/**
 * Incremental CRLF/LF line splitter for child-process output.
 *
 * `onLine(line, {truncated})` is called once per complete line, and once more
 * from `flush()` for a trailing partial line. Absurdly long lines without a
 * newline are tail-truncated to `maxLineLength` so a runaway child cannot grow
 * memory without bound; `truncated` tells the callback data was dropped.
 *
 * @param {(line: string, info: {truncated: boolean}) => void} onLine
 * @param {{maxLineLength?: number}} [options]
 */
export function createLineDecoder(onLine, options = {}) {
  const maxLineLength = Math.max(256, Math.trunc(Number(options.maxLineLength) || 16384));
  let buffer = '';
  let truncated = false;

  const emit = () => {
    const line = buffer.endsWith('\r') ? buffer.slice(0, -1) : buffer;
    const wasTruncated = truncated;
    buffer = '';
    truncated = false;
    onLine(line, { truncated: wasTruncated });
  };

  return {
    /** @param {string|Buffer|Uint8Array} chunk */
    push(chunk) {
      buffer += typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8');
      let index = buffer.indexOf('\n');
      while (index >= 0) {
        const rest = buffer.slice(index + 1);
        buffer = buffer.slice(0, index);
        emit();
        buffer = rest;
        index = buffer.indexOf('\n');
      }
      if (buffer.length > maxLineLength) {
        buffer = buffer.slice(buffer.length - maxLineLength);
        truncated = true;
      }
    },
    /** Emit a trailing partial line, if any. */
    flush() {
      if (buffer.length > 0) emit();
    },
  };
}

/** Promise plus its resolve/reject handles. */
export function createDeferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  // Avoid unhandled-rejection noise when a deferred is rejected but a caller
  // has not attached a handler yet (common when several waiters race).
  promise.catch(() => {});
  return { promise, resolve, reject };
}

/**
 * Atomically write a JSON document: mkdir -p, write temp file, rename over the
 * target. Rename is atomic on the same filesystem, so a crash can never leave
 * a half-written settings.json.
 *
 * @param {string} file absolute path
 * @param {unknown} value JSON-serializable value
 * @param {{mode?: number}} [options]
 */
export function atomicWriteJson(file, value, options = {}) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const temp = path.join(
    dir,
    `.${path.basename(file)}.${process.pid}.${Math.random().toString(36).slice(2, 10)}.tmp`,
  );
  const content = `${JSON.stringify(value, null, 2)}\n`;
  try {
    fs.writeFileSync(temp, content, { encoding: 'utf8', mode: options.mode ?? 0o600 });
    fs.renameSync(temp, file);
  } catch (error) {
    try {
      fs.unlinkSync(temp);
    } catch {
      // best effort cleanup only
    }
    throw error;
  }
}

/**
 * Read and parse a JSON file without throwing.
 * @param {string} file
 * @returns {{value: unknown, exists: boolean, error: Error|null}}
 */
export function readJsonFile(file) {
  let text;
  try {
    text = fs.readFileSync(file, 'utf8');
  } catch (error) {
    if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) {
      return { value: undefined, exists: false, error: null };
    }
    return { value: undefined, exists: true, error };
  }
  try {
    return { value: JSON.parse(text), exists: true, error: null };
  } catch (error) {
    return { value: undefined, exists: true, error };
  }
}

/** True when `file` exists and the current user can execute it. */
export function isExecutableFile(file) {
  if (!file) return false;
  try {
    const stat = fs.statSync(file);
    if (!stat.isFile()) return false;
    fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/** True when `target` exists and is a directory. */
export function isDirectory(target) {
  try {
    return fs.statSync(target).isDirectory();
  } catch {
    return false;
  }
}

/** True when `target` exists and is a regular file. */
export function isFile(target) {
  try {
    return fs.statSync(target).isFile();
  } catch {
    return false;
  }
}

/**
 * Merge `patch` onto `base` recursively (plain objects merge, arrays/scalars
 * replace). Returns a new object; inputs are never mutated. `undefined`
 * values in the patch mean "not set" and are skipped; `null` is a real value.
 */
export function deepMerge(base, patch) {
  if (!isPlainObject(patch)) return cloneJson(base);
  const result = isPlainObject(base) ? { ...base } : {};
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue;
    if (isPlainObject(value) && isPlainObject(result[key])) {
      result[key] = deepMerge(result[key], value);
    } else {
      result[key] = cloneJson(value);
    }
  }
  return result;
}

/** Structured-clone for JSON-ish values (undefined returns undefined). */
export function cloneJson(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

/**
 * Keep only the keys of `patch` that differ from `base`, recursing into plain
 * objects. Used to persist user overrides instead of a full resolved config.
 */
export function diffAgainstBase(base, patch) {
  if (!isPlainObject(patch)) return cloneJson(patch);
  const diff = {};
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue;
    if (isPlainObject(value) && isPlainObject(base?.[key])) {
      const nested = diffAgainstBase(base[key], value);
      if (Object.keys(nested).length > 0) diff[key] = nested;
    } else if (JSON.stringify(base?.[key]) !== JSON.stringify(value)) {
      diff[key] = cloneJson(value);
    }
  }
  return diff;
}

/** Last `n` characters of a string, for log tails in error details. */
export function tailText(text, n) {
  const value = String(text ?? '');
  const count = Math.max(0, Math.trunc(Number(n) || 0));
  return count >= value.length ? value : value.slice(value.length - count);
}
