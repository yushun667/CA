/**
 * Long-running CA CLI task manager.
 *
 * The unified `ca serve` backend has a durable job queue but no executor, so
 * index-maintenance work (index/update/ensure-deep/prewarm/resolve-callbacks/
 * watch) is executed as child processes through `backend.execCa` — locally, or
 * over SSH when the configured backend is remote. This module owns:
 *
 * - parameter validation and mapping to the real `ca` CLI argparse flags
 *   (src/ca/cli/main.py),
 * - a bounded queue with a fixed concurrency limit (default 2),
 * - the state machine `queued -> running -> succeeded|failed|cancelled`,
 * - a per-task ring log (default 1 MiB) with line-numbered pagination,
 * - cancellation and change notifications for the RPC/tool layers.
 *
 * Nothing here touches the network, DSH or Cordis; the backend manager is
 * injected, which keeps the manager unit-testable with a fake.
 */

import { BackendError, isAbortError, normalizeError } from '../backend/errors.js'
import {
  clampInt,
  createDeferred,
  createLineDecoder,
  isPlainObject,
} from '../backend/util.js'

/** Kinds accepted by `start()`; they all map to a real CA CLI subcommand. */
export const TASK_KINDS = Object.freeze([
  'index',
  'update',
  'ensure_deep',
  'prewarm',
  'resolve_callbacks',
  'watch',
])

const DEFAULT_CONCURRENCY = 2
const DEFAULT_MAX_LOG_BYTES = 1024 * 1024
const DEFAULT_LOG_LIMIT = 200
const MAX_LOG_LIMIT = 2000
const MAX_LINE_CHARS = 8192
const MAX_PARAMS_BYTES = 32 * 1024
const MAX_STDERR_TAIL_LINES = 20

/** Stable task-manager error with an RPC-friendly code. */
export class TaskError extends Error {
  /**
   * @param {string} code stable uppercase code, e.g. INVALID_TASK_PARAMS
   * @param {string} message actionable user-facing message
   * @param {Record<string, unknown>} [details] small JSON-serializable extras
   */
  constructor(code, message, details) {
    super(message)
    this.name = 'TaskError'
    this.code = code
    if (details !== undefined) this.details = details
  }
}

/** @param {unknown} error */
export function isTaskError(error) {
  return error instanceof TaskError
}

// --------------------------------------------------------------------------- //
// Parameter specifications (mirrors src/ca/cli/main.py argparse)
// --------------------------------------------------------------------------- //

/**
 * One CLI parameter definition.
 *
 * `type` drives both validation and argv rendering:
 *   string | int | number | bool | triBool | enum | stringList
 * `flag` is the argv prefix; triBool uses `flagTrue`/`flagFalse`.
 * `required` params may still be satisfied by configured defaults.
 *
 * @typedef {{
 *   type: string,
 *   flag?: string,
 *   flagTrue?: string,
 *   flagFalse?: string,
 *   required?: boolean,
 *   min?: number,
 *   max?: number,
 *   maxLength?: number,
 *   maxItems?: number,
 *   split?: boolean,
 *   values?: readonly string[],
 *   description?: string,
 * }} ParamSpec
 */

const PATH = {
  type: 'string',
  maxLength: 4096,
  description: 'absolute or ~-relative path',
}

/** @type {Record<string, {command: string, params: Record<string, ParamSpec>}>} */
const KIND_SPECS = {
  index: {
    command: 'index',
    params: {
      repo: { ...PATH, flag: '--repo', required: true },
      db: { ...PATH, flag: '--db', required: true },
      compdb: { ...PATH, flag: '--compdb' },
      jobs: { type: 'int', flag: '--jobs', min: 1, max: 256 },
      maxTus: { type: 'int', flag: '--max-tus', min: 1, max: 1000000 },
      source: {
        type: 'enum',
        flag: '--source',
        values: ['auto', 'compdb', 'capture', 'synthesized'],
      },
      buildCmd: { type: 'string', flag: '--build-cmd', maxLength: 8192 },
      incremental: { type: 'bool', flag: '--incremental' },
      cacheDir: { ...PATH, flag: '--cache-dir' },
      clearCache: { type: 'bool', flag: '--clear-cache' },
      analyzeScope: {
        type: 'enum',
        flag: '--analyze-scope',
        values: ['main', 'all', 'none'],
      },
      headerDeclarations: {
        type: 'triBool',
        flagTrue: '--header-declarations',
        flagFalse: '--no-header-declarations',
      },
      resolveCallbacks: { type: 'bool', flag: '--resolve-callbacks' },
      layer: { type: 'enum', flag: '--layer', values: ['deep', 'base'] },
      recordScope: {
        type: 'enum',
        flag: '--record-scope',
        values: ['auto', 'all', 'main_file'],
      },
      transportFilter: {
        type: 'enum',
        flag: '--transport-filter',
        values: ['none', 'declarations'],
      },
      measureTransport: { type: 'bool', flag: '--measure-transport' },
      workspacePath: { ...PATH, flag: '--workspace-path' },
      workspaceName: { type: 'string', flag: '--workspace-name', maxLength: 256 },
      projectPath: { ...PATH, flag: '--project-path' },
      projectName: { type: 'string', flag: '--project-name', maxLength: 256 },
      projectRepo: { type: 'string', flag: '--project-repo', maxLength: 256 },
      projectProduct: { type: 'string', flag: '--project-product', maxLength: 256 },
      projectBoard: { type: 'string', flag: '--project-board', maxLength: 256 },
      projectVersion: { type: 'string', flag: '--project-version', maxLength: 256 },
      variantName: { type: 'string', flag: '--variant-name', maxLength: 256 },
      variantProduct: { type: 'string', flag: '--variant-product', maxLength: 256 },
      variantBoard: { type: 'string', flag: '--variant-board', maxLength: 256 },
      variantVersion: { type: 'string', flag: '--variant-version', maxLength: 256 },
      variantDb: { ...PATH, flag: '--variant-db' },
    },
  },
  update: {
    command: 'update',
    params: {
      repo: { ...PATH, flag: '--repo', required: true },
      db: { ...PATH, flag: '--db', required: true },
      changedFiles: {
        type: 'stringList',
        flag: '--changed-files',
        required: true,
        maxItems: 5000,
        maxLength: 4096,
        description: 'repo-relative or absolute changed source/header/build files',
      },
      compdb: { ...PATH, flag: '--compdb' },
      jobs: { type: 'int', flag: '--jobs', min: 1, max: 256 },
      cacheDir: { ...PATH, flag: '--cache-dir' },
      dryRun: { type: 'bool', flag: '--dry-run' },
      analyzeScope: {
        type: 'enum',
        flag: '--analyze-scope',
        values: ['main', 'all', 'none'],
      },
      recordScope: {
        type: 'enum',
        flag: '--record-scope',
        values: ['auto', 'all', 'main_file'],
      },
      layer: { type: 'enum', flag: '--layer', values: ['deep', 'base'] },
      headerDeclarations: {
        type: 'triBool',
        flagTrue: '--header-declarations',
        flagFalse: '--no-header-declarations',
      },
    },
  },
  ensure_deep: {
    command: 'ensure-deep',
    params: {
      db: { ...PATH, flag: '--db', required: true },
      repo: { ...PATH, flag: '--repo', required: true },
      compdb: { ...PATH, flag: '--compdb', required: true },
      symbols: {
        type: 'stringList',
        flag: '--symbols',
        required: true,
        maxItems: 2000,
        maxLength: 1024,
        split: true,
      },
      jobs: { type: 'int', flag: '--jobs', min: 1, max: 64 },
      json: { type: 'bool', flag: '--json' },
    },
  },
  prewarm: {
    command: 'prewarm',
    params: {
      db: { ...PATH, flag: '--db', required: true },
      repo: { ...PATH, flag: '--repo', required: true },
      compdb: { ...PATH, flag: '--compdb', required: true },
      limit: { type: 'int', flag: '--limit', min: 1, max: 10000 },
      jobs: { type: 'int', flag: '--jobs', min: 1, max: 64 },
      symbols: {
        type: 'stringList',
        flag: '--symbols',
        maxItems: 2000,
        maxLength: 1024,
        split: true,
      },
    },
  },
  resolve_callbacks: {
    command: 'resolve-callbacks',
    params: {
      db: { ...PATH, flag: '--db', required: true },
      repo: { ...PATH, flag: '--repo' },
      compdb: { ...PATH, flag: '--compdb' },
      jobs: { type: 'int', flag: '--jobs', min: 1, max: 64 },
      maxTus: { type: 'int', flag: '--max-tus', min: 1, max: 1000000 },
      batchSize: { type: 'int', flag: '--batch-size', min: 1, max: 64 },
      noFinalize: { type: 'bool', flag: '--no-finalize' },
      json: { type: 'bool', flag: '--json' },
    },
  },
  watch: {
    command: 'watch',
    params: {
      repo: { ...PATH, flag: '--repo', required: true },
      db: { ...PATH, flag: '--db', required: true },
      compdb: { ...PATH, flag: '--compdb' },
      interval: { type: 'number', flag: '--interval', min: 0.1, max: 3600 },
      debounce: { type: 'number', flag: '--debounce', min: 0, max: 600 },
      jobs: { type: 'int', flag: '--jobs', min: 1, max: 256 },
      layer: { type: 'enum', flag: '--layer', values: ['deep', 'base'] },
      analyzeScope: {
        type: 'enum',
        flag: '--analyze-scope',
        values: ['main', 'all', 'none'],
      },
      recordScope: {
        type: 'enum',
        flag: '--record-scope',
        values: ['auto', 'all', 'main_file'],
      },
      cacheDir: { ...PATH, flag: '--cache-dir' },
      once: { type: 'bool', flag: '--once' },
      backend: {
        type: 'enum',
        flag: '--backend',
        values: ['auto', 'native', 'poll'],
      },
      json: { type: 'bool', flag: '--json' },
      changedFiles: {
        type: 'stringList',
        flag: '--changed-files',
        maxItems: 5000,
        maxLength: 4096,
      },
    },
  },
}

const KIND_ALIASES = {
  ensuredep: 'ensure_deep',
  resolvedeep: 'resolve_callbacks',
  resolvecallbacks: 'resolve_callbacks',
}

/** Canonical kinds map shared with tool descriptions. */
export const TASK_KIND_LIST = Object.freeze([...TASK_KINDS])

/** @param {unknown} kind @returns {string} canonical kind */
function resolveKind(kind) {
  if (typeof kind !== 'string' || kind.trim().length === 0) {
    throw new TaskError(
      'UNKNOWN_TASK_KIND',
      `task kind is required; one of: ${TASK_KINDS.join(', ')}`,
      { kinds: [...TASK_KINDS] },
    )
  }
  const raw = kind.trim().toLowerCase()
  const normalized = raw.replace(/[-\s]/g, '_')
  if (KIND_SPECS[normalized]) return normalized
  const compact = raw.replace(/[-\s_]/g, '')
  if (KIND_ALIASES[compact]) return KIND_ALIASES[compact]
  throw new TaskError(
    'UNKNOWN_TASK_KIND',
    `unknown task kind "${kind}"; expected one of: ${TASK_KINDS.join(', ')}`,
    { kinds: [...TASK_KINDS] },
  )
}

/** Lookup table from normalized param spelling to canonical spec key. */
function paramLookup(spec) {
  /** @type {Map<string, string>} */
  const lookup = new Map()
  const add = (spelling, canonical) => lookup.set(spelling.replace(/[_\s]/g, '').toLowerCase(), canonical)
  for (const key of Object.keys(spec.params)) {
    add(key, key)
  }
  // snake_case spellings are accepted in addition to the camelCase canonical
  // name; the CLI flag is derived from the spec, not from the caller spelling.
  for (const key of Object.keys(spec.params)) {
    const snake = key.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`)
    add(snake, key)
  }
  return lookup
}

/** @param {string} spelling @param {string} canonical */
function paramError(message) {
  return new TaskError('INVALID_TASK_PARAMS', message)
}

/**
 * Validate one parameter value against its spec.
 * @param {string} key canonical key
 * @param {ParamSpec} spec
 * @param {unknown} value
 */
function validateValue(key, spec, value) {
  const label = `params.${key}`
  switch (spec.type) {
    case 'string': {
      if (typeof value !== 'string') {
        throw paramError(`${label} must be a string`)
      }
      const text = value.trim()
      if (text.length === 0) return undefined
      if (/[\0\r\n]/.test(text)) {
        throw paramError(`${label} must not contain NUL or newline characters`)
      }
      if (spec.maxLength !== undefined && text.length > spec.maxLength) {
        throw paramError(`${label} is too long (maximum ${spec.maxLength} characters)`)
      }
      return text
    }
    case 'int': {
      const number =
        typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : NaN
      if (!Number.isFinite(number) || !Number.isInteger(number)) {
        throw paramError(`${label} must be an integer`)
      }
      const min = spec.min ?? 0
      const max = spec.max ?? Number.MAX_SAFE_INTEGER
      if (number < min || number > max) {
        throw paramError(`${label} must be between ${min} and ${max}`)
      }
      return number
    }
    case 'number': {
      const number = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : NaN
      if (!Number.isFinite(number)) throw paramError(`${label} must be a number`)
      const min = spec.min ?? Number.MIN_SAFE_INTEGER
      const max = spec.max ?? Number.MAX_SAFE_INTEGER
      if (number < min || number > max) {
        throw paramError(`${label} must be between ${min} and ${max}`)
      }
      return number
    }
    case 'bool':
    case 'triBool': {
      if (typeof value === 'boolean') return value
      if (value === 'true' || value === '1') return true
      if (value === 'false' || value === '0') return false
      throw paramError(`${label} must be a boolean`)
    }
    case 'enum': {
      if (typeof value !== 'string') throw paramError(`${label} must be a string`)
      const text = value.trim().toLowerCase()
      if (text.length === 0) return undefined
      if (!spec.values.includes(text)) {
        throw paramError(`${label} must be one of: ${spec.values.join(', ')}`)
      }
      return text
    }
    case 'stringList': {
      let items
      if (typeof value === 'string') {
        const text = value.trim()
        if (text.length === 0) return []
        items = spec.split ? text.split(/[\s,]+/).filter(Boolean) : [text]
      } else if (Array.isArray(value)) {
        items = value.map((item) => {
          if (typeof item !== 'string') throw paramError(`${label} must contain only strings`)
          return item.trim()
        })
      } else {
        throw paramError(`${label} must be an array of strings or a delimited string`)
      }
      const maxItems = spec.maxItems ?? 5000
      if (items.length > maxItems) {
        throw paramError(`${label} has too many entries (maximum ${maxItems})`)
      }
      const maxLength = spec.maxLength ?? 4096
      for (const item of items) {
        if (item.length === 0) throw paramError(`${label} must not contain empty entries`)
        if (/[\0\r\n]/.test(item)) {
          throw paramError(`${label} entries must not contain NUL or newline characters`)
        }
        if (item.length > maxLength) {
          throw paramError(`${label} entries are too long (maximum ${maxLength} characters)`)
        }
      }
      return items
    }
    default:
      throw paramError(`${label} uses an unsupported parameter type`)
  }
}

/**
 * Pull configured fallbacks for the paths every task needs.
 * @param {Record<string, any>} config
 * @param {any} backend
 */
function configuredDefaults(config, backend) {
  const sources = [
    config?.taskDefaults,
    backend?.config,
    config,
  ].filter(isPlainObject)
  const pick = (...names) => {
    for (const source of sources) {
      for (const name of names) {
        const value = source[name]
        if (typeof value === 'string' && value.trim().length > 0) return value.trim()
      }
    }
    return undefined
  }
  return {
    repo: pick('repo', 'repository', 'repoPath'),
    db: pick('db', 'database', 'dbPath'),
    compdb: pick('compdb', 'compileCommands', 'compileCommandsPath'),
  }
}

/**
 * Validate and normalize caller params, then render the real CLI argv.
 *
 * @param {string} kind canonical kind
 * @param {unknown} rawParams caller params
 * @param {Record<string, any>} config plugin config
 * @param {any} backend backend manager (for configured path fallbacks)
 * @returns {{args: string[], params: Record<string, unknown>}}
 */
export function buildTaskCommand(kind, rawParams, config = {}, backend = undefined) {
  const canonical = KIND_SPECS[kind] ? kind : resolveKind(kind)
  const spec = KIND_SPECS[canonical]
  if (rawParams !== undefined && rawParams !== null && !isPlainObject(rawParams)) {
    throw paramError('task params must be a JSON object')
  }
  const input = rawParams ?? {}
  let size = 0
  try {
    size = JSON.stringify(input).length
  } catch {
    throw paramError('task params must be JSON-serializable')
  }
  if (size > MAX_PARAMS_BYTES) {
    throw paramError(`task params are too large (maximum ${MAX_PARAMS_BYTES} bytes)`)
  }

  const lookup = paramLookup(spec)
  /** @type {Record<string, unknown>} */
  const values = {}
  const defaults = configuredDefaults(config, backend)

  for (const [spelling, value] of Object.entries(input)) {
    if (spelling === 'label' || spelling === 'note') continue // display-only metadata
    const key = lookup.get(String(spelling).replace(/[_\s]/g, '').toLowerCase())
    if (key === undefined) {
      const supported = [...Object.keys(spec.params), 'label'].sort()
      throw new TaskError(
        'INVALID_TASK_PARAMS',
        `unknown params key "${spelling}" for kind "${canonical}"; supported keys: ${supported.join(', ')}`,
        { kind: canonical, supported, unknown: spelling },
      )
    }
    if (value === undefined || value === null) continue
    const normalized = validateValue(key, spec.params[key], value)
    if (normalized !== undefined) values[key] = normalized
  }

  // Configured paths fill in omitted required path params.
  for (const key of ['repo', 'db', 'compdb']) {
    if (values[key] === undefined && defaults[key] !== undefined) {
      values[key] = defaults[key]
    }
  }

  const missing = []
  for (const [key, paramSpec] of Object.entries(spec.params)) {
    if (paramSpec.required && values[key] === undefined) missing.push(key)
  }
  if (missing.length > 0) {
    throw new TaskError(
      'MISSING_TASK_PARAM',
      `kind "${canonical}" requires params.${missing.join(', params.')}; ` +
        'pass them explicitly or configure repo/db/compdb in the CA backend settings.',
      { kind: canonical, missing },
    )
  }

  const args = [spec.command]
  for (const [key, paramSpec] of Object.entries(spec.params)) {
    const value = values[key]
    if (value === undefined) continue
    if (paramSpec.type === 'bool') {
      if (value === true) args.push(paramSpec.flag)
    } else if (paramSpec.type === 'triBool') {
      args.push(value ? paramSpec.flagTrue : paramSpec.flagFalse)
    } else if (paramSpec.type === 'stringList') {
      args.push(paramSpec.flag, ...value)
    } else {
      args.push(paramSpec.flag, String(value))
    }
  }
  return { args, params: values }
}

// --------------------------------------------------------------------------- //
// Manager
// --------------------------------------------------------------------------- //

const TASK_FIELDS = [
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
]

/** JSON-safe copy of one task's public state. */
function snapshot(entry) {
  const task = entry.task
  const copy = {
    id: task.id,
    kind: task.kind,
    status: task.status,
    params: cloneJsonSafe(task.params),
    createdAt: task.createdAt,
    startedAt: task.startedAt,
    finishedAt: task.finishedAt,
    exitCode: task.exitCode,
    error: task.error ? cloneJsonSafe(task.error) : null,
    logBytes: entry.bytes,
  }
  // Keep the public field order stable for diffs/log readability.
  const ordered = {}
  for (const field of TASK_FIELDS) ordered[field] = copy[field]
  return ordered
}

function cloneJsonSafe(value) {
  try {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value))
  } catch {
    return undefined
  }
}

function normalizeErrorPayload(error) {
  if (error instanceof TaskError) {
    const payload = { code: error.code, message: error.message }
    if (error.details !== undefined) payload.details = cloneJsonSafe(error.details)
    return payload
  }
  if (error instanceof BackendError) {
    const payload = { code: error.code, message: error.message }
    if (error.details !== undefined) payload.details = cloneJsonSafe(error.details)
    return payload
  }
  if (isAbortError(error)) {
    return { code: 'CANCELLED', message: error?.message ? String(error.message) : 'task cancelled' }
  }
  // Preserve stable application codes from the backend/task layers.
  if (error && typeof error === 'object' && typeof error.code === 'string' && /^[A-Z][A-Z0-9_]*$/.test(error.code)) {
    const payload = { code: error.code, message: typeof error.message === 'string' ? error.message : String(error) }
    if (error.details !== undefined) payload.details = cloneJsonSafe(error.details)
    return payload
  }
  const normalized = normalizeError(error, 'TASK_EXEC_FAILED')
  return { code: normalized.code, message: normalized.message }
}

function tailLines(entry, count) {
  const lines = entry.lines
  const start = Math.max(entry.lineStart, lines.length - count)
  return lines.slice(start).map((line) => line.text)
}

/**
 * @param {import('node:events').EventEmitter | Record<string, unknown>} ctx
 * @param {{
 *   backend: any,
 *   config?: Record<string, any>,
 * }} deps
 */
export function createTaskManager(ctx, deps = {}) {
  const backend = deps.backend
  const config = deps.config ?? {}
  if (!backend || typeof backend.execCa !== 'function') {
    throw new Error('dsh-ca: createTaskManager requires a backend manager with execCa()')
  }
  const maxConcurrent = clampInt(
    config?.maxConcurrentTasks ?? config?.tasks?.maxConcurrent,
    1,
    8,
    DEFAULT_CONCURRENCY,
  )
  const maxLogBytes = clampInt(
    config?.maxTaskLogBytes ?? config?.tasks?.maxLogBytes,
    64 * 1024,
    16 * 1024 * 1024,
    DEFAULT_MAX_LOG_BYTES,
  )
  const autoStart = config?.autoStart !== false

  let seq = 0
  let running = 0
  let disposed = false
  /** @type {string[]} */
  const queue = []
  /** @type {Map<string, any>} */
  const entries = new Map()
  /** @type {Set<(task: any, reason: string) => void>} */
  const listeners = new Set()

  function emit(entry, reason) {
    if (listeners.size === 0) return
    const task = snapshot(entry)
    for (const listener of [...listeners]) {
      try {
        listener(task, reason)
      } catch {
        // A broken observer must never break task execution.
      }
    }
  }

  function makeEntry(kind, params, args) {
    const deferred = createDeferred()
    const task = {
      id: `task-${++seq}`,
      kind,
      status: 'queued',
      params,
      createdAt: Date.now(),
      startedAt: null,
      finishedAt: null,
      exitCode: null,
      error: null,
    }
    return {
      task,
      args,
      controller: null,
      cancelRequested: false,
      deferred,
      finished: false,
      lines: [],
      lineStart: 0,
      nextLine: 1,
      bytes: 0,
      droppedLines: 0,
      stderrTail: [],
    }
  }

  function pushLine(entry, stream, text, wasTruncated) {
    let value = String(text ?? '')
    if (wasTruncated || value.length > MAX_LINE_CHARS) {
      value = `${value.slice(0, MAX_LINE_CHARS - 16)} …[truncated]`
    }
    const bytes = Buffer.byteLength(value, 'utf8') + 1
    entry.lines.push({ n: entry.nextLine++, stream, text: value, bytes })
    entry.bytes += bytes
    if (stream === 'stderr') {
      entry.stderrTail.push(value)
      if (entry.stderrTail.length > MAX_STDERR_TAIL_LINES) entry.stderrTail.shift()
    }
    // Trim oldest lines until the task log is back within its budget. A single
    // line is never dropped, so one pathological line stays readable.
    while (entry.bytes > maxLogBytes && entry.lines.length - entry.lineStart > 1) {
      entry.bytes -= entry.lines[entry.lineStart].bytes
      entry.lineStart += 1
      entry.droppedLines += 1
    }
    if (entry.lineStart > 1024 && entry.lineStart * 2 > entry.lines.length) {
      entry.lines = entry.lines.slice(entry.lineStart)
      entry.lineStart = 0
    }
  }

  function finishTask(entry, status, errorPayload, exitCode) {
    if (entry.finished) return
    entry.finished = true
    entry.task.status = status
    entry.task.finishedAt = Date.now()
    if (exitCode !== undefined) entry.task.exitCode = exitCode
    entry.task.error = errorPayload ?? null
    entry.controller = null
    emit(entry, 'completed')
    entry.deferred.resolve(snapshot(entry))
  }

  async function ensureBackendReady(entry) {
    let status = typeof backend.getStatus === 'function' ? backend.getStatus() : {}
    if (status && status.state === 'running') return
    // A backend that is already starting (UI pressed Start just before the
    // task) is waited for instead of being started twice.
    if (status?.state === 'starting') {
      const timeoutMs = clampInt(config?.backendStartTimeoutMs, 1000, 120000, 30000)
      const deadline = Date.now() + timeoutMs
      while (Date.now() < deadline && !entry.cancelRequested) {
        await new Promise((resolve) => setTimeout(resolve, 50))
        status = typeof backend.getStatus === 'function' ? backend.getStatus() : {}
        if (status?.state === 'running') return
        if (status?.state !== 'starting') break
      }
      if (entry.cancelRequested) return
    }
    const state = status?.state ?? 'unconfigured'
    const mode = status?.mode ?? 'unconfigured'
    if (mode === 'unconfigured') {
      throw new BackendError(
        'BACKEND_UNCONFIGURED',
        'CA backend is not configured. Open the CA backend page to configure a local or remote Linux target, then start it.',
      )
    }
    if (!autoStart) {
      throw new BackendError(
        'BACKEND_NOT_RUNNING',
        `CA backend is not running (state: ${state}). Start it from the CA backend page or enable autoStart.`,
      )
    }
    if (typeof backend.start !== 'function') {
      throw new BackendError('BACKEND_NOT_RUNNING', 'CA backend is not running and cannot be started automatically.')
    }
    if (entry.cancelRequested) return
    await backend.start()
    const after = typeof backend.getStatus === 'function' ? backend.getStatus() : {}
    if (!after || after.state !== 'running') {
      throw new BackendError(
        'BACKEND_NOT_RUNNING',
        `CA backend did not reach the running state (state: ${after?.state ?? 'unknown'}). Check the CA backend page for its error.`,
      )
    }
  }

  async function runEntry(entry) {
    const { task } = entry
    if (entry.cancelRequested) {
      finishTask(entry, 'cancelled', { code: 'CANCELLED', message: 'task cancelled before start' })
      return
    }
    task.status = 'running'
    task.startedAt = Date.now()
    running += 1
    emit(entry, 'started')

    const controller = new AbortController()
    entry.controller = controller
    const stdoutDecoder = createLineDecoder(
      (line, info) => pushLine(entry, 'stdout', line, info.truncated),
      { maxLineLength: MAX_LINE_CHARS },
    )
    const stderrDecoder = createLineDecoder(
      (line, info) => pushLine(entry, 'stderr', line, info.truncated),
      { maxLineLength: MAX_LINE_CHARS },
    )

    try {
      await ensureBackendReady(entry)
      if (entry.cancelRequested || controller.signal.aborted) {
        finishTask(entry, 'cancelled', { code: 'CANCELLED', message: 'task cancelled before start' })
        return
      }
      const result = await backend.execCa(entry.args, {
        onStdout: (chunk) => stdoutDecoder.push(chunk),
        onStderr: (chunk) => stderrDecoder.push(chunk),
        signal: controller.signal,
      })
      stdoutDecoder.flush()
      stderrDecoder.flush()
      if (entry.cancelRequested || controller.signal.aborted) {
        finishTask(entry, 'cancelled', { code: 'CANCELLED', message: 'task cancelled by the user' }, readExitCode(result))
        return
      }
      const exitCode = readExitCode(result)
      if (exitCode === null) {
        finishTask(
          entry,
          'failed',
          {
            code: 'TASK_RESULT_INVALID',
            message: 'CA CLI finished without an exit code; the backend returned no result.',
            details: { args: entry.args },
          },
          null,
        )
        return
      }
      if (exitCode === 0) {
        finishTask(entry, 'succeeded', null, 0)
        return
      }
      finishTask(
        entry,
        'failed',
        {
          code: 'CA_CLI_EXIT',
          message: `ca ${entry.args[0]} exited with code ${exitCode}`,
          details: { exitCode, stderrTail: entry.stderrTail.slice(-10) },
        },
        exitCode,
      )
    } catch (error) {
      stdoutDecoder.flush()
      stderrDecoder.flush()
      if (entry.cancelRequested || controller.signal.aborted || isAbortError(error)) {
        finishTask(entry, 'cancelled', { code: 'CANCELLED', message: 'task cancelled by the user' })
        return
      }
      const payload = normalizeErrorPayload(error)
      payload.details = {
        ...(isPlainObject(payload.details) ? payload.details : {}),
        stderrTail: entry.stderrTail.slice(-10),
      }
      finishTask(entry, 'failed', payload)
    } finally {
      if (!entry.finished) finishTask(entry, 'failed', { code: 'INTERNAL', message: 'task ended in an unexpected state' })
      running = Math.max(0, running - 1)
      pump()
    }
  }

  function readExitCode(result) {
    if (!result || typeof result !== 'object') return null
    const value = Number(result.exitCode)
    if (!Number.isFinite(value)) return null
    return Math.trunc(value)
  }

  function pump() {
    if (disposed) return
    while (running < maxConcurrent && queue.length > 0) {
      const id = queue.shift()
      const entry = entries.get(id)
      if (!entry || entry.finished || entry.task.status !== 'queued') continue
      runEntry(entry).catch(() => {
        // runEntry never rejects by design; this is a last-resort guard.
      })
    }
  }

  /** @param {string} kind @param {unknown} params @returns {any} */
  function start(kind, params = {}) {
    if (disposed) {
      throw new TaskError('TASK_MANAGER_DISPOSED', 'CA task manager has been disposed; reload the CA plugin.')
    }
    const canonical = resolveKind(kind)
    const { args, params: normalized } = buildTaskCommand(canonical, params, config, backend)
    const label = typeof params === 'object' && params !== null && typeof params.label === 'string'
      ? params.label.slice(0, 120)
      : undefined
    if (label !== undefined) normalized.label = label
    const entry = makeEntry(canonical, normalized, args)
    entries.set(entry.task.id, entry)
    queue.push(entry.task.id)
    emit(entry, 'queued')
    const queued = snapshot(entry)
    pump()
    return queued
  }

  /** @returns {any[]} newest first */
  function list() {
    return [...entries.values()]
      .filter((entry) => !entry.removed)
      .map((entry) => snapshot(entry))
      .sort((a, b) => b.createdAt - a.createdAt || String(b.id).localeCompare(String(a.id)))
  }

  /** @param {unknown} id @returns {any|null} */
  function get(id) {
    const entry = lookup(id)
    return entry ? snapshot(entry) : null
  }

  function lookup(id) {
    if (typeof id !== 'string' || id.trim().length === 0) {
      throw new TaskError('INVALID_TASK_ID', 'task id is required')
    }
    return entries.get(id.trim()) ?? null
  }

  /**
   * Paginated ring-log read. `offset` is an absolute line number (1-based);
   * omitted/too-small offsets are clamped to the oldest retained line.
   *
   * @param {unknown} id
   * @param {{offset?: unknown, limit?: unknown}} [options]
   */
  function logs(id, options = {}) {
    const entry = lookup(id)
    if (!entry) {
      throw new TaskError('TASK_NOT_FOUND', `no CA task with id "${id}"`)
    }
    const limit = clampInt(options?.limit, 1, MAX_LOG_LIMIT, DEFAULT_LOG_LIMIT)
    const total = entry.nextLine - 1
    const available = entry.lines.slice(entry.lineStart)
    const firstAvailable = available.length > 0 ? available[0].n : entry.nextLine
    let offset = clampInt(options?.offset, 0, Number.MAX_SAFE_INTEGER, 0)
    let clipped = false
    if (offset < firstAvailable) {
      clipped = firstAvailable > 1 && offset > 0
      offset = firstAvailable
    }
    const startIndex = Math.max(0, Math.min(available.length, offset - firstAvailable))
    const picked = available.slice(startIndex, startIndex + limit).map((line) => ({
      n: line.n,
      stream: line.stream,
      text: line.text,
    }))
    // nextOffset is the next absolute line number to request. It is null only
    // when the response returned no lines so pollers can cleanly keep their
    // current offset and retry later without re-reading a consumed line.
    const nextOffset = picked.length > 0 ? picked[picked.length - 1].n + 1 : null
    return {
      id: entry.task.id,
      lines: picked,
      offset,
      limit,
      total,
      nextOffset,
      logBytes: entry.bytes,
      droppedLines: entry.droppedLines,
      clipped,
    }
  }

  /** @param {unknown} id */
  function cancel(id) {
    const entry = lookup(id)
    if (!entry) {
      throw new TaskError('TASK_NOT_FOUND', `no CA task with id "${id}"`)
    }
    if (entry.finished) {
      throw new TaskError(
        'TASK_NOT_CANCELLABLE',
        `task ${entry.task.id} is already ${entry.task.status}`,
        { status: entry.task.status },
      )
    }
    entry.cancelRequested = true
    if (entry.task.status === 'queued') {
      const index = queue.indexOf(entry.task.id)
      if (index >= 0) queue.splice(index, 1)
      finishTask(entry, 'cancelled', { code: 'CANCELLED', message: 'task cancelled before start' })
      return snapshot(entry)
    }
    try {
      entry.controller?.abort()
    } catch {
      // AbortController.abort() does not throw for plain controllers.
    }
    emit(entry, 'updated')
    return snapshot(entry)
  }

  /**
   * Await terminal state (test/tool convenience; not part of the RPC contract).
   * @param {unknown} id
   */
  function wait(id) {
    const entry = lookup(id)
    if (!entry) {
      throw new TaskError('TASK_NOT_FOUND', `no CA task with id "${id}"`)
    }
    return entry.deferred.promise
  }

  /** Status histogram for RPC status/tools. */
  function counts() {
    const result = { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 }
    for (const entry of entries.values()) {
      if (result[entry.task.status] !== undefined) result[entry.task.status] += 1
    }
    return result
  }

  function onChange(listener) {
    if (typeof listener !== 'function') {
      throw new TaskError('INVALID_ARGUMENT', 'onChange expects a listener function')
    }
    listeners.add(listener)
    return () => listeners.delete(listener)
  }

  function dispose() {
    if (disposed) return
    disposed = true
    for (const entry of entries.values()) {
      if (entry.finished) continue
      entry.cancelRequested = true
      if (entry.task.status === 'queued') {
        finishTask(entry, 'cancelled', { code: 'CANCELLED', message: 'task manager disposed' })
        continue
      }
      try {
        entry.controller?.abort()
      } catch {
        // ignore
      }
    }
    queue.length = 0
    listeners.clear()
  }

  return {
    start,
    list,
    get,
    logs,
    cancel,
    onChange,
    dispose,
    wait,
    counts,
  }
}
