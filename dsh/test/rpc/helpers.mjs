/**
 * Shared fakes for the `dsh/test/rpc` suite: a DSH web-server route capture,
 * a connection trust stub, a backend manager, a task manager and an explain
 * service. No DSH runtime and no CA process is involved.
 */

import { EventEmitter } from 'node:events'
import { Readable } from 'node:stream'

import { BackendError } from '../../lib/host/backend/errors.js'

export function tick(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Fake backend manager exposing the RPC-facing surface.
 * @param {{status?: any, config?: Record<string, any>, handlers?: Record<string, Function>}} [options]
 */
export function createFakeBackend(options = {}) {
  let status = options.status ?? {
    mode: 'local',
    state: 'running',
    baseUrl: 'http://127.0.0.1:8765',
    db: '/tmp/ca.db',
    version: '1.2.3',
    features: { jobs: true, sse: true },
  }
  const handlers = options.handlers ?? {}
  const calls = {
    request: [],
    start: 0,
    stop: 0,
    restart: 0,
    configure: [],
    probeLocal: 0,
    probeRemote: [],
    startRemoteInstall: [],
    installRemote: [],
    installLocal: [],
    startLocalInstall: [],
    listDatabases: [],
    useDatabase: [],
    listProjects: 0,
    saveProject: [],
    deleteProject: [],
    activateProject: [],
  }
  let projects = (Array.isArray(options.projects) ? options.projects : []).map((project) => ({ ...project }))
  let activeProjectId =
    options.activeProjectId !== undefined
      ? options.activeProjectId
      : projects.find((project) => project.active === true)?.id ?? null
  /** @type {Array<{options: any, resolve: (value: any) => void, reject: (error: any) => void}>} */
  const pendingLocalInstall = []
  const backend = {
    calls,
    pendingLocalInstall,
    config: options.config ?? {
      backend: 'local',
      db: '/tmp/ca.db',
      repo: '/repo',
      remote: { ssh: { host: 'linux-box', user: 'yu', keyPath: '/home/yu/.ssh/id_ed25519' } },
      auth: { password: 'top-secret', token: 'abc123' },
    },
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
      return { stopped: true }
    },
    async restart() {
      calls.restart += 1
      return { restarted: true }
    },
    async request(path, requestOptions = {}) {
      calls.request.push({ path, options: requestOptions })
      const handler = handlers[path] ?? handlers['*']
      if (handler) {
        const result = await handler(requestOptions, path)
        if (result instanceof Error) throw result
        return result
      }
      return { ok: true, data: { path, query: requestOptions.query ?? null } }
    },
    async configure(patch) {
      calls.configure.push(patch)
      backend.config = { ...backend.config, ...patch }
      return { config: { ...backend.config, db: '/tmp/new.db' }, changed: true, restartRequired: false }
    },
    async probeLocal() {
      calls.probeLocal += 1
      return { ok: true, target: 'local', checks: [{ id: 'python', status: 'ok' }] }
    },
    async probeRemote(ssh) {
      calls.probeRemote.push(ssh)
      return { ok: true, target: 'remote', ssh: ssh ?? null, checks: [] }
    },
    async startRemoteInstall(installOptions) {
      calls.startRemoteInstall.push(installOptions)
      return { started: true, startedAt: Date.now() }
    },
    ...(options.startLocalInstall === true
      ? {
          startLocalInstall(installOptions) {
            calls.startLocalInstall.push(installOptions)
            return {
              started: true,
              running: true,
              scope: 'local',
              startedAt: Date.now(),
            }
          },
        }
      : {}),
    installLocal(installOptions) {
      calls.installLocal.push(installOptions)
      if (options.installLocalMode === 'sync') {
        return {
          started: true,
          state: 'succeeded',
          mode: installOptions.mode,
          offline: installOptions.offline === true,
          installDir: '/opt/ca-local',
          venvPython: '/opt/ca-local/bin/python',
          version: '0.1.0',
          steps: [{ id: 'venv', title: 'create venv', status: 'succeeded' }],
        }
      }
      return new Promise((resolve, reject) => {
        pendingLocalInstall.push({ options: installOptions, resolve, reject })
      })
    },
    getInstallSummary() {
      return {
        platform: {
          os: 'darwin',
          arch: 'arm64',
          platformId: 'macos-arm64',
          label: 'macOS arm64',
          supported: true,
          sep: '/',
        },
        local: {
          scope: 'local',
          installed: true,
          installDir: '/opt/ca-local',
          venvDir: '/opt/ca-local',
          venvPython: '/opt/ca-local/bin/python',
          caScript: '/opt/ca-local/bin/ca',
          version: { version: '0.1.0', schemaVersion: '2' },
          mode: 'offline',
          offline: true,
          state: 'succeeded',
        },
        remote: null,
        wheel: {
          bundled: true,
          name: 'ca_analyzer-0.1.0-py3-none-any.whl',
          version: '0.1.0',
          bytes: 359899,
        },
        wheelhouse: {
          defaultPath: '/tmp/wheelhouse/macos-arm64',
          defaultPlatform: 'macos-arm64',
          defaultExists: true,
          defaultWheelCount: 9,
          availablePlatforms: [
            { platform: 'linux-x64', dir: '/wh/linux-x64', wheelCount: 9, caWheel: 'ca_analyzer.whl' },
            { platform: 'macos-arm64', dir: '/wh/macos-arm64', wheelCount: 9, caWheel: 'ca_analyzer.whl' },
          ],
        },
      }
    },
    getInstallStatus() {
      return {
        state: 'running',
        steps: [{ id: 'rsync', title: 'copy sources', status: 'running' }],
        logs: ['copying…'],
        error: null,
      }
    },
    async listProjects() {
      calls.listProjects += 1
      return {
        projects: projects.map((project) => ({
          ...project,
          active: project.id === activeProjectId || project.active === true,
        })),
        activeProjectId,
      }
    },
    async getActiveProject() {
      return projects.find((project) => project.id === activeProjectId) ?? null
    },
    async saveProject(input) {
      calls.saveProject.push(input)
      const existing = input.id ? projects.find((project) => project.id === input.id) : null
      const project = {
        ...(existing ?? {}),
        ...input,
        id: input.id ?? `proj-${projects.length + 1}`,
      }
      projects = existing
        ? projects.map((item) => (item.id === project.id ? project : item))
        : [...projects, project]
      return { project, projects, activeProjectId }
    },
    async deleteProject(id) {
      calls.deleteProject.push(id)
      const project = projects.find((item) => item.id === id)
      if (!project) {
        const error = new Error(`project not found: ${id}`)
        error.code = 'PROJECT_NOT_FOUND'
        throw error
      }
      const activeCleared = activeProjectId === id
      projects = projects.filter((item) => item.id !== id)
      if (activeCleared) activeProjectId = null
      return { deleted: project, projects, activeProjectId, activeCleared }
    },
    async activateProject(id) {
      calls.activateProject.push(id)
      const project = projects.find((item) => item.id === id)
      if (!project) {
        const error = new Error(`project not found: ${id}`)
        error.code = 'PROJECT_NOT_FOUND'
        throw error
      }
      activeProjectId = id
      return { project, config: backend.config, status: backend.getStatus() }
    },
    async listDatabases(options) {
      calls.listDatabases.push(options)
      return {
        mode: 'local',
        current: { db: '/tmp/ca.db', dbDir: null },
        candidates: [
          {
            kind: 'db',
            path: '/tmp/ca.db',
            label: 'ca.db',
            sizeBytes: 2048,
            mtime: 1700000000,
            source: 'configured',
          },
          {
            kind: 'dbDir',
            path: '/tmp/index',
            label: 'index',
            dbCount: 3,
            source: 'scan',
          },
        ],
      }
    },
    async useDatabase(request) {
      calls.useDatabase.push(request)
      backend.config = {
        ...backend.config,
        ...(request.kind === 'db' ? { db: request.path } : { dbDir: request.path }),
      }
      return {
        config: backend.config,
        status: { mode: 'local', state: 'running', db: request.path },
      }
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

/** Fake task manager as consumed by the RPC layer. */
export function createFakeTasks(seed = []) {
  const tasks = seed.map((task) => ({ ...task }))
  const calls = { start: [], get: [], logs: [], cancel: [], list: 0 }
  return {
    calls,
    start(kind, params) {
      calls.start.push({ kind, params })
      if (kind === 'bogus') {
        const error = new Error(`unknown task kind "${kind}"`)
        error.code = 'UNKNOWN_TASK_KIND'
        throw error
      }
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
    counts() {
      const counts = { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 }
      for (const task of tasks) if (counts[task.status] !== undefined) counts[task.status] += 1
      return counts
    },
    get(id) {
      calls.get.push(id)
      return tasks.find((task) => task.id === id) ?? null
    },
    logs(id, options = {}) {
      calls.logs.push({ id, options })
      return {
        id,
        lines: [{ n: 1, stream: 'stdout', text: 'started' }],
        offset: options.offset ?? 0,
        limit: options.limit ?? 200,
        total: 1,
        nextOffset: null,
        logBytes: 10,
        droppedLines: 0,
        clipped: false,
      }
    },
    cancel(id) {
      calls.cancel.push(id)
      const task = tasks.find((item) => item.id === id)
      if (!task) {
        const error = new Error(`no CA task with id "${id}"`)
        error.code = 'TASK_NOT_FOUND'
        throw error
      }
      task.status = 'cancelled'
      return task
    },
  }
}

/** Fake explain service capturing the exact request (including signal). */
export function createFakeExplain(result = {}) {
  const calls = []
  return {
    calls,
    async explain(request) {
      calls.push(request)
      return {
        symbol: request.symbol,
        text: 'kfree releases the object when its reference count reaches zero.',
        model: { provider: 'deepseek', model: 'deepseek-chat' },
        evidenceCount: 8,
        usage: { inputTokens: 100, outputTokens: 50 },
        ...result,
      }
    },
  }
}

/**
 * Minimal `node:http` request double for the web route handler.
 * @param {{method?: string, url?: string, headers?: Record<string, string>, body?: string}} [options]
 */
export function createFakeReq(options = {}) {
  const body = options.body ?? ''
  const req = Readable.from(body.length > 0 ? [Buffer.from(body)] : [])
  req.method = options.method ?? 'POST'
  req.url = options.url ?? '/dsh-ca/ping'
  req.headers = options.headers ?? { host: '127.0.0.1:61872' }
  return req
}

/** Minimal `node:http` response double capturing status/headers/body. */
export function createFakeRes() {
  const res = new EventEmitter()
  res.statusCode = null
  res.headers = null
  res.headersSent = false
  res.body = ''
  res.finished = false
  res.writeHead = (status, headers) => {
    res.statusCode = status
    res.headers = headers ?? {}
    res.headersSent = true
    return res
  }
  res.end = (body) => {
    if (body !== undefined) res.body += String(body)
    res.finished = true
    return res
  }
  return res
}

/**
 * Fake Cordis context: captures the web route and supports `ctx.effect`.
 * @param {{services?: Record<string, any>, rejection?: 401|403|undefined, useGetter?: boolean}} [options]
 */
export function createFakeCtx(options = {}) {
  const services = options.services ?? {}
  const effects = []
  const ctx = {
    services,
    effects,
    route: null,
    routeDisposed: false,
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
  const webServer = {
    getOrigin: () => 'http://127.0.0.1:61872',
    register(route) {
      ctx.route = route
      return () => {
        ctx.routeDisposed = true
      }
    },
  }
  const connection = {
    requestRejection(request) {
      if (typeof options.rejection === 'function') return options.rejection(request)
      return options.rejection
    },
  }
  if (options.useGetter) {
    services.webServer = webServer
    services.connection = connection
  } else {
    ctx.webServer = webServer
    ctx.connection = connection
  }
  return ctx
}

/**
 * POST one endpoint through the registered web route.
 *
 * @param {any} ctx fake context with a captured route
 * @param {string} endpoint
 * @param {unknown} [payload]
 * @param {{method?: string, url?: string, rawBody?: string, headers?: Record<string, string>}} [options]
 * @returns {Promise<{status: number, headers: Record<string, string>, body: string, envelope: any}>}
 */
export async function invoke(ctx, endpoint, payload, options = {}) {
  if (!ctx.route) throw new Error('web route not registered')
  const rawBody =
    options.rawBody !== undefined
      ? options.rawBody
      : payload === undefined
        ? ''
        : JSON.stringify(payload)
  const req = createFakeReq({
    method: options.method ?? 'POST',
    url: options.url ?? `/dsh-ca/${endpoint}`,
    headers: options.headers,
    body: rawBody,
  })
  const res = createFakeRes()
  await ctx.route.handler(req, res)
  let envelope = null
  try {
    envelope = res.body ? JSON.parse(res.body) : null
  } catch {
    envelope = null
  }
  return { status: res.statusCode, headers: res.headers, body: res.body, envelope }
}

export { BackendError }
