/**
 * Host channel tests: DSH web-route transport (auth/status/body limits), endpoint
 * dispatch, envelope discipline, parameter/path validation and the guarantee
 * that the route handler never throws.
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import { RPC_ENDPOINTS, registerRpc } from '../../lib/host/rpc/index.js'
import {
  BackendError,
  createFakeBackend,
  createFakeCtx,
  createFakeExplain,
  createFakeTasks,
  invoke,
  tick,
} from './helpers.mjs'

function setup(options = {}) {
  const ctx = createFakeCtx({
    services: options.services,
    rejection: options.rejection,
    useGetter: options.useGetter,
  })
  const backend =
    options.backend ??
    createFakeBackend({
      status: options.status,
      handlers: options.handlers,
      config: options.backendConfig,
    })
  const tasks = options.tasks ?? createFakeTasks(options.taskSeed ?? [])
  const explain = options.explain === undefined ? createFakeExplain() : options.explain
  const dispose = registerRpc(ctx, { backend, tasks, explain, config: options.config ?? {} })
  const call = (endpoint, payload, requestOptions) => invoke(ctx, endpoint, payload, requestOptions)
  return { ctx, backend, tasks, explain, dispose, call }
}

function valueOf(response) {
  assert.equal(
    response.envelope?.ok,
    true,
    `expected ok envelope (status ${response.status}), got ${response.body}`,
  )
  return response.envelope.value
}

function errorOf(response) {
  assert.equal(
    response.envelope?.ok,
    false,
    `expected error envelope (status ${response.status}), got ${response.body}`,
  )
  const error = response.envelope.error
  assert.equal(typeof error.code, 'string')
  assert.ok(error.code.length > 0)
  assert.equal(typeof error.message, 'string')
  assert.ok(error.message.length > 0, 'error message is actionable')
  assert.equal(typeof error.details, 'object')
  return error
}

function jsonHeaders(response) {
  assert.match(String(response.headers?.['content-type']), /application\/json/)
  assert.equal(String(response.headers?.['cache-control']), 'no-store')
}

// --------------------------------------------------------------------------- //
// Registration + transport
// --------------------------------------------------------------------------- //

test('registerRpc requires webServer, connection, backend and task services', () => {
  const backend = createFakeBackend()
  const tasks = createFakeTasks()
  assert.throws(() => registerRpc(createFakeCtx(), { backend: undefined, tasks }), /backend manager/)
  assert.throws(() => registerRpc(createFakeCtx(), { backend, tasks: undefined }), /task manager/)

  const bare = createFakeCtx()
  bare.webServer = undefined
  assert.throws(() => registerRpc(bare, { backend, tasks }), /webServer service/)

  const noConnection = createFakeCtx()
  noConnection.connection = undefined
  assert.throws(() => registerRpc(noConnection, { backend, tasks }), /connection service/)
})

test('registers one prefix web route and disposes with the context', async () => {
  const { ctx, dispose } = setup()
  assert.equal(ctx.route.kind, 'prefix')
  assert.equal(ctx.route.path, '/dsh-ca')
  assert.equal(typeof ctx.route.handler, 'function')
  assert.equal(ctx.route.handler.length, 2, 'handler is (req,res)')
  dispose()
  await tick(5)
  assert.equal(ctx.routeDisposed, true)
})

test('services may be resolved through ctx.get()', async () => {
  const context = setup({ useGetter: true })
  const pong = valueOf(await context.call('ping'))
  assert.equal(pong.pong, true)
})

test('POST /dsh-ca/ping returns a JSON envelope with security headers', async () => {
  const context = setup()
  const response = await context.call('ping')
  assert.equal(response.status, 200)
  jsonHeaders(response)
  assert.equal(typeof response.headers['content-length'], 'string')
  assert.equal(response.headers['x-content-type-options'], 'nosniff')
  const pong = valueOf(response)
  assert.deepEqual(pong, { pong: true, version: '0.1.0' })
})

test('rejected trust checks answer 401/403 with a JSON error envelope', async () => {
  const unauthorized = setup({ rejection: 401 })
  const response401 = await unauthorized.call('ping')
  assert.equal(response401.status, 401)
  jsonHeaders(response401)
  assert.equal(errorOf(response401).code, 'UNAUTHORIZED')

  const forbidden = setup({ rejection: 403 })
  const response403 = await forbidden.call('ping')
  assert.equal(response403.status, 403)
  assert.equal(errorOf(response403).code, 'FORBIDDEN')

  const throwing = setup({
    rejection: () => {
      throw new Error('trust service failure')
    },
  })
  const responseFailClosed = await throwing.call('ping')
  assert.equal(responseFailClosed.status, 403, 'a throwing trust check fails closed')

  const odd = setup({ rejection: 'denied' })
  const responseOdd = await odd.call('ping')
  assert.equal(responseOdd.status, 403, 'unknown rejection values fail closed')
})

test('non-POST methods answer 405 with Allow: POST', async () => {
  const context = setup()
  const response = await context.call('ping', undefined, { method: 'GET' })
  assert.equal(response.status, 405)
  assert.equal(errorOf(response).code, 'METHOD_NOT_ALLOWED')
  assert.equal(response.headers.allow, 'POST')
})

test('routes outside /dsh-ca/<endpoint> answer 404', async () => {
  const context = setup()
  const outside = await context.call('ping', undefined, { url: '/other' })
  assert.equal(outside.status, 404)
  assert.equal(errorOf(outside).code, 'NOT_FOUND')

  const bare = await context.call('ping', undefined, { url: '/dsh-ca' })
  assert.equal(bare.status, 404)

  const nested = await context.call('ping', undefined, { url: '/dsh-ca/a/b' })
  assert.equal(nested.status, 404)

  const unknown = await context.call('nope', {})
  assert.equal(unknown.status, 404)
  const error = errorOf(unknown)
  assert.equal(error.code, 'UNKNOWN_ENDPOINT')
  assert.deepEqual(error.details.endpoints, [...RPC_ENDPOINTS])
})

test('invalid JSON bodies answer 400, empty bodies default to {}', async () => {
  const context = setup()
  const invalid = await context.call('ping', undefined, { rawBody: 'not-json' })
  assert.equal(invalid.status, 400)
  assert.equal(errorOf(invalid).code, 'INVALID_JSON')

  const empty = await context.call('ping', undefined, { rawBody: '' })
  assert.equal(empty.status, 200)
  assert.equal(valueOf(empty).pong, true)
})

test('request bodies over 1 MiB answer 413', async () => {
  const context = setup()
  const response = await context.call('ping', undefined, {
    rawBody: 'x'.repeat(1024 * 1024 + 1),
  })
  assert.equal(response.status, 413)
  assert.equal(errorOf(response).code, 'BODY_TOO_LARGE')
})

test('HTTP statuses separate business errors (200), argument errors (400) and internals (500)', async () => {
  const context = setup({ taskSeed: [] })
  const invalid = await context.call('tasks.get', {})
  assert.equal(invalid.status, 400)
  assert.equal(errorOf(invalid).code, 'INVALID_ARGUMENT')

  const missing = await context.call('tasks.get', { id: 'task-404' })
  assert.equal(missing.status, 200, 'missing task is a business outcome')
  assert.equal(errorOf(missing).code, 'TASK_NOT_FOUND')

  const forbidden = await context.call('ca.request', { path: '/etc/passwd' })
  assert.equal(forbidden.status, 400)
  assert.equal(errorOf(forbidden).code, 'FORBIDDEN_PATH')
})

// --------------------------------------------------------------------------- //
// status
// --------------------------------------------------------------------------- //

test('status reports backend, recent tasks, counts and the explain model', async () => {
  const context = setup({
    taskSeed: [
      { id: 'task-1', kind: 'index', status: 'succeeded', params: {}, createdAt: 1 },
      { id: 'task-2', kind: 'index', status: 'running', params: {}, createdAt: 2 },
      { id: 'task-3', kind: 'index', status: 'queued', params: {}, createdAt: 3 },
    ],
    services: {
      agentDefaultModel: {
        currentSelection: () => ({ provider: 'deepseek', model: 'deepseek-chat' }),
      },
    },
  })
  const status = valueOf(await context.call('status'))
  assert.equal(status.backend.mode, 'local')
  assert.equal(status.backend.state, 'running')
  assert.equal(status.backend.db, '/tmp/ca.db')
  assert.equal(status.tasks.length, 3)
  assert.equal(status.tasks[0].id, 'task-3')
  assert.deepEqual(status.counts, { queued: 1, running: 1, succeeded: 1, failed: 0, cancelled: 0 })
  assert.deepEqual(status.model, { provider: 'deepseek', model: 'deepseek-chat' })
})

test('status never reads optional services directly (Cordis inject guard regression)', async () => {
  const context = setup()
  Object.defineProperty(context.ctx, 'agentDefaultModel', {
    configurable: true,
    get() {
      throw new Error('cannot get property "agentDefaultModel" without inject')
    },
  })
  const response = await context.call('status')
  assert.equal(response.status, 200)
  const status = valueOf(response)
  assert.equal(status.model, null)
})

test('status returns null model when no selection service exists', async () => {
  const context = setup({ explain: null })
  const status = valueOf(await context.call('status'))
  assert.equal(status.model, null)
})

// --------------------------------------------------------------------------- //
// ca.request passthrough
// --------------------------------------------------------------------------- //

test('ca.request passes /api/* calls through and returns the CA envelope verbatim', async () => {
  const context = setup({
    handlers: {
      '/api/search': (options) => ({
        ok: true,
        data: { items: [{ name: 'kfree' }], query: options.query.q },
      }),
    },
  })
  const response = await context.call('ca.request', {
    method: 'GET',
    path: '/api/search',
    query: { q: 'kfree', limit: 20 },
  })
  assert.equal(response.status, 200)
  const envelope = valueOf(response)
  assert.equal(envelope.ok, true)
  assert.equal(envelope.data.items[0].name, 'kfree')
  const recorded = context.backend.calls.request[0]
  assert.equal(recorded.path, '/api/search')
  assert.equal(recorded.options.method, 'GET')
  assert.deepEqual(recorded.options.query, { q: 'kfree', limit: '20' })
  assert.equal(recorded.options.body, undefined)
  assert.equal(typeof recorded.options.signal?.aborted, 'boolean', 'handler signal is forwarded')
})

test('ca.request defaults to GET, accepts HEAD/POST, and forwards JSON bodies', async () => {
  const context = setup()
  valueOf(await context.call('ca.request', { path: '/api/health' }))
  assert.equal(context.backend.calls.request[0].options.method, 'GET')

  valueOf(await context.call('ca.request', { method: 'HEAD', path: '/api/health' }))
  assert.equal(context.backend.calls.request[1].options.method, 'HEAD')

  valueOf(
    await context.call('ca.request', {
      method: 'POST',
      path: '/api/registry/active',
      body: { variant_id: 'v2' },
    }),
  )
  assert.equal(context.backend.calls.request[2].options.method, 'POST')
  assert.deepEqual(context.backend.calls.request[2].options.body, { variant_id: 'v2' })
})

test('ca.request rejects non-/api paths and malformed paths without touching the backend', async () => {
  const context = setup()
  for (const path of ['/etc/passwd', 'api/health', '/api/../etc/passwd', '/api//health', '/api/a b']) {
    const error = errorOf(await context.call('ca.request', { path }))
    assert.equal(error.code, 'FORBIDDEN_PATH', `path ${path} -> ${error.code}`)
  }
  const withQuery = errorOf(await context.call('ca.request', { path: '/api/search?q=x' }))
  assert.equal(withQuery.code, 'INVALID_ARGUMENT')
  assert.equal(errorOf(await context.call('ca.request', {})).code, 'INVALID_ARGUMENT')
  assert.equal(context.backend.calls.request.length, 0)
})

test('ca.request validates method, query and body shapes', async () => {
  const context = setup()
  assert.equal(
    errorOf(await context.call('ca.request', { method: 'DELETE', path: '/api/health' })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(await context.call('ca.request', { method: 'GET', path: '/api/search', body: { a: 1 } }))
      .code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(
      await context.call('ca.request', { path: '/api/search', query: { limit: { nested: true } } }),
    ).code,
    'INVALID_ARGUMENT',
  )
  const manyQuery = Object.fromEntries(Array.from({ length: 65 }, (_, index) => [`q${index}`, 'x']))
  assert.equal(
    errorOf(await context.call('ca.request', { path: '/api/search', query: manyQuery })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(
      await context.call('ca.request', {
        method: 'POST',
        path: '/api/jobs',
        body: { blob: 'x'.repeat(600 * 1024) },
      }),
    ).code,
    'PAYLOAD_TOO_LARGE',
  )
  assert.equal(context.backend.calls.request.length, 0)
})

test('ca.request returns CA error envelopes as values, transport errors as channel errors', async () => {
  const caError = setup({
    handlers: {
      '/api/function': () => ({ ok: false, error: { code: 'NOT_FOUND', message: 'no function' } }),
    },
  })
  const envelope = valueOf(
    await caError.call('ca.request', { path: '/api/function', query: { symbol: 'x' } }),
  )
  assert.equal(envelope.ok, false)
  assert.equal(envelope.error.code, 'NOT_FOUND')

  const transport = setup({
    handlers: {
      '/api/x': () => new BackendError('HTTP_TIMEOUT', 'CA request timed out after 30000ms'),
    },
  })
  const timeout = await transport.call('ca.request', { path: '/api/x' })
  assert.equal(timeout.status, 200, 'transport failures are still a channel-level envelope')
  const timeoutError = errorOf(timeout)
  assert.equal(timeoutError.code, 'HTTP_TIMEOUT')
  assert.match(timeoutError.message, /timed out/)

  const crashed = setup({
    handlers: {
      '/api/y': () => {
        throw new Error('boom at /secret/path')
      },
    },
  })
  const crashResponse = await crashed.call('ca.request', { path: '/api/y' })
  assert.equal(crashResponse.status, 500)
  const internal = errorOf(crashResponse)
  assert.equal(internal.code, 'INTERNAL')
  assert.match(internal.message, /boom/)
  assert.ok(!/\n\s+at\s/.test(internal.message), 'no stack leaked')
})

// --------------------------------------------------------------------------- //
// backend.*
// --------------------------------------------------------------------------- //

test('backend.get returns status plus a redacted config', async () => {
  const context = setup()
  const value = valueOf(await context.call('backend.get'))
  assert.equal(value.status.state, 'running')
  assert.equal(value.config.repo, '/repo')
  assert.deepEqual(value.config.remote.ssh.host, 'linux-box')
  assert.equal(value.config.remote.ssh.keyPath, '/home/yu/.ssh/id_ed25519')
  assert.equal(value.config.auth.password, '***')
  assert.equal(value.config.auth.token, '***')
})

test('backend.configure validates the patch and forwards it', async () => {
  const context = setup()
  const value = valueOf(await context.call('backend.configure', { patch: { backend: 'remote' } }))
  assert.equal(value.changed, true)
  assert.equal(value.config.db, '/tmp/new.db')
  assert.deepEqual(context.backend.calls.configure, [{ backend: 'remote' }])
  assert.equal(
    errorOf(await context.call('backend.configure', { patch: 'nope' })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(await context.call('backend.configure', { patch: {} })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(errorOf(await context.call('backend.configure', {})).code, 'INVALID_ARGUMENT')
})

test('backend lifecycle endpoints call the manager', async () => {
  const context = setup()
  const started = valueOf(await context.call('backend.start'))
  assert.equal(started.status.state, 'running')
  assert.equal(context.backend.calls.start, 1)

  const stopped = valueOf(await context.call('backend.stop'))
  assert.equal(typeof stopped.status, 'object')
  assert.equal(context.backend.calls.stop, 1)

  const restarted = valueOf(await context.call('backend.restart'))
  assert.equal(restarted.result.restarted, true)
  assert.equal(context.backend.calls.restart, 1)
})

test('backend.probe handles local and remote targets with ssh validation', async () => {
  const context = setup()
  const local = valueOf(await context.call('backend.probe', { target: 'local' }))
  assert.equal(local.ok, true)
  assert.equal(context.backend.calls.probeLocal, 1)

  const remote = valueOf(
    await context.call('backend.probe', {
      target: 'remote',
      ssh: { host: 'linux-box', user: 'yu', port: 22 },
    }),
  )
  assert.equal(remote.target, 'remote')
  assert.deepEqual(context.backend.calls.probeRemote[0], { host: 'linux-box', user: 'yu', port: 22 })

  valueOf(await context.call('backend.probe', { target: 'remote' }))
  assert.equal(context.backend.calls.probeRemote[1], undefined)

  assert.equal(
    errorOf(await context.call('backend.probe', { target: 'remote', ssh: { user: 'yu' } })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(await context.call('backend.probe', { target: 'cloud' })).code,
    'INVALID_ARGUMENT',
  )
})

test('backend.install starts a background install and installStatus polls it', async () => {
  const context = setup()
  const started = valueOf(
    await context.call('backend.install', {
      ssh: { host: 'linux-box', user: 'yu' },
      opts: { repoPath: '/srv/CA' },
    }),
  )
  assert.equal(started.started, true)
  assert.equal(started.status, 'running')
  assert.match(started.hint, /backend\.installStatus/)
  assert.deepEqual(context.backend.calls.startRemoteInstall[0], {
    repoPath: '/srv/CA',
    ssh: { host: 'linux-box', user: 'yu' },
  })

  const status = valueOf(await context.call('backend.installStatus'))
  assert.equal(status.status, 'running')
  assert.equal(status.state, 'running')
  assert.equal(status.steps.length, 1)

  assert.equal(
    errorOf(await context.call('backend.install', { ssh: { user: 'yu' } })).code,
    'INVALID_ARGUMENT',
  )
})

test('backend.install falls back to a foreground installer promise', async () => {
  const backend = createFakeBackend()
  delete backend.startRemoteInstall
  delete backend.getInstallStatus
  let resolveInstall
  backend.installRemote = () =>
    new Promise((resolve) => {
      resolveInstall = resolve
    })
  const context = setup({ backend })
  const started = valueOf(await context.call('backend.install', { ssh: 'user@linux-box' }))
  assert.equal(started.started, true)
  const running = valueOf(await context.call('backend.installStatus'))
  assert.equal(running.status, 'running')
  resolveInstall({ ok: true, version: '0.1.0' })
  await tick(5)
  const finished = valueOf(await context.call('backend.installStatus'))
  assert.equal(finished.status, 'succeeded')
  assert.deepEqual(finished.result, { ok: true, version: '0.1.0' })
})

// --------------------------------------------------------------------------- //
// tasks.*
// --------------------------------------------------------------------------- //

test('tasks.start forwards kind/params and preserves task-manager error codes', async () => {
  const context = setup()
  const value = valueOf(
    await context.call('tasks.start', { kind: 'index', params: { repo: '/r', db: '/d.db' } }),
  )
  assert.equal(value.task.kind, 'index')
  assert.equal(value.task.status, 'queued')
  assert.deepEqual(context.tasks.calls.start[0], {
    kind: 'index',
    params: { repo: '/r', db: '/d.db' },
  })

  assert.equal(
    errorOf(await context.call('tasks.start', { kind: 'bogus' })).code,
    'UNKNOWN_TASK_KIND',
  )
  assert.equal(errorOf(await context.call('tasks.start', {})).code, 'INVALID_ARGUMENT')
  assert.equal(
    errorOf(await context.call('tasks.start', { kind: 'index', params: [] })).code,
    'INVALID_ARGUMENT',
  )
})

test('tasks.list/get/logs/cancel validate ids and return task data', async () => {
  const taskSeed = [
    {
      id: 'task-1',
      kind: 'index',
      status: 'running',
      params: { repo: '/r' },
      createdAt: 1700000000000,
      startedAt: 1700000000100,
      finishedAt: null,
      exitCode: null,
      error: null,
      logBytes: 12,
    },
  ]
  const context = setup({ taskSeed })
  const list = valueOf(await context.call('tasks.list'))
  assert.equal(list.tasks.length, 1)
  assert.deepEqual(list.counts, { queued: 0, running: 1, succeeded: 0, failed: 0, cancelled: 0 })

  const got = valueOf(await context.call('tasks.get', { id: 'task-1' }))
  assert.equal(got.task.id, 'task-1')

  const logs = valueOf(await context.call('tasks.logs', { id: 'task-1', offset: 0, limit: 50 }))
  assert.equal(logs.lines.length, 1)
  assert.equal(logs.limit, 50)

  const cancelled = valueOf(await context.call('tasks.cancel', { id: 'task-1' }))
  assert.equal(cancelled.task.status, 'cancelled')

  assert.equal(errorOf(await context.call('tasks.get', {})).code, 'INVALID_ARGUMENT')
  assert.equal(errorOf(await context.call('tasks.get', { id: 'task-404' })).code, 'TASK_NOT_FOUND')
  assert.equal(
    errorOf(await context.call('tasks.logs', { id: 'task-1', limit: 5000 })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(await context.call('tasks.logs', { id: 'task-1', offset: -1 })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(await context.call('tasks.cancel', { id: 'task-404' })).code,
    'TASK_NOT_FOUND',
  )
})

// --------------------------------------------------------------------------- //
// explain
// --------------------------------------------------------------------------- //

test('explain delegates to the explain service with validation and an abort signal', async () => {
  const context = setup()
  const value = valueOf(
    await context.call('explain', {
      symbol: 'kfree',
      language: 'zh',
      question: 'why is this hot?',
    }),
  )
  assert.equal(value.symbol, 'kfree')
  assert.match(value.text, /reference count/)
  assert.deepEqual(value.model, { provider: 'deepseek', model: 'deepseek-chat' })
  assert.equal(value.evidenceCount, 8)
  assert.deepEqual(value.usage, { inputTokens: 100, outputTokens: 50 })
  assert.equal(context.explain.calls[0].symbol, 'kfree')
  assert.equal(context.explain.calls[0].language, 'zh')
  assert.equal(context.explain.calls[0].question, 'why is this hot?')
  assert.ok(context.explain.calls[0].signal instanceof AbortSignal)
})

test('explain reports unavailable/failed services and invalid payloads', async () => {
  const missing = setup({ explain: null })
  const unavailable = errorOf(await missing.call('explain', { symbol: 'x' }))
  assert.equal(unavailable.code, 'EXPLAIN_UNAVAILABLE')
  assert.match(unavailable.message, /no model selection service/)

  const context = setup()
  assert.equal(errorOf(await context.call('explain', {})).code, 'INVALID_ARGUMENT')
  assert.equal(
    errorOf(await context.call('explain', { symbol: 'x', language: 'fr' })).code,
    'INVALID_ARGUMENT',
  )
  assert.equal(
    errorOf(await context.call('explain', { symbol: 'x', question: 'q'.repeat(4001) })).code,
    'INVALID_ARGUMENT',
  )

  const broken = setup({ explain: createFakeExplain({ text: '' }) })
  assert.equal(errorOf(await broken.call('explain', { symbol: 'x' })).code, 'EXPLAIN_FAILED')
})

// --------------------------------------------------------------------------- //
// robustness
// --------------------------------------------------------------------------- //

test('the route handler never throws, for any endpoint or body shape', async () => {
  const context = setup()
  for (const endpoint of ['ping', 'status', 'ca.request', 'backend.get', 'tasks.list', 'explain', '???']) {
    for (const rawBody of ['', 'null', '[1,2,3]', '"text"', '123', 'not-json', '{}']) {
      const response = await context.call(endpoint, undefined, { rawBody })
      assert.equal(typeof response.envelope, 'object')
      assert.equal(typeof response.envelope.ok, 'boolean')
      if (!response.envelope.ok) assert.equal(typeof response.envelope.error.code, 'string')
    }
  }
})

test('oversized handler responses fail with RESPONSE_TOO_LARGE instead of crashing', async () => {
  const tasks = createFakeTasks()
  tasks.logs = (id) => ({
    id,
    lines: [{ n: 1, stream: 'stdout', text: 'x'.repeat(5 * 1024 * 1024) }],
    offset: 0,
    limit: 200,
    total: 1,
    nextOffset: null,
    logBytes: 5 * 1024 * 1024,
    droppedLines: 0,
    clipped: false,
  })
  const context = setup({ tasks })
  const response = await context.call('tasks.logs', { id: 'task-1' })
  assert.equal(response.status, 500)
  assert.equal(errorOf(response).code, 'RESPONSE_TOO_LARGE')
})
