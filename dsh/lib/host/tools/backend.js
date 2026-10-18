/**
 * Backend access helpers shared by the native `ca_*` tools.
 *
 * Tools must stay useful when the backend is not running yet: a configured
 * backend is started on demand (unless `config.autoStart === false`), and every
 * failure becomes a `ToolError` with a stable code plus an actionable hint that
 * points at the CA backend page. CA `/api/*` envelopes are unwrapped here so
 * tool implementations work with plain data.
 */

import { isAbortError, normalizeError } from '../backend/errors.js'
import { isPlainObject, throwIfAborted } from '../backend/util.js'

export const BACKEND_HINT =
  'Open the CA panel (Backend page) to configure a local CA checkout/venv or a remote Linux host, then start it.'

/** Stable error for tool execution; the message is shown to the model. */
export class ToolError extends Error {
  /**
   * @param {string} code stable uppercase code, e.g. BACKEND_UNCONFIGURED
   * @param {string} message actionable, user-facing message
   * @param {Record<string, unknown>} [details] small JSON-serializable extras
   */
  constructor(code, message, details) {
    super(message)
    this.name = 'ToolError'
    this.code = code
    if (details !== undefined) this.details = details
  }
}

function safeStatus(backend) {
  try {
    return (typeof backend?.getStatus === 'function' ? backend.getStatus() : null) ?? {}
  } catch (error) {
    return { state: 'error', mode: 'unconfigured', error: { message: String(error?.message ?? error) } }
  }
}

/**
 * Return a running backend status, starting a configured backend when allowed.
 *
 * @param {any} backend backend manager (backend-ops)
 * @param {Record<string, any>} config plugin config
 * @param {AbortSignal} [signal] tool-call cancellation
 */
export async function ensureBackend(backend, config = {}, signal) {
  throwIfAborted(signal)
  const status = safeStatus(backend)
  if (status.state === 'running') return status
  const state = status.state ?? 'stopped'
  const mode = status.mode ?? 'unconfigured'
  if (mode === 'unconfigured') {
    throw new ToolError(
      'BACKEND_UNCONFIGURED',
      `CA backend is not configured yet (state: ${state}). ${BACKEND_HINT}`,
      { state },
    )
  }
  if (config?.autoStart === false) {
    throw new ToolError(
      'BACKEND_NOT_RUNNING',
      `CA backend is not running (mode: ${mode}, state: ${state}). Start it from the CA backend page or enable autoStart. ${BACKEND_HINT}`,
      { state, mode },
    )
  }
  if (typeof backend?.start !== 'function') {
    throw new ToolError(
      'BACKEND_NOT_RUNNING',
      `CA backend is not running (mode: ${mode}, state: ${state}). ${BACKEND_HINT}`,
      { state, mode },
    )
  }
  try {
    await backend.start()
  } catch (error) {
    if (isAbortError(error)) {
      throw new ToolError('CANCELLED', 'CA backend start was cancelled', {})
    }
    const normalized = normalizeError(error, 'BACKEND_START_FAILED')
    throw new ToolError(
      'BACKEND_START_FAILED',
      `CA backend failed to start: ${normalized.message}. ${BACKEND_HINT}`,
      { backendCode: normalized.code },
    )
  }
  throwIfAborted(signal)
  const after = safeStatus(backend)
  if (after.state !== 'running') {
    const detail = after.error?.message ?? after.error ?? ''
    throw new ToolError(
      'BACKEND_NOT_RUNNING',
      `CA backend did not reach the running state (state: ${after.state ?? 'unknown'}${detail ? `, ${detail}` : ''}). ${BACKEND_HINT}`,
      { state: after.state ?? 'unknown', mode: after.mode ?? mode },
    )
  }
  return after
}

/** Prefix CA error codes so tool error codes stay namespaced/stable. */
function toolCodeFromCa(code) {
  if (typeof code !== 'string' || code.length === 0) return 'CA_REQUEST_FAILED'
  if (code.startsWith('CA_')) return code
  return `CA_${code.replace(/[^A-Za-z0-9_]+/g, '_').toUpperCase()}`
}

/**
 * Call one CA `/api/*` endpoint through the backend manager and unwrap the
 * `{ok:true,data}` envelope. Transport errors and CA error envelopes both
 * become ToolError with an actionable message.
 *
 * @param {any} backend
 * @param {string} path
 * @param {{method?: 'GET'|'HEAD'|'POST', query?: Record<string, unknown>, body?: unknown, timeoutMs?: number, signal?: AbortSignal}} [options]
 * @returns {Promise<any>}
 */
export async function caRequest(backend, path, options = {}) {
  throwIfAborted(options.signal)
  if (typeof backend?.request !== 'function') {
    throw new ToolError('BACKEND_UNSUPPORTED', 'The CA backend manager does not support HTTP requests.')
  }
  let envelope
  try {
    envelope = await backend.request(path, {
      method: options.method ?? 'GET',
      query: options.query,
      body: options.body,
      timeoutMs: options.timeoutMs,
      signal: options.signal,
    })
  } catch (error) {
    if (isAbortError(error)) {
      throw new ToolError('CANCELLED', 'CA request was cancelled', {})
    }
    const normalized = normalizeError(error, 'CA_REQUEST_FAILED')
    throw new ToolError(
      normalized.code === 'BACKEND_NOT_RUNNING' ? 'BACKEND_NOT_RUNNING' : 'CA_REQUEST_FAILED',
      normalized.code === 'BACKEND_NOT_RUNNING'
        ? `CA backend is not running: ${normalized.message}. ${BACKEND_HINT}`
        : `CA request ${path} failed: ${normalized.message}`,
      { path, backendCode: normalized.code },
    )
  }
  throwIfAborted(options.signal)
  if (!isPlainObject(envelope)) {
    throw new ToolError('CA_INVALID_RESPONSE', `CA backend returned an invalid response for ${path}`, { path })
  }
  if (envelope.ok === true) return envelope.data
  const error = isPlainObject(envelope.error) ? envelope.error : {}
  const code = toolCodeFromCa(error.code)
  throw new ToolError(
    code,
    `CA API ${path} failed: ${error.message || error.code || 'unknown CA error'}`,
    { path, caCode: error.code ?? null },
  )
}

/** Convenience GET wrapper. @param {any} backend @param {string} path @param {Record<string, unknown>} [query] */
export function caGet(backend, path, query, options = {}) {
  return caRequest(backend, path, { ...options, method: 'GET', query })
}

/** Convenience POST wrapper. @param {any} backend @param {string} path @param {unknown} body */
export function caPost(backend, path, body, options = {}) {
  return caRequest(backend, path, { ...options, method: 'POST', body })
}

/** Clamp text to the tool output budget, telling the model how to see more. */
export function clipText(text, maxChars, hint) {
  const value = typeof text === 'string' ? text : String(text ?? '')
  const limit = Number.isFinite(maxChars) && maxChars > 0 ? Math.trunc(maxChars) : 20000
  if (value.length <= limit) return value
  const notice = hint || 'narrow the query (limit/offset/compact) to see the rest'
  return `${value.slice(0, limit)}\n…[output truncated after ${limit} characters; ${notice}]`
}

/** Render a string of numbered source lines with a fixed gutter. */
export function numberLines(lines) {
  if (!Array.isArray(lines) || lines.length === 0) return ''
  const width = String(lines[lines.length - 1].line ?? 0).length
  return lines
    .map((entry) => `${String(entry.line ?? 0).padStart(width, ' ')}  ${entry.text ?? ''}`)
    .join('\n')
}

/** One-line duration from epoch-millisecond timestamps. */
export function formatDuration(startedAt, finishedAt) {
  if (!Number.isFinite(startedAt)) return '—'
  const end = Number.isFinite(finishedAt) ? finishedAt : Date.now()
  const totalMs = Math.max(0, end - startedAt)
  if (totalMs < 1000) return `${totalMs}ms`
  const seconds = Math.round(totalMs / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes < 60) return `${minutes}m${rest > 0 ? `${rest}s` : ''}`
  const hours = Math.floor(minutes / 60)
  return `${hours}h${minutes % 60 > 0 ? `${minutes % 60}m` : ''}`
}

/** ISO timestamp for stable, locale-independent tool output. */
export function formatTime(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  return new Date(ms).toISOString()
}
