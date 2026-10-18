/**
 * Host side of the Client <-> Host channel (`/dsh-ca`).
 *
 * Transport is a DSH-owned same-origin HTTP route rather than
 * `ctx.connection.rpc.handle`: the native RPC registry requires services that a
 * plain-JS plugin context cannot read (activation fails with
 * "cannot get property webServer without inject"), so the plugin registers one
 * `ctx.webServer.register({kind:'prefix', path:'/dsh-ca', handler})` route.
 *
 *   Host:   POST /dsh-ca/<endpoint>  (DSH auth via connection.requestRejection)
 *   Client: fetch('/dsh-ca/<endpoint>', {method:'POST', body: JSON payload})
 *
 * The route never throws: every outcome is an envelope
 * `{ok:true,value}` or `{ok:false,error:{code,message,details}}`, and every
 * payload is validated (shape, ranges, path allow-list, byte budgets) before
 * it reaches a service. Stable error codes let the UI switch on failures.
 *
 * HTTP statuses: business outcomes 200, invalid arguments/paths 400, oversized
 * bodies 413, unknown endpoints 404, non-POST 405, trust rejection 401/403.
 */

import { BackendError, isAbortError, isBackendError, normalizeError } from '../backend/errors.js'
import { clampInt, isPlainObject } from '../backend/util.js'

export const CHANNEL = '/dsh-ca'
export const PLUGIN_VERSION = '0.1.0'

/** Endpoints exposed on the channel; kept in sync with the dispatcher. */
export const RPC_ENDPOINTS = Object.freeze([
  'ping',
  'status',
  'ca.request',
  'backend.get',
  'backend.configure',
  'backend.start',
  'backend.stop',
  'backend.restart',
  'backend.probe',
  'backend.install',
  'backend.installStatus',
  'backend.databases',
  'backend.useDatabase',
  'tasks.start',
  'tasks.list',
  'tasks.get',
  'tasks.logs',
  'tasks.cancel',
  'explain',
])

const MAX_PAYLOAD_BYTES = 512 * 1024
const MAX_CONFIG_PATCH_BYTES = 64 * 1024
const MAX_CA_BODY_BYTES = 1024 * 1024
const MAX_PATH_LENGTH = 2048
const MAX_SSH_BYTES = 16 * 1024
const MAX_OPTIONS_BYTES = 64 * 1024
const MAX_QUERY_ENTRIES = 64
const MAX_QUERY_VALUE_LENGTH = 2048
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024
const MAX_HTTP_BODY_BYTES = 1024 * 1024
const MAX_SYMBOL_LENGTH = 1024
const MAX_QUESTION_LENGTH = 4000
const MAX_ID_LENGTH = 128
const MAX_DB_PATH_LENGTH = 4096

/** Stable RPC error carrying a machine-readable code. */
export class RpcError extends Error {
  /**
   * @param {string} code stable uppercase code, e.g. INVALID_ARGUMENT
   * @param {string} message actionable user-facing message
   * @param {Record<string, unknown>} [details] small JSON-serializable extras
   */
  constructor(code, message, details) {
    super(message)
    this.name = 'RpcError'
    this.code = code
    if (details !== undefined) this.details = details
  }
}

function fail(code, message, details) {
  throw new RpcError(code, message, details)
}

// --------------------------------------------------------------------------- //
// Payload helpers
// --------------------------------------------------------------------------- //

function jsonBytes(value) {
  try {
    return Buffer.byteLength(JSON.stringify(value), 'utf8')
  } catch {
    return Number.POSITIVE_INFINITY
  }
}

/** JSON-safe clone; drops functions/undefined; never throws for JSON input. */
function jsonSafe(value) {
  if (value === undefined) return undefined
  try {
    return JSON.parse(JSON.stringify(value))
  } catch {
    return undefined
  }
}

/** @param {unknown} payload */
function validatePayload(payload) {
  if (payload === undefined || payload === null) return {}
  if (!isPlainObject(payload)) {
    fail('INVALID_ARGUMENT', 'RPC payload must be a JSON object')
  }
  if (jsonBytes(payload) > MAX_PAYLOAD_BYTES) {
    fail('PAYLOAD_TOO_LARGE', `RPC payload exceeds ${MAX_PAYLOAD_BYTES} bytes`)
  }
  return payload
}

/**
 * @param {Record<string, unknown>} payload
 * @param {string} name
 * @param {{required?: boolean, maxLength?: number}} [options]
 * @returns {string|undefined}
 */
function optionalString(payload, name, options = {}) {
  const value = payload[name]
  if (value === undefined || value === null) {
    if (options.required) fail('INVALID_ARGUMENT', `payload.${name} is required`)
    return undefined
  }
  if (typeof value !== 'string') {
    fail('INVALID_ARGUMENT', `payload.${name} must be a string`)
  }
  const text = value.trim()
  if (text.length === 0) {
    if (options.required) fail('INVALID_ARGUMENT', `payload.${name} must not be empty`)
    return undefined
  }
  const maxLength = options.maxLength ?? 4096
  if (text.length > maxLength) {
    fail('INVALID_ARGUMENT', `payload.${name} is too long (maximum ${maxLength} characters)`)
  }
  return text
}

/**
 * @param {Record<string, unknown>} payload
 * @param {string} name
 * @returns {number|undefined}
 */
function optionalInt(payload, name, min, max) {
  const value = payload[name]
  if (value === undefined || value === null) return undefined
  const number = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : NaN
  if (!Number.isFinite(number) || !Number.isInteger(number)) {
    fail('INVALID_ARGUMENT', `payload.${name} must be an integer`)
  }
  if (number < min || number > max) {
    fail('INVALID_ARGUMENT', `payload.${name} must be between ${min} and ${max}`)
  }
  return number
}

/** Optional boolean payload field (accepts JSON booleans only). */
function optionalBool(payload, name) {
  const value = payload[name]
  if (value === undefined || value === null) return undefined
  if (typeof value === 'boolean') return value
  fail('INVALID_ARGUMENT', `payload.${name} must be a boolean`)
}

/** Validate a database file or directory path coming from the UI. */
function validateDatabasePath(value) {
  if (typeof value !== 'string') fail('INVALID_ARGUMENT', 'payload.path must be a string')
  const path = value.trim()
  if (path.length === 0) fail('INVALID_ARGUMENT', 'payload.path must not be empty')
  if (path.length > MAX_DB_PATH_LENGTH) {
    fail('INVALID_ARGUMENT', `payload.path is too long (maximum ${MAX_DB_PATH_LENGTH} characters)`)
  }
  if (/[\0\r\n]/.test(path)) {
    fail('INVALID_ARGUMENT', 'payload.path must not contain NUL or newline characters')
  }
  if (!path.startsWith('/')) {
    fail('INVALID_ARGUMENT', 'payload.path must be an absolute path (start with "/")')
  }
  if (path.split('/').includes('..')) {
    fail('INVALID_ARGUMENT', 'payload.path must not contain ".." segments')
  }
  return path
}

/**
 * Map backend failures onto the small database-switch vocabulary the UI knows:
 * INVALID_ARGUMENT (bad input/config), DB_NOT_FOUND (missing target), and
 * BACKEND_ERROR (anything else, original code kept in details).
 */
function databaseSwitchFailure(error) {
  if (!isBackendError(error)) return error
  const details = detailsOf(error.details)
  if (error.code === 'DB_NOT_FOUND') return new RpcError('DB_NOT_FOUND', error.message, details)
  if (error.code === 'INVALID_ARGUMENT' || error.code === 'CONFIG_INVALID') {
    return new RpcError('INVALID_ARGUMENT', error.message, details)
  }
  return new RpcError('BACKEND_ERROR', error.message, { ...details, backendCode: error.code })
}

/** Trim the CA registry snapshot to the fields the database picker needs. */
function registrySummary(data) {
  if (!isPlainObject(data)) return null
  const strings = (items) =>
    Array.isArray(items)
      ? items
          .filter((item) => isPlainObject(item))
          .slice(0, 100)
          .map((item) => jsonSafe(item) ?? {})
      : []
  const active = isPlainObject(data.active_variant) ? data.active_variant : null
  return {
    available:
      data.available === true ||
      data.active_variant_id != null ||
      (Array.isArray(data.variants) && data.variants.length > 0),
    variants: strings(data.variants),
    workspaces: strings(data.workspaces),
    projects: strings(data.projects),
    active_variant_id: data.active_variant_id ?? active?.id ?? null,
  }
}

/** Validate `payload.id` for task endpoints. */
function taskId(payload) {
  return optionalString(payload, 'id', { required: true, maxLength: MAX_ID_LENGTH })
}

function requirePlainObject(value, label, maxBytes) {
  if (!isPlainObject(value)) {
    fail('INVALID_ARGUMENT', `${label} must be a JSON object`)
  }
  if (jsonBytes(value) > maxBytes) {
    fail('PAYLOAD_TOO_LARGE', `${label} exceeds ${maxBytes} bytes`)
  }
  return value
}

// --------------------------------------------------------------------------- //
// Config / ssh helpers
// --------------------------------------------------------------------------- //

const SECRET_KEY = /(?:password|passphrase|secret|token|credential|private[_-]?key|api[_-]?key)/i

/**
 * Deep-copy settings for display, replacing credential-like values.
 * @param {unknown} value
 * @param {number} [depth]
 */
function publicConfig(value, depth = 0) {
  if (depth > 8) return undefined
  if (Array.isArray(value)) {
    return value.slice(0, 200).map((item) => publicConfig(item, depth + 1))
  }
  if (isPlainObject(value)) {
    const result = {}
    for (const [key, item] of Object.entries(value)) {
      if (item === undefined) continue
      result[key] = SECRET_KEY.test(key) ? '***' : publicConfig(item, depth + 1)
    }
    return result
  }
  if (typeof value === 'function' || typeof value === 'symbol') return undefined
  return value
}

/** @param {unknown} ssh */
function validateSsh(ssh) {
  if (typeof ssh === 'string') {
    const text = ssh.trim()
    if (text.length === 0) fail('INVALID_ARGUMENT', 'payload.ssh must not be empty')
    if (text.length > MAX_SSH_BYTES) fail('INVALID_ARGUMENT', 'payload.ssh is too long')
    return text
  }
  if (!isPlainObject(ssh)) {
    fail('INVALID_ARGUMENT', 'payload.ssh must be an SSH target object or "user@host" string')
  }
  if (jsonBytes(ssh) > MAX_SSH_BYTES) {
    fail('PAYLOAD_TOO_LARGE', `payload.ssh exceeds ${MAX_SSH_BYTES} bytes`)
  }
  const host = ssh.host ?? ssh.hostname
  if (typeof host !== 'string' || host.trim().length === 0) {
    fail('INVALID_ARGUMENT', 'payload.ssh.host is required')
  }
  for (const key of ['user', 'keyPath', 'identityFile', 'alias', 'installPath', 'repoPath']) {
    if (ssh[key] !== undefined && typeof ssh[key] !== 'string') {
      fail('INVALID_ARGUMENT', `payload.ssh.${key} must be a string`)
    }
  }
  if (ssh.port !== undefined) {
    const port = Number(ssh.port)
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      fail('INVALID_ARGUMENT', 'payload.ssh.port must be an integer between 1 and 65535')
    }
  }
  return jsonSafe(ssh)
}

/** Redact credential-like ssh fields for status display. */
function redactSsh(ssh) {
  if (typeof ssh === 'string') return ssh
  return publicConfig(ssh)
}

// --------------------------------------------------------------------------- //
// Backend adapter helpers
// --------------------------------------------------------------------------- //

/** Read an optional Cordis service without tripping an inject guard. */
function optionalService(ctx, name) {
  try {
    return ctx?.get?.(name)
  } catch {
    return undefined
  }
}

function backendMethod(backend, names) {
  for (const name of names) {
    const candidate = backend?.[name]
    if (typeof candidate === 'function') return candidate.bind(backend)
  }
  return null
}

function safeStatus(backend) {
  try {
    const status = typeof backend.getStatus === 'function' ? backend.getStatus() : null
    return jsonSafe(status) ?? { mode: 'unconfigured', state: 'stopped' }
  } catch (error) {
    return {
      mode: 'unconfigured',
      state: 'error',
      error: { code: 'STATUS_FAILED', message: errorMessage(error) },
    }
  }
}

function errorMessage(error) {
  if (!error || typeof error !== 'object') return String(error ?? 'unexpected error')
  const raw = typeof error.message === 'string' ? error.message : String(error)
  return raw.split(/\n\s+at\s/)[0].trim().slice(0, 1000)
}

/** Always expose failure details as a plain object (ConnectionRpcFailure). */
function detailsOf(value) {
  const safe = jsonSafe(value)
  return isPlainObject(safe) ? safe : {}
}

/** Normalize anything thrown into the RPC failure shape. */
function failure(error) {
  if (error instanceof RpcError) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: error.message,
        details: detailsOf(error.details),
      },
    }
  }
  if (isBackendError(error)) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: error.message,
        details: detailsOf(error.details),
      },
    }
  }
  if (isAbortError(error)) {
    return {
      ok: false,
      error: { code: 'CANCELLED', message: 'RPC request was cancelled', details: {} },
    }
  }
  // TaskError and other stable application errors carry uppercase codes.
  if (error && typeof error === 'object' && typeof error.code === 'string' && /^[A-Z][A-Z0-9_]*$/.test(error.code)) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: errorMessage(error),
        details: detailsOf(error.details),
      },
    }
  }
  const normalized = normalizeError(error, 'INTERNAL')
  return {
    ok: false,
    error: { code: 'INTERNAL', message: normalized.message, details: {} },
  }
}

// --------------------------------------------------------------------------- //
// Dispatcher
// --------------------------------------------------------------------------- //

/**
 * Build the endpoint dispatcher with its service references captured.
 * @param {{backend: any, tasks: any, explain?: any, config?: Record<string, any>, ctx?: any}} deps
 */
function makeDispatcher({ backend, tasks, explain, config = {}, ctx }) {
  /** @type {{startedAt: number, started: boolean, status: string, ssh?: unknown, error?: unknown, result?: unknown, finishedAt?: number} | null} */
  let installState = null

  async function currentModel() {
    const descriptors = [explain, optionalService(ctx, 'agentDefaultModel')]
    for (const service of descriptors) {
      if (!service) continue
      for (const name of ['currentModel', 'getModel', 'currentSelection']) {
        if (typeof service[name] !== 'function') continue
        try {
          let selection = service[name]()
          if (selection && typeof selection.then === 'function') selection = await selection
          const provider = selection?.provider ?? selection?.providerId
          const model = selection?.model ?? selection?.modelId
          if (typeof provider === 'string' && typeof model === 'string' && provider && model) {
            return { provider, model }
          }
        } catch {
          // Model info is advisory; never fail status because of it.
        }
      }
    }
    return null
  }

  function taskCounts(items) {
    const counts = { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 }
    for (const item of items) {
      if (counts[item?.status] !== undefined) counts[item.status] += 1
    }
    return counts
  }

  function serverValue(value) {
    const bytes = jsonBytes(value ?? null)
    if (!Number.isFinite(bytes) || bytes > MAX_RESPONSE_BYTES) {
      fail('RESPONSE_TOO_LARGE', `RPC response exceeds ${MAX_RESPONSE_BYTES} bytes; narrow the request`)
    }
    return { ok: true, value: value ?? null }
  }

  /** @type {Record<string, (payload: Record<string, unknown>, signal?: AbortSignal) => Promise<any>>} */
  const routes = {
    async ping() {
      return { pong: true, version: PLUGIN_VERSION }
    },

    async status() {
      let all = []
      try {
        all = typeof tasks.list === 'function' ? tasks.list() : []
      } catch {
        all = []
      }
      const items = Array.isArray(all) ? all : []
      return {
        backend: safeStatus(backend),
        tasks: items.slice(0, 50),
        model: await currentModel(),
        counts: taskCounts(items),
      }
    },

    async 'ca.request'(payload, signal) {
      const methodRaw = optionalString(payload, 'method', { maxLength: 16 })
      const method = (methodRaw ?? 'GET').toUpperCase()
      if (!['GET', 'HEAD', 'POST'].includes(method)) {
        fail('INVALID_ARGUMENT', 'payload.method must be GET, HEAD or POST')
      }
      const path = validateCaPath(optionalString(payload, 'path', { required: true, maxLength: MAX_PATH_LENGTH }))
      const query = validateQuery(payload.query)
      let body
      if (method === 'GET' || method === 'HEAD') {
        if (payload.body !== undefined && payload.body !== null) {
          const provided = isPlainObject(payload.body) ? Object.keys(payload.body).length > 0 : true
          if (provided) fail('INVALID_ARGUMENT', `${method} requests must not include a body`)
        }
      } else if (payload.body !== undefined && payload.body !== null) {
        if (!isPlainObject(payload.body) && !Array.isArray(payload.body)) {
          fail('INVALID_ARGUMENT', 'payload.body must be a JSON object or array')
        }
        if (jsonBytes(payload.body) > MAX_CA_BODY_BYTES) {
          fail('PAYLOAD_TOO_LARGE', `request body exceeds ${MAX_CA_BODY_BYTES} bytes`)
        }
        body = jsonSafe(payload.body)
      }
      const timeoutMs = optionalInt(payload, 'timeoutMs', 1000, 300000)
      if (typeof backend.request !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager does not support HTTP requests')
      }
      // CA returns its own envelope; pass it through unchanged so the client
      // can expose CA error codes exactly as the server reports them.
      const envelope = await backend.request(path, { method, query, body, timeoutMs, signal })
      if (!isPlainObject(envelope)) {
        fail('BACKEND_ERROR', 'CA backend returned an invalid response envelope')
      }
      return envelope
    },

    async 'backend.get'() {
      return {
        status: safeStatus(backend),
        config: publicConfig(backend.config ?? config) ?? {},
      }
    },

    async 'backend.configure'(payload) {
      const patch = requirePlainObject(payload.patch, 'payload.patch', MAX_CONFIG_PATCH_BYTES)
      if (Object.keys(patch).length === 0) {
        fail('INVALID_ARGUMENT', 'payload.patch must contain at least one setting')
      }
      const configure = backendMethod(backend, ['configure', 'updateConfig', 'applyConfig', 'setConfig'])
      if (configure === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not accept configuration over RPC; edit the settings file shown on the CA backend page.',
        )
      }
      const result = await configure(jsonSafe(patch))
      const safeResult = isPlainObject(result) ? jsonSafe(result) : {}
      return {
        ...safeResult,
        config: publicConfig(safeResult?.config ?? backend.config ?? config) ?? {},
        status: safeStatus(backend),
      }
    },

    async 'backend.start'() {
      if (typeof backend.start !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager cannot start a backend')
      }
      const result = await backend.start()
      return { status: safeStatus(backend), result: jsonSafe(result) ?? null }
    },

    async 'backend.stop'() {
      if (typeof backend.stop !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager cannot stop a backend')
      }
      await backend.stop()
      return { status: safeStatus(backend) }
    },

    async 'backend.restart'() {
      if (typeof backend.restart !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager cannot restart a backend')
      }
      const result = await backend.restart()
      return { status: safeStatus(backend), result: jsonSafe(result) ?? null }
    },

    async 'backend.probe'(payload) {
      const target = (optionalString(payload, 'target', { maxLength: 16 }) ?? 'local').toLowerCase()
      if (target === 'local') {
        const probeLocal = backendMethod(backend, ['probeLocal'])
        if (probeLocal === null) {
          fail('BACKEND_UNSUPPORTED', 'The CA backend manager does not support local probing')
        }
        return jsonSafe(await probeLocal()) ?? null
      }
      if (target !== 'remote') {
        fail('INVALID_ARGUMENT', 'payload.target must be "local" or "remote"')
      }
      const ssh = payload.ssh === undefined || payload.ssh === null ? undefined : validateSsh(payload.ssh)
      const probeRemote = backendMethod(backend, ['probeRemote', 'probe'])
      if (probeRemote === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'Remote probing is not available in this CA backend build; check the CA backend page for the remote setup state.',
        )
      }
      return jsonSafe(await probeRemote(ssh)) ?? null
    },

    async 'backend.install'(payload) {
      const opts = payload.opts === undefined || payload.opts === null
        ? {}
        : requirePlainObject(payload.opts, 'payload.opts', MAX_OPTIONS_BYTES)
      const ssh = payload.ssh === undefined || payload.ssh === null
        ? opts.ssh === undefined || opts.ssh === null
          ? undefined
          : validateSsh(opts.ssh)
        : validateSsh(payload.ssh)
      // backend-ops exposes both a foreground installer and a background
      // starter; prefer the latter so the unary RPC returns immediately.
      const startInstall = backendMethod(backend, ['startRemoteInstall'])
      const options = { ...jsonSafe(opts) }
      if (ssh !== undefined) options.ssh = ssh
      const noteInstallStart = (result) => {
        installState = {
          started: result?.started !== false,
          status: result?.running === true ? 'running' : (result?.status ?? (result?.started === false ? 'running' : 'running')),
          startedAt: result?.startedAt ?? Date.now(),
          ssh: ssh === undefined ? undefined : redactSsh(ssh),
          error: null,
          result: null,
        }
        return {
          ...(isPlainObject(result) ? jsonSafe(result) : {}),
          started: result?.started !== false,
          status: installState.status,
          hint: 'Poll backend.installStatus for progress and logs.',
        }
      }
      if (startInstall !== null) {
        return noteInstallStart(await startInstall(options))
      }
      const install = backendMethod(backend, ['installRemote', 'install', 'remoteInstall'])
      if (install === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'Remote installation is not available in this CA backend build; check the CA backend page for the remote setup state.',
        )
      }
      const result = install(options)
      if (result && typeof result.then === 'function') {
        installState = {
          started: true,
          status: 'running',
          startedAt: Date.now(),
          ssh: ssh === undefined ? undefined : redactSsh(ssh),
          error: null,
          result: null,
        }
        result.then(
          (finalResult) => {
            if (!installState || installState.status !== 'running') return
            installState.status = 'succeeded'
            installState.finishedAt = Date.now()
            installState.result = jsonSafe(finalResult) ?? null
          },
          (error) => {
            if (!installState || installState.status !== 'running') return
            installState.status = 'failed'
            installState.finishedAt = Date.now()
            const payload = failure(error)
            installState.error = payload.error
          },
        )
        return {
          started: true,
          status: 'running',
          hint: 'Poll backend.installStatus for progress.',
        }
      }
      return noteInstallStart(result)
    },

    async 'backend.installStatus'() {
      const readStatus = backendMethod(backend, ['getInstallStatus', 'installStatus'])
      if (readStatus !== null) {
        const status = await readStatus()
        const safe = jsonSafe(status)
        // The backend reports `state`; the UI polling contract uses `status`.
        if (isPlainObject(safe) && safe.status === undefined && typeof safe.state === 'string') {
          safe.status = safe.state
        }
        return safe ?? { started: false, status: 'idle', steps: [], logs: [] }
      }
      if (installState !== null) return jsonSafe(installState) ?? { started: false, status: 'idle' }
      return { started: false, status: 'idle', steps: [], logs: [] }
    },

    async 'backend.databases'(payload) {
      const refresh = optionalBool(payload, 'refresh')
      const listDatabases = backendMethod(backend, ['listDatabases'])
      if (listDatabases === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not support database discovery yet; start it from the CA backend page instead.',
        )
      }
      let report
      try {
        report = await listDatabases(refresh === true ? { refresh: true } : undefined)
      } catch (error) {
        throw databaseSwitchFailure(error)
      }
      if (!isPlainObject(report)) {
        fail('BACKEND_ERROR', 'The CA backend returned an invalid database discovery report')
      }
      const safe = jsonSafe(report) ?? {}
      const status = safeStatus(backend)
      const current = isPlainObject(safe.current)
        ? safe.current
        : { db: null, dbDir: null }
      // Best effort: merge the CA registry (variants/workspaces) only while the
      // backend answers; a registry failure must never fail database discovery.
      let registry = null
      if (status.state === 'running' && typeof backend.request === 'function') {
        try {
          const envelope = await backend.request('/api/registry', { method: 'GET', timeoutMs: 5000 })
          if (isPlainObject(envelope) && envelope.ok === true) {
            registry = registrySummary(envelope.data)
          }
        } catch {
          registry = null
        }
      }
      const candidates = Array.isArray(safe.candidates) ? safe.candidates.slice(0, 500) : []
      // `mode/current/candidates/registry` are the picker contract; the scan
      // metadata below is additive so the settings page can show totals and
      // truncation without another RPC.
      const value = {
        ok: safe.ok !== false,
        mode: typeof safe.mode === 'string' ? safe.mode : (status.mode ?? 'unconfigured'),
        current,
        configured: isPlainObject(safe.configured) ? safe.configured : null,
        candidates,
        total: Number.isFinite(Number(safe.total)) ? Number(safe.total) : candidates.length,
        truncated: safe.truncated === true,
        roots: Array.isArray(safe.roots) ? safe.roots.slice(0, 20).map((root) => String(root)) : [],
        registry,
      }
      if (Number.isFinite(Number(safe.scannedAt))) value.scannedAt = Number(safe.scannedAt)
      if (safe.error !== undefined) value.error = jsonSafe(safe.error) ?? null
      return value
    },

    async 'backend.useDatabase'(payload) {
      const kindRaw = optionalString(payload, 'kind', { required: true, maxLength: 16 })
      const normalized = kindRaw.toLowerCase()
      const kind =
        normalized === 'db'
          ? 'db'
          : normalized === 'dbdir' || normalized === 'db_dir'
            ? 'dbDir'
            : null
      if (kind === null) {
        fail('INVALID_ARGUMENT', 'payload.kind must be "db" or "dbDir"')
      }
      const target = validateDatabasePath(payload.path)
      const useDatabase = backendMethod(backend, ['useDatabase'])
      if (useDatabase === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not support switching databases yet; configure the database on the CA backend page instead.',
        )
      }
      let result
      try {
        result = await useDatabase({ kind, path: target })
      } catch (error) {
        throw databaseSwitchFailure(error)
      }
      const safe = isPlainObject(result) ? jsonSafe(result) : {}
      return {
        ...safe,
        config: publicConfig(safe?.config ?? backend.config ?? config) ?? {},
        status: safeStatus(backend),
      }
    },

    async 'tasks.start'(payload) {
      if (typeof tasks.start !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const kind = optionalString(payload, 'kind', { required: true, maxLength: 64 })
      let params = payload.params
      if (params === undefined || params === null) params = {}
      if (!isPlainObject(params)) fail('INVALID_ARGUMENT', 'payload.params must be a JSON object')
      if (jsonBytes(params) > MAX_PAYLOAD_BYTES) {
        fail('PAYLOAD_TOO_LARGE', `payload.params exceeds ${MAX_PAYLOAD_BYTES} bytes`)
      }
      const task = tasks.start(kind, params)
      return { task: jsonSafe(task) ?? null }
    },

    async 'tasks.list'() {
      if (typeof tasks.list !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const items = tasks.list()
      const list = Array.isArray(items) ? items.slice(0, 500) : []
      return {
        tasks: jsonSafe(list) ?? [],
        counts: typeof tasks.counts === 'function' ? tasks.counts() : taskCounts(list),
      }
    },

    async 'tasks.get'(payload) {
      const id = taskId(payload)
      const task = typeof tasks.get === 'function' ? tasks.get(id) : null
      if (!task) fail('TASK_NOT_FOUND', `no CA task with id "${id}"`, { id })
      return { task: jsonSafe(task) ?? null }
    },

    async 'tasks.logs'(payload) {
      const id = taskId(payload)
      const offset = optionalInt(payload, 'offset', 0, Number.MAX_SAFE_INTEGER) ?? 0
      const limit = optionalInt(payload, 'limit', 1, 2000) ?? 200
      if (typeof tasks.logs !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const result = tasks.logs(id, { offset, limit })
      return jsonSafe(result) ?? null
    },

    async 'tasks.cancel'(payload) {
      const id = taskId(payload)
      if (typeof tasks.cancel !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const task = tasks.cancel(id)
      return { task: jsonSafe(task) ?? null }
    },

    async explain(payload, signal) {
      const symbol = optionalString(payload, 'symbol', { required: true, maxLength: MAX_SYMBOL_LENGTH })
      const language = (optionalString(payload, 'language', { maxLength: 8 }) ?? 'en').toLowerCase()
      if (language !== 'en' && language !== 'zh') {
        fail('INVALID_ARGUMENT', 'payload.language must be "en" or "zh"')
      }
      const question = optionalString(payload, 'question', { maxLength: MAX_QUESTION_LENGTH })
      if (!explain || typeof explain.explain !== 'function') {
        fail(
          'EXPLAIN_UNAVAILABLE',
          'AI explanation is unavailable in this DSH session (no model selection service). Use ca_symbol/ca_facts and explain manually.',
        )
      }
      const result = await explain.explain({ symbol, language, question, signal })
      if (!isPlainObject(result) || typeof result.text !== 'string' || result.text.trim().length === 0) {
        fail('EXPLAIN_FAILED', 'The explain service returned no explanation text')
      }
      const safe = jsonSafe(result) ?? {}
      return {
        symbol: safe.symbol ?? symbol,
        text: safe.text,
        model: safe.model ?? null,
        evidenceCount: Number.isFinite(Number(safe.evidenceCount)) ? Number(safe.evidenceCount) : 0,
        ...(safe.usage !== undefined ? { usage: safe.usage } : {}),
      }
    },
  }

  const handler = async (endpoint, payload, signal) => {
    try {
      if (typeof endpoint !== 'string' || endpoint.trim().length === 0) {
        fail('UNKNOWN_ENDPOINT', 'RPC endpoint name is required')
      }
      const route = routes[endpoint]
      if (!route) {
        fail(
          'UNKNOWN_ENDPOINT',
          `unknown CA plugin endpoint "${endpoint}"; expected one of: ${RPC_ENDPOINTS.join(', ')}`,
          { endpoints: [...RPC_ENDPOINTS] },
        )
      }
      const safePayload = validatePayload(payload)
      const value = await route(safePayload, signal)
      return serverValue(value)
    } catch (error) {
      return failure(error)
    }
  }
  return handler
}

/** Enforce the read-only `/api/` allow-list for CA passthrough requests. */
function validateCaPath(path) {
  if (typeof path !== 'string') fail('INVALID_ARGUMENT', 'payload.path is required')
  if (!path.startsWith('/api/')) {
    fail('FORBIDDEN_PATH', 'CA API path must start with "/api/"', { path })
  }
  if (path.length > MAX_PATH_LENGTH) {
    fail('INVALID_ARGUMENT', `payload.path exceeds ${MAX_PATH_LENGTH} characters`)
  }
  if (/[\\\s\0]/.test(path) || path.includes('://') || path.includes('..') || path.includes('//')) {
    fail('FORBIDDEN_PATH', 'CA API path contains a forbidden character or segment', { path })
  }
  if (path.includes('?') || path.includes('#')) {
    fail('INVALID_ARGUMENT', 'pass query parameters in payload.query, not inside payload.path')
  }
  return path
}

/** Normalize/validate query params for the CA passthrough endpoint. */
function validateQuery(query) {
  if (query === undefined || query === null) return undefined
  if (!isPlainObject(query)) fail('INVALID_ARGUMENT', 'payload.query must be a JSON object')
  const entries = Object.entries(query)
  if (entries.length > MAX_QUERY_ENTRIES) {
    fail('INVALID_ARGUMENT', `payload.query exceeds ${MAX_QUERY_ENTRIES} entries`)
  }
  const result = {}
  for (const [key, value] of entries) {
    if (value === undefined || value === null) continue
    if (typeof value === 'object') {
      fail('INVALID_ARGUMENT', `payload.query.${key} must be a string, number or boolean`)
    }
    const text = String(value)
    if (text.length > MAX_QUERY_VALUE_LENGTH) {
      fail('INVALID_ARGUMENT', `payload.query.${key} is too long`)
    }
    if (key.length === 0 || key.length > 128) {
      fail('INVALID_ARGUMENT', 'payload.query keys must be 1-128 characters')
    }
    result[key] = text
  }
  return result
}

/**
 * Wrap one registration in the owning Cordis context so it is disposed with
 * the plugin. Returns the exact disposer (plain-call fallback for tests).
 *
 * @param {any} ctx
 * @param {string} label
 * @param {() => (() => void) | void} install
 */
function registerEffect(ctx, label, install) {
  if (ctx && typeof ctx.effect === 'function') {
    return ctx.effect(() => {
      const dispose = install()
      return typeof dispose === 'function' ? dispose : () => {}
    }, label)
  }
  const dispose = install()
  return dispose ?? (() => {})
}

// --------------------------------------------------------------------------- //
// HTTP transport (DSH-owned same-origin web route)
// --------------------------------------------------------------------------- //

/** HTTP-mapped error used by the transport layer. */
function httpError(code, message, status) {
  const error = new Error(message)
  error.code = code
  error.status = status
  return error
}

/** Business outcomes stay 200; only protocol/argument failures get statuses. */
function statusForEnvelope(envelope) {
  if (!envelope || envelope.ok !== false) return 200
  const code = envelope.error?.code
  if (code === 'PAYLOAD_TOO_LARGE') return 413
  if (code === 'UNKNOWN_ENDPOINT') return 404
  if (code === 'INVALID_ARGUMENT' || code === 'FORBIDDEN_PATH') return 400
  if (code === 'INTERNAL' || code === 'RESPONSE_TOO_LARGE') return 500
  return 200
}

function errorEnvelope(code, message, details = {}) {
  return { ok: false, error: { code, message, details } }
}

/** JSON.stringify that never throws; BigInt becomes a decimal string. */
function safeStringify(value) {
  try {
    return JSON.stringify(value, (_key, item) => (typeof item === 'bigint' ? item.toString() : item))
  } catch {
    return JSON.stringify(
      errorEnvelope('INTERNAL', 'CA plugin failed to serialize the response'),
    )
  }
}

function writeJson(res, status, envelope, extraHeaders = {}) {
  if (!res || typeof res.end !== 'function') return
  if (res.headersSent) {
    try {
      res.end()
    } catch {
      // Response already gone.
    }
    return
  }
  const body = safeStringify(
    envelope ?? errorEnvelope('INTERNAL', 'CA plugin produced an empty response'),
  )
  try {
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': String(Buffer.byteLength(body, 'utf8')),
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
      ...extraHeaders,
    })
    res.end(body)
  } catch {
    try {
      res.end()
    } catch {
      // Client already disconnected; nothing to answer.
    }
  }
}

/** Read the request body, enforcing the contract's 1 MiB limit. */
function readRequestBody(req, limit = MAX_HTTP_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    let settled = false
    const chunks = []
    let size = 0
    const cleanup = () => {
      req.removeListener?.('data', onData)
      req.removeListener?.('end', onEnd)
      req.removeListener?.('error', onError)
      req.removeListener?.('aborted', onAborted)
    }
    const settle = (callback, value) => {
      if (settled) return
      settled = true
      cleanup()
      callback(value)
    }
    // Reject as soon as the declared budget is exceeded, but keep draining a
    // moderately oversized body so the client still receives the 413 response
    // instead of a socket error. Pathological uploads are cut off.
    const DRAIN_LIMIT = 16 * 1024 * 1024
    let oversized = false
    let drained = 0
    const onData = (chunk) => {
      if (settled) return
      const buffer = typeof chunk === 'string' ? Buffer.from(chunk) : chunk
      const length = buffer?.length ?? 0
      size += length
      if (oversized) {
        drained += length
        if (drained > DRAIN_LIMIT) {
          settle(reject, httpError('BODY_TOO_LARGE', `request body exceeds ${limit} bytes`, 413))
          try {
            req.destroy?.()
          } catch {
            // Already closed.
          }
        }
        return
      }
      if (size > limit) {
        oversized = true
        chunks.length = 0
        return
      }
      chunks.push(buffer)
    }
    const onEnd = () => {
      if (oversized) {
        settle(reject, httpError('BODY_TOO_LARGE', `request body exceeds ${limit} bytes`, 413))
        return
      }
      settle(resolve, Buffer.concat(chunks).toString('utf8'))
    }
    const onError = (error) =>
      settle(
        reject,
        httpError('BODY_READ_FAILED', `cannot read request body: ${errorMessage(error)}`, 400),
      )
    const onAborted = () =>
      settle(reject, httpError('BODY_ABORTED', 'request was aborted before the body was read', 400))
    req.on('data', onData)
    req.on('end', onEnd)
    req.on('error', onError)
    req.on('aborted', onAborted)
  })
}

/** Extract `<endpoint>` from `/dsh-ca/<endpoint>`; null when outside/ambiguous. */
function endpointFromRequest(req) {
  const rawUrl = typeof req?.url === 'string' ? req.url : '/'
  const queryIndex = rawUrl.indexOf('?')
  const pathname = queryIndex >= 0 ? rawUrl.slice(0, queryIndex) : rawUrl
  if (pathname !== CHANNEL && !pathname.startsWith(`${CHANNEL}/`)) return null
  const tail = pathname === CHANNEL ? '' : pathname.slice(CHANNEL.length + 1)
  if (tail.length === 0 || tail.includes('/')) return null
  try {
    return decodeURIComponent(tail)
  } catch {
    return null
  }
}

/** Apply DSH's Host/Origin + browser authentication check. */
function rejectionStatus(connection, req) {
  try {
    const rejection = connection.requestRejection(req)
    if (rejection === 401) return 401
    // Any other truthy rejection is a refusal; unknown values fail closed.
    return rejection ? 403 : 0
  } catch {
    // A throwing trust check must fail closed.
    return 403
  }
}

/**
 * Build the `(req,res)` handler registered on the DSH web server.
 * Owns the full response lifecycle and never throws.
 *
 * @param {any} connection DSH connection service (`requestRejection`)
 * @param {(endpoint: string, payload: unknown, signal: AbortSignal) => Promise<any>} dispatch
 */
function makeHttpHandler(connection, dispatch) {
  return async (req, res) => {
    try {
      const rejection = rejectionStatus(connection, req)
      if (rejection !== 0) {
        writeJson(
          res,
          rejection,
          errorEnvelope(
            rejection === 401 ? 'UNAUTHORIZED' : 'FORBIDDEN',
            rejection === 401
              ? 'authentication is required for the CA plugin channel'
              : 'the DSH connection trust policy rejected this request',
          ),
        )
        return
      }
      if (req?.method !== 'POST') {
        writeJson(res, 405, errorEnvelope('METHOD_NOT_ALLOWED', 'the CA plugin channel only accepts POST'), {
          allow: 'POST',
        })
        return
      }
      const endpoint = endpointFromRequest(req)
      if (!endpoint) {
        writeJson(
          res,
          404,
          errorEnvelope('NOT_FOUND', `no CA plugin endpoint at ${req?.url ?? ''}`),
        )
        return
      }
      if (!RPC_ENDPOINTS.includes(endpoint)) {
        writeJson(
          res,
          404,
          errorEnvelope(
            'UNKNOWN_ENDPOINT',
            `unknown CA plugin endpoint "${endpoint}"; expected one of: ${RPC_ENDPOINTS.join(', ')}`,
            { endpoints: [...RPC_ENDPOINTS] },
          ),
        )
        return
      }
      let rawBody
      try {
        rawBody = await readRequestBody(req)
      } catch (error) {
        writeJson(res, error?.status ?? 400, errorEnvelope(error?.code ?? 'BAD_REQUEST', errorMessage(error)))
        return
      }
      let payload
      if (rawBody.length === 0) {
        payload = {}
      } else {
        try {
          payload = JSON.parse(rawBody)
        } catch {
          writeJson(res, 400, errorEnvelope('INVALID_JSON', 'request body must be valid JSON'))
          return
        }
      }
      const controller = new AbortController()
      const onClose = () => controller.abort()
      if (typeof res.on === 'function') res.on('close', onClose)
      let envelope
      try {
        envelope = await dispatch(endpoint, payload, controller.signal)
      } finally {
        if (typeof res.off === 'function') res.off('close', onClose)
      }
      writeJson(res, statusForEnvelope(envelope), envelope)
    } catch {
      // The transport must never surface an unhandled rejection.
      writeJson(res, 500, errorEnvelope('INTERNAL', 'CA plugin internal error'))
    }
  }
}

/**
 * Register the `/dsh-ca` HTTP channel on the DSH web server.
 *
 * @param {any} ctx Cordis plugin context (hard deps: webServer, connection)
 * @param {{backend: any, tasks: any, explain?: any, config?: Record<string, any>}} deps
 * @returns {() => void} disposer
 */
export function registerRpc(ctx, deps = {}) {
  const { backend, tasks } = deps
  if (!backend || typeof backend.getStatus !== 'function') {
    throw new Error('dsh-ca: registerRpc requires the backend manager (backend-ops)')
  }
  if (!tasks || typeof tasks.list !== 'function') {
    throw new Error('dsh-ca: registerRpc requires the task manager (host-tools)')
  }
  const webServer = ctx?.webServer ?? ctx?.get?.('webServer')
  if (!webServer || typeof webServer.register !== 'function') {
    throw new Error('dsh-ca: registerRpc requires the DSH webServer service (inject "webServer")')
  }
  const connection = ctx?.connection ?? ctx?.get?.('connection')
  if (!connection || typeof connection.requestRejection !== 'function') {
    throw new Error(
      'dsh-ca: registerRpc requires the DSH connection service with requestRejection (inject "connection")',
    )
  }
  const dispatch = makeDispatcher({ ...deps, ctx })
  return registerEffect(ctx, 'dsh-ca: http channel', () => {
    const off = webServer.register({
      kind: 'prefix',
      path: CHANNEL,
      handler: makeHttpHandler(connection, dispatch),
    })
    return () => {
      try {
        const result = typeof off === 'function' ? off() : undefined
        if (result && typeof result.catch === 'function') result.catch(() => {})
      } catch {
        // The route is already gone; nothing left to dispose.
      }
    }
  })
}

/** @param {unknown} value */
export function isPlainRpcObject(value) {
  return isPlainObject(value)
}

/** Expose the payload limits for tests/documentation. */
export const RPC_LIMITS = Object.freeze({
  httpBodyBytes: MAX_HTTP_BODY_BYTES,
  payloadBytes: MAX_PAYLOAD_BYTES,
  caBodyBytes: MAX_CA_BODY_BYTES,
  configPatchBytes: MAX_CONFIG_PATCH_BYTES,
  responseBytes: MAX_RESPONSE_BYTES,
  taskLogLimit: clampInt(2000, 1, 2000, 200),
})
