/**
 * Tests for the Host AI explanation service (`dsh/lib/host/explain/index.js`).
 *
 * Everything runs against fake Cordis/LLM/backend objects: no real model call and
 * no CA process. The assertions pin the behaviour the RPC layer and the
 * `ca_explain` tool rely on: bounded read-only fact collection, evidence-numbered
 * prompt construction, text-delta aggregation, finish-chunk error mapping, abort
 * and timeout handling, and concurrency limiting.
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  createExplainService,
  ExplainError,
  collectExplainFacts,
  buildExplainPrompt,
  DEFAULT_LIMITS,
  DEFAULT_EXPLAIN_OPTIONS,
} from '../../lib/host/explain/index.js'

const FUNCTION = {
  id: 'fn:do_open',
  tu_id: 'tu:drivers/core.c',
  name: 'do_open',
  qualified_name: 'driver::do_open',
  usr: 'c:@F@do_open',
  kind: 'function',
  file: 'drivers/core.c',
  line: 10,
  col: 1,
  end_line: 42,
  end_col: 2,
  is_definition: true,
  return_type: 'int',
  signature: 'static int do_open(struct file *f, int flags)',
  display_name: 'do_open',
  storage_class: 'static',
  linkage: 'internal',
  is_static: true,
  is_inline: false,
  is_variadic: false,
  parent_id: '',
  params: '[{"name":"f","type":"struct file *"},{"name":"flags","type":"int"}]',
}

const SOURCE_LINES = []
for (let line = 10; line <= 42; line += 1) {
  let text = `    /* line ${line} */`
  if (line === 10) text = 'static int do_open(struct file *f, int flags)'
  if (line === 15) text = '    if (!(flags & O_NONBLOCK)) {'
  if (line === 20) text = '        mutex_lock(&drv->lock);'
  if (line === 42) text = '}'
  SOURCE_LINES.push({ line, text })
}

function detailPayload() {
  return {
    function: { ...FUNCTION },
    deep_ready: true,
    shard: null,
    branches: [
      {
        id: 'br1',
        function_id: 'fn:do_open',
        line: 15,
        kind: 'if',
        condition: '!(flags & O_NONBLOCK)',
        var_names: 'flags',
        true_block: 1,
        false_block: 2,
        is_negated: 1,
        case_value: null,
      },
    ],
    assignments: [
      {
        id: 'as1',
        line: 14,
        lhs: 'ret',
        rhs: '-EINVAL',
        operator: '=',
        is_decl_init: 1,
        type_spelling: 'int',
        var_names: 'ret',
      },
    ],
    returns: [{ id: 're1', line: 40, expr: 'ret', is_implicit: 0, var_names: 'ret' }],
    resource_ops: [
      {
        id: 'ro1',
        line: 20,
        op: 'acquire',
        resource_kind: 'lock',
        api_name: 'mutex_lock',
        target: '&drv->lock',
        target_vars: 'drv',
        matcher: 'mutex_lock',
      },
    ],
    findings: [
      {
        id: 'fi1',
        kind: 'resource-leak',
        severity: 'warning',
        message: 'lock not released on error path',
        resource_kind: 'lock',
        line: 22,
      },
    ],
  }
}

function sourcePayload() {
  return {
    symbol: 'do_open',
    function: FUNCTION,
    file: 'drivers/core.c',
    language: 'c',
    repo_root: '/repo',
    path: '/repo/drivers/core.c',
    start: 10,
    end: 42,
    total_lines: 120,
    function_start: 10,
    function_end: 42,
    context: 0,
    truncated: false,
    lines: SOURCE_LINES,
    shard: null,
  }
}

function callersPayload() {
  return {
    symbol: 'do_open',
    items: [
      {
        caller_id: 'fn:probe',
        caller_name: 'probe',
        caller_file: 'drivers/probe.c',
        caller_line: 88,
        callee_name: 'do_open',
        call_file: 'drivers/probe.c',
        call_line: 90,
        is_resolved: 1,
        kind: 'call',
        source: 'call',
        candidate: 0,
        reason: '',
      },
    ],
    count: 1,
    limit: 30,
    offset: 0,
  }
}

function calleesPayload() {
  return {
    symbol: 'do_open',
    items: [
      {
        callee_id: 'fn:mutex_lock',
        callee_name: 'mutex_lock',
        callee_file: 'kernel/lock.c',
        callee_line: 5,
        call_file: 'drivers/core.c',
        call_line: 20,
        is_resolved: 1,
        kind: 'call',
        source: 'call',
        candidate: 0,
        reason: '',
      },
    ],
    count: 1,
    limit: 30,
    offset: 0,
  }
}

function graphPayload() {
  return {
    root: { id: 'fn:do_open', name: 'do_open', file: 'drivers/core.c', line: 10 },
    direction: 'both',
    depth: 1,
    nodes: [
      { id: 'fn:do_open', name: 'do_open', file: 'drivers/core.c', line: 10, depth: 0, direction: 'root' },
      { id: 'fn:mutex_lock', name: 'mutex_lock', file: 'kernel/lock.c', line: 5, depth: 1, direction: 'forward' },
      { id: 'fn:probe', name: 'probe', file: 'drivers/probe.c', line: 88, depth: 1, direction: 'reverse' },
    ],
    edges: [
      {
        call_id: 'call:1',
        caller_id: 'fn:do_open',
        caller_name: 'do_open',
        caller_file: 'drivers/core.c',
        caller_line: 10,
        callee_id: 'fn:mutex_lock',
        callee_name: 'mutex_lock',
        callee_file: 'kernel/lock.c',
        callee_line: 5,
        call_file: 'drivers/core.c',
        call_line: 20,
        kind: 'call',
        is_resolved: 1,
        source: 'call',
        candidate: 0,
        reason: '',
        depth: 1,
        direction: 'forward',
      },
      {
        call_id: 'call:2',
        caller_id: 'fn:probe',
        caller_name: 'probe',
        caller_file: 'drivers/probe.c',
        caller_line: 88,
        callee_id: 'fn:do_open',
        callee_name: 'do_open',
        callee_file: 'drivers/core.c',
        callee_line: 10,
        call_file: 'drivers/probe.c',
        call_line: 90,
        kind: 'call',
        is_resolved: 1,
        source: 'call',
        candidate: 0,
        reason: '',
        depth: 1,
        direction: 'reverse',
      },
    ],
    cycles: [],
    truncated: false,
    found: true,
    stats: { nodes: 3, edges: 2 },
  }
}

function defaultResponse(pathname) {
  if (pathname.startsWith('/api/function/')) return { ok: true, data: detailPayload() }
  if (pathname === '/api/source') return { ok: true, data: sourcePayload() }
  if (pathname === '/api/callers') return { ok: true, data: callersPayload() }
  if (pathname === '/api/callees') return { ok: true, data: calleesPayload() }
  if (pathname === '/api/graph') return { ok: true, data: graphPayload() }
  return { ok: false, error: { code: 'UNKNOWN_ENDPOINT', message: `unexpected path ${pathname}` } }
}

/**
 * Fake BackendManager. `overrides` is keyed by the endpoint family
 * (`function`, `source`, `callers`, `callees`, `graph`) and may be an envelope,
 * a thrown error factory, or a function returning either.
 */
function makeBackend(overrides = {}) {
  const calls = []
  const backend = {
    calls,
    async request(pathname, options = {}) {
      calls.push({ pathname, options })
      const key = pathname.startsWith('/api/function/') ? 'function' : pathname.replace('/api/', '')
      if (Object.prototype.hasOwnProperty.call(overrides, key)) {
        const override = overrides[key]
        if (typeof override === 'function') return override(pathname, options)
        if (override instanceof Error) throw override
        return override
      }
      return defaultResponse(pathname)
    },
  }
  return backend
}

/** Fake `ctx.llm.stream`: `produce` returns an array or an async iterable. */
function makeLlm(produce) {
  const calls = []
  return {
    calls,
    stream(options) {
      calls.push(options)
      return {
        async *[Symbol.asyncIterator]() {
          for await (const chunk of produce(options)) yield chunk
        },
      }
    },
  }
}

const DEFAULT_CHUNKS = [
  { type: 'block-start', index: 0, blockType: 'text' },
  { type: 'text-delta', index: 0, text: '## Purpose\n' },
  { type: 'text-delta', index: 0, text: 'Opens a device and validates flags [E1].' },
  { type: 'usage', usage: { inputTokens: 120, outputTokens: 24, totalTokens: 144 } },
  { type: 'finish', reason: { kind: 'stop' } },
]

/** Host context with direct service properties (the normal injected case). */
function makeCtx({ llm = makeLlm(() => DEFAULT_CHUNKS), selection = { provider: 'deepseek', model: 'deepseek-chat', reasoningEffort: 'high' } } = {}) {
  const agentDefaultModel = { currentSelection: () => selection }
  return { ctx: { llm, agentDefaultModel }, llm, agentDefaultModel }
}

async function waitUntil(predicate, timeoutMs = 1000) {
  const started = Date.now()
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error('waitUntil timed out')
    await new Promise((resolve) => setImmediate(resolve))
  }
}

test('collects bounded read-only facts and aggregates text-deltas', async () => {
  const backend = makeBackend()
  const { ctx, llm } = makeCtx()
  const service = createExplainService(ctx, { backend })

  const result = await service.explain({ symbol: 'do_open', language: 'en' })

  assert.equal(result.symbol, 'do_open')
  assert.equal(result.text, '## Purpose\nOpens a device and validates flags [E1].')
  assert.deepEqual(result.model, { provider: 'deepseek', model: 'deepseek-chat' })
  assert.deepEqual(result.usage, { inputTokens: 120, outputTokens: 24, totalTokens: 144 })
  assert.ok(result.evidenceCount >= 10, `expected >= 10 evidence items, got ${result.evidenceCount}`)

  const paths = backend.calls.map((call) => call.pathname)
  assert.equal(paths[0], '/api/function/do_open')
  for (const expected of ['/api/source', '/api/callers', '/api/callees', '/api/graph']) {
    assert.ok(paths.includes(expected), `expected a request to ${expected}`)
  }
  const requestFor = (pathname) => backend.calls.find((call) => call.pathname === pathname)
  assert.deepEqual(requestFor('/api/source').options.query, { symbol: 'do_open', context: 0 })
  assert.deepEqual(requestFor('/api/callers').options.query, { symbol: 'do_open', limit: 30 })
  assert.deepEqual(requestFor('/api/callees').options.query, { symbol: 'do_open', limit: 30 })
  assert.deepEqual(requestFor('/api/graph').options.query, {
    symbol: 'do_open',
    direction: 'both',
    depth: 1,
    limit: 200,
  })

  assert.equal(llm.calls.length, 1)
  const options = llm.calls[0]
  assert.equal(options.provider, 'deepseek')
  assert.equal(options.model, 'deepseek-chat')
  assert.equal(options.temperature, 0.2)
  assert.equal(options.maxTokens, 2000)
  assert.ok(options.signal instanceof AbortSignal)
  assert.equal(options.messages.length, 1)
  assert.equal(options.messages[0].role, 'user')
  assert.equal(options.messages[0].content.length, 1)
  assert.equal(options.messages[0].content[0].type, 'text')

  const user = options.messages[0].content[0].text
  assert.match(user, /\[E1\] function driver::do_open/)
  assert.match(user, /drivers\/core\.c:15/)
  assert.ok(user.includes('!(flags & O_NONBLOCK)'))
  assert.match(user, /mutex_lock/)
  assert.match(user, /lock not released on error path/)
  assert.match(user, /caller probe at drivers\/probe\.c:88/)
  assert.match(user, /this function calls mutex_lock/)
  assert.match(user, /call graph \(direction=both, depth=1\)/)
  assert.match(user, /Language: English/)
  assert.match(options.system, /Use ONLY the provided evidence/)
})

test('honours zh language and forwards the optional question', async () => {
  const backend = makeBackend()
  const { ctx, llm } = makeCtx()
  const service = createExplainService(ctx, { backend })

  await service.explain({ symbol: 'do_open', language: 'zh-CN', question: '  what happens on the error path?  ' })

  const user = llm.calls[0].messages[0].content[0].text
  assert.match(user, /简体中文/)
  assert.match(user, /The developer also asked: what happens on the error path\?/)
})

test('degrades when optional fact endpoints fail instead of failing the explanation', async () => {
  const backend = makeBackend({
    source: { ok: false, error: { code: 'NOT_FOUND', message: 'no source location recorded' } },
    callers: new Error('backend transport exploded'),
    callees: { ok: false, error: { code: 'DB_ERROR', message: 'shard is busy' } },
  })
  const { ctx, llm } = makeCtx()
  const service = createExplainService(ctx, { backend })

  const result = await service.explain({ symbol: 'do_open', language: 'en' })
  assert.ok(result.text.length > 0)

  const user = llm.calls[0].messages[0].content[0].text
  assert.match(user, /## Missing or unavailable evidence/)
  assert.match(user, /source window unavailable/)
  assert.match(user, /caller list unavailable/)
  assert.match(user, /callee list unavailable/)
  assert.match(user, /no source window is available/)
  assert.match(user, /this function calls|call graph/)
})

test('maps a CA NOT_FOUND envelope to EXPLAIN_NOT_FOUND without calling the model', async () => {
  const backend = makeBackend({
    function: { ok: false, error: { code: 'NOT_FOUND', message: "no function matching 'nope'" } },
  })
  const { ctx, llm } = makeCtx()
  const service = createExplainService(ctx, { backend })

  await assert.rejects(service.explain({ symbol: 'nope' }), (error) => {
    assert.ok(error instanceof ExplainError)
    assert.equal(error.code, 'EXPLAIN_NOT_FOUND')
    assert.match(error.message, /no function matching/)
    return true
  })
  assert.equal(llm.calls.length, 0)
  assert.equal(backend.calls.length, 1)
})

test('maps a terminal finish error chunk to EXPLAIN_MODEL_ERROR', async () => {
  const llm = makeLlm(() => [
    { type: 'text-delta', index: 0, text: 'partial answer' },
    {
      type: 'finish',
      reason: { kind: 'error', failure: { message: 'rate limited by provider', code: 'RATE_LIMITED', status: 429 } },
    },
  ])
  const service = createExplainService(makeCtx({ llm }).ctx, { backend: makeBackend() })

  await assert.rejects(service.explain({ symbol: 'do_open' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_MODEL_ERROR')
    assert.match(error.message, /rate limited by provider/)
    assert.equal(error.details.providerCode, 'RATE_LIMITED')
    assert.equal(error.details.status, 429)
    assert.ok(error instanceof Error)
    return true
  })
})

test('maps a terminal aborted finish chunk to EXPLAIN_ABORTED', async () => {
  const llm = makeLlm(() => [
    { type: 'finish', reason: { kind: 'aborted', failure: { message: 'aborted by adapter', code: 'ABORTED' } } },
  ])
  const service = createExplainService(makeCtx({ llm }).ctx, { backend: makeBackend() })

  await assert.rejects(service.explain({ symbol: 'do_open' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_ABORTED')
    return true
  })
})

test('rejects an already-aborted caller signal before touching the backend', async () => {
  const backend = makeBackend()
  const { ctx } = makeCtx()
  const service = createExplainService(ctx, { backend })
  const controller = new AbortController()
  controller.abort()

  await assert.rejects(service.explain({ symbol: 'do_open', signal: controller.signal }), (error) => {
    assert.equal(error.code, 'EXPLAIN_ABORTED')
    return true
  })
  assert.equal(backend.calls.length, 0)
})

test('aborts mid-stream when the caller signal fires', async () => {
  const controller = new AbortController()
  const llm = makeLlm(async function* () {
    yield { type: 'text-delta', index: 0, text: 'partial ' }
    controller.abort()
    yield { type: 'text-delta', index: 0, text: 'late text' }
  })
  const service = createExplainService(makeCtx({ llm }).ctx, { backend: makeBackend() })

  await assert.rejects(service.explain({ symbol: 'do_open', signal: controller.signal }), (error) => {
    assert.equal(error.code, 'EXPLAIN_ABORTED')
    return true
  })
})

test('times out a slow model call with EXPLAIN_TIMEOUT', async () => {
  const llm = makeLlm(async function* () {
    await new Promise((resolve) => setTimeout(resolve, 60))
    yield { type: 'text-delta', index: 0, text: 'too late' }
  })
  const service = createExplainService(makeCtx({ llm }).ctx, {
    backend: makeBackend(),
    config: { timeoutMs: 20 },
  })

  await assert.rejects(service.explain({ symbol: 'do_open' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_TIMEOUT')
    assert.match(error.message, /timed out/)
    return true
  })
})

test('caps fact lists, source lines and the total prompt size', async () => {
  const huge = {
    function: { ...FUNCTION },
    deep_ready: false,
    branches: Array.from({ length: 500 }, (_, index) => ({
      id: `br${index}`,
      line: 15 + index,
      kind: 'if',
      condition: `condition_${index}`,
      var_names: 'flags',
    })),
    assignments: Array.from({ length: 500 }, (_, index) => ({ id: `as${index}`, line: 20, lhs: `lhs_${index}`, rhs: 'value' })),
    returns: Array.from({ length: 500 }, (_, index) => ({ id: `re${index}`, line: 30, expr: `ret_${index}` })),
    resource_ops: Array.from({ length: 500 }, (_, index) => ({ id: `ro${index}`, line: 25, op: 'acquire', api_name: `api_${index}` })),
    findings: Array.from({ length: 500 }, (_, index) => ({ id: `fi${index}`, kind: 'finding', message: `finding message ${index}` })),
  }
  const manySourceLines = Array.from({ length: 400 }, (_, index) => ({
    line: index + 1,
    text: `    int variable_${index} = ${index}; /* padding padding padding padding */`,
  }))
  const manyEdges = Array.from({ length: 100 }, (_, index) => ({
    caller_name: `caller_${index}`,
    caller_file: 'drivers/many.c',
    caller_line: index + 1,
    callee_name: 'do_open',
    call_file: 'drivers/many.c',
    call_line: index + 10,
  }))
  const backend = makeBackend({
    function: { ok: true, data: huge },
    source: {
      ok: true,
      data: {
        ...sourcePayload(),
        function_start: 1,
        function_end: 400,
        start: 1,
        end: 400,
        lines: manySourceLines,
      },
    },
    callers: { ok: true, data: { items: manyEdges } },
    callees: { ok: true, data: { items: manyEdges } },
  })
  const { ctx, llm } = makeCtx()
  const service = createExplainService(ctx, { backend })

  const result = await service.explain({ symbol: 'do_open' })
  const user = llm.calls[0].messages[0].content[0].text

  assert.ok(user.length <= DEFAULT_EXPLAIN_OPTIONS.maxPromptChars, `prompt too long: ${user.length}`)
  assert.ok(result.evidenceCount <= 1 + 1 + 1 + 12 + 10 + 10 + 10 + 10 + 30 + 30 + 1)
  assert.ok(user.includes('truncated for this prompt'))
  // Bounded fact lists: only the first conditions can appear.
  assert.match(user, /condition_0\b/)
  assert.doesNotMatch(user, /condition_12\b/)
  assert.match(user, /caller_0\b/)
  assert.doesNotMatch(user, /caller_30\b/)
})

test('buildExplainPrompt enforces the explicit character budget', async () => {
  const backend = makeBackend()
  const facts = await collectExplainFacts({ backend, symbol: 'do_open' })
  facts.source.lines = Array.from({ length: 120 }, (_, index) => ({
    line: index + 1,
    text: `    int long_variable_${index} = ${index}; /* ${'x'.repeat(80)} */`,
  }))
  const prompt = buildExplainPrompt(facts, { language: 'en', maxPromptChars: 4000 })
  assert.ok(prompt.user.length <= 4000, `prompt not bounded: ${prompt.user.length}`)
  assert.ok(prompt.user.includes('[prompt truncated to fit the context budget]'))
  assert.equal(prompt.language, 'en')
  assert.ok(prompt.evidenceCount > 0)
})

test('uses the ctx.get() degradation path for optional services', async () => {
  const llm = makeLlm(() => DEFAULT_CHUNKS)
  const services = {
    llm,
    agentDefaultModel: { currentSelection: () => ({ provider: 'deepseek', model: 'deepseek-chat' }) },
  }
  const ctx = {
    get(name) {
      return services[name]
    },
  }
  const service = createExplainService(ctx, { backend: makeBackend() })
  const result = await service.explain({ symbol: 'do_open' })
  assert.deepEqual(result.model, { provider: 'deepseek', model: 'deepseek-chat' })
})

test('throws EXPLAIN_UNAVAILABLE when the native model capability is missing', async () => {
  const serviceWithoutLlm = createExplainService(
    { agentDefaultModel: { currentSelection: () => ({ provider: 'p', model: 'm' }) } },
    { backend: makeBackend() },
  )
  await assert.rejects(serviceWithoutLlm.explain({ symbol: 'do_open' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_UNAVAILABLE')
    assert.match(error.message, /llm/)
    return true
  })

  const llm = makeLlm(() => DEFAULT_CHUNKS)
  const serviceWithoutSelection = createExplainService(
    { llm, agentDefaultModel: { currentSelection: () => null } },
    { backend: makeBackend() },
  )
  await assert.rejects(serviceWithoutSelection.explain({ symbol: 'do_open' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_UNAVAILABLE')
    assert.match(error.message, /agentDefaultModel/)
    return true
  })
  assert.equal(llm.calls.length, 0)
})

test('rejects invalid arguments before any I/O', async () => {
  const backend = makeBackend()
  const service = createExplainService(makeCtx().ctx, { backend })
  await assert.rejects(service.explain({}), (error) => {
    assert.equal(error.code, 'EXPLAIN_INVALID_ARGUMENT')
    return true
  })
  await assert.rejects(service.explain({ symbol: 'x'.repeat(600) }), (error) => {
    assert.equal(error.code, 'EXPLAIN_INVALID_ARGUMENT')
    return true
  })
  assert.equal(backend.calls.length, 0)
})

test('limits concurrent explanations to maxConcurrent', async () => {
  const backend = makeBackend()
  let release
  const gate = new Promise((resolve) => {
    release = resolve
  })
  let active = 0
  let maxActive = 0
  const llm = makeLlm(async function* () {
    active += 1
    maxActive = Math.max(maxActive, active)
    await gate
    active -= 1
    yield { type: 'text-delta', index: 0, text: 'done' }
    yield { type: 'finish', reason: { kind: 'stop' } }
  })
  const service = createExplainService(makeCtx({ llm }).ctx, {
    backend,
    config: { maxConcurrent: 1 },
  })

  const first = service.explain({ symbol: 'alpha' })
  await waitUntil(() => llm.calls.length === 1)
  const second = service.explain({ symbol: 'beta' })
  await new Promise((resolve) => setImmediate(resolve))
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(llm.calls.length, 1)

  release()
  const [firstResult, secondResult] = await Promise.all([first, second])
  assert.equal(firstResult.text, 'done')
  assert.equal(secondResult.text, 'done')
  assert.equal(llm.calls.length, 2)
  assert.equal(maxActive, 1)
})

test('collectExplainFacts exposes bounded limits and notes', async () => {
  const backend = makeBackend()
  const facts = await collectExplainFacts({ backend, symbol: 'do_open' })
  assert.equal(facts.symbol, 'do_open')
  assert.ok(facts.detail.function)
  assert.equal(facts.detail.deep_ready, true)
  assert.equal(facts.detail.branches.length, 1)
  assert.equal(facts.source.lines.length, SOURCE_LINES.length)
  assert.equal(facts.callers.length, 1)
  assert.equal(facts.callees.length, 1)
  assert.equal(facts.graph.found, true)
  assert.equal(facts.graph.directEdges, 2)
  assert.equal(facts.graph.candidateEdges, 0)
  assert.equal(facts.notes.length, 0)
  assert.equal(DEFAULT_LIMITS.sourceLines, 200)
})
