/**
 * Bounded log sink for the backend layer.
 *
 * Two destinations, both fail-safe (a logging error never breaks the service):
 *   - a fixed-capacity in-memory ring buffer for UI/RPC `logs` endpoints;
 *   - an optional on-disk log file with daily naming and size-based rotation.
 *
 * The logger is intentionally synchronous: process output callbacks arrive in
 * bursts and small appendFileSync writes are cheaper than managing a queue.
 * File size is tracked per current file; rotation renames the current file to
 * `<base>.<day>.log.1` and shifts older rotations up to `keepFiles`.
 */

import fs from 'node:fs';
import path from 'node:path';

import { createRingBuffer, nowIso } from './util.js';

/**
 * @typedef {object} LogEntry
 * @property {number} at epoch milliseconds
 * @property {string} stream `stdout` | `stderr` | `info` | `warn` | `error`
 * @property {string} line one line without trailing newline
 */

/** @param {number} ms @returns {string} YYYY-MM-DD (local time) */
function dayStamp(ms) {
  const date = new Date(ms);
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

/**
 * @param {object} [options]
 * @param {string|null} [options.dir] log directory; null disables file output
 * @param {string} [options.fileBase] base file name without extension
 * @param {number} [options.maxBytes] rotate when a file grows past this size
 * @param {number} [options.keepFiles] number of rotated files to keep
 * @param {number} [options.ringSize] in-memory entries
 * @param {() => number} [options.now] injectable clock
 * @param {typeof import('node:fs')} [options.fsImpl] injectable fs (tests)
 */
export function createLogger(options = {}) {
  const dir = options.dir ? path.resolve(String(options.dir)) : null;
  const fileBase = String(options.fileBase || 'backend');
  const maxBytes = Math.max(4096, Math.trunc(Number(options.maxBytes) || 512 * 1024));
  const keepFiles = Math.max(1, Math.trunc(Number(options.keepFiles) || 3));
  const now = typeof options.now === 'function' ? options.now : Date.now;
  const fsImpl = options.fsImpl || fs;
  const ring = createRingBuffer(Math.max(16, Math.trunc(Number(options.ringSize) || 500)));
  /** @type {Set<(entry: LogEntry) => void>} */
  const listeners = new Set();

  let currentDay = dayStamp(now());
  let currentFile = dir ? path.join(dir, `${fileBase}.${currentDay}.log`) : null;
  let bytes = 0;
  if (currentFile) {
    try {
      bytes = fsImpl.statSync(currentFile).size;
    } catch {
      bytes = 0;
    }
  }

  const filePath = () => currentFile;

  /** @param {string} stream @param {string} line */
  function appendToFile(stream, line) {
    if (!dir || !currentFile) return;
    try {
      fsImpl.mkdirSync(dir, { recursive: true });
      const day = dayStamp(now());
      if (day !== currentDay) {
        currentDay = day;
        currentFile = path.join(dir, `${fileBase}.${currentDay}.log`);
        bytes = 0;
      }
      const text = `[${nowIso(now())}] [${stream}] ${line}\n`;
      const size = Buffer.byteLength(text, 'utf8');
      if (bytes > 0 && bytes + size > maxBytes) rotateFile();
      fsImpl.appendFileSync(currentFile, text, { encoding: 'utf8', mode: 0o600 });
      bytes += size;
    } catch {
      // Logging must never take the backend down; memory ring still has it.
    }
  }

  function rotateFile() {
    if (!currentFile) return;
    try {
      if (fsImpl.existsSync(currentFile)) {
        for (let index = keepFiles - 1; index >= 1; index -= 1) {
          const from = `${currentFile}.${index}`;
          const to = `${currentFile}.${index + 1}`;
          if (fsImpl.existsSync(from)) fsImpl.renameSync(from, to);
        }
        fsImpl.renameSync(currentFile, `${currentFile}.1`);
      }
    } catch {
      // best-effort rotation; keep appending to the current file instead
    }
    bytes = 0;
  }

  /** Record one line (process output or a plugin log line). */
  function record(stream, rawLine) {
    const line = String(rawLine ?? '').replace(/\r$/, '');
    const entry = { at: now(), stream: String(stream || 'info'), line };
    ring.push(entry);
    appendToFile(entry.stream, line);
    for (const listener of listeners) {
      try {
        listener({ ...entry });
      } catch {
        // a bad listener must not affect other listeners
      }
    }
    return entry;
  }

  return {
    /** Current log file path (null when file output is disabled). */
    get filePath() {
      return currentFile;
    },
    /** Configured log directory (null when disabled). */
    get dir() {
      return dir;
    },
    /** Bytes written to the current file. */
    get bytes() {
      return bytes;
    },
    /** Bounded in-memory history, oldest first. */
    history(count = 200) {
      return ring.last(count);
    },
    /** Subscribe to newly recorded lines; returns an unsubscribe function. */
    subscribe(listener) {
      if (typeof listener !== 'function') return () => {};
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    record,
    /** @param {string} message */
    info(message) {
      return record('info', message);
    },
    /** @param {string} message */
    warn(message) {
      return record('warn', message);
    },
    /** @param {string} message */
    error(message) {
      return record('error', message);
    },
    /** No resources are held open by the synchronous logger. */
    close() {
      listeners.clear();
    },
  };
}
