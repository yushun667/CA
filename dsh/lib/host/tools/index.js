/**
 * Native DSH tools for the CA source analyzer.
 *
 * Eleven model-facing tools map onto CA's read-only `/api/*` query surface plus
 * the task manager and the AI explain service:
 *
 *   ca_status  ca_search  ca_symbol  ca_graph  ca_facts  ca_source
 *   ca_stats   ca_callbacks  ca_registry  ca_explain  ca_task
 *
 * Implementation rules:
 * - plain JSON Schema only (no defineTool/schemastery/npm imports),
 * - arguments are validated locally with actionable messages,
 * - every tool returns `{text}` and renders it as one text block,
 * - text is bounded (default 20k chars) and explains how to page/narrow,
 * - `execute` observes the tool-call cancellation signal.
 */

import { normalizeError } from '../backend/errors.js'
import { clampInt, isPlainObject } from '../backend/util.js'
import {
  BACKEND_HINT,
  caGet,
  caPost,
  clipText,
  ensureBackend,
  ToolError,
} from './backend.js'
import * as fmt from './format.js'
import {
  fetchActiveProject,
  fetchProjects,
  findProject,
  supportsProjects,
} from './projects.js'

/** Fixed public tool names (CONTRACT.md). */
export const CA_TOOL_NAMES = Object.freeze([
  'ca_status',
  'ca_search',
  'ca_symbol',
  'ca_graph',
  'ca_facts',
  'ca_source',
  'ca_stats',
  'ca_callbacks',
  'ca_registry',
  'ca_project',
  'ca_explain',
  'ca_task',
])

const DEFAULT_MAX_OUTPUT = 20000
const MIN_MAX_OUTPUT = 4000
const MAX_MAX_OUTPUT = 60000
const MAX_FACT_FETCH = 2000
const MAX_TASK_PARAMS_BYTES = 32 * 1024

const TASK_KINDS = ['index', 'update', 'ensure_deep', 'prewarm', 'resolve_callbacks', 'watch']
const FACT_KINDS = ['branches', 'assignments', 'returns', 'resources', 'findings']
const FACT_ENDPOINTS = {
  branches: '/api/branches',
  assignments: '/api/assignments',
  returns: '/api/returns',
  resources: '/api/resources',
}

// --------------------------------------------------------------------------- //
// Argument specs -> plain JSON Schema
// --------------------------------------------------------------------------- //

/**
 * @typedef {{
 *   type: 'int'|'string'|'bool'|'enum'|'object',
 *   description: string,
 *   required?: boolean,
 *   values?: string[],
 *   min?: number,
 *   max?: number,
 *   maxLength?: number,
 *   schema?: Record<string, unknown>,
 * }} ArgSpec
 */

function intArg(description, options = {}) {
  return { type: 'int', description, ...options }
}
function strArg(description, options = {}) {
  return { type: 'string', description, ...options }
}
function boolArg(description) {
  return { type: 'bool', description }
}
function enumArg(description, values, options = {}) {
  return { type: 'enum', description, values, ...options }
}
function objectArg(description) {
  return {
    type: 'object',
    description,
    schema: { type: 'object', description, additionalProperties: true },
  }
}

/** JSON Schema node for one argument. */
function argSchema(spec) {
  if (spec.schema) return spec.schema
  switch (spec.type) {
    case 'int': {
      const schema = { type: 'integer', description: spec.description }
      if (spec.min !== undefined) schema.minimum = spec.min
      if (spec.max !== undefined) schema.maximum = spec.max
      return schema
    }
    case 'string': {
      const schema = { type: 'string', description: spec.description }
      if (spec.minLength !== undefined) schema.minLength = spec.minLength
      if (spec.maxLength !== undefined) schema.maxLength = spec.maxLength
      return schema
    }
    case 'bool':
      return { type: 'boolean', description: spec.description }
    case 'enum':
      return { type: 'string', enum: spec.values, description: spec.description }
    default:
      return { description: spec.description }
  }
}

// --------------------------------------------------------------------------- //
// Validation
// --------------------------------------------------------------------------- //

function argError(toolName, message) {
  return new ToolError('INVALID_ARGUMENT', `${toolName}: ${message}`)
}

/**
 * Validate model-supplied arguments against this tool's specs.
 * @param {string} toolName
 * @param {Record<string, ArgSpec>} specs
 * @param {unknown} raw
 * @returns {Record<string, unknown>}
 */
function parseArgs(toolName, specs, raw) {
  if (raw === undefined || raw === null) raw = {}
  if (!isPlainObject(raw)) {
    throw argError(toolName, 'arguments must be a JSON object')
  }
  const supported = Object.keys(specs)
  for (const key of Object.keys(raw)) {
    if (!Object.prototype.hasOwnProperty.call(specs, key)) {
      throw argError(toolName, `unknown argument "${key}"; supported arguments: ${supported.join(', ') || '(none)'}`)
    }
  }
  const result = {}
  for (const [key, spec] of Object.entries(specs)) {
    let value = raw[key]
    if (value === undefined || value === null) {
      if (spec.required) throw argError(toolName, `"${key}" is required — ${spec.description}`)
      if (spec.default !== undefined) result[key] = spec.default
      continue
    }
    switch (spec.type) {
      case 'string': {
        if (typeof value !== 'string') throw argError(toolName, `"${key}" must be a string`)
        value = value.trim()
        if (value.length === 0) {
          if (spec.required) throw argError(toolName, `"${key}" must not be empty`)
          continue
        }
        const maxLength = spec.maxLength ?? 4096
        if (value.length > maxLength) {
          throw argError(toolName, `"${key}" is too long (maximum ${maxLength} characters)`)
        }
        result[key] = value
        break
      }
      case 'int': {
        const number = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : NaN
        if (!Number.isFinite(number) || !Number.isInteger(number)) {
          throw argError(toolName, `"${key}" must be an integer`)
        }
        const min = spec.min ?? 0
        const max = spec.max ?? Number.MAX_SAFE_INTEGER
        if (number < min || number > max) {
          throw argError(toolName, `"${key}" must be between ${min} and ${max}`)
        }
        result[key] = number
        break
      }
      case 'bool': {
        if (typeof value === 'boolean') result[key] = value
        else if (value === 'true') result[key] = true
        else if (value === 'false') result[key] = false
        else throw argError(toolName, `"${key}" must be a boolean`)
        break
      }
      case 'enum': {
        if (typeof value !== 'string') throw argError(toolName, `"${key}" must be a string`)
        const normalized = value.trim().toLowerCase()
        if (!spec.values.includes(normalized)) {
          throw argError(toolName, `"${key}" must be one of: ${spec.values.join(', ')}`)
        }
        result[key] = normalized
        break
      }
      case 'object': {
        if (!isPlainObject(value)) throw argError(toolName, `"${key}" must be a JSON object`)
        let size = 0
        try {
          size = JSON.stringify(value).length
        } catch {
          throw argError(toolName, `"${key}" must be JSON-serializable`)
        }
        if (size > MAX_TASK_PARAMS_BYTES) {
          throw argError(toolName, `"${key}" is too large (maximum ${MAX_TASK_PARAMS_BYTES} bytes)`)
        }
        result[key] = value
        break
      }
      default:
        throw argError(toolName, `"${key}" has an unsupported type`)
    }
  }
  return result
}

/** Convert failures into clean model-facing errors (no internal stacks). */
function toolFailure(toolName, error) {
  if (error instanceof ToolError) {
    const wrapped = new Error(error.message)
    wrapped.name = 'ToolError'
    wrapped.code = error.code
    return wrapped
  }
  if (error && (error.name === 'AbortError' || error.code === 'ABORT_ERR' || error.code === 'CANCELLED')) {
    const wrapped = new Error(`${toolName} was cancelled`)
    wrapped.name = 'ToolError'
    wrapped.code = 'CANCELLED'
    return wrapped
  }
  const normalized = normalizeError(error, 'TOOL_FAILED')
  const wrapped = new Error(`${toolName} failed: ${normalized.message}`)
  wrapped.name = 'ToolError'
  wrapped.code = normalized.code
  return wrapped
}

// --------------------------------------------------------------------------- //
// Registration
// --------------------------------------------------------------------------- //

/**
 * Build one native tool definition (plain schema, `{text}` output).
 *
 * @param {{name: string, description: string, title: string, args: Record<string, ArgSpec>, execute: (args: any, exec: any) => Promise<string>}} spec
 * @param {number} maxOutput
 */
function makeTool(spec, maxOutput) {
  const properties = {}
  const required = []
  for (const [key, argSpec] of Object.entries(spec.args)) {
    properties[key] = argSchema(argSpec)
    if (argSpec.required) required.push(key)
  }
  return {
    name: spec.name,
    description: spec.description,
    parameters: {
      type: 'object',
      properties,
      required,
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: { text: { type: 'string' } },
        required: ['text'],
        additionalProperties: false,
      },
      render: (_args, value) => [
        { type: 'text', text: typeof value?.text === 'string' ? value.text : '' },
      ],
    },
    isConcurrencySafe: () => true,
    presentCall: () => ({ card: 'generic', title: spec.title, kind: 'other' }),
    execute: async (rawArgs, exec) => {
      const args = parseArgs(spec.name, spec.args, rawArgs)
      try {
        const text = await spec.execute(args, exec)
        return {
          text: clipText(
            typeof text === 'string' ? text : String(text ?? ''),
            maxOutput,
            'narrow the query with limit/offset/compact or use a more specific ca_* tool',
          ),
        }
      } catch (error) {
        throw toolFailure(spec.name, error)
      }
    },
  }
}

function taskCounts(tasks) {
  try {
    if (typeof tasks.counts === 'function') return tasks.counts()
    const items = typeof tasks.list === 'function' ? tasks.list() : []
    const counts = { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 }
    for (const task of Array.isArray(items) ? items : []) {
      if (counts[task?.status] !== undefined) counts[task.status] += 1
    }
    return counts
  } catch {
    return { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 }
  }
}

/** Fetch the project list or fail with an actionable tool error. */
async function requireProjects(backend) {
  const list = await fetchProjects(backend)
  if (!list) {
    throw new ToolError(
      'BACKEND_UNSUPPORTED',
      'This CA backend build does not support the project model yet; configure the database on the CA backend page instead.',
    )
  }
  return list
}

function taskStatus(backend) {
  try {
    return typeof backend.getStatus === 'function' ? backend.getStatus() ?? {} : {}
  } catch {
    return {}
  }
}

/**
 * @param {string} symbol
 * @param {{limit: number, offset: number, compact: boolean}} args
 */
async function fetchFacts(backend, symbol, kind, args, signal) {
  const pageLimit = clampInt(args.limit, 1, 1000, 50)
  const offset = clampInt(args.offset, 0, 100000, 0)
  if (offset + pageLimit > MAX_FACT_FETCH) {
    throw new ToolError(
      'INVALID_ARGUMENT',
      `ca_facts: limit ${pageLimit} + offset ${offset} exceeds ${MAX_FACT_FETCH}; use a smaller page`,
    )
  }
  const fetchLimit = Math.min(MAX_FACT_FETCH, offset + pageLimit)
  if (kind === 'findings') {
    const detail = await caGet(backend, '/api/function', { symbol }, { signal })
    const all = Array.isArray(detail?.findings) ? detail.findings : []
    const items = all.slice(offset, offset + pageLimit)
    return { symbol: detail?.function?.name ?? symbol, items }
  }
  const endpoint = FACT_ENDPOINTS[kind]
  const data = await caGet(backend, endpoint, { symbol, limit: fetchLimit }, { signal })
  const rows = Array.isArray(data?.items) ? data.items : []
  return { symbol: data?.symbol ?? symbol, items: rows.slice(offset, offset + pageLimit) }
}

/**
 * @param {any} ctx Cordis plugin context (hard dep: tools)
 * @param {{backend: any, tasks: any, explain?: any, config?: Record<string, any>}} deps
 * @returns {() => void} disposer
 */
export function registerTools(ctx, deps = {}) {
  const { backend, tasks, explain, config = {} } = deps
  if (!backend || typeof backend.request !== 'function') {
    throw new Error('dsh-ca: registerTools requires the backend manager (backend-ops)')
  }
  if (!tasks || typeof tasks.start !== 'function') {
    throw new Error('dsh-ca: registerTools requires the task manager (host-tools)')
  }
  const tools = ctx?.tools ?? ctx?.get?.('tools')
  if (!tools || typeof tools.register !== 'function') {
    throw new Error('dsh-ca: registerTools requires the DSH tools service (inject "tools")')
  }
  const maxOutput = clampInt(
    config?.maxToolOutputChars ?? config?.tools?.maxOutputChars,
    MIN_MAX_OUTPUT,
    MAX_MAX_OUTPUT,
    DEFAULT_MAX_OUTPUT,
  )

  const specs = [
    {
      name: 'ca_status',
      title: 'CA backend status',
      description:
        'Report the CA backend, the active project and the index: project (source tree + database + backend location), configured target (local or remote Linux), running state, database path, index table counts, health and task summary. Call it when a ca_* call fails with a backend error, or when the user asks whether CA is available, which project/database is active, or what is indexed.',
      args: {},
      async execute() {
        const status = taskStatus(backend)
        let health = null
        let healthError = null
        if (status.state === 'running') {
          try {
            health = await caGet(backend, '/api/health', undefined, { timeoutMs: 5000 })
          } catch (error) {
            healthError = error?.message ?? String(error)
          }
        }
        const counts = taskCounts(tasks)
        const project = await fetchActiveProject(backend)
        const rendered = fmt.formatStatus({
          status,
          health,
          counts,
          project,
          projectsSupported: supportsProjects(backend),
        })
        return healthError ? `${rendered}\nhealth error: ${healthError}` : rendered
      },
    },

    {
      name: 'ca_search',
      title: 'CA symbol search',
      description:
        'Search indexed functions by exact id/name/qualified name and then substring. Use it to resolve the spelling of a symbol before ca_symbol/ca_graph/ca_facts; it matches symbol names only (not comments or arbitrary text). Returns bounded rows; page with offset.',
      args: {
        query: strArg('Function name, id, qualified name, or a unique substring to search for.', {
          required: true,
          maxLength: 512,
        }),
        limit: intArg('Maximum rows to return (1-200, default 20); limit+offset must stay <= 2000.', { min: 1, max: 200 }),
        offset: intArg('Rows to skip for pagination (default 0); limit+offset must stay <= 2000.', { min: 0, max: 100000 }),
        compact: boolArg('Return one line per match without signatures/ids (default false).'),
      },
      async execute(args, exec) {
        const limit = args.limit ?? 20
        const offset = args.offset ?? 0
        if (limit + offset > 2000) {
          throw new ToolError('INVALID_ARGUMENT', `ca_search: limit ${limit} + offset ${offset} exceeds 2000; page with a smaller limit`)
        }
        await ensureBackend(backend, config, exec?.signal)
        const data = await caGet(
          backend,
          '/api/search',
          { q: args.query, limit, offset },
          { signal: exec?.signal },
        )
        return fmt.formatSearch(data, { compact: args.compact === true })
      },
    },

    {
      name: 'ca_symbol',
      title: 'CA symbol detail',
      description:
        'One indexed function/type record plus its indexed facts: signature, location, deep_ready, and counts of branches/assignments/returns/resources/findings with key conditions and findings. Use ca_source for code, ca_graph for callers/callees, ca_facts for complete fact lists. Set compact for a one-screen summary.',
      args: {
        symbol: strArg('Function name, id or a unique substring (resolved by CA).', {
          required: true,
          maxLength: 1024,
        }),
        compact: boolArg('Summary only: identity, counts and the first branch (default false).'),
      },
      async execute(args, exec) {
        await ensureBackend(backend, config, exec?.signal)
        const data = await caGet(backend, '/api/function', { symbol: args.symbol }, { signal: exec?.signal })
        return fmt.formatSymbol(data, { compact: args.compact === true })
      },
    },

    {
      name: 'ca_graph',
      title: 'CA call graph',
      description:
        'Call graph around one symbol. direction=forward follows callees, reverse follows callers, both expands outward. Direct calls are marked [direct]; function-pointer/virtual candidates are marked [candidate]. depth 0-12 and limit caps the node count; prefer depth 1-2 for context.',
      args: {
        symbol: strArg('Root function name, id or a unique substring.', { required: true, maxLength: 1024 }),
        direction: enumArg('Traversal direction: forward = callees, reverse = callers, both (default forward).', [
          'forward',
          'reverse',
          'both',
        ]),
        depth: intArg('Traversal depth 0-12 (default 1).', { min: 0, max: 12 }),
        limit: intArg('Maximum graph nodes, 1-2000 (default 200).', { min: 1, max: 2000 }),
        compact: boolArg('Only the root/counts/cycles summary (default false).'),
      },
      async execute(args, exec) {
        await ensureBackend(backend, config, exec?.signal)
        const data = await caGet(
          backend,
          '/api/graph',
          {
            symbol: args.symbol,
            direction: args.direction ?? 'forward',
            depth: args.depth ?? 1,
            limit: args.limit ?? 200,
          },
          { signal: exec?.signal },
        )
        return fmt.formatGraph(data, { compact: args.compact === true })
      },
    },

    {
      name: 'ca_facts',
      title: 'CA symbol facts',
      description:
        'One kind of deep fact for a symbol: branches (conditions), assignments, returns, resources (acquire/release operations), or findings (data-flow leaks/unsafe patterns). Only available after deep analysis; ca_symbol reports deep_ready and the fact counts. Page with offset.',
      args: {
        symbol: strArg('Function name, id or a unique substring.', { required: true, maxLength: 1024 }),
        kind: enumArg('Fact kind to return.', FACT_KINDS, { required: true }),
        limit: intArg('Maximum rows, 1-1000 (default 50).', { min: 1, max: 1000 }),
        offset: intArg('Rows to skip, 0-100000 (default 0); limit+offset must stay <= 2000.', {
          min: 0,
          max: 100000,
        }),
        compact: boolArg('Omit findings evidence payloads (default false).'),
      },
      async execute(args, exec) {
        await ensureBackend(backend, config, exec?.signal)
        const kind = args.kind
        const limit = args.limit ?? 50
        const offset = args.offset ?? 0
        const page = await fetchFacts(
          backend,
          args.symbol,
          kind,
          { limit, offset, compact: args.compact === true },
          exec?.signal,
        )
        return fmt.formatFacts(kind, page, {
          compact: args.compact === true,
          offset,
          limit,
        })
      },
    },

    {
      name: 'ca_source',
      title: 'CA source window',
      description:
        'Read the real source lines recorded for a symbol: the function window plus `context` lines before/after (CA caps the window at 400 lines). Source comes from the indexed repository on the backend. Use it after ca_symbol to quote or reason about code.',
      args: {
        symbol: strArg('Function name, id or a unique substring.', { required: true, maxLength: 1024 }),
        context: intArg('Extra context lines around the function, 0-10 (default 3).', { min: 0, max: 10 }),
        compact: boolArg('Omit the signature/repo header (default false).'),
      },
      async execute(args, exec) {
        await ensureBackend(backend, config, exec?.signal)
        const data = await caGet(
          backend,
          '/api/source',
          { symbol: args.symbol, context: args.context ?? 3 },
          { signal: exec?.signal },
        )
        return fmt.formatSource(data, { compact: args.compact === true })
      },
    },

    {
      name: 'ca_stats',
      title: 'CA index statistics',
      description:
        'Index-wide statistics: table row counts, database size, schema/sqlite versions, callback registry stats and federated shards. Use it for coverage questions ("is this repo indexed?") and to verify an index exists before querying.',
      args: {
        compact: boolArg('Only database size and core table counts (default false).'),
      },
      async execute(args, exec) {
        await ensureBackend(backend, config, exec?.signal)
        const data = await caGet(backend, '/api/stats', undefined, { signal: exec?.signal })
        return fmt.formatStats(data, { compact: args.compact === true })
      },
    },

    {
      name: 'ca_callbacks',
      title: 'CA callback registrations',
      description:
        'Cross-TU callback registration facts: object path, field, module, registration kind, target function, call site and evidence. Use it to answer "who registers/consumes this callback" in C/C++ codebases. path is a prefix filter; field/module/kind are exact filters.',
      args: {
        path: strArg('Object-path prefix filter, e.g. "g_dsoftbus" (optional).', { maxLength: 512 }),
        field: strArg('Object field name filter (exact).', { maxLength: 256 }),
        module: strArg('Module name filter (exact).', { maxLength: 256 }),
        kind: strArg('Registration kind filter (exact, e.g. registration kind stored by CA).', { maxLength: 128 }),
        limit: intArg('Maximum rows, 1-1000 (default 50); limit+offset must stay <= 1000.', { min: 1, max: 1000 }),
        offset: intArg('Rows to skip, 0-100000 (default 0); limit+offset must stay <= 1000.', { min: 0, max: 100000 }),
        compact: boolArg('Omit the evidence payload per row (default false).'),
      },
      async execute(args, exec) {
        await ensureBackend(backend, config, exec?.signal)
        const limit = args.limit ?? 50
        const offset = args.offset ?? 0
        if (limit + offset > 1000) {
          throw new ToolError('INVALID_ARGUMENT', `ca_callbacks: limit ${limit} + offset ${offset} exceeds 1000; page with a smaller limit`)
        }
        const query = { limit: offset + limit }
        if (args.path !== undefined) query.path = args.path
        if (args.field !== undefined) query.field = args.field
        if (args.module !== undefined) query.module = args.module
        if (args.kind !== undefined) query.kind = args.kind
        const data = await caGet(backend, '/api/callbacks', query, { signal: exec?.signal })
        const items = Array.isArray(data?.items) ? data.items.slice(offset, offset + limit) : []
        return fmt.formatCallbacks(
          { ...data, items },
          { compact: args.compact === true, offset, limit },
        )
      },
    },

    {
      name: 'ca_registry',
      title: 'CA registry',
      description:
        'Registered workspaces, projects and build variants, plus the backend\'s active variant. action=overview (default) is read-only; action=activate switches the running backend to another registered variant and is in-memory only. Only activate when the user explicitly asks to switch variants.',
      args: {
        action: enumArg('overview = inspect the registry (default); activate = switch the active variant.', [
          'overview',
          'activate',
        ]),
        variant: strArg('Variant id or name; required for action=activate.', { maxLength: 256 }),
        compact: boolArg('Omit workspace/project/variant listings (default false).'),
      },
      async execute(args, exec) {
        await ensureBackend(backend, config, exec?.signal)
        const action = args.action ?? 'overview'
        if (action === 'activate') {
          if (!args.variant) {
            throw new ToolError('INVALID_ARGUMENT', 'ca_registry: "variant" is required for action=activate')
          }
          const activated = await caPost(
            backend,
            '/api/registry/active',
            { variant_id: args.variant },
            { signal: exec?.signal },
          )
          const data = await caGet(backend, '/api/registry', undefined, { signal: exec?.signal })
          const activeName =
            activated?.active_variant?.name ??
            data?.active_variant?.name ??
            args.variant
          return `CA registry: activated variant ${activeName}\n\n${fmt.formatRegistry(data, { compact: args.compact === true })}`
        }
        const data = await caGet(backend, '/api/registry', undefined, { signal: exec?.signal })
        return fmt.formatRegistry(data, { compact: args.compact === true })
      },
    },

    {
      name: 'ca_project',
      title: 'CA projects',
      description:
        'List, inspect or activate CA projects. A project pins one source tree to one database (a single .db file or a shard directory) and one backend location (local machine or remote Linux host). Projects are created, edited and removed by the user in the CA page; this tool only lists and activates existing ones, it never creates or deletes them. action=list shows the projects and the active id; action=use activates an existing project by name or id (the backend reconfigures and restarts, which can take a few seconds); action=status shows the active project and backend state. If there are no projects, ask the user to create one in the CA page.',
      args: {
        action: enumArg('list projects, activate an existing project, or show the active project status.', [
          'list',
          'use',
          'status',
        ], { required: true }),
        target: strArg(
          'Project name or id for action=use; must be an existing project (create projects in the CA page).',
          { maxLength: 256 },
        ),
        compact: boolArg('One line per project in action=list (default false).'),
      },
      async execute(args, exec) {
        const action = args.action
        if (action === 'list') {
          const list = await requireProjects(backend)
          return fmt.formatProjects(list, { compact: args.compact === true })
        }
        if (action === 'use') {
          if (!args.target) {
            throw new ToolError('INVALID_ARGUMENT', 'ca_project: "target" (project name or id) is required for action=use')
          }
          const list = await requireProjects(backend)
          const project = findProject(list.projects, args.target)
          if (!project) {
            const available = list.projects
              .map((item) => item.name || item.id)
              .filter(Boolean)
              .slice(0, 20)
              .join(', ')
            throw new ToolError(
              'PROJECT_NOT_FOUND',
              `no CA project named or with id "${args.target}"${available ? `; available projects: ${available}` : ''}. Projects are created in the CA page (one project = source tree + database + backend location).`,
            )
          }
          if (typeof backend.activateProject !== 'function') {
            throw new ToolError('BACKEND_UNSUPPORTED', 'This CA backend build cannot activate projects yet')
          }
          const result = await backend.activateProject(project.id)
          const active = result && typeof result === 'object' && result.project ? result.project : project
          return fmt.formatProjectActivated({
            project: active,
            config: result?.config,
            status: taskStatus(backend),
          })
        }
        // status: stay useful even when the project list cannot be read.
        let list = { projects: [], activeProjectId: null }
        try {
          const fetched = await fetchProjects(backend)
          if (fetched) list = fetched
        } catch {
          // Degrade: show the active project/backend state without the list.
        }
        const project = await fetchActiveProject(backend)
        return fmt.formatProjectStatus({ project, list, status: taskStatus(backend) })
      },
    },

    {
      name: 'ca_explain',
      title: 'CA AI explanation',
      description:
        'Explain one indexed symbol with evidence gathered from CA (signature, source window, callers/callees, branches, resources, findings) using the session\'s AI model. Use it for "why/how does this function work" questions instead of guessing; `question` focuses the explanation and `language` selects en/zh.',
      args: {
        symbol: strArg('Function name, id or a unique substring to explain.', {
          required: true,
          maxLength: 1024,
        }),
        question: strArg('Optional focus question or angle for the explanation.', { maxLength: 4000 }),
        language: enumArg('Explanation language (default en).', ['en', 'zh']),
      },
      async execute(args, exec) {
        if (!explain || typeof explain.explain !== 'function') {
          throw new ToolError(
            'EXPLAIN_UNAVAILABLE',
            'AI explanation is unavailable in this DSH session (no model selection service). Use ca_symbol/ca_facts/ca_source and explain manually.',
          )
        }
        await ensureBackend(backend, config, exec?.signal)
        const result = await explain.explain({
          symbol: args.symbol,
          language: args.language ?? 'en',
          question: args.question,
          signal: exec?.signal,
        })
        if (!result || typeof result.text !== 'string' || result.text.trim().length === 0) {
          throw new ToolError('EXPLAIN_FAILED', 'The CA explain service returned no explanation text.')
        }
        return fmt.formatExplain(result)
      },
    },

    {
      name: 'ca_task',
      title: 'CA long-running task',
      description:
        'Run and observe long CA CLI jobs: index, update, ensure_deep, prewarm, resolve_callbacks, watch. action=start requires kind (params holds CLI options such as repo/db/compdb/jobs; missing repo/db/compdb fall back to backend settings) and returns a task id immediately — poll action=logs (pass nextOffset as offset) or action=status; action=cancel aborts. Full indexing can take minutes to hours; never block waiting for it.',
      args: {
        action: enumArg('start a task, list tasks, show status, read logs, or cancel.', [
          'start',
          'list',
          'status',
          'logs',
          'cancel',
        ], { required: true }),
        kind: enumArg('Task kind for action=start.', TASK_KINDS),
        params: objectArg(
          'kind-specific CLI options for action=start (e.g. {"repo":"/src","db":"/d/x.db","jobs":8}; also changedFiles for update, symbols+compdb+repo for ensure_deep, limit for prewarm). Unknown keys are rejected with the supported list.',
        ),
        id: strArg('Task id for action=status|logs|cancel (e.g. "task-3").', { maxLength: 128 }),
        offset: intArg('Log line offset for action=logs; pass the previous nextOffset (default 0).', {
          min: 0,
          max: Number.MAX_SAFE_INTEGER,
        }),
        limit: intArg('Maximum log lines for action=logs, 1-2000 (default 200).', { min: 1, max: 2000 }),
        compact: boolArg('Shorten each log line to 500 characters (default false).'),
      },
      async execute(args, exec) {
        const action = args.action
        if (action === 'start') {
          if (!args.kind) throw new ToolError('INVALID_ARGUMENT', 'ca_task: "kind" is required for action=start')
          await ensureBackend(backend, config, exec?.signal)
          const task = tasks.start(args.kind, args.params ?? {})
          return `${fmt.formatTask(task)}\n\nnext: ca_task action=logs id=${task.id} offset=0 (poll with nextOffset), or action=status/cancel`
        }
        if (action === 'list') {
          return fmt.formatTaskList(tasks.list(), taskCounts(tasks))
        }
        if (!args.id) {
          throw new ToolError('INVALID_ARGUMENT', `ca_task: "id" is required for action=${action}`)
        }
        if (action === 'status') {
          const task = tasks.get(args.id)
          if (!task) throw new ToolError('TASK_NOT_FOUND', `ca_task: no task with id "${args.id}"`)
          return fmt.formatTask(task)
        }
        if (action === 'logs') {
          const logs = tasks.logs(args.id, {
            offset: args.offset ?? 0,
            limit: args.limit ?? 200,
          })
          return fmt.formatTaskLogs(logs, { compact: args.compact === true })
        }
        const task = tasks.cancel(args.id)
        return `${fmt.formatTask(task)}\n\nnote: cancellation aborts the CLI process; the task moves to cancelled once it exits.`
      },
    },
  ]

  return registerEffect(ctx, 'dsh-ca: native tools', () => {
    const disposers = specs.map((spec) => tools.register(makeTool(spec, maxOutput)))
    return () => {
      for (const dispose of disposers) {
        try {
          if (typeof dispose === 'function') dispose()
        } catch {
          // Registration disposal is best effort; the scope is going away.
        }
      }
    }
  })
}

/**
 * Run one registration inside the owning Cordis context so it disposes with
 * the plugin; falls back to a plain call in unit tests.
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

export { BACKEND_HINT }
