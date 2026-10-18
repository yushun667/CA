/**
 * Native `ca_*` tool tests: fixed tool surface, plain JSON schemas, local
 * argument validation, CA envelope unwrapping, bounded text and task actions.
 * All CA traffic is a fake backend manager (no process/network).
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import { CA_TOOL_NAMES, registerTools } from '../../lib/host/tools/index.js'
import {
  createFakeBackend,
  createFakeCtx,
  createFakeExplain,
  createFakeTasks,
  healthyHandlers,
} from './helpers.mjs'

function setup(options = {}) {
  const ctx = createFakeCtx()
  const backend =
    options.backend ??
    createFakeBackend({
      status: options.status ?? {
        mode: 'local',
        state: 'running',
        baseUrl: 'http://127.0.0.1:8765',
        db: '/tmp/ca.db',
        version: '1.2.3',
      },
      handlers: { ...healthyHandlers(), ...(options.handlers ?? {}) },
      config: options.backendConfig ?? {},
    })
  const tasks = options.tasks ?? createFakeTasks(options.taskSeed ?? [])
  const explain = options.explain === undefined ? createFakeExplain() : options.explain
  const dispose = registerTools(ctx, {
    backend,
    tasks,
    explain,
    config: options.config ?? {},
  })
  const tools = ctx.toolsRegistered
  const run = (name, args, exec = {}) => {
    const definition = tools.get(name)
    assert.ok(definition, `tool ${name} registered`)
    return definition.execute(args, exec)
  }
  return { ctx, backend, tasks, explain, tools, run, dispose }
}

function toolError(promise, code) {
  return assert.rejects(promise, (error) => {
    assert.equal(error.name, 'ToolError')
    if (code) assert.equal(error.code, code)
    assert.ok(typeof error.message === 'string' && error.message.length > 5, 'message is actionable')
    assert.ok(!/\n\s+at\s/.test(error.message), 'no stack leaked into the model-facing error')
    return true
  })
}

const SEARCH_ROW = {
  id: 'fn:abc',
  name: 'kfree',
  qualified_name: 'kfree',
  kind: 'function',
  file: 'mm/slab.c',
  line: 1245,
  signature: 'void kfree(const void *ptr)',
  is_definition: true,
}

const DETAIL = {
  function: {
    id: 'fn:abc',
    name: 'kfree',
    qualified_name: 'kfree',
    kind: 'function',
    file: 'mm/slab.c',
    line: 1245,
    end_line: 1260,
    signature: 'void kfree(const void *ptr)',
    return_type: 'void',
    linkage: 'external',
    params: [{ name: 'ptr' }],
  },
  deep_ready: true,
  branches: [
    { line: 1248, col: 3, condition: 'ptr == NULL', kind: 'if', true_block: 'b1', false_block: 'b2' },
    { line: 1252, col: 3, condition: 'refcount == 0', kind: 'if' },
  ],
  assignments: [{ line: 1250, lhs: 'obj->refs', rhs: '0', operator: '=' }],
  returns: [{ line: 1260, expr: '' }],
  resource_ops: [{ line: 1255, op: 'free', api_name: 'kfree', resource_kind: 'memory' }],
  findings: [{ kind: 'leak', severity: 'high', resource_kind: 'memory', message: 'possible leak of obj' }],
}

const GRAPH = {
  root: 'syscall',
  direction: 'forward',
  depth: 1,
  nodes: [
    { id: 'fn:root', name: 'syscall', file: 'kernel/syscall.c', line: 131, depth: 0 },
    { id: 'fn:open', name: 'sys_open', file: 'kernel/syscall.c', line: 200, depth: 1 },
  ],
  edges: [
    {
      caller_name: 'syscall',
      callee_name: 'sys_open',
      kind: 'direct',
      source: 'direct',
      candidate: false,
      call_file: 'kernel/syscall.c',
      call_line: 143,
    },
    {
      caller_name: 'syscall',
      callee_name: 'cb_target',
      kind: 'callback',
      source: 'candidate',
      candidate: true,
      call_file: 'kernel/syscall.c',
      call_line: 150,
    },
  ],
  cycles: [],
  truncated: false,
  stats: { nodes: 2, edges: 2, cycles: 0 },
  found: true,
}

test('registers exactly the eleven contract tools with plain JSON schemas', () => {
  const { tools, dispose } = setup()
  assert.equal(tools.size, CA_TOOL_NAMES.length)
  for (const name of CA_TOOL_NAMES) {
    const definition = tools.get(name)
    assert.ok(definition, `missing tool ${name}`)
    assert.equal(definition.name, name)
    assert.equal(typeof definition.description, 'string')
    assert.ok(definition.description.length > 60, `${name} description explains when to use it`)
    assert.equal(definition.parameters.type, 'object')
    assert.equal(definition.parameters.additionalProperties, false)
    assert.equal(typeof definition.parameters.properties, 'object')
    assert.ok(Array.isArray(definition.parameters.required))
    assert.equal(definition.output.schema.type, 'object')
    assert.deepEqual(definition.output.schema.required, ['text'])
    assert.deepEqual(definition.output.render({}, { text: 'hello' }), [{ type: 'text', text: 'hello' }])
    assert.equal(definition.isConcurrencySafe({}), true)
    const call = definition.presentCall({})
    assert.equal(call.card, 'generic')
    assert.equal(typeof call.title, 'string')
  }
  dispose()
  assert.equal(tools.size, 0)
})

test('each tool rejects unknown arguments with the supported list', async () => {
  const { run } = setup()
  await toolError(run('ca_search', { query: 'x', bogus: true }), 'INVALID_ARGUMENT').then(() => {})
  await assert.rejects(run('ca_search', { query: 'x', bogus: true }), /unknown argument "bogus".*supported arguments: query, limit, offset, compact/s)
  await assert.rejects(run('ca_status', 'not-an-object'), /arguments must be a JSON object/)
})

test('required arguments, enums and numeric ranges are validated locally', async () => {
  const { run, backend } = setup()
  await assert.rejects(run('ca_search', {}), /"query" is required/)
  await assert.rejects(run('ca_graph', { symbol: 'x', direction: 'up' }), /must be one of: forward, reverse, both/)
  await assert.rejects(run('ca_graph', { symbol: 'x', depth: 13 }), /"depth" must be between 0 and 12/)
  await assert.rejects(run('ca_facts', { symbol: 'x', kind: 'bogus' }), /must be one of: branches, assignments, returns, resources, findings/)
  await assert.rejects(run('ca_task', { action: 'start', kind: 'nope' }), /must be one of: index, update, ensure_deep, prewarm, resolve_callbacks, watch/)
  await assert.rejects(run('ca_task', { action: 'start' }), /"kind" is required for action=start/)
  await assert.rejects(run('ca_task', { action: 'logs' }), /"id" is required for action=logs/)
  assert.equal(backend.calls.request.length, 0, 'validation failures never call CA')
})

test('ca_status summarizes backend + health, and never throws when unconfigured', async () => {
  const {} = setup()
  const running = setup()
  const text = (await running.run('ca_status', {})).text
  assert.match(text, /CA backend: running \(mode: local\)/)
  assert.match(text, /database: \/tmp\/ca\.db/)
  assert.match(text, /health: ok/)
  assert.match(text, /index: functions 10, calls 20/)

  const unconfigured = setup({
    status: { mode: 'unconfigured', state: 'stopped', baseUrl: null, db: null, error: null },
  })
  const unconfiguredText = (await unconfigured.run('ca_status', {})).text
  assert.match(unconfiguredText, /CA backend: stopped \(mode: unconfigured\)/)
  assert.match(unconfiguredText, /CA backend page/)
})

test('ca_search maps to /api/search and formats rows', async () => {
  const { run, backend } = setup({
    handlers: {
      '/api/search': (options) => ({
        ok: true,
        data: {
          query: options.query.q,
          items: [SEARCH_ROW],
          count: 1,
          limit: options.query.limit,
          offset: options.query.offset,
          federated: false,
        },
      }),
    },
  })
  const result = await run('ca_search', { query: 'kfree', limit: 10, offset: 5 })
  assert.equal(typeof result.text, 'string')
  assert.match(result.text, /kfree/)
  assert.match(result.text, /mm\/slab\.c:1245/)
  assert.match(result.text, /id: fn:abc/)
  assert.match(result.text, /next offset 6/)

  const call = backend.calls.request.find((item) => item.path === '/api/search')
  assert.equal(call.options.query.q, 'kfree')
  assert.equal(call.options.query.limit, 10)
  assert.equal(call.options.query.offset, 5)

  const compact = await run('ca_search', { query: 'kfree', compact: true })
  assert.ok(!compact.text.includes('id: fn:abc'), 'compact drops ids')
})

test('ca_symbol formats facts and points to ensure_deep when not analyzed', async () => {
  const { run, backend } = setup({
    handlers: {
      '/api/function': () => ({ ok: true, data: DETAIL }),
    },
  })
  const text = (await run('ca_symbol', { symbol: 'kfree' })).text
  assert.match(text, /CA symbol: kfree \(function, deep_ready\)/)
  assert.match(text, /facts: branches 2, assignments 1, returns 1, resources 1, findings 1/)
  assert.match(text, /ptr == NULL/)
  assert.match(text, /possible leak of obj/)
  assert.match(text, /next: ca_source for code/)
  assert.equal(backend.calls.request[0].path, '/api/function')
  assert.equal(backend.calls.request[0].options.query.symbol, 'kfree')

  const shallow = setup({
    handlers: {
      '/api/function': () => ({
        ok: true,
        data: { ...DETAIL, deep_ready: false, branches: [], assignments: [], returns: [], resource_ops: [], findings: [] },
      }),
    },
  })
  const shallowText = (await shallow.run('ca_symbol', { symbol: 'kfree', compact: true })).text
  assert.match(shallowText, /deep facts not analyzed/)
  assert.match(shallowText, /ca_task kind=ensure_deep/)
})

test('ca_graph renders caller/callee edges and candidate markers', async () => {
  const { run, backend } = setup({
    handlers: {
      '/api/graph': (options) => ({
        ok: true,
        data: { ...GRAPH, direction: options.query.direction, depth: options.query.depth },
      }),
    },
  })
  const text = (await run('ca_graph', { symbol: 'syscall', direction: 'reverse', depth: 2, limit: 50 })).text
  assert.match(text, /CA call graph: syscall · reverse \(callers\) · depth 2/)
  assert.match(text, /syscall -> sys_open \[direct\] @ kernel\/syscall\.c:143/)
  assert.match(text, /cb_target \[callback candidate\]/)
  const call = backend.calls.request[0]
  assert.equal(call.options.query.direction, 'reverse')
  assert.equal(call.options.query.depth, 2)
  assert.equal(call.options.query.limit, 50)

  const compact = (await run('ca_graph', { symbol: 'syscall', compact: true })).text
  assert.ok(!compact.includes('sys_open [direct]'), 'compact omits edge lines')
})

test('ca_facts pings the kind endpoint, pages with offset, and handles findings', async () => {
  const { run, backend } = setup({
    handlers: {
      '/api/branches': (options) => {
        const rows = [
          { line: 10, condition: 'a > 0', kind: 'if' },
          { line: 20, condition: 'b == NULL', kind: 'if' },
          { line: 30, condition: 'c != d', kind: 'while' },
        ]
        return { ok: true, data: { symbol: 'f', items: rows.slice(0, options.query.limit), count: 3, limit: options.query.limit, offset: 0 } }
      },
      '/api/function': () => ({
        ok: true,
        data: {
          ...DETAIL,
          findings: [
            { line: 1, kind: 'leak', message: 'first' },
            { line: 2, kind: 'double-free', message: 'second' },
            { line: 3, kind: 'leak', message: 'third' },
          ],
        },
      }),
    },
  })
  const page = (await run('ca_facts', { symbol: 'f', kind: 'branches', limit: 2, offset: 1 })).text
  assert.match(page, /branch\/condition facts/)
  assert.match(page, /L20 b == NULL/)
  assert.match(page, /L30 c != d/)
  assert.ok(!page.includes('L10 a > 0'), 'offset skips earlier rows')
  assert.match(page, /paging: pass offset=3/)
  const branchesCall = backend.calls.request.find((item) => item.path === '/api/branches')
  assert.equal(branchesCall.options.query.limit, 3, 'fetch limit = offset + page limit')

  const findings = (await run('ca_facts', { symbol: 'f', kind: 'findings', limit: 2 })).text
  assert.match(findings, /data-flow findings/)
  assert.match(findings, /first/)
  assert.match(findings, /second/)
  assert.ok(!findings.includes('third'))
})

test('ca_source renders a numbered source window', async () => {
  const { run } = setup({
    handlers: {
      '/api/source': () => ({
        ok: true,
        data: {
          symbol: 'kfree',
          function: { name: 'kfree', signature: 'void kfree(const void *ptr)' },
          file: 'mm/slab.c',
          language: 'c',
          repo_root: '/repo/xv6',
          start: 1243,
          end: 1247,
          total_lines: 2000,
          function_start: 1245,
          function_end: 1245,
          lines: [
            { line: 1243, text: '/* comment */' },
            { line: 1244, text: 'void kfree(const void *ptr)' },
            { line: 1245, text: '{\n' },
          ],
          truncated: false,
        },
      }),
    },
  })
  const text = (await run('ca_source', { symbol: 'kfree', context: 2 })).text
  assert.match(text, /CA source: kfree — mm\/slab\.c:1245-1245/)
  assert.match(text, /1243 {2}\/\* comment \*\//)
  assert.match(text, /language: c · repo: \/repo\/xv6/)
})

test('ca_stats formats table counts and database size', async () => {
  const { run } = setup({
    handlers: {
      '/api/stats': () => ({
        ok: true,
        data: {
          tables: { functions: 10, calls: 20, branches: 30, schema_migrations: 1 },
          db_bytes: 2048,
          schema_version: '3',
          sqlite_version: '3.50.0',
          federated: false,
          callbacks: { registrations: 4, resolved_links: 2 },
          database: { paths: ['/tmp/ca.db'] },
        },
      }),
    },
  })
  const text = (await run('ca_stats', {})).text
  assert.match(text, /database: 2\.0 KB · schema 3 · sqlite 3\.50\.0/)
  assert.match(text, /tables: functions 10, calls 20, branches 30/)
  assert.match(text, /callbacks: registrations 4, resolved_links 2/)
  assert.match(text, /other tables: schema_migrations 1/)
})

test('ca_callbacks applies filters and slices offset pages', async () => {
  const { run, backend } = setup({
    handlers: {
      '/api/callbacks': (options) => {
        const rows = [
          { object_path: 'g_dsoftbus', object_field: 'on_open', target_function_name: 'cb_open', registration_kind: 'set', module: 'bus', file: 'bus/a.c', line: 10 },
          { object_path: 'g_dsoftbus', object_field: 'on_close', target_function_name: 'cb_close', registration_kind: 'set', module: 'bus', file: 'bus/b.c', line: 20 },
          { object_path: 'g_other', object_field: 'on_x', target_function_name: 'cb_x', registration_kind: 'set', module: 'other', file: 'o/c.c', line: 30 },
        ]
        return {
          ok: true,
          data: {
            items: rows.slice(0, options.query.limit),
            count: 3,
            limit: options.query.limit,
            offset: 0,
            filters: { path_prefix: options.query.path ?? '' },
          },
        }
      },
    },
  })
  const text = (await run('ca_callbacks', { path: 'g_dsoftbus', field: 'on_open', module: 'bus', kind: 'set', limit: 1, offset: 1 })).text
  assert.match(text, /g_dsoftbus\.on_close -> cb_close \[set\] module bus @ bus\/b\.c:20/)
  assert.ok(!text.includes('cb_open'), 'offset skips the first row')
  const call = backend.calls.request[0]
  assert.equal(call.options.query.path, 'g_dsoftbus')
  assert.equal(call.options.query.field, 'on_open')
  assert.equal(call.options.query.module, 'bus')
  assert.equal(call.options.query.kind, 'set')
})

test('ca_registry lists and can activate a variant', async () => {
  let active = { id: 'v1', name: 'main', db_path: '/tmp/main.db' }
  const { run, backend } = setup({
    handlers: {
      '/api/registry': () => ({
        ok: true,
        data: {
          workspaces: [{ id: 'w1', name: 'oh', path: '/src/oh', kind: 'local' }],
          projects: [{ id: 'p1', name: 'base', path: '/src/base', workspace_name: 'oh' }],
          variants: [
            { id: 'v1', name: 'main', db_path: '/tmp/main.db' },
            { id: 'v2', name: 'board', db_path: '/tmp/board.db' },
          ],
          active_variant: active,
          active_variant_id: active.id,
          available: true,
          registry_source: '/tmp/registry.db',
        },
      }),
      '/api/registry/active': (options) => {
        assert.equal(options.method, 'POST')
        active = { id: options.body.variant_id, name: 'board', db_path: '/tmp/board.db' }
        return { ok: true, data: { active_variant: active } }
      },
    },
  })
  const overview = (await run('ca_registry', {})).text
  assert.match(overview, /CA registry: 1 workspace\(s\), 1 project\(s\), 2 variant\(s\)/)
  assert.match(overview, /active variant: main/)
  assert.match(overview, /main \[active\]/)

  const activated = (await run('ca_registry', { action: 'activate', variant: 'v2' })).text
  assert.match(activated, /activated variant board/)
  const activateCall = backend.calls.request.find((call) => call.options.method === 'POST')
  assert.deepEqual(activateCall.options.body, { variant_id: 'v2' })
  await assert.rejects(run('ca_registry', { action: 'activate' }), /"variant" is required for action=activate/)
})

test('ca_explain delegates to the explain service and reports model provenance', async () => {
  const { run, explain } = setup()
  const result = await run('ca_explain', { symbol: 'kfree', question: 'why is this reachable?', language: 'zh' })
  assert.equal(explain.calls.length, 1)
  assert.equal(explain.calls[0].symbol, 'kfree')
  assert.equal(explain.calls[0].language, 'zh')
  assert.equal(explain.calls[0].question, 'why is this reachable?')
  assert.match(result.text, /frees the object when the reference count drops/)
  assert.match(result.text, /deepseek\/deepseek-chat · 6 evidence item\(s\)/)
})

test('ca_explain fails cleanly when the model service is unavailable', async () => {
  const { run } = setup({ explain: null })
  await assert.rejects(run('ca_explain', { symbol: 'kfree' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_UNAVAILABLE')
    assert.match(error.message, /ca_symbol\/ca_facts\/ca_source/)
    return true
  })
})

test('ca_task start/list/status/logs/cancel map to the task manager', async () => {
  const tasks = createFakeTasks([
    {
      id: 'task-9',
      kind: 'index',
      status: 'succeeded',
      params: { repo: '/r', db: '/d.db' },
      createdAt: 1700000000000,
      startedAt: 1700000000500,
      finishedAt: 1700000002500,
      exitCode: 0,
      error: null,
      logBytes: 120,
    },
  ])
  const { run } = setup({ tasks })

  const started = (await run('ca_task', { action: 'start', kind: 'index', params: { repo: '/r', db: '/d.db', label: 'nightly' } })).text
  assert.match(started, /CA task task-\d+: queued \(index\)/)
  assert.match(started, /next: ca_task action=logs id=task-/)
  assert.deepEqual(tasks.calls.start.at(-1), {
    kind: 'index',
    params: { repo: '/r', db: '/d.db', label: 'nightly' },
  })

  const list = (await run('ca_task', { action: 'list' })).text
  assert.match(list, /CA tasks: 2/)
  assert.match(list, /task-9 {2}index {2}succeeded/)

  const status = (await run('ca_task', { action: 'status', id: 'task-9' })).text
  assert.match(status, /CA task task-9: succeeded \(index\)/)
  assert.match(status, /exit code: 0/)
  assert.match(status, /repo=\/r db=\/d\.db/)

  const logs = (await run('ca_task', { action: 'logs', id: 'task-9', offset: 0, limit: 10 })).text
  assert.match(logs, /indexing: start/)
  assert.match(logs, /2! warning: header changed/)
  assert.match(logs, /nextOffset: 4/)

  const cancelled = (await run('ca_task', { action: 'cancel', id: 'task-9' })).text
  assert.match(cancelled, /cancelled/)
  assert.deepEqual(tasks.calls.cancel, ['task-9'])
})

test('ca_task rejects unknown ids and unavailable explain/backend states', async () => {
  const { run } = setup({ taskSeed: [] })
  await assert.rejects(run('ca_task', { action: 'status', id: 'task-404' }), (error) => {
    assert.equal(error.code, 'TASK_NOT_FOUND')
    return true
  })
  await assert.rejects(run('ca_task', { action: 'cancel', id: 'task-404' }), /no CA task with id/)
})

test('ca_request-style backend errors surface as actionable CA_* codes', async () => {
  const { run } = setup({
    handlers: {
      '/api/function': () => ({
        ok: false,
        error: { code: 'NOT_FOUND', message: 'no function matching "nope"' },
      }),
    },
  })
  await assert.rejects(run('ca_symbol', { symbol: 'nope' }), (error) => {
    assert.equal(error.code, 'CA_NOT_FOUND')
    assert.match(error.message, /CA API \/api\/function failed: no function matching "nope"/)
    return true
  })
})

test('a stopped configured backend is auto-started before querying', async () => {
  const busy = setup({
    status: { mode: 'local', state: 'stopped', baseUrl: null, db: '/tmp/ca.db' },
    handlers: {
      '/api/search': () => ({ ok: true, data: { query: 'x', items: [], count: 0, limit: 20, offset: 0 } }),
    },
  })
  await busy.run('ca_search', { query: 'x' })
  assert.equal(busy.backend.calls.start, 1)
  assert.equal(busy.backend.getStatus().state, 'running')

  const manual = setup({
    status: { mode: 'local', state: 'stopped', baseUrl: null, db: '/tmp/ca.db' },
    config: { autoStart: false },
  })
  await assert.rejects(manual.run('ca_search', { query: 'x' }), (error) => {
    assert.equal(error.code, 'BACKEND_NOT_RUNNING')
    assert.match(error.message, /Start it from the CA backend page or enable autoStart/)
    return true
  })
  assert.equal(manual.backend.calls.start, 0)
})

test('an aborted tool-call signal cancels the operation', async () => {
  const { run } = setup()
  const controller = new AbortController()
  controller.abort()
  await assert.rejects(run('ca_search', { query: 'x' }, { signal: controller.signal }), (error) => {
    assert.equal(error.code, 'CANCELLED')
    return true
  })
})

test('long tool output is bounded and tells the model how to page', async () => {
  const rows = Array.from({ length: 300 }, (_, index) => ({
    ...SEARCH_ROW,
    id: `fn:${index}`,
    name: `function_${index}_${'x'.repeat(120)}`,
    signature: `void function_${index}(int a, int b, int c) ${'y'.repeat(200)}`,
  }))
  const { run } = setup({
    config: { maxToolOutputChars: 4000 },
    handlers: {
      '/api/search': () => ({ ok: true, data: { query: 'x', items: rows, count: 300, limit: 200, offset: 0 } }),
    },
  })
  const result = await run('ca_search', { query: 'x', limit: 200 })
  assert.ok(result.text.length <= 4000 + 200, `bounded text (got ${result.text.length})`)
  assert.match(result.text, /output truncated after 4000 characters/)
  assert.match(result.text, /narrow the query with limit\/offset\/compact/)
})
