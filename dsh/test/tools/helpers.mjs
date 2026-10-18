/**
 * Shared fakes for the `dsh/test/tools` suite.
 *
 * The task manager and the native tools are tested against in-memory services
 * only: no CA process, no network, no DSH runtime.
 */

import { BackendError } from '../../lib/host/backend/errors.js'

/** Small deterministic tick helpers. */
export function tick(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Fake backend manager matching the backend-ops contract.
 *
 * @param {{
 *   status?: any,
 *   config?: Record<string, any>,
 *   handlers?: Record<string, (options: any) => any>,
 *   autoExits?: boolean,
 *   execDelayMs?: number,
 * }} [options]
 */
export function createFakeBackend(options = {}) {
  let status = options.status ?? {
    mode: 'local',
    state: 'running',
    baseUrl: 'http://127.0.0.1:8765',
    db: '/tmp/ca.db',
    version: '1.2.3',
    features: { jobs: true, sse: true, mcp: true },
  }
  const handlers = options.handlers ?? {}
  const calls = {
    request: [],
    execCa: [],
    start: 0,
    stop: 0,
    restart: 0,
    probeLocal: 0,
    probeRemote: [],
    configure: [],
  }
  /** @type {Array<{spec: any, options: any, resolve: (value: any) => void, reject: (error: any) => void}>} */
  const pending = []

  const backend = {
    calls,
    pending,
    config: options.config ?? { backend: 'local', db: '/tmp/ca.db' },
    getStatus() {
      return typeof status === 'function' ? status() : { ...status }
    },
    setStatus(next) {
      status = next
    },
    async start() {
      calls.start += 1
      const current = typeof status === 'function' ? status() : { ...status }
      status = { ...current, state: 'running' }
      return backend.getStatus()
    },
    async stop() {
      calls.stop += 1
      const current = typeof status === 'function' ? status() : { ...status }
      status = { ...current, state: 'stopped' }
      return backend.getStatus()
    },
    async restart() {
      calls.restart += 1
      return backend.getStatus()
    },
    async request(path, requestOptions = {}) {
      calls.request.push({ path, options: requestOptions })
      const handler = handlers[path] ?? handlers['*']
      if (handler) {
        const result = await handler(requestOptions, path)
        if (result instanceof Error) throw result
        return result
      }
      return { ok: true, data: { path } }
    },
    async execCa(spec, execOptions = {}) {
      calls.execCa.push({ spec, options: execOptions })
      if (options.autoExits) {
        execOptions.onStdout?.(`ran ${Array.isArray(spec) ? spec[0] : 'ca'}\n`)
        return { exitCode: 0, signal: null }
      }
      return new Promise((resolve, reject) => {
        const entry = { spec, options: execOptions, resolve, reject }
        pending.push(entry)
        execOptions.signal?.addEventListener?.('abort', () => {
          const error = new Error('aborted')
          error.name = 'AbortError'
          error.code = 'ABORT_ERR'
          reject(error)
        })
      })
    },
    async probeLocal() {
      calls.probeLocal += 1
      return { ok: true, target: 'local', checks: [] }
    },
    async probeRemote(ssh) {
      calls.probeRemote.push(ssh)
      return { ok: true, target: 'remote', ssh: ssh ?? null, checks: [] }
    },
    async configure(patch) {
      calls.configure.push(patch)
      backend.config = { ...backend.config, ...patch }
      return { config: backend.config, changed: true, restartRequired: false }
    },
    async startRemoteInstall(installOptions) {
      return { started: true, startedAt: Date.now(), installOptions }
    },
    getInstallStatus() {
      return { state: 'idle', steps: [], logs: [], error: null }
    },
    on() {
      return () => {}
    },
    onChange() {
      return () => {}
    },
    async dispose() {},
  }
  return backend
}

/** Convenience: fail a pending exec. */
export function takePending(backend) {
  const entry = backend.pending.shift()
  if (!entry) throw new Error('no pending execCa call')
  return entry
}

/** Fake explain service. */
export function createFakeExplain(result = {}) {
  const calls = []
  return {
    calls,
    async explain(request) {
      calls.push(request)
      return {
        symbol: request.symbol,
        text: 'This function frees the object when the reference count drops to zero.',
        model: { provider: 'deepseek', model: 'deepseek-chat' },
        evidenceCount: 6,
        ...result,
      }
    },
  }
}

/**
 * Fake task manager for tool tests.
 * @param {Array<any>} [seed]
 */
export function createFakeTasks(seed = []) {
  const tasks = seed.map((task) => ({ ...task }))
  const calls = { start: [], logs: [], cancel: [], get: [], list: 0 }
  const tasksApi = {
    calls,
    start(kind, params) {
      calls.start.push({ kind, params })
      const task = {
        id: `task-${tasks.length + 1}`,
        kind,
        status: 'queued',
        params,
        createdAt: Date.now(),
        startedAt: null,
        finishedAt: null,
        exitCode: null,
        error: null,
        logBytes: 0,
      }
      tasks.push(task)
      return task
    },
    list() {
      calls.list += 1
      return tasks.slice().reverse()
    },
    get(id) {
      calls.get.push(id)
      return tasks.find((task) => task.id === id) ?? null
    },
    logs(id, options = {}) {
      calls.logs.push({ id, options })
      const lines = [
        { n: 1, stream: 'stdout', text: 'indexing: start' },
        { n: 2, stream: 'stderr', text: 'warning: header changed' },
        { n: 3, stream: 'stdout', text: 'indexing: done' },
      ]
      return {
        id,
        lines,
        offset: options.offset ?? 0,
        limit: options.limit ?? 200,
        total: 3,
        nextOffset: 4,
        logBytes: 80,
        droppedLines: 0,
        clipped: false,
      }
    },
    cancel(id) {
      calls.cancel.push(id)
      const task = tasks.find((item) => item.id === id)
      if (!task) throw Object.assign(new Error(`no CA task with id "${id}"`), { code: 'TASK_NOT_FOUND' })
      task.status = 'cancelled'
      return task
    },
    counts() {
      const counts = { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 }
      for (const task of tasks) if (counts[task.status] !== undefined) counts[task.status] += 1
      return counts
    },
  }
  return tasksApi
}

/**
 * Fake Cordis context capturing tool registrations and effects.
 * @param {{services?: Record<string, any>}} [options]
 */
export function createFakeCtx(options = {}) {
  const services = options.services ?? {}
  const tools = new Map()
  const effects = []
  return {
    tools: {
      register(definition) {
        if (tools.has(definition.name)) throw new Error(`duplicate tool ${definition.name}`)
        tools.set(definition.name, definition)
        return () => tools.delete(definition.name)
      },
    },
    toolsRegistered: tools,
    effects,
    get(name) {
      return services[name]
    },
    effect(callback, label) {
      const dispose = callback()
      effects.push({ label, dispose })
      return () => {
        try {
          dispose?.()
        } catch {
          // ignore
        }
      }
    },
  }
}

/** A running backend status with a healthy CA envelope handler. */
export function healthyHandlers() {
  return {
    '/api/health': () => ({
      ok: true,
      data: {
        status: 'ok',
        version: '1.2.3',
        uptime_seconds: 42,
        database: {
          paths: ['/tmp/ca.db'],
          db_bytes: 2048,
          schema_version: '3',
          counts: { functions: 10, calls: 20 },
        },
        features: { jobs: true, sse: true, mcp: true, federated: false },
      },
    }),
  }
}

export { BackendError }
