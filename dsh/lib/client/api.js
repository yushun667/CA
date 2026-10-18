/**
 * CA DSH client → Host API.
 *
 * The Client never talks to the CA HTTP server directly. Every call goes
 * through the same-origin DSH web route registered by the Host half:
 *
 *   POST /dsh-ca/<endpoint>  (JSON envelope in the response body)
 *     -> { ok: true, value } | { ok: false, error: { code, message, details } }
 *
 * The page is already authenticated by the DSH webserver (signed cookie); the
 * Host route rejects foreign origins, so `credentials: 'same-origin'` is enough.
 *
 * `caRequest` forwards to the Host endpoint `ca.request`, which proxies to the
 * CA `/api/*` HTTP API and returns the CA envelope `{ ok, data | error }`.
 * Both envelopes are unwrapped here: successful calls resolve to the payload,
 * failures throw {@link ApiError}, and transport failures (non-2xx HTTP,
 * network error, abort) are normalized to `TRANSPORT`/`ABORTED` too.
 *
 * All functions accept an optional `AbortSignal`; callers should abort on
 * unmount or route change instead of letting stale responses land.
 */

/** Same-origin web route prefix registered by the Host half. */
export const RPC_CHANNEL = '/dsh-ca'

/** Host endpoint names (keep in sync with dsh/lib/host/rpc/index.js). */
export const Endpoints = Object.freeze({
  ping: 'ping',
  status: 'status',
  caRequest: 'ca.request',
  backendGet: 'backend.get',
  backendConfigure: 'backend.configure',
  backendStart: 'backend.start',
  backendStop: 'backend.stop',
  backendRestart: 'backend.restart',
  backendProbe: 'backend.probe',
  backendInstall: 'backend.install',
  backendInstallStatus: 'backend.installStatus',
  tasksStart: 'tasks.start',
  tasksList: 'tasks.list',
  tasksGet: 'tasks.get',
  tasksLogs: 'tasks.logs',
  tasksCancel: 'tasks.cancel',
  explain: 'explain',
})

const CA_METHODS = new Set(['GET', 'HEAD', 'POST'])

/** Error raised for every CA/RPC failure. */
export class ApiError extends Error {
  /**
   * @param {string} code stable error code (e.g. TRANSPORT, NOT_FOUND)
   * @param {string} message human-readable message
   * @param {object} [details] structured details
   * @param {{ cause?: unknown, transport?: boolean }} [options]
   */
  constructor(code, message, details, options) {
    super(message || code || 'ApiError')
    this.name = 'ApiError'
    this.code = code || 'INTERNAL'
    this.details = details && typeof details === 'object' ? details : {}
    if (options && Object.prototype.hasOwnProperty.call(options, 'cause')) this.cause = options.cause
    this.transport = Boolean(options && options.transport)
  }

  toJSON() {
    return { name: this.name, code: this.code, message: this.message, details: this.details }
  }
}

/** True when `error` is an {@link ApiError}. */
export function isApiError(error) {
  return Boolean(error) && error.name === 'ApiError' && typeof error.code === 'string'
}

/** Normalize any thrown value into an ApiError. */
export function toApiError(error, fallbackCode) {
  if (isApiError(error)) return error
  if (error && typeof error === 'object') {
    if (error.name === 'AbortError') {
      return new ApiError('ABORTED', 'The request was cancelled.', {}, { cause: error })
    }
    const code = typeof error.code === 'string' && error.code ? error.code : fallbackCode || 'INTERNAL'
    const message = typeof error.message === 'string' && error.message ? error.message : String(error)
    const details = error.details && typeof error.details === 'object' ? error.details : {}
    return new ApiError(code, message, details, { cause: error, transport: true })
  }
  return new ApiError(fallbackCode || 'INTERNAL', String(error), {}, { cause: error })
}

/** Map a CA/Host error object to an ApiError. */
function envelopeError(error, fallbackCode, fallbackMessage) {
  if (error && typeof error === 'object') {
    const code = typeof error.code === 'string' && error.code ? error.code : fallbackCode
    const message =
      typeof error.message === 'string' && error.message
        ? error.message
        : fallbackMessage || (code ? String(code) : 'CA request failed')
    const details = error.details && typeof error.details === 'object' ? error.details : {}
    return new ApiError(code || 'INTERNAL', message, details)
  }
  return new ApiError(fallbackCode || 'INTERNAL', fallbackMessage || 'CA request failed')
}

function abortError(signal) {
  const error = new ApiError('ABORTED', 'The request was cancelled.', {})
  if (signal && signal.reason !== undefined) error.cause = signal.reason
  return error
}

function assertSignal(signal) {
  if (signal && signal.aborted) throw abortError(signal)
}

function buildPath(path, query) {
  let target = String(path || '')
  if (!target.startsWith('/')) target = `/${target}`
  if (!query || typeof query !== 'object') return target
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue
    if (Array.isArray(value)) {
      for (const item of value) if (item !== undefined && item !== null) params.append(key, String(item))
    } else {
      params.append(key, String(value))
    }
  }
  const suffix = params.toString()
  if (!suffix) return target
  return target.includes('?') ? `${target}&${suffix}` : `${target}?${suffix}`
}

// --------------------------------------------------------------------------
// formatting helpers shared by shell/pages
// --------------------------------------------------------------------------

/** Format a byte count using binary units. */
export function formatBytes(value) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = bytes / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`
}

/** Format a millisecond duration compactly (e.g. 1.2s, 3m 4s). */
export function formatDuration(ms) {
  const value = Number(ms)
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value < 1000) return `${Math.round(value)}ms`
  const seconds = value / 1000
  if (seconds < 60) return `${seconds >= 10 ? seconds.toFixed(1) : seconds.toFixed(2)}s`
  const whole = Math.round(seconds)
  const minutes = Math.floor(whole / 60)
  if (minutes < 60) return `${minutes}m ${whole % 60}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

/** Format an epoch-millisecond timestamp as a local time string. */
export function formatTimestamp(value) {
  const time = Number(value)
  if (!Number.isFinite(time) || time <= 0) return '—'
  try {
    return new Date(time).toLocaleString()
  } catch (_error) {
    return new Date(time).toISOString()
  }
}

/** Format an elapsed interval between two epoch-millisecond timestamps. */
export function elapsedBetween(start, end) {
  const from = Number(start)
  const to = Number(end)
  if (!Number.isFinite(from) || from <= 0) return '—'
  const until = Number.isFinite(to) && to > 0 ? to : Date.now()
  return formatDuration(Math.max(0, until - from))
}

/** One-line error text for banners/status rows. */
export function shortError(error) {
  const apiError = toApiError(error)
  return apiError.code ? `${apiError.message} (${apiError.code})` : apiError.message
}

// --------------------------------------------------------------------------
// API factory
// --------------------------------------------------------------------------

/**
 * Create the CA API facade.
 *
 * @param {object} ctx DSH Client plugin context.
 * @returns {object} API object (see CONTRACT.md "Client").
 */
export function createApi(ctx) {
  void ctx
  const channel = RPC_CHANNEL

  function endpointUrl(endpoint) {
    return `${channel}/${encodeURIComponent(endpoint)}`
  }

  function transportError(endpoint, status, detail) {
    const suffix = status ? `HTTP ${status}` : 'network error'
    const message = `CA plugin host request failed (${suffix})${
      detail ? `: ${detail}` : ''
    }`
    return new ApiError('TRANSPORT', message, { endpoint, status: status || 0 }, { transport: true })
  }

  /**
   * Call one Host endpoint and return its raw `{ ok, value | error }` JSON
   * envelope. Transport failures (network, non-2xx, malformed body) throw.
   */
  async function request(endpoint, payload, signal) {
    if (typeof endpoint !== 'string' || endpoint.length === 0) {
      throw new ApiError('INVALID_ARGUMENT', 'RPC endpoint must be a non-empty string.')
    }
    assertSignal(signal)
    if (typeof fetch !== 'function') {
      throw new ApiError('TRANSPORT', 'fetch() is not available in this environment.', {}, { transport: true })
    }
    let response
    try {
      response = await fetch(endpointUrl(endpoint), {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify(payload === undefined ? {} : payload),
        credentials: 'same-origin',
        signal,
      })
    } catch (error) {
      if (signal && signal.aborted) throw abortError(signal)
      throw toApiError(error, 'TRANSPORT')
    }
    assertSignal(signal)
    let text = ''
    try {
      text = typeof response.text === 'function' ? await response.text() : ''
    } catch (error) {
      if (signal && signal.aborted) throw abortError(signal)
      throw transportError(endpoint, response && response.status, error && error.message)
    }
    if (!response.ok) {
      // 401/403/404 may answer with text/plain from the webserver; keep the
      // status in the message so the page can show something actionable.
      const trimmed = String(text || '').trim().slice(0, 300)
      throw transportError(endpoint, response.status, trimmed || undefined)
    }
    let envelope = null
    if (text) {
      try {
        envelope = JSON.parse(text)
      } catch (_error) {
        throw new ApiError(
          'INVALID_RESPONSE',
          `CA plugin host returned a non-JSON response for ${endpoint}.`,
          { endpoint, body: String(text).slice(0, 300) },
        )
      }
    }
    if (!envelope || typeof envelope !== 'object') {
      throw new ApiError('INVALID_RESPONSE', `CA plugin host returned an empty response for ${endpoint}.`, {
        endpoint,
      })
    }
    return envelope
  }

  /** Call one Host endpoint; resolve with `value`, throw on `{ok:false}`. */
  async function rpc(endpoint, payload, signal) {
    const envelope = await request(endpoint, payload, signal)
    if (envelope.ok === true) return envelope.value
    throw envelopeError(envelope.error, 'RPC_ERROR', `RPC ${endpoint} failed`)
  }

  /**
   * Forward to the CA `/api/*` HTTP API through the Host.
   * @param {{method?: string, path: string, query?: object, body?: object, signal?: AbortSignal}} options
   * @returns {Promise<any>} the CA envelope `data` payload
   */
  async function caRequest(options) {
    const settings = options && typeof options === 'object' ? options : {}
    const method = String(settings.method || 'GET').toUpperCase()
    const path = String(settings.path || '')
    if (!CA_METHODS.has(method)) {
      throw new ApiError('INVALID_ARGUMENT', `Unsupported CA request method: ${method}`)
    }
    if (!path.startsWith('/api/')) {
      throw new ApiError('FORBIDDEN_PATH', `CA request path must start with /api/: ${path}`, { path })
    }
    const envelope = await request(
      Endpoints.caRequest,
      {
        method,
        path,
        query: settings.query || undefined,
        body: settings.body === undefined ? undefined : settings.body,
        timeoutMs: settings.timeoutMs,
      },
      settings.signal,
    )
    if (envelope.ok !== true) {
      throw envelopeError(envelope.error, 'CA_RPC_ERROR', `CA request ${method} ${path} failed`)
    }
    const value = envelope.value
    if (value && typeof value === 'object' && value.ok === true && 'data' in value) {
      return value.data
    }
    if (value && typeof value === 'object' && value.ok === false && value.error) {
      throw envelopeError(value.error, 'CA_ERROR', `CA request ${method} ${path} failed`)
    }
    // Host passed the payload through without the CA envelope; accept it.
    return value
  }

  /** GET a CA `/api/*` endpoint. */
  function caGet(path, options) {
    const settings = options && typeof options === 'object' ? options : {}
    return caRequest({ method: 'GET', path, query: settings.query, signal: settings.signal, timeoutMs: settings.timeoutMs })
  }

  /** POST a CA `/api/*` endpoint with a JSON body. */
  function caPost(path, body, options) {
    const settings = options && typeof options === 'object' ? options : {}
    return caRequest({ method: 'POST', path, body, signal: settings.signal, timeoutMs: settings.timeoutMs })
  }

  // -- convenience wrappers ------------------------------------------------- //

  const status = (signal) => rpc(Endpoints.status, {}, signal)
  const ping = (signal) => rpc(Endpoints.ping, {}, signal)

  const backend = {
    get: (signal) => rpc(Endpoints.backendGet, {}, signal),
    configure: (patch, signal) => rpc(Endpoints.backendConfigure, { patch: patch || {} }, signal),
    start: (payload, signal) => rpc(Endpoints.backendStart, payload || {}, signal),
    stop: (payload, signal) => rpc(Endpoints.backendStop, payload || {}, signal),
    restart: (payload, signal) => rpc(Endpoints.backendRestart, payload || {}, signal),
    probe: (payload, signal) => rpc(Endpoints.backendProbe, payload || { target: 'local' }, signal),
    install: (payload, signal) => rpc(Endpoints.backendInstall, payload || {}, signal),
    installStatus: (signal) => rpc(Endpoints.backendInstallStatus, {}, signal),
  }

  const tasks = {
    start: (kind, params, signal) =>
      rpc(Endpoints.tasksStart, { kind, params: params || {} }, signal),
    list: (signal) => rpc(Endpoints.tasksList, {}, signal),
    get: (id, signal) => rpc(Endpoints.tasksGet, { id }, signal),
    logs: (id, options, signal) => {
      const settings = options && typeof options === 'object' ? options : {}
      return rpc(
        Endpoints.tasksLogs,
        {
          id,
          offset: Number.isInteger(settings.offset) ? settings.offset : 0,
          limit: Number.isInteger(settings.limit) ? settings.limit : 200,
        },
        signal || settings.signal,
      )
    },
    cancel: (id, signal) => rpc(Endpoints.tasksCancel, { id }, signal),
  }

  const explain = (params, signal) => rpc(Endpoints.explain, params || {}, signal)

  // -- active variant store -------------------------------------------------- //
  // The shell's top-bar selector writes here; pages subscribe to refresh after
  // a variant switch (POST /api/registry/active).
  const variantListeners = new Set()
  let variantState = {
    loading: false,
    registry: null,
    error: null,
    revision: 0,
  }

  function publishVariants(patch) {
    variantState = Object.assign({}, variantState, patch, { revision: variantState.revision + 1 })
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
      try {
        window.dispatchEvent(new CustomEvent('ca:variantchange', { detail: variantState }))
      } catch (_error) {
        /* the event is a convenience for legacy consumers */
      }
    }
    for (const fn of [...variantListeners]) {
      try {
        fn(variantState)
      } catch (error) {
        console.error('[ca] variant subscriber crashed:', error)
      }
    }
  }

  async function loadRegistry(options) {
    const settings = options && typeof options === 'object' ? options : {}
    publishVariants({ loading: true, error: null })
    try {
      const registry = await caGet('/api/registry', { signal: settings.signal })
      publishVariants({ loading: false, registry: registry || null, error: null })
      return registry
    } catch (error) {
      const apiError = toApiError(error)
      if (apiError.code === 'ABORTED') return null
      publishVariants({ loading: false, error: apiError })
      return null
    }
  }

  async function setActiveVariant(variantId, options) {
    const settings = options && typeof options === 'object' ? options : {}
    const data = await caPost('/api/registry/active', { variant_id: variantId }, { signal: settings.signal })
    await loadRegistry({ signal: settings.signal })
    return data
  }

  function activeVariant() {
    const registry = variantState.registry
    if (!registry || !Array.isArray(registry.variants)) return null
    const id = registry.active_variant_id
    return registry.variants.find((variant) => variant.id === id) || registry.active_variant || null
  }

  const api = {
    channel,
    rpc,
    request,
    caRequest,
    caGet,
    caPost,
    status,
    getStatus: status,
    ping,
    backend,
    tasks,
    explain,
    variant: {
      get: () => variantState,
      subscribe: (fn) => {
        if (typeof fn !== 'function') return () => {}
        variantListeners.add(fn)
        return () => variantListeners.delete(fn)
      },
      refresh: loadRegistry,
      setActive: setActiveVariant,
      active: activeVariant,
    },
    /** Raw utilities for pages that need envelope-level control. */
    endpoints: Endpoints,
    ApiError,
  }

  return api
}

export default createApi
