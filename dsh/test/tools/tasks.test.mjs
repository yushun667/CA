/**
 * TaskManager tests: CLI argv mapping, state machine, concurrency, ring logs,
 * cancellation and backend auto-start. All services are fakes (no CA process).
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  TASK_KINDS,
  buildTaskCommand,
  createTaskManager,
} from '../../lib/host/tasks/index.js'
import { createFakeBackend, tick } from './helpers.mjs'

async function waitFor(predicate, { timeoutMs = 2000, intervalMs = 5 } = {}) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    const value = predicate()
    if (value) return value
    await tick(intervalMs)
  }
  throw new Error('timed out waiting for condition')
}

function expectTaskError(fn, code) {
  try {
    fn()
  } catch (error) {
    assert.equal(error.code, code, `expected ${code}, got ${error.code}: ${error.message}`)
    assert.ok(typeof error.message === 'string' && error.message.length > 10)
    return error
  }
  assert.fail(`expected TaskError ${code}`)
}

function makeManager(overrides = {}) {
  const backend = createFakeBackend({
    status: overrides.status ?? {
      mode: 'local',
      state: 'running',
      baseUrl: 'http://127.0.0.1:8765',
      db: '/tmp/ca.db',
    },
    config: overrides.backendConfig ?? {},
    autoExits: overrides.autoExits ?? false,
  })
  const manager = createTaskManager(
    {},
    {
      backend,
      config: {
        autoStart: overrides.autoStart ?? false,
        maxConcurrentTasks: overrides.maxConcurrentTasks ?? 2,
        ...overrides.config,
      },
    },
  )
  return { backend, manager }
}

// --------------------------------------------------------------------------- //
// argv mapping (src/ca/cli/main.py)
// --------------------------------------------------------------------------- //

test('buildTaskCommand maps index params to real ca CLI flags in argparse order', () => {
  const { args, params } = buildTaskCommand(
    'index',
    {
      repo: '/repo',
      db: '/tmp/x.db',
      compdb: '/repo/compile_commands.json',
      jobs: 8,
      source: 'compdb',
      buildCmd: 'make -j8',
      incremental: true,
      cacheDir: '/tmp/cache',
      clearCache: true,
      analyzeScope: 'all',
      headerDeclarations: false,
      resolveCallbacks: true,
      layer: 'base',
      recordScope: 'main_file',
      transportFilter: 'declarations',
      measureTransport: true,
    },
    {},
    {},
  )
  assert.deepEqual(args, [
    'index',
    '--repo',
    '/repo',
    '--db',
    '/tmp/x.db',
    '--compdb',
    '/repo/compile_commands.json',
    '--jobs',
    '8',
    '--source',
    'compdb',
    '--build-cmd',
    'make -j8',
    '--incremental',
    '--cache-dir',
    '/tmp/cache',
    '--clear-cache',
    '--analyze-scope',
    'all',
    '--no-header-declarations',
    '--resolve-callbacks',
    '--layer',
    'base',
    '--record-scope',
    'main_file',
    '--transport-filter',
    'declarations',
    '--measure-transport',
  ])
  assert.equal(params.repo, '/repo')
  assert.equal(params.jobs, 8)
})

test('snake_case spellings and string booleans are accepted', () => {
  const { args } = buildTaskCommand(
    'update',
    {
      repo: '/r',
      db: '/d.db',
      changed_files: ['kernel/a.c', 'include/b.h'],
      jobs: '4',
      dry_run: 'true',
      header_declarations: true,
    },
    {},
    {},
  )
  assert.deepEqual(args.slice(0, 6), ['update', '--repo', '/r', '--db', '/d.db', '--changed-files'])
  assert.ok(args.includes('kernel/a.c'))
  assert.ok(args.includes('--jobs'))
  assert.ok(args.includes('4'))
  assert.ok(args.includes('--dry-run'))
  assert.ok(args.includes('--header-declarations'))
})

test('ensure_deep splits a delimited symbols string and keeps flags stable', () => {
  const { args } = buildTaskCommand(
    'ensure_deep',
    { db: '/d.db', repo: '/r', compdb: '/c.json', symbols: 'foo bar,baz', jobs: 2, json: true },
    {},
    {},
  )
  assert.deepEqual(args, [
    'ensure-deep',
    '--db',
    '/d.db',
    '--repo',
    '/r',
    '--compdb',
    '/c.json',
    '--symbols',
    'foo',
    'bar',
    'baz',
    '--jobs',
    '2',
    '--json',
  ])
})

test('resolve_callbacks maps optional flags and kind aliases', () => {
  const { args } = buildTaskCommand(
    'resolve-callbacks',
    { db: '/d.db', max_tus: 10, batch_size: 2, no_finalize: true, json: true },
    {},
    {},
  )
  assert.deepEqual(args, [
    'resolve-callbacks',
    '--db',
    '/d.db',
    '--max-tus',
    '10',
    '--batch-size',
    '2',
    '--no-finalize',
    '--json',
  ])
})

test('configured repo/db/compdb fall back when params omit them', () => {
  const { args } = buildTaskCommand(
    'prewarm',
    {},
    {},
    { config: { repo: '/cfg/repo', db: '/cfg/db', compdb: '/cfg/compdb.json' } },
  )
  assert.deepEqual(args, [
    'prewarm',
    '--db',
    '/cfg/db',
    '--repo',
    '/cfg/repo',
    '--compdb',
    '/cfg/compdb.json',
  ])
})

test('parameter validation returns actionable stable codes', () => {
  expectTaskError(() => buildTaskCommand('nope', {}), 'UNKNOWN_TASK_KIND')
  const unknown = expectTaskError(() => buildTaskCommand('index', { repo: '/r', db: '/d', bogus: 1 }), 'INVALID_TASK_PARAMS')
  assert.match(unknown.message, /supported keys: .*analyzeScope/s)
  expectTaskError(() => buildTaskCommand('index', { repo: '/r', db: '/d', jobs: 0 }), 'INVALID_TASK_PARAMS')
  expectTaskError(() => buildTaskCommand('index', { repo: '/r' }), 'MISSING_TASK_PARAM')
  expectTaskError(() => buildTaskCommand('update', { repo: '/r', db: '/d' }), 'MISSING_TASK_PARAM')
  expectTaskError(
    () => buildTaskCommand('prewarm', { db: '/d', repo: '/r', compdb: '/c', symbols: 42 }),
    'INVALID_TASK_PARAMS',
  )
  expectTaskError(
    () => buildTaskCommand('index', { repo: '/r', db: '/d', source: 'nope' }),
    'INVALID_TASK_PARAMS',
  )
})

test('TASK_KINDS lists the supported CA CLI long jobs', () => {
  assert.deepEqual([...TASK_KINDS].sort(), [
    'ensure_deep',
    'index',
    'prewarm',
    'resolve_callbacks',
    'update',
    'watch',
  ])
})

// --------------------------------------------------------------------------- //
// Lifecycle
// --------------------------------------------------------------------------- //

test('state machine respects the concurrency limit and records logs', async (t) => {
  const { backend, manager } = makeManager({ maxConcurrentTasks: 2 })
  t.after(() => manager.dispose())

  const one = manager.start('index', { repo: '/r', db: '/d.db' })
  const two = manager.start('prewarm', { db: '/d.db', repo: '/r', compdb: '/c.json' })
  const three = manager.start('ensure_deep', {
    db: '/d.db',
    repo: '/r',
    compdb: '/c.json',
    symbols: ['fn_a'],
  })
  assert.equal(one.status, 'queued')
  assert.deepEqual(Object.keys(one), [
    'id',
    'kind',
    'status',
    'params',
    'createdAt',
    'startedAt',
    'finishedAt',
    'exitCode',
    'error',
    'logBytes',
  ])

  await waitFor(() => backend.calls.execCa.length === 2)
  assert.equal(manager.get(one.id).status, 'running')
  assert.equal(manager.get(two.id).status, 'running')
  assert.equal(manager.get(three.id).status, 'queued')
  assert.equal(backend.pending.length, 2)

  const first = backend.pending[0]
  first.options.onStdout('line one\nline two\n')
  first.options.onStderr('warning: header changed\n')
  const page = manager.logs(one.id)
  assert.deepEqual(
    page.lines.map((line) => [line.n, line.stream, line.text]),
    [
      [1, 'stdout', 'line one'],
      [2, 'stdout', 'line two'],
      [3, 'stderr', 'warning: header changed'],
    ],
  )
  assert.equal(page.total, 3)
  assert.equal(page.nextOffset, 4)

  first.resolve({ exitCode: 0, signal: null })
  const finished = await manager.wait(one.id)
  assert.equal(finished.status, 'succeeded')
  assert.equal(finished.exitCode, 0)
  assert.equal(manager.get(one.id).error, null)

  await waitFor(() => manager.get(three.id).status === 'running')
  assert.equal(backend.calls.execCa.length, 3)

  const ensureEntry = backend.pending.find((entry) => entry.spec[0] === 'ensure-deep')
  assert.ok(ensureEntry, 'ensure-deep child process started')
  const cancelling = manager.cancel(three.id)
  assert.equal(cancelling.status, 'running')
  assert.equal(ensureEntry.options.signal.aborted, true)
  const cancelled = await manager.wait(three.id)
  assert.equal(cancelled.status, 'cancelled')
  assert.equal(cancelled.error.code, 'CANCELLED')

  const prewarmEntry = backend.pending.find((entry) => entry.spec[0] === 'prewarm')
  prewarmEntry.options.onStderr('error: cannot open database\n')
  prewarmEntry.resolve({ exitCode: 3, signal: null })
  const failed = await manager.wait(two.id)
  assert.equal(failed.status, 'failed')
  assert.equal(failed.exitCode, 3)
  assert.equal(failed.error.code, 'CA_CLI_EXIT')
  assert.match(JSON.stringify(failed.error.details.stderrTail), /cannot open database/)

  assert.deepEqual(manager.counts(), {
    queued: 0,
    running: 0,
    succeeded: 1,
    failed: 1,
    cancelled: 1,
  })
})

test('logs paginate by absolute line number and report null nextOffset when caught up', async (t) => {
  const { backend, manager } = makeManager()
  t.after(() => manager.dispose())
  const task = manager.start('index', { repo: '/r', db: '/d.db' })
  await waitFor(() => backend.pending.length === 1)
  const entry = backend.pending[0]
  entry.options.onStdout('a\nb\nc\nd\ne\n')

  let page = manager.logs(task.id, { offset: 0, limit: 2 })
  assert.deepEqual(page.lines.map((line) => line.n), [1, 2])
  assert.equal(page.nextOffset, 3)
  page = manager.logs(task.id, { offset: 3, limit: 2 })
  assert.deepEqual(page.lines.map((line) => line.n), [3, 4])
  page = manager.logs(task.id, { offset: 5, limit: 5 })
  assert.deepEqual(page.lines.map((line) => line.n), [5])
  assert.equal(page.nextOffset, 6, 'nextOffset advances past the last returned line')
  page = manager.logs(task.id, { offset: 6, limit: 5 })
  assert.deepEqual(page.lines, [])
  assert.equal(page.nextOffset, null, 'caught up => null so pollers keep offset')
  assert.equal(page.total, 5)
})

test('ring log trims oldest lines and flags clipped offsets', async (t) => {
  const { backend, manager } = makeManager({ config: { maxTaskLogBytes: 64 * 1024 } })
  t.after(() => manager.dispose())
  const task = manager.start('index', { repo: '/r', db: '/d.db' })
  await waitFor(() => backend.pending.length === 1)
  const entry = backend.pending[0]
  const chunk = `${'x'.repeat(4096)}\n`
  for (let index = 0; index < 30; index += 1) entry.options.onStdout(chunk)

  const fromOldest = manager.logs(task.id, { offset: 0, limit: 2 })
  assert.equal(fromOldest.total, 30)
  assert.ok(fromOldest.droppedLines > 0, 'early lines were dropped from the ring buffer')
  assert.equal(fromOldest.clipped, false)
  assert.equal(fromOldest.offset, fromOldest.lines[0].n)
  assert.ok(fromOldest.offset > 1)

  const stale = manager.logs(task.id, { offset: 1, limit: 2 })
  assert.equal(stale.clipped, true)
  assert.equal(stale.offset, fromOldest.offset)
})

test('cancel works for queued tasks and terminal tasks are not cancellable', async (t) => {
  const { backend, manager } = makeManager({ maxConcurrentTasks: 1 })
  t.after(() => manager.dispose())
  const running = manager.start('index', { repo: '/r', db: '/d.db' })
  const queued = manager.start('index', { repo: '/r', db: '/d2.db' })
  await waitFor(() => backend.pending.length === 1)
  assert.equal(backend.calls.execCa.length, 1)

  const cancelled = manager.cancel(queued.id)
  assert.equal(cancelled.status, 'cancelled')
  assert.equal(cancelled.error.code, 'CANCELLED')
  assert.ok(Number.isFinite(cancelled.finishedAt))
  await tick(20)
  assert.equal(backend.calls.execCa.length, 1, 'cancelled queued task never spawns')

  expectTaskError(() => manager.cancel(queued.id), 'TASK_NOT_CANCELLABLE')
  expectTaskError(() => manager.get(undefined), 'INVALID_TASK_ID')
  assert.equal(manager.get('task-999'), null)

  backend.pending[0].resolve({ exitCode: 0, signal: null })
  await manager.wait(running.id)
})

test('onChange publishes snapshots and unsubscribes cleanly', async (t) => {
  const { backend, manager } = makeManager()
  t.after(() => manager.dispose())
  const events = []
  const off = manager.onChange((task, reason) => events.push(`${task.status}:${reason}`))
  const task = manager.start('index', { repo: '/r', db: '/d.db' })
  await waitFor(() => backend.pending.length === 1)
  backend.pending[0].resolve({ exitCode: 0, signal: null })
  await manager.wait(task.id)
  assert.deepEqual(events, ['queued:queued', 'running:started', 'succeeded:completed'])
  off()
  manager.start('index', { repo: '/r', db: '/d2.db' })
  assert.equal(events.length, 3, 'unsubscribed listener stops receiving events')
})

test('dispose cancels running tasks and rejects new starts', async (t) => {
  const { backend, manager } = makeManager()
  const task = manager.start('index', { repo: '/r', db: '/d.db' })
  await waitFor(() => backend.pending.length === 1)
  manager.dispose()
  assert.equal(backend.pending[0].options.signal.aborted, true)
  const finished = await manager.wait(task.id)
  assert.equal(finished.status, 'cancelled')
  expectTaskError(() => manager.start('index', { repo: '/r', db: '/d.db' }), 'TASK_MANAGER_DISPOSED')
  t.after(() => manager.dispose())
})

test('a configured stopped backend is started before the CLI runs', async (t) => {
  const { backend, manager } = makeManager({
    status: { mode: 'local', state: 'stopped', baseUrl: null, db: '/tmp/ca.db' },
    autoStart: true,
  })
  t.after(() => manager.dispose())
  const task = manager.start('index', { repo: '/r', db: '/d.db' })
  await waitFor(() => backend.pending.length === 1)
  assert.equal(backend.calls.start, 1)
  backend.pending[0].resolve({ exitCode: 0, signal: null })
  assert.equal((await manager.wait(task.id)).status, 'succeeded')
})

test('waits for an already-starting backend instead of starting it twice', async (t) => {
  let state = 'starting'
  const backend = createFakeBackend({
    status: () => ({
      mode: 'local',
      state,
      baseUrl: state === 'running' ? 'http://127.0.0.1:8765' : null,
      db: '/tmp/ca.db',
    }),
  })
  const manager = createTaskManager({}, { backend, config: { autoStart: true } })
  t.after(() => manager.dispose())
  setTimeout(() => {
    state = 'running'
  }, 30)
  const task = manager.start('index', { repo: '/r', db: '/d.db' })
  await waitFor(() => backend.pending.length === 1, { timeoutMs: 3000 })
  assert.equal(backend.calls.start, 0, 'existing start is not duplicated')
  backend.pending[0].resolve({ exitCode: 0, signal: null })
  assert.equal((await manager.wait(task.id)).status, 'succeeded')
})

test('an unconfigured backend fails fast with an actionable message', async (t) => {
  const { backend, manager } = makeManager({
    status: { mode: 'unconfigured', state: 'stopped', baseUrl: null, db: null },
  })
  t.after(() => manager.dispose())
  const task = manager.start('index', { repo: '/r', db: '/d.db' })
  const finished = await manager.wait(task.id)
  assert.equal(finished.status, 'failed')
  assert.equal(finished.error.code, 'BACKEND_UNCONFIGURED')
  assert.match(finished.error.message, /CA backend page/)
  assert.equal(backend.calls.execCa.length, 0)
  assert.equal(backend.calls.start, 0)
})

test('execCa receipt errors are normalized into failed tasks with stderr tail', async (t) => {
  const backend = createFakeBackend({
    config: {},
    status: { mode: 'local', state: 'running', baseUrl: 'http://127.0.0.1:8765', db: '/tmp/ca.db' },
  })
  backend.execCa = async () => {
    const error = new Error('spawn ca ENOENT')
    error.code = 'SPAWN_FAILED'
    throw error
  }
  const manager = createTaskManager({}, { backend, config: { autoStart: false } })
  t.after(() => manager.dispose())
  const task = manager.start('prewarm', { db: '/d', repo: '/r', compdb: '/c' })
  const finished = await manager.wait(task.id)
  assert.equal(finished.status, 'failed')
  assert.equal(finished.error.code, 'SPAWN_FAILED')
  assert.match(finished.error.message, /spawn ca ENOENT/)
})
