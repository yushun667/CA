/**
 * Text formatters for the native `ca_*` tools.
 *
 * Every formatter turns a CA `/api/*` payload into compact, model-readable
 * text: identity first, then the facts that answer the question, with explicit
 * counts and truncation markers. Pure functions only — no I/O, no DSH imports
 * — so they are cheap to unit test. The registration layer owns the final
 * character budget (`clipText`).
 */

import { formatBytes } from '../backend/util.js'
import { formatDuration, formatTime, numberLines } from './backend.js'

/** @param {unknown} value */
function text(value) {
  return value === undefined || value === null ? '' : String(value)
}

/** @param {unknown} value */
function oneLine(value) {
  return text(value).replace(/\s+/g, ' ').trim()
}

function location(row) {
  const file = text(row?.file)
  const line = Number(row?.line)
  if (!file) return ''
  return Number.isFinite(line) && line > 0 ? `${file}:${line}` : file
}

/** Header line for backend/health status. */
export function formatStatus({ status, health, counts, model } = {}) {
  const backendStatus = status ?? {}
  const mode = text(backendStatus.mode) || 'unconfigured'
  const state = text(backendStatus.state) || 'stopped'
  const lines = []
  const error = backendStatus.error
  const errorText = error ? (typeof error === 'string' ? error : error.message ?? JSON.stringify(error)) : ''
  lines.push(`CA backend: ${state} (mode: ${mode})${backendStatus.version ? `, version ${backendStatus.version}` : ''}`)
  if (backendStatus.baseUrl) lines.push(`base URL: ${backendStatus.baseUrl}`)
  if (backendStatus.db) lines.push(`database: ${backendStatus.db}`)
  if (Number.isFinite(backendStatus.since)) lines.push(`state since: ${formatTime(backendStatus.since)}`)
  if (errorText) lines.push(`backend error: ${oneLine(errorText)}`)
  if (model?.provider && model?.model) lines.push(`explain model: ${model.provider}/${model.model}`)
  if (counts) {
    lines.push(
      `tasks: running ${counts.running ?? 0}, queued ${counts.queued ?? 0}, succeeded ${counts.succeeded ?? 0}, failed ${counts.failed ?? 0}, cancelled ${counts.cancelled ?? 0}`,
    )
  }
  if (health && typeof health === 'object') {
    const db = health.database ?? {}
    const countsByTable = db.counts ?? {}
    lines.push(
      `health: ${text(health.status) || 'unknown'}${health.uptime_seconds !== undefined ? `, uptime ${Math.round(health.uptime_seconds)}s` : ''}${db.schema_version ? `, schema ${db.schema_version}` : ''}${Number.isFinite(db.db_bytes) ? `, db ${formatBytes(db.db_bytes)}` : ''}${health.features?.federated ? ', federated' : ''}`,
    )
    const tableSummary = Object.entries(countsByTable)
      .map(([name, value]) => `${name} ${value}`)
      .join(', ')
    if (tableSummary) lines.push(`index: ${tableSummary}`)
  } else if (state === 'running') {
    lines.push('health: unavailable (the CA query API did not answer /api/health)')
  }
  if (mode === 'unconfigured') {
    lines.push('hint: CA has no target yet; open the CA backend page to configure a local or remote Linux backend.')
  } else if (state !== 'running') {
    lines.push('hint: the first CA tool call tries to start a configured backend automatically; if that fails, start it from the CA backend page.')
  }
  return lines.join('\n')
}

/** Search results: `{query,items,count,limit,offset}`. */
export function formatSearch(data, { compact = false } = {}) {
  const query = text(data?.query)
  const items = Array.isArray(data?.items) ? data.items : []
  const lines = [`CA search "${query}" — ${items.length} row(s)${data?.federated ? ' (federated)' : ''}`]
  if (items.length === 0) {
    lines.push('No function matched. Try a shorter substring of a function or qualified name (symbol search, not full-text).')
    return lines.join('\n')
  }
  items.forEach((row, index) => {
    const name = text(row.name) || text(row.display_name) || '?'
    const qualified = text(row.qualified_name)
    const kind = text(row.kind) || 'function'
    const signature = oneLine(row.signature)
    const flag = row.is_definition === false ? ' (declaration)' : ''
    const head = `${(data?.offset ?? 0) + index + 1}. ${name}${qualified && qualified !== name ? ` [${qualified}]` : ''} — ${location(row) || '?'} (${kind})${flag}`
    lines.push(head)
    if (!compact) {
      if (signature) lines.push(`   ${signature}`)
      if (row.id) lines.push(`   id: ${row.id}`)
    }
  })
  const next = Number(data?.offset ?? 0) + items.length
  lines.push(`paging: limit ${data?.limit ?? items.length}, offset ${data?.offset ?? 0}; next offset ${next} if more rows exist`)
  return lines.join('\n')
}

/** Function detail: `{function,deep_ready,branches,assignments,returns,resource_ops,findings}`. */
export function formatSymbol(data, { compact = false } = {}) {
  const fn = data?.function
  if (!fn) {
    return 'CA symbol: not found (the detail endpoint returned no function record).'
  }
  const name = text(fn.name) || text(fn.display_name) || '?'
  const kind = text(fn.kind) || 'function'
  const deepReady = data?.deep_ready === true
  const branches = Array.isArray(data?.branches) ? data.branches : []
  const assignments = Array.isArray(data?.assignments) ? data.assignments : []
  const returns = Array.isArray(data?.returns) ? data.returns : []
  const resources = Array.isArray(data?.resource_ops) ? data.resource_ops : []
  const findings = Array.isArray(data?.findings) ? data.findings : []

  const lines = [`CA symbol: ${name} (${kind}${deepReady ? ', deep_ready' : ', deep facts not analyzed'})`]
  if (fn.qualified_name && fn.qualified_name !== name) lines.push(`qualified: ${fn.qualified_name}`)
  if (fn.signature) lines.push(`signature: ${oneLine(fn.signature)}`)
  lines.push(`location: ${location(fn) || '?'}${Number(fn.end_line) > 0 ? `-${fn.end_line}` : ''}`)
  if (fn.id) lines.push(`id: ${fn.id}`)
  const attributes = []
  if (fn.return_type) attributes.push(`returns ${oneLine(fn.return_type)}`)
  if (fn.linkage) attributes.push(`linkage ${fn.linkage}`)
  if (fn.is_static === true) attributes.push('static')
  if (fn.is_inline === true) attributes.push('inline')
  if (fn.is_virtual === true) attributes.push('virtual')
  if (Array.isArray(fn.params) && fn.params.length > 0) {
    attributes.push(`params ${fn.params.length}`)
  }
  if (attributes.length > 0) lines.push(attributes.join(' · '))
  lines.push(
    `facts: branches ${branches.length}, assignments ${assignments.length}, returns ${returns.length}, resources ${resources.length}, findings ${findings.length}`,
  )

  if (!deepReady) {
    lines.push(
      'hint: deep facts are missing; start ca_task kind=ensure_deep with {"symbols":["' +
        name +
        '"]} (repo/db/compdb come from the backend settings) to analyze this function.',
    )
  }

  if (!compact) {
    if (branches.length > 0) {
      lines.push('key branches:')
      for (const branch of branches.slice(0, 8)) {
        const condition = oneLine(branch.condition) || nextCondition(branch)
        const flags = []
        if (branch.kind) flags.push(text(branch.kind))
        if (branch.is_negated === true) flags.push('negated')
        lines.push(`  L${branch.line ?? '?'}: ${condition}${flags.length ? ` [${flags.join(', ')}]` : ''}`)
      }
      if (branches.length > 8) lines.push(`  …(+${branches.length - 8} more branches; use ca_facts kind=branches)`)
    }
    if (returns.length > 0) {
      const rendered = returns
        .slice(0, 4)
        .map((row) => `L${row.line ?? '?'}: ${oneLine(row.expr) || '(empty return)'}`)
      lines.push(`returns: ${rendered.join(' · ')}${returns.length > 4 ? ` …(+${returns.length - 4})` : ''}`)
    }
    if (resources.length > 0) {
      const rendered = resources
        .slice(0, 5)
        .map((row) => `L${row.line ?? '?'}: ${text(row.op)} ${text(row.api_name) || text(row.target)}`)
      lines.push(`resources: ${rendered.join(' · ')}${resources.length > 5 ? ` …(+${resources.length - 5})` : ''}`)
    }
    if (assignments.length > 0) {
      lines.push(`assignments: ${assignments.length} (use ca_facts kind=assignments for lhs/rhs)`)
    }
    if (findings.length > 0) {
      lines.push('findings:')
      for (const finding of findings.slice(0, 5)) {
        const tags = [text(finding.kind), text(finding.severity), text(finding.resource_kind)].filter(Boolean)
        lines.push(`  ${tags.length ? `[${tags.join('/')}] ` : ''}${oneLine(finding.message) || '(no message)'}`)
      }
      if (findings.length > 5) lines.push(`  …(+${findings.length - 5} more findings)`)
    }
  } else if (branches.length > 0) {
    const first = branches[0]
    lines.push(`first branch: L${first.line ?? '?'}: ${oneLine(first.condition)}`)
  }
  lines.push('next: ca_source for code, ca_graph for callers/callees, ca_facts for complete branch/assignment lists')
  return lines.join('\n')
}

function nextCondition(branch) {
  const trueBlock = text(branch.true_block)
  const falseBlock = text(branch.false_block)
  if (trueBlock || falseBlock) return `T:${trueBlock || '?'} F:${falseBlock || '?'}`
  return '(condition unavailable)'
}

/** Call graph: `{root,direction,depth,nodes,edges,cycles,truncated,stats}`. */
export function formatGraph(data, { compact = false } = {}) {
  const direction = text(data?.direction) || 'forward'
  const directionText =
    direction === 'forward' ? 'callees' : direction === 'reverse' ? 'callers' : 'both directions'
  const nodes = Array.isArray(data?.nodes) ? data.nodes : []
  const edges = Array.isArray(data?.edges) ? data.edges : []
  const cycles = Array.isArray(data?.cycles) ? data.cycles : []
  const stats = data?.stats ?? {}
  const lines = [
    `CA call graph: ${text(data?.root) || 'root'} · ${direction} (${directionText}) · depth ${data?.depth ?? '?'}`,
    `nodes ${stats.nodes ?? nodes.length}, edges ${stats.edges ?? edges.length}, cycles ${stats.cycles ?? cycles.length}${data?.truncated ? ', truncated' : ''}${data?.federated ? ', federated' : ''}`,
  ]
  if (data?.found === false || (nodes.length === 0 && edges.length === 0)) {
    lines.push('No call-graph nodes matched this symbol. Check the spelling with ca_search; the symbol may be a declaration or not indexed.')
    return lines.join('\n')
  }
  if (compact) {
    if (cycles.length > 0) lines.push(`cycles: ${cycles.map((cycle) => oneLine(JSON.stringify(cycle))).join(' · ')}`)
    return lines.join('\n')
  }
  const edgeLimit = 120
  if (edges.length > 0) {
    lines.push('edges (caller -> callee, call site):')
    for (const edge of edges.slice(0, edgeLimit)) {
      const marker =
        edge.candidate === true || edge.source === 'candidate'
          ? ` [${text(edge.kind) || 'candidate'} candidate]`
          : edge.source === 'direct'
            ? ' [direct]'
            : ''
      const callSite = location({ file: edge.call_file, line: edge.call_line })
      lines.push(`  ${text(edge.caller_name) || '?'} -> ${text(edge.callee_name) || '?'}${marker}${callSite ? ` @ ${callSite}` : ''}`)
    }
    if (edges.length > edgeLimit) {
      lines.push(`  …(+${edges.length - edgeLimit} more edges; raise limit or narrow depth/direction)`)
    }
  }
  if (cycles.length > 0) {
    lines.push(`cycles: ${cycles.slice(0, 5).map((cycle) => oneLine(JSON.stringify(cycle))).join(' · ')}${cycles.length > 5 ? ` …(+${cycles.length - 5})` : ''}`)
  }
  return lines.join('\n')
}

const FACT_TITLES = {
  branches: 'branch/condition facts',
  assignments: 'assignment facts',
  returns: 'return facts',
  resources: 'resource operations',
  findings: 'data-flow findings',
}

/** One fact kind: kind-specific row rendering plus pagination footer. */
export function formatFacts(kind, data, { compact = false, offset = 0, limit = 50 } = {}) {
  const items = Array.isArray(data?.items) ? data.items : []
  const title = FACT_TITLES[kind] ?? `${kind} facts`
  const lines = [`CA ${title} for "${text(data?.symbol) || '?'}" — ${items.length} row(s) (offset ${offset}, limit ${limit})`]
  if (items.length === 0) {
    lines.push('No rows returned. If ca_symbol shows deep facts as not analyzed, run ca_task kind=ensure_deep first; else the symbol may not contain this fact kind.')
    return lines.join('\n')
  }
  items.forEach((row) => {
    lines.push(formatFactRow(kind, row, compact))
  })
  if (items.length >= limit) {
    lines.push(`paging: pass offset=${offset + items.length} (limit+offset must stay within 2000) for the next page`)
  }
  return lines.join('\n')
}

function formatFactRow(kind, row, compact) {
  const line = `L${row.line ?? '?'}${row.col !== undefined ? `:${row.col}` : ''}`
  switch (kind) {
    case 'branches': {
      const flags = [text(row.kind), row.is_negated === true ? 'negated' : ''].filter(Boolean)
      const targets = [row.true_block ? `T:${row.true_block}` : '', row.false_block ? `F:${row.false_block}` : ''].filter(Boolean)
      return `${line} ${oneLine(row.condition) || nextCondition(row)}${flags.length ? ` [${flags.join(', ')}]` : ''}${targets.length ? ` ${targets.join(' ')}` : ''}`
    }
    case 'assignments':
      return `${line} ${oneLine(row.lhs) || '?'} = ${oneLine(row.rhs) || '?'}${row.operator && row.operator !== '=' ? ` (${row.operator})` : ''}${row.type_spelling ? ` : ${oneLine(row.type_spelling)}` : ''}${row.is_decl_init === true ? ' [declaration init]' : ''}`
    case 'returns':
      return `${line} return ${oneLine(row.expr) || '(empty)'}${row.is_implicit === true ? ' [implicit]' : ''}`
    case 'resources': {
      const target = [text(row.api_name), text(row.target)].filter(Boolean)
      return `${line} ${text(row.op) || '?'} ${target.join(' ')}${row.resource_kind ? ` [${row.resource_kind}]` : ''}`
    }
    case 'findings': {
      const tags = [text(row.kind), text(row.severity), text(row.resource_kind)].filter(Boolean)
      const evidence = compact ? '' : summarizeEvidence(row)
      return `${line} ${tags.length ? `[${tags.join('/')}] ` : ''}${oneLine(row.message) || '(no message)'}${evidence ? ` — ${evidence}` : ''}`
    }
    default:
      return `${line} ${oneLine(JSON.stringify(row))}`
  }
}

function summarizeEvidence(row) {
  const evidence = row.evidence
  if (evidence === undefined || evidence === null) return ''
  try {
    const serialized = JSON.stringify(evidence)
    return serialized.length > 300 ? `${serialized.slice(0, 300)}…` : serialized
  } catch {
    return ''
  }
}

/** Source window: `{function,file,start,end,lines,truncated}`. */
export function formatSource(data, { compact = false } = {}) {
  const fn = data?.function ?? {}
  const lines = Array.isArray(data?.lines) ? data.lines : []
  const name = text(fn.name) || text(data?.symbol) || '?'
  const header = `CA source: ${name} — ${text(data?.file) || '?'}:${data?.function_start ?? '?'}-${data?.function_end ?? '?'} (lines ${data?.start ?? '?'}-${data?.end ?? '?'} of ${data?.total_lines ?? '?'})`
  const result = [header]
  if (!compact) {
    if (fn.signature) result.push(`signature: ${oneLine(fn.signature)}`)
    if (data?.language) result.push(`language: ${text(data.language)}${data?.repo_root ? ` · repo: ${text(data.repo_root)}` : ''}`)
    if (data?.federated) result.push('source: federated shard')
  }
  result.push(numberLines(lines))
  if (data?.truncated) result.push('note: CA truncated the window at 400 lines (function body is longer).')
  if (lines.length === 0) result.push('No source lines available.')
  return result.join('\n')
}

/** Index statistics. */
export function formatStats(data, { compact = false } = {}) {
  const tables = data?.tables ?? {}
  const preferred = [
    'functions',
    'types',
    'fields',
    'calls',
    'branches',
    'assignments',
    'returns',
    'resource_ops',
    'findings',
    'callback_registrations',
    'tus',
  ]
  const tableParts = []
  for (const name of preferred) {
    if (tables[name] !== undefined) tableParts.push(`${name} ${tables[name]}`)
  }
  const known = new Set(preferred)
  const others = Object.entries(tables).filter(([name]) => !known.has(name))
  if (tableParts.length === 0) {
    for (const [name, value] of Object.entries(tables)) tableParts.push(`${name} ${value}`)
  }
  const lines = ['CA index stats:']
  lines.push(
    `database: ${Number.isFinite(data?.db_bytes) ? formatBytes(data.db_bytes) : 'unknown'}${data?.schema_version ? ` · schema ${data.schema_version}` : ''}${data?.sqlite_version ? ` · sqlite ${data.sqlite_version}` : ''}${data?.federated ? ' · federated' : ''}`,
  )
  if (tableParts.length > 0) lines.push(`tables: ${tableParts.join(', ')}`)
  if (!compact) {
    if (others.length > 0) lines.push(`other tables: ${others.map(([name, value]) => `${name} ${value}`).join(', ')}`)
    const callbacks = data?.callbacks
    if (callbacks && typeof callbacks === 'object') {
      const summary = Object.entries(callbacks)
        .map(([name, value]) => `${name} ${typeof value === 'object' ? JSON.stringify(value) : value}`)
        .join(', ')
      if (summary) lines.push(`callbacks: ${summary}`)
    }
    const database = data?.database
    if (database && typeof database === 'object') {
      const paths = Array.isArray(database.paths) ? database.paths.filter(Boolean) : [database.path]
      if (paths.length > 0) lines.push(`paths: ${paths.slice(0, 4).join(', ')}${paths.length > 4 ? ` …(+${paths.length - 4})` : ''}`)
      if (Array.isArray(database.shards) && database.shards.length > 0) lines.push(`shards: ${database.shards.length}`)
    }
  }
  return lines.join('\n')
}

/** Cross-TU callback registrations. */
export function formatCallbacks(data, { compact = false, offset = 0, limit = 50 } = {}) {
  const items = Array.isArray(data?.items) ? data.items : []
  const filters = data?.filters ?? {}
  const filterText = Object.entries(filters)
    .map(([key, value]) => `${key}=${value}`)
    .join(', ')
  const lines = [
    `CA callback registrations${filterText ? ` (${filterText})` : ''} — ${items.length} row(s) (offset ${offset}, limit ${limit})`,
  ]
  if (items.length === 0) {
    lines.push('No callback registrations matched. Filters match exact values except path, which is a prefix; drop filters or widen the path prefix.')
    return lines.join('\n')
  }
  for (const row of items) {
    const target = text(row.target_function_name) || text(row.target_function_id) || '?'
    const kind = text(row.registration_kind) || 'registration'
    const objectPath = text(row.object_path) || '?'
    const field = text(row.object_field)
    const where = location(row)
    lines.push(
      `${objectPath}${field ? `.${field}` : ''} -> ${target} [${kind}]${text(row.module) ? ` module ${text(row.module)}` : ''}${where ? ` @ ${where}` : ''}`,
    )
    if (!compact && row.evidence_json !== undefined) {
      const evidence = text(typeof row.evidence_json === 'string' ? row.evidence_json : JSON.stringify(row.evidence_json))
      if (evidence) lines.push(`   evidence: ${oneLine(evidence).slice(0, 240)}`)
    }
  }
  if (items.length >= limit) {
    lines.push(`paging: pass offset=${offset + items.length} for the next page`)
  }
  return lines.join('\n')
}

/** Workspace/project/variant registry snapshot. */
export function formatRegistry(data, { compact = false } = {}) {
  const workspaces = Array.isArray(data?.workspaces) ? data.workspaces : []
  const projects = Array.isArray(data?.projects) ? data.projects : []
  const variants = Array.isArray(data?.variants) ? data.variants : []
  const active = data?.active_variant
  const lines = [`CA registry: ${workspaces.length} workspace(s), ${projects.length} project(s), ${variants.length} variant(s)${data?.available ? '' : ' (no registry rows)'}`]
  if (active) {
    lines.push(`active variant: ${text(active.name) || text(active.id) || '?'}${active.id ? ` (id ${active.id})` : ''}${text(active.db_path) ? ` db ${text(active.db_path)}` : ''}`)
  } else {
    lines.push('active variant: none (the service queries its configured database)')
  }
  if (!compact) {
    if (workspaces.length > 0) {
      lines.push('workspaces:')
      for (const row of workspaces.slice(0, 20)) {
        lines.push(`  ${text(row.name) || text(row.id) || '?'} — ${text(row.path) || '?'}${text(row.kind) ? ` (${text(row.kind)})` : ''}`)
      }
      if (workspaces.length > 20) lines.push(`  …(+${workspaces.length - 20} more)`)
    }
    if (projects.length > 0) {
      lines.push('projects:')
      for (const row of projects.slice(0, 20)) {
        lines.push(`  ${text(row.name) || text(row.id) || '?'} — ${text(row.path) || '?'}${text(row.workspace_name) ? ` (workspace ${text(row.workspace_name)})` : ''}`)
      }
      if (projects.length > 20) lines.push(`  …(+${projects.length - 20} more)`)
    }
    if (variants.length > 0) {
      lines.push('variants:')
      for (const row of variants.slice(0, 20)) {
        const activeId = data?.active_variant_id
        const marker = activeId && row.id === activeId ? ' [active]' : ''
        lines.push(`  ${text(row.name) || text(row.id) || '?'}${marker}${text(row.db_path) ? ` — ${text(row.db_path)}` : ''}`)
      }
      if (variants.length > 20) lines.push(`  …(+${variants.length - 20} more)`)
    }
    if (data?.registry_source) lines.push(`registry: ${text(data.registry_source)}`)
  }
  return lines.join('\n')
}

/** AI explanation result with provenance footer. */
export function formatExplain(result) {
  const body = text(result?.text).trim()
  const model = result?.model
  const modelText = model?.provider && model?.model ? `${model.provider}/${model.model}` : 'unknown model'
  const evidence = Number.isFinite(Number(result?.evidenceCount)) ? Number(result.evidenceCount) : 0
  const footer = `— ${text(result?.symbol) || 'symbol'} · ${modelText} · ${evidence} evidence item(s)`
  return body ? `${body}\n\n${footer}` : footer
}

/** One task summary. */
export function formatTask(task) {
  if (!task) return 'CA task: not found'
  const lines = [`CA task ${text(task.id)}: ${text(task.status)} (${text(task.kind)})`]
  if (task.createdAt) lines.push(`created: ${formatTime(task.createdAt)}${task.startedAt ? ` · started ${formatTime(task.startedAt)}` : ''}${task.finishedAt ? ` · finished ${formatTime(task.finishedAt)}` : ''}`)
  if (task.finishedAt && task.startedAt) lines.push(`duration: ${formatDuration(task.startedAt, task.finishedAt)}`)
  if (task.exitCode !== undefined && task.exitCode !== null) lines.push(`exit code: ${task.exitCode}`)
  if (task.logBytes !== undefined) lines.push(`log bytes: ${formatBytes(task.logBytes)}`)
  const params = task.params && typeof task.params === 'object' ? task.params : {}
  const shown = Object.entries(params)
    .slice(0, 8)
    .map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(',') : oneLine(value)}`)
  if (shown.length > 0) lines.push(`params: ${shown.join(' ')}`)
  if (task.error) {
    const code = text(task.error.code)
    const message = text(task.error.message)
    lines.push(`error: ${code ? `[${code}] ` : ''}${oneLine(message) || '(no message)'}`)
    const stderrTail = task.error.details?.stderrTail
    if (Array.isArray(stderrTail) && stderrTail.length > 0) {
      lines.push('stderr tail:')
      for (const line of stderrTail.slice(-5)) lines.push(`  ${oneLine(line)}`)
    }
  }
  return lines.join('\n')
}

/** Task list, newest first. */
export function formatTaskList(tasks, counts) {
  const rows = Array.isArray(tasks) ? tasks : []
  const lines = [`CA tasks: ${rows.length}${counts ? ` (running ${counts.running}, queued ${counts.queued}, succeeded ${counts.succeeded}, failed ${counts.failed}, cancelled ${counts.cancelled})` : ''}`]
  if (rows.length === 0) {
    lines.push('No CA tasks in this session. Start one with ca_task action=start.')
    return lines.join('\n')
  }
  for (const task of rows.slice(0, 50)) {
    const duration = task.startedAt ? formatDuration(task.startedAt, task.finishedAt) : '—'
    lines.push(
      `${text(task.id)}  ${text(task.kind)}  ${text(task.status)}  ${duration}${task.error ? `  error ${text(task.error.code) || ''}` : ''}`,
    )
  }
  if (rows.length > 50) lines.push(`…(+${rows.length - 50} more; the RPC tools page shows all)`)
  lines.push('next: ca_task action=status|logs with id, or action=cancel to abort a queued/running task')
  return lines.join('\n')
}

/** Task log page. */
export function formatTaskLogs(logs, { compact = false } = {}) {
  if (!logs || !Array.isArray(logs.lines)) return 'CA task logs: not found'
  const first = logs.lines.length > 0 ? logs.lines[0].n : logs.offset
  const last = logs.lines.length > 0 ? logs.lines[logs.lines.length - 1].n : logs.offset - 1
  const lines = [
    `CA task ${text(logs.id)} logs — lines ${first}-${last} of ${logs.total} (limit ${logs.limit}, ${formatBytes(logs.logBytes)} buffered${logs.droppedLines ? `, ${logs.droppedLines} early lines dropped` : ''})`,
  ]
  if (logs.clipped) lines.push('note: the requested offset was older than the retained ring buffer; showing the oldest available line.')
  if (logs.lines.length === 0) {
    lines.push(logs.total > 0 ? `no new lines after line ${Math.max(0, logs.offset - 1)}` : 'no log output yet')
  } else {
    for (const line of logs.lines) {
      const marker = line.stream === 'stderr' ? '!' : ' '
      lines.push(`${String(line.n).padStart(5, ' ')}${marker} ${compact ? oneLine(line.text).slice(0, 500) : line.text}`)
    }
  }
  if (logs.nextOffset === null) lines.push('nextOffset: null (no new lines; poll again with the same offset)')
  else lines.push(`nextOffset: ${logs.nextOffset} (pass as offset to fetch only new lines)`)
  return lines.join('\n')
}
