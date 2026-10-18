/**
 * Host side of the Client <-> Host channel (`/dsh-ca`).
 *
 * Transport is a DSH-owned same-origin HTTP route rather than
 * `ctx.connection.rpc.handle`: the native RPC registry requires services that a
 * plain-JS plugin context cannot read (activation fails with
 * "cannot get property webServer without inject"), so the plugin registers one
 * `ctx.webServer.register({kind:'prefix', path:'/dsh-ca', handler})` route.
 *
 *   Host:   POST /dsh-ca/<endpoint>  (DSH auth via connection.requestRejection)
 *   Client: fetch('/dsh-ca/<endpoint>', {method:'POST', body: JSON payload})
 *
 * The route never throws: every outcome is an envelope
 * `{ok:true,value}` or `{ok:false,error:{code,message,details}}`, and every
 * payload is validated (shape, ranges, path allow-list, byte budgets) before
 * it reaches a service. Stable error codes let the UI switch on failures.
 *
 * HTTP statuses: business outcomes 200, invalid arguments/paths 400, oversized
 * bodies 413, unknown endpoints 404, non-POST 405, trust rejection 401/403.
 */

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { BackendError, isAbortError, isBackendError, normalizeError } from '../backend/errors.js'
import { atomicWriteJson, clampInt, isPlainObject } from '../backend/util.js'
import { fetchActiveProject } from '../tools/projects.js'
import { browseLocalDirs, MAX_LOCAL_PATH_LENGTH } from '../backend/localbrowse.js'

export const CHANNEL = '/dsh-ca'
export const PLUGIN_VERSION = '0.1.0'

/** Endpoints exposed on the channel; kept in sync with the dispatcher. */
export const RPC_ENDPOINTS = Object.freeze([
  'ping',
  'status',
  'ca.request',
  'backend.get',
  'backend.configure',
  'backend.start',
  'backend.stop',
  'backend.restart',
  'backend.probe',
  'backend.install',
  'backend.installLocal',
  'backend.installStatus',
  'backend.databases',
  'backend.useDatabase',
  'projects.list',
  'projects.save',
  'projects.remove',
  'projects.activate',
  'projects.createSimple',
  'projects.prepare',
  'remote.connect',
  'remote.browse',
  'local.browse',
  'ui.selftest.pending',
  'ui.selftest.report',
  'tasks.start',
  'tasks.list',
  'tasks.get',
  'tasks.logs',
  'tasks.cancel',
  'explain',
])

const MAX_PAYLOAD_BYTES = 512 * 1024
const MAX_CONFIG_PATCH_BYTES = 64 * 1024
const MAX_CA_BODY_BYTES = 1024 * 1024
const MAX_PATH_LENGTH = 2048
const MAX_SSH_BYTES = 16 * 1024
const MAX_OPTIONS_BYTES = 64 * 1024
const MAX_QUERY_ENTRIES = 64
const MAX_QUERY_VALUE_LENGTH = 2048
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024
const MAX_HTTP_BODY_BYTES = 1024 * 1024
const MAX_SYMBOL_LENGTH = 1024
const MAX_QUESTION_LENGTH = 4000
const MAX_ID_LENGTH = 128
const MAX_DB_PATH_LENGTH = 4096
const MAX_PROJECT_ID_LENGTH = 256
const SELFTEST_REPORT_MAX_BYTES = 512 * 1024
const MAX_INSTALL_PATH_LENGTH = 4096
const MAX_PYTHON_ARG_LENGTH = 1024
const MAX_PASSWORD_LENGTH = 1024
const MAX_REMOTE_PATH_LENGTH = 4096

/** Stable RPC error carrying a machine-readable code. */
export class RpcError extends Error {
  /**
   * @param {string} code stable uppercase code, e.g. INVALID_ARGUMENT
   * @param {string} message actionable user-facing message
   * @param {Record<string, unknown>} [details] small JSON-serializable extras
   */
  constructor(code, message, details) {
    super(message)
    this.name = 'RpcError'
    this.code = code
    if (details !== undefined) this.details = details
  }
}

function fail(code, message, details) {
  throw new RpcError(code, message, details)
}

// --------------------------------------------------------------------------- //
// Payload helpers
// --------------------------------------------------------------------------- //

function jsonBytes(value) {
  try {
    return Buffer.byteLength(JSON.stringify(value), 'utf8')
  } catch {
    return Number.POSITIVE_INFINITY
  }
}

/** JSON-safe clone; drops functions/undefined; never throws for JSON input. */
function jsonSafe(value) {
  if (value === undefined) return undefined
  try {
    return JSON.parse(JSON.stringify(value))
  } catch {
    return undefined
  }
}

/** @param {unknown} payload */
function validatePayload(payload) {
  if (payload === undefined || payload === null) return {}
  if (!isPlainObject(payload)) {
    fail('INVALID_ARGUMENT', 'RPC payload must be a JSON object')
  }
  if (jsonBytes(payload) > MAX_PAYLOAD_BYTES) {
    fail('PAYLOAD_TOO_LARGE', `RPC payload exceeds ${MAX_PAYLOAD_BYTES} bytes`)
  }
  return payload
}

/**
 * @param {Record<string, unknown>} payload
 * @param {string} name
 * @param {{required?: boolean, maxLength?: number}} [options]
 * @returns {string|undefined}
 */
function optionalString(payload, name, options = {}) {
  const value = payload[name]
  if (value === undefined || value === null) {
    if (options.required) fail('INVALID_ARGUMENT', `payload.${name} is required`)
    return undefined
  }
  if (typeof value !== 'string') {
    fail('INVALID_ARGUMENT', `payload.${name} must be a string`)
  }
  const text = value.trim()
  if (text.length === 0) {
    if (options.required) fail('INVALID_ARGUMENT', `payload.${name} must not be empty`)
    return undefined
  }
  const maxLength = options.maxLength ?? 4096
  if (text.length > maxLength) {
    fail('INVALID_ARGUMENT', `payload.${name} is too long (maximum ${maxLength} characters)`)
  }
  return text
}

/**
 * @param {Record<string, unknown>} payload
 * @param {string} name
 * @returns {number|undefined}
 */
function optionalInt(payload, name, min, max) {
  const value = payload[name]
  if (value === undefined || value === null) return undefined
  const number = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : NaN
  if (!Number.isFinite(number) || !Number.isInteger(number)) {
    fail('INVALID_ARGUMENT', `payload.${name} must be an integer`)
  }
  if (number < min || number > max) {
    fail('INVALID_ARGUMENT', `payload.${name} must be between ${min} and ${max}`)
  }
  return number
}

/** Optional boolean payload field (accepts JSON booleans only). */
function optionalBool(payload, name) {
  const value = payload[name]
  if (value === undefined || value === null) return undefined
  if (typeof value === 'boolean') return value
  fail('INVALID_ARGUMENT', `payload.${name} must be a boolean`)
}

/** Validate a database file or directory path coming from the UI. */
function validateDatabasePath(value) {
  if (typeof value !== 'string') fail('INVALID_ARGUMENT', 'payload.path must be a string')
  const path = value.trim()
  if (path.length === 0) fail('INVALID_ARGUMENT', 'payload.path must not be empty')
  if (path.length > MAX_DB_PATH_LENGTH) {
    fail('INVALID_ARGUMENT', `payload.path is too long (maximum ${MAX_DB_PATH_LENGTH} characters)`)
  }
  if (/[\0\r\n]/.test(path)) {
    fail('INVALID_ARGUMENT', 'payload.path must not contain NUL or newline characters')
  }
  if (!path.startsWith('/')) {
    fail('INVALID_ARGUMENT', 'payload.path must be an absolute path (start with "/")')
  }
  if (path.split('/').includes('..')) {
    fail('INVALID_ARGUMENT', 'payload.path must not contain ".." segments')
  }
  return path
}

/** Validate one installer field value (mode/offline/wheelhousePath/installDir/python). */
function normalizeInstallValue(name, value) {
  if (value === undefined || value === null) return undefined
  switch (name) {
    case 'mode': {
      if (typeof value !== 'string') {
        fail('INVALID_ARGUMENT', 'payload.mode must be "wheel" or "source"')
      }
      const mode = value.trim().toLowerCase()
      if (mode !== 'wheel' && mode !== 'source') {
        fail('INVALID_ARGUMENT', 'payload.mode must be "wheel" or "source"')
      }
      return mode
    }
    case 'offline': {
      if (typeof value !== 'boolean') fail('INVALID_ARGUMENT', 'payload.offline must be a boolean')
      return value
    }
    case 'wheelhousePath':
    case 'installDir': {
      if (typeof value !== 'string') fail('INVALID_ARGUMENT', `payload.${name} must be a string`)
      const text = value.trim()
      if (text.length === 0) fail('INVALID_ARGUMENT', `payload.${name} must not be empty`)
      if (text.length > MAX_INSTALL_PATH_LENGTH) {
        fail('INVALID_ARGUMENT', `payload.${name} is too long (maximum ${MAX_INSTALL_PATH_LENGTH} characters)`)
      }
      if (/[\0\r\n]/.test(text)) {
        fail('INVALID_ARGUMENT', `payload.${name} must not contain NUL or newline characters`)
      }
      if (!path.isAbsolute(text)) {
        fail('INVALID_ARGUMENT', `payload.${name} must be an absolute path`)
      }
      if (text.split(/[\\/]/).includes('..')) {
        fail('INVALID_ARGUMENT', `payload.${name} must not contain ".." segments`)
      }
      return text
    }
    case 'python': {
      if (typeof value !== 'string') fail('INVALID_ARGUMENT', 'payload.python must be a string')
      const text = value.trim()
      if (text.length === 0) fail('INVALID_ARGUMENT', 'payload.python must not be empty')
      if (text.length > MAX_PYTHON_ARG_LENGTH) {
        fail('INVALID_ARGUMENT', `payload.python is too long (maximum ${MAX_PYTHON_ARG_LENGTH} characters)`)
      }
      if (/[\0\r\n]/.test(text)) {
        fail('INVALID_ARGUMENT', 'payload.python must not contain NUL or newline characters')
      }
      return text
    }
    default:
      fail('INTERNAL', `unknown installer option ${name}`)
  }
}

/**
 * Collect installer options from the payload, falling back to a nested legacy
 * options object. `mode` defaults to `wheel` (source installs are opt-in).
 */
function pickInstallerOptions(payload, fallback = {}) {
  const options = {}
  for (const name of ['mode', 'offline', 'wheelhousePath', 'installDir', 'python']) {
    const raw = payload[name] !== undefined ? payload[name] : fallback[name]
    const normalized = normalizeInstallValue(name, raw)
    if (normalized !== undefined) options[name] = normalized
  }
  if (options.mode === undefined) options.mode = 'wheel'
  return options
}

/** Shape one installation scope from a manager report or local tracker. */
function normalizeInstallScope(primary, fallback) {
  const source = isPlainObject(primary) ? primary : isPlainObject(fallback) ? fallback : null
  if (!source) {
    return {
      started: false,
      state: 'idle',
      status: 'idle',
      startedAt: null,
      finishedAt: null,
      steps: [],
      logs: [],
      error: null,
      result: null,
    }
  }
  const safe = jsonSafe(source) ?? {}
  const state =
    typeof safe.state === 'string'
      ? safe.state
      : typeof safe.status === 'string'
        ? safe.status
        : 'idle'
  return {
    ...safe,
    started: safe.started === true || state !== 'idle',
    state,
    status: state,
    startedAt: Number.isFinite(Number(safe.startedAt)) ? Number(safe.startedAt) : null,
    finishedAt: Number.isFinite(Number(safe.finishedAt)) ? Number(safe.finishedAt) : null,
    steps: Array.isArray(safe.steps) ? safe.steps.slice(0, 200) : [],
    logs: Array.isArray(safe.logs) ? safe.logs.slice(-500) : [],
    error: isPlainObject(safe.error) ? safe.error : safe.error ?? null,
    result: safe.result ?? null,
  }
}

/** Normalize a prepare snapshot (`getPrepareStatus()` / getInstallStatus().prepare). */
function normalizePrepareStatus(value) {
  if (!isPlainObject(value)) return null
  const safe = jsonSafe(value) ?? {}
  return {
    state: typeof safe.state === 'string' ? safe.state : 'idle',
    projectId: typeof safe.projectId === 'string' ? safe.projectId : null,
    installScope:
      safe.installScope === 'local' || safe.installScope === 'remote' ? safe.installScope : null,
    startedAt: Number.isFinite(Number(safe.startedAt)) ? Number(safe.startedAt) : null,
    finishedAt: Number.isFinite(Number(safe.finishedAt)) ? Number(safe.finishedAt) : null,
    error: isPlainObject(safe.error) ? safe.error : safe.error ?? null,
    result: safe.result ?? null,
    ...(typeof safe.scope === 'string' ? { scope: safe.scope } : {}),
  }
}

/** Legacy top-level aliases (old clients read value.status/state/steps/logs). */
function installScopeAliases(scope) {
  return {
    started: scope.started,
    state: scope.state,
    status: scope.status,
    startedAt: scope.startedAt,
    finishedAt: scope.finishedAt,
    steps: scope.steps,
    logs: scope.logs,
    error: scope.error,
    result: scope.result,
  }
}

/**
 * Merge manager install status (new `{local,remote}` or legacy remote shape)
 * with the RPC-side trackers.
 */
function mergeInstallStatus(managerStatus, fallback) {
  const manager = isPlainObject(managerStatus) ? managerStatus : null
  const mergedShape =
    manager !== null && (isPlainObject(manager.local) || isPlainObject(manager.remote))
  const legacyRemote = manager !== null && !mergedShape ? manager : null
  const local = normalizeInstallScope(manager?.local, fallback?.local)
  const remote = normalizeInstallScope(manager?.remote ?? legacyRemote, fallback?.remote)
  // The real manager mirrors the most recently active scope at the top level;
  // older/merged-only responses fall back to the remote scope (old behavior).
  const hasTopLevel =
    manager !== null &&
    (typeof manager.state === 'string' || Array.isArray(manager.steps) || manager.scope !== undefined)
  const aggregate = normalizeInstallScope(
    hasTopLevel ? manager : manager?.remote ?? legacyRemote,
    fallback?.remote,
  )
  return {
    local,
    remote,
    aggregate,
    scopes: [local, remote],
  }
}

/** Project one installation scope onto the compact install-info shape. */
function installInfoFromScope(scope) {
  if (!isPlainObject(scope)) return null
  const state = typeof scope.state === 'string' ? scope.state : 'idle'
  const result = isPlainObject(scope.result) ? scope.result : {}
  const pickString = (...values) => {
    for (const value of values) if (typeof value === 'string' && value.length > 0) return value
    return null
  }
  const rawVersion = scope.version !== undefined ? scope.version : result.version
  const version =
    typeof rawVersion === 'string'
      ? rawVersion
      : isPlainObject(rawVersion) && typeof rawVersion.version === 'string'
        ? rawVersion.version
        : null
  const installDir = pickString(scope.installDir, result.installDir)
  const venvDir = pickString(scope.venvDir, result.venvDir)
  const venvPython = pickString(scope.venvPython, result.venvPython)
  const caScript = pickString(scope.caScript, result.caScript)
  const mode = pickString(scope.mode, result.mode)
  const offline =
    typeof scope.offline === 'boolean'
      ? scope.offline
      : typeof result.offline === 'boolean'
        ? result.offline
        : null
  const installed =
    typeof scope.installed === 'boolean'
      ? scope.installed
      : state === 'succeeded' || installDir !== null
  const repoPath = pickString(scope.repoPath, result.repoPath)
  return {
    installed,
    state,
    installDir,
    venvDir,
    venvPython,
    caScript,
    version,
    mode,
    offline,
    repoPath,
  }
}

function normalizeWheelInfo(value) {
  if (!isPlainObject(value)) return null
  return {
    bundled: value.bundled === true,
    name: typeof value.name === 'string' ? value.name : null,
    version: typeof value.version === 'string' ? value.version : null,
    bytes: Number.isFinite(Number(value.bytes)) ? Number(value.bytes) : null,
    path: typeof value.path === 'string' ? value.path : null,
  }
}

function normalizeWheelhouseInfo(value) {
  if (!isPlainObject(value)) return null
  const platforms = []
  if (Array.isArray(value.availablePlatforms)) {
    for (const item of value.availablePlatforms.slice(0, 32)) {
      const platform =
        typeof item === 'string' ? item : isPlainObject(item) ? item.platform : null
      if (typeof platform === 'string' && platform.length > 0) platforms.push(platform)
    }
  }
  return {
    availablePlatforms: platforms,
    defaultPath: typeof value.defaultPath === 'string' ? value.defaultPath : null,
    defaultPlatform: typeof value.defaultPlatform === 'string' ? value.defaultPlatform : null,
    defaultExists: value.defaultExists === true,
    defaultWheelCount: Number.isFinite(Number(value.defaultWheelCount))
      ? Number(value.defaultWheelCount)
      : null,
  }
}

/** Normalize a manager install summary; null when nothing is available. */
function normalizeInstallInfo(value) {
  if (!isPlainObject(value)) return null
  const safe = jsonSafe(value) ?? {}
  const local = installInfoFromScope(safe.local)
  const remote = installInfoFromScope(safe.remote)
  const wheel = normalizeWheelInfo(safe.wheel)
  const wheelhouse = normalizeWheelhouseInfo(safe.wheelhouse)
  if (local === null && remote === null && wheel === null && wheelhouse === null) return null
  const platform = isPlainObject(safe.platform)
    ? {
        os: typeof safe.platform.os === 'string' ? safe.platform.os : null,
        arch: typeof safe.platform.arch === 'string' ? safe.platform.arch : null,
        platformId: typeof safe.platform.platformId === 'string' ? safe.platform.platformId : null,
        label: typeof safe.platform.label === 'string' ? safe.platform.label : null,
        supported: safe.platform.supported === true,
      }
    : null
  return { platform, local, remote, wheel, wheelhouse }
}

/**
 * Map backend mutation failures onto the small vocabulary the UI knows:
 * INVALID_ARGUMENT (bad input/config), DB_NOT_FOUND (missing database or
 * project target), and BACKEND_ERROR (anything else, original code kept in
 * details).
 */
function backendMutationFailure(error) {
  // BackendError instances and plain errors carrying a stable uppercase code
  // are both mapped; anything else falls through to INTERNAL.
  if (!error || typeof error !== 'object' || typeof error.code !== 'string') return error
  const details = detailsOf(error.details)
  const message = typeof error.message === 'string' ? error.message : String(error.code)
  if (
    error.code === 'DB_NOT_FOUND' ||
    error.code === 'PROJECT_NOT_FOUND' ||
    error.code === 'NOT_FOUND'
  ) {
    return new RpcError('DB_NOT_FOUND', message, details)
  }
  if (error.code === 'INVALID_ARGUMENT' || error.code === 'CONFIG_INVALID') {
    return new RpcError('INVALID_ARGUMENT', message, details)
  }
  return new RpcError('BACKEND_ERROR', message, { ...details, backendCode: error.code })
}

/** Directory holding CA plugin state (settings/selftest files). */
function resolveStateDir(backend, config) {
  for (const candidate of [backend?.paths?.stateDir, config?.paths?.stateDir]) {
    if (typeof candidate === 'string' && candidate.length > 0) return candidate
  }
  const dshHome = process.env.DSH_HOME || path.join(os.homedir(), '.dsh')
  return path.join(dshHome, 'plugin-data', 'dsh-ca')
}

/** Absolute paths used by the in-page UI self-test handshake. */
function selftestPaths(backend, config) {
  const stateDir = resolveStateDir(backend, config)
  return {
    stateDir,
    pendingPath: path.join(stateDir, 'selftest-pending.json'),
    reportPath: path.join(stateDir, 'selftest-report.json'),
  }
}

/** Normalize a project list/save/remove/activate manager result. */
function projectPayload(result) {
  const safe = isPlainObject(result) ? jsonSafe(result) ?? {} : {}
  const mask = (value) => (isPlainObject(value) ? publicConfig(value) ?? {} : null)
  return {
    ...safe,
    project: mask(safe.project),
    projects: Array.isArray(safe.projects)
      ? safe.projects.slice(0, 500).map((project) => mask(project)).filter(Boolean)
      : [],
    activeProjectId:
      typeof safe.activeProjectId === 'string' && safe.activeProjectId.length > 0
        ? safe.activeProjectId
        : null,
  }
}

/** Trim the CA registry snapshot to the fields the database picker needs. */
function registrySummary(data) {
  if (!isPlainObject(data)) return null
  const strings = (items) =>
    Array.isArray(items)
      ? items
          .filter((item) => isPlainObject(item))
          .slice(0, 100)
          .map((item) => jsonSafe(item) ?? {})
      : []
  const active = isPlainObject(data.active_variant) ? data.active_variant : null
  return {
    available:
      data.available === true ||
      data.active_variant_id != null ||
      (Array.isArray(data.variants) && data.variants.length > 0),
    variants: strings(data.variants),
    workspaces: strings(data.workspaces),
    projects: strings(data.projects),
    active_variant_id: data.active_variant_id ?? active?.id ?? null,
  }
}

/** Validate `payload.id` for task endpoints. */
function taskId(payload) {
  return optionalString(payload, 'id', { required: true, maxLength: MAX_ID_LENGTH })
}

function requirePlainObject(value, label, maxBytes) {
  if (!isPlainObject(value)) {
    fail('INVALID_ARGUMENT', `${label} must be a JSON object`)
  }
  if (jsonBytes(value) > maxBytes) {
    fail('PAYLOAD_TOO_LARGE', `${label} exceeds ${maxBytes} bytes`)
  }
  return value
}

// --------------------------------------------------------------------------- //
// Config / ssh helpers
// --------------------------------------------------------------------------- //

const SECRET_KEY = /(?:password|passphrase|secret|token|credential|private[_-]?key|api[_-]?key)/i

/**
 * Deep-copy settings for display, replacing credential-like values.
 * @param {unknown} value
 * @param {number} [depth]
 */
function publicConfig(value, depth = 0) {
  if (depth > 8) return undefined
  if (Array.isArray(value)) {
    return value.slice(0, 200).map((item) => publicConfig(item, depth + 1))
  }
  if (isPlainObject(value)) {
    const result = {}
    for (const [key, item] of Object.entries(value)) {
      if (item === undefined) continue
      result[key] = SECRET_KEY.test(key) ? '***' : publicConfig(item, depth + 1)
    }
    return result
  }
  if (typeof value === 'function' || typeof value === 'symbol') return undefined
  return value
}

/** @param {unknown} ssh */
function validateSsh(ssh) {
  if (typeof ssh === 'string') {
    const text = ssh.trim()
    if (text.length === 0) fail('INVALID_ARGUMENT', 'payload.ssh must not be empty')
    if (text.length > MAX_SSH_BYTES) fail('INVALID_ARGUMENT', 'payload.ssh is too long')
    return text
  }
  if (!isPlainObject(ssh)) {
    fail('INVALID_ARGUMENT', 'payload.ssh must be an SSH target object or "user@host" string')
  }
  if (jsonBytes(ssh) > MAX_SSH_BYTES) {
    fail('PAYLOAD_TOO_LARGE', `payload.ssh exceeds ${MAX_SSH_BYTES} bytes`)
  }
  const host = ssh.host ?? ssh.hostname
  if (typeof host !== 'string' || host.trim().length === 0) {
    fail('INVALID_ARGUMENT', 'payload.ssh.host is required')
  }
  for (const key of ['user', 'keyPath', 'identityFile', 'alias', 'installPath', 'repoPath']) {
    if (ssh[key] !== undefined && typeof ssh[key] !== 'string') {
      fail('INVALID_ARGUMENT', `payload.ssh.${key} must be a string`)
    }
  }
  if (ssh.port !== undefined) {
    const port = Number(ssh.port)
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      fail('INVALID_ARGUMENT', 'payload.ssh.port must be an integer between 1 and 65535')
    }
  }
  if (ssh.password !== undefined && ssh.password !== null) {
    if (typeof ssh.password !== 'string') {
      fail('INVALID_ARGUMENT', 'payload.ssh.password must be a string')
    }
    if (ssh.password.length > MAX_PASSWORD_LENGTH) {
      fail('INVALID_ARGUMENT', `payload.ssh.password is too long (maximum ${MAX_PASSWORD_LENGTH} characters)`)
    }
    if (ssh.password.includes('\0')) {
      fail('INVALID_ARGUMENT', 'payload.ssh.password must not contain NUL characters')
    }
  }
  return jsonSafe(ssh)
}

/** Remote/auth error codes that the UI should see verbatim (not BACKEND_ERROR). */
const REMOTE_ERROR_CODES = new Set([
  'AUTH_FAILED',
  'SSH_UNREACHABLE',
  'SSH_AUTH_FAILED',
  'SSH_HOST_UNKNOWN',
  'HOST_KEY',
  'HOST_KEY_UNKNOWN',
  'HOST_KEY_CHANGED',
  'HOST_KEY_VERIFICATION_FAILED',
  'PASSWORD_AUTH_UNSUPPORTED_ON_WINDOWS',
  'REMOTE_PATH_NOT_FOUND',
  'PATH_OUTSIDE_HOME',
  'DIR_NOT_FOUND',
  'DIR_NOT_A_DIRECTORY',
  'DIR_NOT_READABLE',
  'PROBE_FAILED',
])

/** Normalize a remote/auth failure; secrets never reach errors or responses. */
function remoteFailure(error) {
  if (!error || typeof error !== 'object' || typeof error.code !== 'string') return error
  const details = publicConfig(detailsOf(error.details)) ?? {}
  const message = typeof error.message === 'string' ? error.message : String(error.code)
  if (error.code === 'INVALID_ARGUMENT' || error.code === 'CONFIG_INVALID') {
    return new RpcError('INVALID_ARGUMENT', message, details)
  }
  if (error.code === 'DB_NOT_FOUND' || error.code === 'PROJECT_NOT_FOUND' || error.code === 'NOT_FOUND') {
    return new RpcError('DB_NOT_FOUND', message, details)
  }
  if (REMOTE_ERROR_CODES.has(error.code) || error.code.startsWith('SSH_')) {
    return new RpcError(error.code, message, details)
  }
  return new RpcError('BACKEND_ERROR', message, { ...details, backendCode: error.code })
}

/** Build an RpcError from a structured `{ok:false,error}` remote report. */
function remoteReportFailure(errorLike) {
  const safe = isPlainObject(errorLike) ? jsonSafe(errorLike) ?? {} : {}
  return remoteFailure({
    code: typeof safe.code === 'string' ? safe.code : 'BACKEND_ERROR',
    message: typeof safe.message === 'string' ? safe.message : 'remote operation failed',
    details: safe.details,
  })
}

/** SSH auth mode for display (never the credential itself). */
function sshAuthMode(ssh) {
  if (isPlainObject(ssh)) {
    if (typeof ssh.password === 'string' && ssh.password.length > 0) return 'password'
    if (typeof ssh.keyPath === 'string' || typeof ssh.identityFile === 'string') return 'key'
  }
  return 'agent'
}

/** Remote directory path validation (POSIX absolute path, no escapes). */
function validateRemotePath(payload) {
  const value = payload.path
  if (value === undefined || value === null) return undefined
  if (typeof value !== 'string') fail('INVALID_ARGUMENT', 'payload.path must be a string')
  const text = value.trim()
  if (text.length === 0) return undefined
  if (text.length > MAX_REMOTE_PATH_LENGTH) {
    fail('INVALID_ARGUMENT', `payload.path is too long (maximum ${MAX_REMOTE_PATH_LENGTH} characters)`)
  }
  if (/[\0\r\n]/.test(text)) {
    fail('INVALID_ARGUMENT', 'payload.path must not contain NUL or newline characters')
  }
  if (!text.startsWith('/')) {
    fail('INVALID_ARGUMENT', 'payload.path must be an absolute remote path (start with "/")')
  }
  if (text.split('/').includes('..')) {
    fail('INVALID_ARGUMENT', 'payload.path must not contain ".." segments')
  }
  return text
}

/** Required absolute directory path for simple projects. */
function requiredDirectoryPath(payload) {
  const value = payload.directory
  if (value === undefined || value === null) {
    fail('INVALID_ARGUMENT', 'payload.directory is required')
  }
  if (typeof value !== 'string') fail('INVALID_ARGUMENT', 'payload.directory must be a string')
  const text = value.trim()
  if (text.length === 0) fail('INVALID_ARGUMENT', 'payload.directory must not be empty')
  if (text.length > MAX_REMOTE_PATH_LENGTH) {
    fail('INVALID_ARGUMENT', `payload.directory is too long (maximum ${MAX_REMOTE_PATH_LENGTH} characters)`)
  }
  if (/[\0\r\n]/.test(text)) {
    fail('INVALID_ARGUMENT', 'payload.directory must not contain NUL or newline characters')
  }
  if (!path.isAbsolute(text)) {
    fail('INVALID_ARGUMENT', 'payload.directory must be an absolute path')
  }
  if (text.split(/[\\/]/).includes('..')) {
    fail('INVALID_ARGUMENT', 'payload.directory must not contain ".." segments')
  }
  return text
}

/** Normalize a remote `platform` value (string or structured object). */
function normalizeRemotePlatform(value) {
  if (typeof value === 'string') return value
  if (!isPlainObject(value)) return null
  const safe = jsonSafe(value) ?? {}
  return {
    os: typeof safe.os === 'string' ? safe.os : null,
    arch: typeof safe.arch === 'string' ? safe.arch : null,
    platformId: typeof safe.platformId === 'string' ? safe.platformId : null,
    label: typeof safe.label === 'string' ? safe.label : null,
  }
}

/** Normalize `remote.connect` into the UI contract. */
function normalizeRemoteConnect(result) {
  const safe = isPlainObject(result) ? jsonSafe(result) ?? {} : {}
  if (safe.ok === false) throw remoteReportFailure(safe.error)
  const pythonRaw = isPlainObject(safe.python) ? safe.python : null
  const python = pythonRaw
    ? {
        path:
          typeof pythonRaw.path === 'string'
            ? pythonRaw.path
            : typeof pythonRaw.executable === 'string'
              ? pythonRaw.executable
              : null,
        version: typeof pythonRaw.version === 'string' ? pythonRaw.version : null,
        versionText: typeof pythonRaw.versionText === 'string' ? pythonRaw.versionText : null,
        supported: pythonRaw.supported === true,
        system: typeof pythonRaw.system === 'string' ? pythonRaw.system : null,
        candidates: Array.isArray(pythonRaw.candidates)
          ? pythonRaw.candidates
              .filter((candidate) => isPlainObject(candidate))
              .slice(0, 16)
              .map((candidate) => ({
                path: typeof candidate.path === 'string' ? candidate.path : null,
                version: typeof candidate.version === 'string' ? candidate.version : null,
                supported: candidate.supported === true,
              }))
          : [],
      }
    : null
  const caRaw = isPlainObject(safe.ca) ? safe.ca : null
  const ca = caRaw
    ? {
        installed: caRaw.installed === true,
        version: typeof caRaw.version === 'string' ? caRaw.version : null,
      }
    : { installed: false, version: null }
  return {
    ok: true,
    authMethod: typeof safe.authMethod === 'string' ? safe.authMethod : null,
    target: typeof safe.target === 'string' ? safe.target : null,
    home: typeof safe.home === 'string' ? safe.home : null,
    platform: normalizeRemotePlatform(safe.platform),
    python,
    ca,
  }
}

/** Normalize `remote.browse` entries into `{name,path}` rows. */
function normalizeRemoteBrowse(result) {
  const safe = isPlainObject(result) ? jsonSafe(result) ?? {} : {}
  if (safe.ok === false) throw remoteReportFailure(safe.error)
  const basePath = typeof safe.path === 'string' ? safe.path.replace(/\/+$/, '') : ''
  const entries = (Array.isArray(safe.entries) ? safe.entries : [])
    .filter((entry) => isPlainObject(entry))
    .slice(0, 2000)
    .map((entry) => ({
      name: typeof entry.name === 'string' ? entry.name : '',
      path:
        typeof entry.path === 'string' && entry.path.length > 0
          ? entry.path
          : basePath && typeof entry.name === 'string'
            ? `${basePath}/${entry.name}`
            : '',
    }))
    .filter((entry) => entry.name.length > 0 && entry.path.length > 0)
  return {
    path: typeof safe.path === 'string' ? safe.path : null,
    parent: typeof safe.parent === 'string' ? safe.parent : null,
    home: typeof safe.home === 'string' ? safe.home : null,
    isHome: safe.isHome === true,
    authMethod: typeof safe.authMethod === 'string' ? safe.authMethod : null,
    entries,
  }
}

/** Redact credential-like ssh fields for status display. */
function redactSsh(ssh) {
  if (typeof ssh === 'string') return ssh
  return publicConfig(ssh)
}

// --------------------------------------------------------------------------- //
// Backend adapter helpers
// --------------------------------------------------------------------------- //

/** Read an optional Cordis service without tripping an inject guard. */
function optionalService(ctx, name) {
  try {
    return ctx?.get?.(name)
  } catch {
    return undefined
  }
}

function backendMethod(backend, names) {
  for (const name of names) {
    const candidate = backend?.[name]
    if (typeof candidate === 'function') return candidate.bind(backend)
  }
  return null
}

function safeStatus(backend) {
  try {
    const status = typeof backend.getStatus === 'function' ? backend.getStatus() : null
    return jsonSafe(status) ?? { mode: 'unconfigured', state: 'stopped' }
  } catch (error) {
    return {
      mode: 'unconfigured',
      state: 'error',
      error: { code: 'STATUS_FAILED', message: errorMessage(error) },
    }
  }
}

function errorMessage(error) {
  if (!error || typeof error !== 'object') return String(error ?? 'unexpected error')
  const raw = typeof error.message === 'string' ? error.message : String(error)
  return raw.split(/\n\s+at\s/)[0].trim().slice(0, 1000)
}

/** Always expose failure details as a plain object (ConnectionRpcFailure). */
function detailsOf(value) {
  const safe = jsonSafe(value)
  return isPlainObject(safe) ? safe : {}
}

/** Normalize anything thrown into the RPC failure shape. */
function failure(error) {
  if (error instanceof RpcError) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: error.message,
        details: detailsOf(error.details),
      },
    }
  }
  if (isBackendError(error)) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: error.message,
        details: detailsOf(error.details),
      },
    }
  }
  if (isAbortError(error)) {
    return {
      ok: false,
      error: { code: 'CANCELLED', message: 'RPC request was cancelled', details: {} },
    }
  }
  // TaskError and other stable application errors carry uppercase codes.
  if (error && typeof error === 'object' && typeof error.code === 'string' && /^[A-Z][A-Z0-9_]*$/.test(error.code)) {
    return {
      ok: false,
      error: {
        code: error.code,
        message: errorMessage(error),
        details: detailsOf(error.details),
      },
    }
  }
  const normalized = normalizeError(error, 'INTERNAL')
  return {
    ok: false,
    error: { code: 'INTERNAL', message: normalized.message, details: {} },
  }
}

// --------------------------------------------------------------------------- //
// Dispatcher
// --------------------------------------------------------------------------- //

/**
 * Build the endpoint dispatcher with its service references captured.
 * @param {{backend: any, tasks: any, explain?: any, config?: Record<string, any>, ctx?: any}} deps
 */
function makeDispatcher({ backend, tasks, explain, config = {}, ctx }) {
  /** @type {{startedAt: number, started: boolean, status: string, ssh?: unknown, error?: unknown, result?: unknown, finishedAt?: number} | null} */
  let installState = null
  /** @type {Record<string, any> | null} */
  let localInstallState = null

  /** Read the manager's raw install status (new merged or legacy shape). */
  async function readManagerInstallStatus() {
    const readStatus = backendMethod(backend, ['getInstallStatus', 'installStatus'])
    if (readStatus === null) return null
    try {
      return jsonSafe(await readStatus()) ?? null
    } catch {
      return null
    }
  }

  /** Compact install state for backend.get/status; null when unavailable. */
  async function readInstallInfo() {
    const summary = backendMethod(backend, ['getInstallSummary', 'installSummary', 'getInstallInfo'])
    if (summary !== null) {
      try {
        const info = normalizeInstallInfo(await summary())
        if (info !== null) return info
      } catch {
        // Fall through to the derived view.
      }
    }
    const status = safeStatus(backend)
    if (isPlainObject(status.install)) {
      const info = normalizeInstallInfo(status.install)
      if (info !== null) return info
    }
    try {
      const managerStatus = await readManagerInstallStatus()
      // Only derive a compact view when some installer state actually exists;
      // otherwise `install` is genuinely unavailable and degrades to null.
      if (managerStatus === null && localInstallState === null && installState === null) {
        return null
      }
      const merged = mergeInstallStatus(managerStatus, {
        local: localInstallState,
        remote: installState,
      })
      const local = installInfoFromScope(merged.local)
      const remote = installInfoFromScope(merged.remote)
      if (local !== null || remote !== null) {
        return { local, remote, wheel: null, wheelhouse: null }
      }
    } catch {
      // Informational only: never fail status because install info is missing.
    }
    return null
  }

  async function currentModel() {
    const descriptors = [explain, optionalService(ctx, 'agentDefaultModel')]
    for (const service of descriptors) {
      if (!service) continue
      for (const name of ['currentModel', 'getModel', 'currentSelection']) {
        if (typeof service[name] !== 'function') continue
        try {
          let selection = service[name]()
          if (selection && typeof selection.then === 'function') selection = await selection
          const provider = selection?.provider ?? selection?.providerId
          const model = selection?.model ?? selection?.modelId
          if (typeof provider === 'string' && typeof model === 'string' && provider && model) {
            return { provider, model }
          }
        } catch {
          // Model info is advisory; never fail status because of it.
        }
      }
    }
    return null
  }

  function taskCounts(items) {
    const counts = { queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0 }
    for (const item of items) {
      if (counts[item?.status] !== undefined) counts[item.status] += 1
    }
    return counts
  }

  function serverValue(value) {
    const bytes = jsonBytes(value ?? null)
    if (!Number.isFinite(bytes) || bytes > MAX_RESPONSE_BYTES) {
      fail('RESPONSE_TOO_LARGE', `RPC response exceeds ${MAX_RESPONSE_BYTES} bytes; narrow the request`)
    }
    return { ok: true, value: value ?? null }
  }

  /** @type {Record<string, (payload: Record<string, unknown>, signal?: AbortSignal) => Promise<any>>} */
  const routes = {
    async ping() {
      return { pong: true, version: PLUGIN_VERSION }
    },

    async status() {
      let all = []
      try {
        all = typeof tasks.list === 'function' ? tasks.list() : []
      } catch {
        all = []
      }
      const items = Array.isArray(all) ? all : []
      return {
        backend: safeStatus(backend),
        tasks: items.slice(0, 50),
        model: await currentModel(),
        counts: taskCounts(items),
        project: await fetchActiveProject(backend),
        install: await readInstallInfo(),
      }
    },

    async 'ca.request'(payload, signal) {
      const methodRaw = optionalString(payload, 'method', { maxLength: 16 })
      const method = (methodRaw ?? 'GET').toUpperCase()
      if (!['GET', 'HEAD', 'POST'].includes(method)) {
        fail('INVALID_ARGUMENT', 'payload.method must be GET, HEAD or POST')
      }
      const path = validateCaPath(optionalString(payload, 'path', { required: true, maxLength: MAX_PATH_LENGTH }))
      const query = validateQuery(payload.query)
      let body
      if (method === 'GET' || method === 'HEAD') {
        if (payload.body !== undefined && payload.body !== null) {
          const provided = isPlainObject(payload.body) ? Object.keys(payload.body).length > 0 : true
          if (provided) fail('INVALID_ARGUMENT', `${method} requests must not include a body`)
        }
      } else if (payload.body !== undefined && payload.body !== null) {
        if (!isPlainObject(payload.body) && !Array.isArray(payload.body)) {
          fail('INVALID_ARGUMENT', 'payload.body must be a JSON object or array')
        }
        if (jsonBytes(payload.body) > MAX_CA_BODY_BYTES) {
          fail('PAYLOAD_TOO_LARGE', `request body exceeds ${MAX_CA_BODY_BYTES} bytes`)
        }
        body = jsonSafe(payload.body)
      }
      const timeoutMs = optionalInt(payload, 'timeoutMs', 1000, 300000)
      if (typeof backend.request !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager does not support HTTP requests')
      }
      // CA returns its own envelope; pass it through unchanged so the client
      // can expose CA error codes exactly as the server reports them.
      const envelope = await backend.request(path, { method, query, body, timeoutMs, signal })
      if (!isPlainObject(envelope)) {
        fail('BACKEND_ERROR', 'CA backend returned an invalid response envelope')
      }
      return envelope
    },

    async 'backend.get'() {
      return {
        status: safeStatus(backend),
        config: publicConfig(backend.config ?? config) ?? {},
        install: await readInstallInfo(),
      }
    },

    async 'backend.configure'(payload) {
      const patch = requirePlainObject(payload.patch, 'payload.patch', MAX_CONFIG_PATCH_BYTES)
      if (Object.keys(patch).length === 0) {
        fail('INVALID_ARGUMENT', 'payload.patch must contain at least one setting')
      }
      const configure = backendMethod(backend, ['configure', 'updateConfig', 'applyConfig', 'setConfig'])
      if (configure === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not accept configuration over RPC; edit the settings file shown on the CA backend page.',
        )
      }
      const result = await configure(jsonSafe(patch))
      const safeResult = isPlainObject(result) ? jsonSafe(result) : {}
      return {
        ...safeResult,
        config: publicConfig(safeResult?.config ?? backend.config ?? config) ?? {},
        status: safeStatus(backend),
      }
    },

    async 'backend.start'() {
      if (typeof backend.start !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager cannot start a backend')
      }
      const result = await backend.start()
      return { status: safeStatus(backend), result: jsonSafe(result) ?? null }
    },

    async 'backend.stop'() {
      if (typeof backend.stop !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager cannot stop a backend')
      }
      await backend.stop()
      return { status: safeStatus(backend) }
    },

    async 'backend.restart'() {
      if (typeof backend.restart !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA backend manager cannot restart a backend')
      }
      const result = await backend.restart()
      return { status: safeStatus(backend), result: jsonSafe(result) ?? null }
    },

    async 'backend.probe'(payload) {
      const target = (optionalString(payload, 'target', { maxLength: 16 }) ?? 'local').toLowerCase()
      if (target === 'local') {
        const probeLocal = backendMethod(backend, ['probeLocal'])
        if (probeLocal === null) {
          fail('BACKEND_UNSUPPORTED', 'The CA backend manager does not support local probing')
        }
        return jsonSafe(await probeLocal()) ?? null
      }
      if (target !== 'remote') {
        fail('INVALID_ARGUMENT', 'payload.target must be "local" or "remote"')
      }
      const ssh = payload.ssh === undefined || payload.ssh === null ? undefined : validateSsh(payload.ssh)
      const probeRemote = backendMethod(backend, ['probeRemote', 'probe'])
      if (probeRemote === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'Remote probing is not available in this CA backend build; check the CA backend page for the remote setup state.',
        )
      }
      return jsonSafe(await probeRemote(ssh)) ?? null
    },

    async 'backend.install'(payload) {
      const legacyOpts = payload.opts === undefined || payload.opts === null
        ? {}
        : requirePlainObject(payload.opts, 'payload.opts', MAX_OPTIONS_BYTES)
      const ssh = payload.ssh === undefined || payload.ssh === null
        ? legacyOpts.ssh === undefined || legacyOpts.ssh === null
          ? undefined
          : validateSsh(legacyOpts.ssh)
        : validateSsh(payload.ssh)
      // Defaults to the bundled-wheel install; `mode:'source'` keeps the old
      // rsync-based remote install available. Installer options may be passed
      // at the top level (new contract) or inside `opts` (old payloads).
      const installerOptions = pickInstallerOptions(payload, legacyOpts)
      const options = { ...jsonSafe(legacyOpts), ...installerOptions }
      if (ssh !== undefined) options.ssh = ssh
      const noteInstallStart = (result) => {
        installState = {
          started: result?.started !== false,
          status:
            result?.running === true
              ? 'running'
              : (result?.status ?? (result?.started === false ? 'running' : 'running')),
          startedAt: result?.startedAt ?? Date.now(),
          ssh: ssh === undefined ? undefined : redactSsh(ssh),
          error: null,
          result: null,
        }
        return {
          ...(isPlainObject(result) ? jsonSafe(result) : {}),
          started: result?.started !== false,
          scope: 'remote',
          status: installState.status,
          mode: options.mode,
          offline: options.offline === true,
          hint: 'Poll backend.installStatus (optionally with {"scope":"remote"}) for progress and logs.',
        }
      }
      // backend-ops exposes both a foreground installer and a background
      // starter; prefer the latter so the unary RPC returns immediately.
      const startInstall = backendMethod(backend, ['startRemoteInstall'])
      if (startInstall !== null) {
        let started
        try {
          started = await startInstall(options)
        } catch (error) {
          throw backendMutationFailure(error)
        }
        return noteInstallStart(started)
      }
      const install = backendMethod(backend, ['installRemote', 'install', 'remoteInstall'])
      if (install === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'Remote installation is not available in this CA backend build; check the CA backend page for the remote setup state.',
        )
      }
      let result
      try {
        result = install(options)
      } catch (error) {
        throw backendMutationFailure(error)
      }
      if (result && typeof result.then === 'function') {
        installState = {
          started: true,
          status: 'running',
          startedAt: Date.now(),
          ssh: ssh === undefined ? undefined : redactSsh(ssh),
          error: null,
          result: null,
        }
        result.then(
          (finalResult) => {
            if (!installState || installState.status !== 'running') return
            installState.status = 'succeeded'
            installState.finishedAt = Date.now()
            installState.result = jsonSafe(finalResult) ?? null
          },
          (error) => {
            if (!installState || installState.status !== 'running') return
            installState.status = 'failed'
            installState.finishedAt = Date.now()
            installState.error = failure(error).error
          },
        )
        return {
          started: true,
          scope: 'remote',
          status: 'running',
          mode: options.mode,
          offline: options.offline === true,
          hint: 'Poll backend.installStatus (optionally with {"scope":"remote"}) for progress.',
        }
      }
      return noteInstallStart(result)
    },

    async 'backend.installLocal'(payload) {
      const legacyOpts = payload.opts === undefined || payload.opts === null
        ? {}
        : requirePlainObject(payload.opts, 'payload.opts', MAX_OPTIONS_BYTES)
      const options = pickInstallerOptions(payload, legacyOpts)
      const installLocal = backendMethod(backend, ['startLocalInstall', 'installLocal'])
      if (installLocal === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'Local backend installation is not available in this CA backend build; check the CA backend page for the local setup state.',
        )
      }
      let result
      try {
        result = installLocal(options)
      } catch (error) {
        throw backendMutationFailure(error)
      }
      if (result && typeof result.then === 'function') {
        localInstallState = {
          started: true,
          state: 'running',
          startedAt: Date.now(),
          steps: [],
          logs: [],
          error: null,
          result: null,
        }
        result.then(
          (finalResult) => {
            if (!localInstallState || localInstallState.state !== 'running') return
            const safe = isPlainObject(finalResult) ? jsonSafe(finalResult) ?? {} : {}
            localInstallState.state = 'succeeded'
            localInstallState.finishedAt = Date.now()
            localInstallState.result = safe
            if (Array.isArray(safe.steps)) localInstallState.steps = safe.steps
            if (Array.isArray(safe.logs)) localInstallState.logs = safe.logs
          },
          (error) => {
            if (!localInstallState || localInstallState.state !== 'running') return
            localInstallState.state = 'failed'
            localInstallState.finishedAt = Date.now()
            localInstallState.error = failure(error).error
          },
        )
        return {
          started: true,
          scope: 'local',
          status: 'running',
          mode: options.mode,
          offline: options.offline === true,
          hint: 'Poll backend.installStatus with {"scope":"local"} for progress and logs.',
        }
      }
      if (!isPlainObject(result)) {
        fail('BACKEND_ERROR', 'The CA backend returned an invalid local installation result')
      }
      const safe = jsonSafe(result) ?? {}
      const state = typeof safe.state === 'string' ? safe.state : typeof safe.status === 'string' ? safe.status : 'running'
      localInstallState = {
        started: safe.started !== false,
        state,
        startedAt: Number.isFinite(Number(safe.startedAt)) ? Number(safe.startedAt) : Date.now(),
        steps: Array.isArray(safe.steps) ? safe.steps : [],
        logs: Array.isArray(safe.logs) ? safe.logs : [],
        error: safe.error ?? null,
        result: safe,
      }
      return {
        ...safe,
        started: safe.started !== false,
        scope: 'local',
        status: state,
        mode: safe.mode ?? options.mode,
        offline: safe.offline ?? options.offline === true,
        hint: 'Poll backend.installStatus with {"scope":"local"} for progress and logs.',
      }
    },

    async 'backend.installStatus'(payload) {
      const scopeRaw = optionalString(payload, 'scope', { maxLength: 16 })
      const scope = scopeRaw === undefined ? null : scopeRaw.toLowerCase()
      if (scope !== null && scope !== 'local' && scope !== 'remote') {
        fail('INVALID_ARGUMENT', 'payload.scope must be "local" or "remote"')
      }
      const managerStatus = await readManagerInstallStatus()
      const merged = mergeInstallStatus(managerStatus, {
        local: localInstallState,
        remote: installState,
      })
      // Preparation progress (install + start) is exposed additively so the
      // UI can show one prepare status without a separate endpoint.
      let prepare = isPlainObject(managerStatus?.prepare)
        ? normalizePrepareStatus(managerStatus.prepare)
        : null
      if (prepare === null) {
        const readPrepare = backendMethod(backend, ['getPrepareStatus'])
        if (readPrepare !== null) {
          try {
            prepare = normalizePrepareStatus(await readPrepare())
          } catch {
            prepare = null
          }
        }
      }
      const selected =
        scope === 'local' ? merged.local : scope === 'remote' ? merged.remote : merged.aggregate
      return {
        // Legacy top-level aliases so pre-project clients keep working; they
        // track the requested scope (remote by default, the old behavior).
        ...installScopeAliases(selected),
        scope,
        local: merged.local,
        remote: merged.remote,
        prepare,
      }
    },

    async 'backend.databases'(payload) {
      const refresh = optionalBool(payload, 'refresh')
      const listDatabases = backendMethod(backend, ['listDatabases'])
      if (listDatabases === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not support database discovery yet; start it from the CA backend page instead.',
        )
      }
      let report
      try {
        report = await listDatabases(refresh === true ? { refresh: true } : undefined)
      } catch (error) {
        throw backendMutationFailure(error)
      }
      if (!isPlainObject(report)) {
        fail('BACKEND_ERROR', 'The CA backend returned an invalid database discovery report')
      }
      const safe = jsonSafe(report) ?? {}
      const status = safeStatus(backend)
      const current = isPlainObject(safe.current)
        ? safe.current
        : { db: null, dbDir: null }
      // Best effort: merge the CA registry (variants/workspaces) only while the
      // backend answers; a registry failure must never fail database discovery.
      let registry = null
      if (status.state === 'running' && typeof backend.request === 'function') {
        try {
          const envelope = await backend.request('/api/registry', { method: 'GET', timeoutMs: 5000 })
          if (isPlainObject(envelope) && envelope.ok === true) {
            registry = registrySummary(envelope.data)
          }
        } catch {
          registry = null
        }
      }
      const candidates = Array.isArray(safe.candidates) ? safe.candidates.slice(0, 500) : []
      // `mode/current/candidates/registry` are the picker contract; the scan
      // metadata below is additive so the settings page can show totals and
      // truncation without another RPC.
      const value = {
        ok: safe.ok !== false,
        mode: typeof safe.mode === 'string' ? safe.mode : (status.mode ?? 'unconfigured'),
        current,
        configured: isPlainObject(safe.configured) ? safe.configured : null,
        candidates,
        total: Number.isFinite(Number(safe.total)) ? Number(safe.total) : candidates.length,
        truncated: safe.truncated === true,
        roots: Array.isArray(safe.roots) ? safe.roots.slice(0, 20).map((root) => String(root)) : [],
        registry,
      }
      if (Number.isFinite(Number(safe.scannedAt))) value.scannedAt = Number(safe.scannedAt)
      if (safe.error !== undefined) value.error = jsonSafe(safe.error) ?? null
      return value
    },

    async 'backend.useDatabase'(payload) {
      const kindRaw = optionalString(payload, 'kind', { required: true, maxLength: 16 })
      const normalized = kindRaw.toLowerCase()
      const kind =
        normalized === 'db'
          ? 'db'
          : normalized === 'dbdir' || normalized === 'db_dir'
            ? 'dbDir'
            : null
      if (kind === null) {
        fail('INVALID_ARGUMENT', 'payload.kind must be "db" or "dbDir"')
      }
      const target = validateDatabasePath(payload.path)
      const useDatabase = backendMethod(backend, ['useDatabase'])
      if (useDatabase === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not support switching databases yet; configure the database on the CA backend page instead.',
        )
      }
      let result
      try {
        result = await useDatabase({ kind, path: target })
      } catch (error) {
        throw backendMutationFailure(error)
      }
      const safe = isPlainObject(result) ? jsonSafe(result) : {}
      return {
        ...safe,
        config: publicConfig(safe?.config ?? backend.config ?? config) ?? {},
        status: safeStatus(backend),
      }
    },

    async 'projects.list'() {
      const listProjects = backendMethod(backend, ['listProjects'])
      if (listProjects === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not support the project model yet; configure the database on the CA backend page instead.',
        )
      }
      let result
      try {
        result = await listProjects()
      } catch (error) {
        throw backendMutationFailure(error)
      }
      const safe = projectPayload(result)
      return { projects: safe.projects, activeProjectId: safe.activeProjectId }
    },

    async 'projects.save'(payload) {
      const project = requirePlainObject(payload.project, 'payload.project', MAX_CONFIG_PATCH_BYTES)
      if (Object.keys(project).length === 0) {
        fail('INVALID_ARGUMENT', 'payload.project must contain at least one field')
      }
      const saveProject = backendMethod(backend, ['saveProject'])
      if (saveProject === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'This CA backend build does not support the project model yet; configure the database on the CA backend page instead.',
        )
      }
      let result
      try {
        result = await saveProject(jsonSafe(project))
      } catch (error) {
        throw backendMutationFailure(error)
      }
      const safe = projectPayload(result)
      return {
        project: safe.project,
        projects: safe.projects,
        activeProjectId: safe.activeProjectId,
      }
    },

    async 'projects.remove'(payload) {
      const id = optionalString(payload, 'id', {
        required: true,
        maxLength: MAX_PROJECT_ID_LENGTH,
      })
      const deleteProject = backendMethod(backend, ['deleteProject'])
      if (deleteProject === null) {
        fail('BACKEND_UNSUPPORTED', 'This CA backend build does not support the project model yet')
      }
      let result
      try {
        result = await deleteProject(id)
      } catch (error) {
        throw backendMutationFailure(error)
      }
      const safe = projectPayload(result)
      return {
        // `deleted`/`activeCleared` are additive; the UI needs them to explain
        // that deleting the active project clears the selection.
        deleted: isPlainObject(safe.deleted) ? safe.deleted : null,
        projects: safe.projects,
        activeProjectId: safe.activeProjectId,
        activeCleared: safe.activeCleared === true,
      }
    },

    async 'projects.activate'(payload) {
      const id = optionalString(payload, 'id', {
        required: true,
        maxLength: MAX_PROJECT_ID_LENGTH,
      })
      const activateProject = backendMethod(backend, ['activateProject'])
      if (activateProject === null) {
        fail('BACKEND_UNSUPPORTED', 'This CA backend build does not support the project model yet')
      }
      let result
      try {
        result = await activateProject(id)
      } catch (error) {
        throw backendMutationFailure(error)
      }
      const safe = projectPayload(result)
      return {
        project: safe.project,
        config: publicConfig(safe.config ?? backend.config ?? config) ?? {},
        status: safeStatus(backend),
      }
    },

    async 'remote.connect'(payload) {
      if (payload.ssh === undefined || payload.ssh === null) {
        fail('INVALID_ARGUMENT', 'payload.ssh is required (host, user and a password or key)')
      }
      const ssh = validateSsh(payload.ssh)
      const connect = backendMethod(backend, [
        'testRemoteConnection',
        'connectRemote',
        'remoteConnect',
        'probeRemoteConnection',
      ])
      if (connect === null) {
        fail('BACKEND_UNSUPPORTED', 'Remote connection testing is not available in this CA backend build.')
      }
      let result
      try {
        result = await connect(ssh)
      } catch (error) {
        throw remoteFailure(error)
      }
      const value = normalizeRemoteConnect(result)
      return { ...value, auth: value.authMethod ?? sshAuthMode(ssh) }
    },

    async 'remote.browse'(payload) {
      if (payload.ssh === undefined || payload.ssh === null) {
        fail('INVALID_ARGUMENT', 'payload.ssh is required (host, user and a password or key)')
      }
      const ssh = validateSsh(payload.ssh)
      const targetPath = validateRemotePath(payload)
      const browse = backendMethod(backend, ['listRemoteDirs', 'browseRemoteDirs', 'remoteListDirs'])
      if (browse === null) {
        fail('BACKEND_UNSUPPORTED', 'Remote directory browsing is not available in this CA backend build.')
      }
      let result
      try {
        result = await browse(ssh, targetPath === undefined ? {} : { path: targetPath })
      } catch (error) {
        throw remoteFailure(error)
      }
      return normalizeRemoteBrowse(result)
    },

    async 'local.browse'(payload) {
      const rawPath = payload.path
      if (rawPath !== undefined && rawPath !== null && typeof rawPath !== 'string') {
        fail('INVALID_ARGUMENT', 'payload.path must be a string')
      }
      if (typeof rawPath === 'string' && rawPath.length > MAX_LOCAL_PATH_LENGTH) {
        fail('INVALID_ARGUMENT', `payload.path is too long (maximum ${MAX_LOCAL_PATH_LENGTH} characters)`)
      }
      try {
        return await browseLocalDirs({ path: rawPath === undefined || rawPath === null ? undefined : rawPath })
      } catch (error) {
        if (isBackendError(error)) throw error
        throw new BackendError('LOCAL_BROWSE_FAILED', error && error.message ? error.message : String(error))
      }
    },

    async 'projects.createSimple'(payload) {
      const kindRaw = optionalString(payload, 'kind', { required: true, maxLength: 16 }).toLowerCase()
      if (kindRaw !== 'local' && kindRaw !== 'remote') {
        fail('INVALID_ARGUMENT', 'payload.kind must be "local" or "remote"')
      }
      const directory = requiredDirectoryPath(payload)
      const name = optionalString(payload, 'name', { maxLength: 256 })
      let ssh
      if (kindRaw === 'remote') {
        if (payload.ssh === undefined || payload.ssh === null) {
          fail('INVALID_ARGUMENT', 'payload.ssh is required for kind "remote"')
        }
        ssh = validateSsh(payload.ssh)
      }
      const create = backendMethod(backend, ['createSimpleProject'])
      if (create === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'Creating simple projects is not available in this CA backend build; check the CA backend page.',
        )
      }
      const input = { kind: kindRaw, directory }
      if (name !== undefined) input.name = name
      if (ssh !== undefined) input.ssh = ssh
      let result
      try {
        result = await create(input)
      } catch (error) {
        throw backendMutationFailure(error)
      }
      const safe = isPlainObject(result) ? jsonSafe(result) ?? {} : {}
      const project = isPlainObject(safe.project)
        ? safe.project
        : isPlainObject(safe) && typeof safe.id === 'string'
          ? safe
          : null
      const out = projectPayload({
        project,
        projects: safe.projects,
        activeProjectId: safe.activeProjectId,
      })
      return {
        project: out.project,
        projects: out.projects,
        activeProjectId: out.activeProjectId,
      }
    },

    async 'projects.prepare'(payload) {
      const id = optionalString(payload, 'id', {
        required: true,
        maxLength: MAX_PROJECT_ID_LENGTH,
      })
      const offline = optionalBool(payload, 'offline')
      const wheelhousePath = normalizeInstallValue('wheelhousePath', payload.wheelhousePath)
      const startPrepare = backendMethod(backend, ['startPrepareProject', 'prepareProject'])
      if (startPrepare === null) {
        fail(
          'BACKEND_UNSUPPORTED',
          'Preparing a project is not available in this CA backend build; configure the backend on the CA backend page.',
        )
      }
      const options = {}
      if (offline !== undefined) options.offline = offline
      if (wheelhousePath !== undefined) options.wheelhousePath = wheelhousePath
      let result
      try {
        result = startPrepare(id, options)
      } catch (error) {
        throw backendMutationFailure(error)
      }
      const hint =
        'Poll backend.installStatus ({"scope":"local"} or {"scope":"remote"}) and status until the backend is running.'
      if (result && typeof result.then === 'function') {
        // Background preparation: failures are surfaced through installStatus,
        // so never let the promise reject unhandled here.
        result.then(
          () => {},
          () => {},
        )
        return {
          started: true,
          scope: 'prepare',
          projectId: id,
          offline: offline === true,
          hint,
        }
      }
      const safe = isPlainObject(result) ? jsonSafe(result) ?? {} : {}
      const installScope =
        safe.installScope === 'local' || safe.installScope === 'remote' ? safe.installScope : null
      return {
        ...safe,
        started: safe.started !== false,
        scope: 'prepare',
        projectId: id,
        offline: offline ?? safe.offline ?? null,
        hint: installScope
          ? `Poll backend.installStatus ({"scope":"${installScope}"}) and status until the backend is running.`
          : hint,
      }
    },

    async 'ui.selftest.pending'() {
      const { pendingPath } = selftestPaths(backend, config)
      let pending = false
      try {
        pending = fs.existsSync(pendingPath)
      } catch {
        pending = false
      }
      return { pending }
    },

    async 'ui.selftest.report'(payload) {
      if (!isPlainObject(payload.report)) {
        fail('INVALID_ARGUMENT', 'payload.report must be a JSON object')
      }
      const report = payload.report
      const bytes = jsonBytes(report)
      if (!Number.isFinite(bytes) || bytes > SELFTEST_REPORT_MAX_BYTES) {
        fail('PAYLOAD_TOO_LARGE', `selftest report exceeds ${SELFTEST_REPORT_MAX_BYTES} bytes`)
      }
      const { pendingPath, reportPath } = selftestPaths(backend, config)
      const document = {
        receivedAt: new Date().toISOString(),
        report,
      }
      try {
        // atomicWriteJson creates the state directory, writes 0600 and renames
        // over the previous report so the Lead can read a complete file.
        atomicWriteJson(reportPath, document, { mode: 0o600 })
      } catch (error) {
        fail(
          'SELFTEST_WRITE_FAILED',
          `cannot write UI self-test report: ${errorMessage(error)}`,
          { path: reportPath },
        )
      }
      let pendingCleared = false
      try {
        if (fs.existsSync(pendingPath)) {
          fs.rmSync(pendingPath, { force: true })
          pendingCleared = true
        }
      } catch {
        pendingCleared = false
      }
      return { saved: true, path: reportPath, pendingCleared, bytes }
    },

    async 'tasks.start'(payload) {
      if (typeof tasks.start !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const kind = optionalString(payload, 'kind', { required: true, maxLength: 64 })
      let params = payload.params
      if (params === undefined || params === null) params = {}
      if (!isPlainObject(params)) fail('INVALID_ARGUMENT', 'payload.params must be a JSON object')
      if (jsonBytes(params) > MAX_PAYLOAD_BYTES) {
        fail('PAYLOAD_TOO_LARGE', `payload.params exceeds ${MAX_PAYLOAD_BYTES} bytes`)
      }
      const task = tasks.start(kind, params)
      return { task: jsonSafe(task) ?? null }
    },

    async 'tasks.list'() {
      if (typeof tasks.list !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const items = tasks.list()
      const list = Array.isArray(items) ? items.slice(0, 500) : []
      return {
        tasks: jsonSafe(list) ?? [],
        counts: typeof tasks.counts === 'function' ? tasks.counts() : taskCounts(list),
      }
    },

    async 'tasks.get'(payload) {
      const id = taskId(payload)
      const task = typeof tasks.get === 'function' ? tasks.get(id) : null
      if (!task) fail('TASK_NOT_FOUND', `no CA task with id "${id}"`, { id })
      return { task: jsonSafe(task) ?? null }
    },

    async 'tasks.logs'(payload) {
      const id = taskId(payload)
      const offset = optionalInt(payload, 'offset', 0, Number.MAX_SAFE_INTEGER) ?? 0
      const limit = optionalInt(payload, 'limit', 1, 2000) ?? 200
      if (typeof tasks.logs !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const result = tasks.logs(id, { offset, limit })
      return jsonSafe(result) ?? null
    },

    async 'tasks.cancel'(payload) {
      const id = taskId(payload)
      if (typeof tasks.cancel !== 'function') {
        fail('BACKEND_UNSUPPORTED', 'The CA task manager is not available')
      }
      const task = tasks.cancel(id)
      return { task: jsonSafe(task) ?? null }
    },

    async explain(payload, signal) {
      const symbol = optionalString(payload, 'symbol', { required: true, maxLength: MAX_SYMBOL_LENGTH })
      const language = (optionalString(payload, 'language', { maxLength: 8 }) ?? 'en').toLowerCase()
      if (language !== 'en' && language !== 'zh') {
        fail('INVALID_ARGUMENT', 'payload.language must be "en" or "zh"')
      }
      const question = optionalString(payload, 'question', { maxLength: MAX_QUESTION_LENGTH })
      if (!explain || typeof explain.explain !== 'function') {
        fail(
          'EXPLAIN_UNAVAILABLE',
          'AI explanation is unavailable in this DSH session (no model selection service). Use ca_symbol/ca_facts and explain manually.',
        )
      }
      const result = await explain.explain({ symbol, language, question, signal })
      if (!isPlainObject(result) || typeof result.text !== 'string' || result.text.trim().length === 0) {
        fail('EXPLAIN_FAILED', 'The explain service returned no explanation text')
      }
      const safe = jsonSafe(result) ?? {}
      return {
        symbol: safe.symbol ?? symbol,
        text: safe.text,
        model: safe.model ?? null,
        evidenceCount: Number.isFinite(Number(safe.evidenceCount)) ? Number(safe.evidenceCount) : 0,
        ...(safe.usage !== undefined ? { usage: safe.usage } : {}),
      }
    },
  }

  const handler = async (endpoint, payload, signal) => {
    try {
      if (typeof endpoint !== 'string' || endpoint.trim().length === 0) {
        fail('UNKNOWN_ENDPOINT', 'RPC endpoint name is required')
      }
      const route = routes[endpoint]
      if (!route) {
        fail(
          'UNKNOWN_ENDPOINT',
          `unknown CA plugin endpoint "${endpoint}"; expected one of: ${RPC_ENDPOINTS.join(', ')}`,
          { endpoints: [...RPC_ENDPOINTS] },
        )
      }
      const safePayload = validatePayload(payload)
      const value = await route(safePayload, signal)
      return serverValue(value)
    } catch (error) {
      return failure(error)
    }
  }
  return handler
}

/** Enforce the read-only `/api/` allow-list for CA passthrough requests. */
function validateCaPath(path) {
  if (typeof path !== 'string') fail('INVALID_ARGUMENT', 'payload.path is required')
  if (!path.startsWith('/api/')) {
    fail('FORBIDDEN_PATH', 'CA API path must start with "/api/"', { path })
  }
  if (path.length > MAX_PATH_LENGTH) {
    fail('INVALID_ARGUMENT', `payload.path exceeds ${MAX_PATH_LENGTH} characters`)
  }
  if (/[\\\s\0]/.test(path) || path.includes('://') || path.includes('..') || path.includes('//')) {
    fail('FORBIDDEN_PATH', 'CA API path contains a forbidden character or segment', { path })
  }
  if (path.includes('?') || path.includes('#')) {
    fail('INVALID_ARGUMENT', 'pass query parameters in payload.query, not inside payload.path')
  }
  return path
}

/** Normalize/validate query params for the CA passthrough endpoint. */
function validateQuery(query) {
  if (query === undefined || query === null) return undefined
  if (!isPlainObject(query)) fail('INVALID_ARGUMENT', 'payload.query must be a JSON object')
  const entries = Object.entries(query)
  if (entries.length > MAX_QUERY_ENTRIES) {
    fail('INVALID_ARGUMENT', `payload.query exceeds ${MAX_QUERY_ENTRIES} entries`)
  }
  const result = {}
  for (const [key, value] of entries) {
    if (value === undefined || value === null) continue
    if (typeof value === 'object') {
      fail('INVALID_ARGUMENT', `payload.query.${key} must be a string, number or boolean`)
    }
    const text = String(value)
    if (text.length > MAX_QUERY_VALUE_LENGTH) {
      fail('INVALID_ARGUMENT', `payload.query.${key} is too long`)
    }
    if (key.length === 0 || key.length > 128) {
      fail('INVALID_ARGUMENT', 'payload.query keys must be 1-128 characters')
    }
    result[key] = text
  }
  return result
}

/**
 * Wrap one registration in the owning Cordis context so it is disposed with
 * the plugin. Returns the exact disposer (plain-call fallback for tests).
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

// --------------------------------------------------------------------------- //
// HTTP transport (DSH-owned same-origin web route)
// --------------------------------------------------------------------------- //

/** HTTP-mapped error used by the transport layer. */
function httpError(code, message, status) {
  const error = new Error(message)
  error.code = code
  error.status = status
  return error
}

/** Business outcomes stay 200; only protocol/argument failures get statuses. */
function statusForEnvelope(envelope) {
  if (!envelope || envelope.ok !== false) return 200
  const code = envelope.error?.code
  if (code === 'PAYLOAD_TOO_LARGE') return 413
  if (code === 'UNKNOWN_ENDPOINT') return 404
  if (code === 'INVALID_ARGUMENT' || code === 'FORBIDDEN_PATH') return 400
  if (code === 'INTERNAL' || code === 'RESPONSE_TOO_LARGE') return 500
  return 200
}

function errorEnvelope(code, message, details = {}) {
  return { ok: false, error: { code, message, details } }
}

/** JSON.stringify that never throws; BigInt becomes a decimal string. */
function safeStringify(value) {
  try {
    return JSON.stringify(value, (_key, item) => (typeof item === 'bigint' ? item.toString() : item))
  } catch {
    return JSON.stringify(
      errorEnvelope('INTERNAL', 'CA plugin failed to serialize the response'),
    )
  }
}

function writeJson(res, status, envelope, extraHeaders = {}) {
  if (!res || typeof res.end !== 'function') return
  if (res.headersSent) {
    try {
      res.end()
    } catch {
      // Response already gone.
    }
    return
  }
  const body = safeStringify(
    envelope ?? errorEnvelope('INTERNAL', 'CA plugin produced an empty response'),
  )
  try {
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': String(Buffer.byteLength(body, 'utf8')),
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
      ...extraHeaders,
    })
    res.end(body)
  } catch {
    try {
      res.end()
    } catch {
      // Client already disconnected; nothing to answer.
    }
  }
}

/** Read the request body, enforcing the contract's 1 MiB limit. */
function readRequestBody(req, limit = MAX_HTTP_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    let settled = false
    const chunks = []
    let size = 0
    const cleanup = () => {
      req.removeListener?.('data', onData)
      req.removeListener?.('end', onEnd)
      req.removeListener?.('error', onError)
      req.removeListener?.('aborted', onAborted)
    }
    const settle = (callback, value) => {
      if (settled) return
      settled = true
      cleanup()
      callback(value)
    }
    // Reject as soon as the declared budget is exceeded, but keep draining a
    // moderately oversized body so the client still receives the 413 response
    // instead of a socket error. Pathological uploads are cut off.
    const DRAIN_LIMIT = 16 * 1024 * 1024
    let oversized = false
    let drained = 0
    const onData = (chunk) => {
      if (settled) return
      const buffer = typeof chunk === 'string' ? Buffer.from(chunk) : chunk
      const length = buffer?.length ?? 0
      size += length
      if (oversized) {
        drained += length
        if (drained > DRAIN_LIMIT) {
          settle(reject, httpError('BODY_TOO_LARGE', `request body exceeds ${limit} bytes`, 413))
          try {
            req.destroy?.()
          } catch {
            // Already closed.
          }
        }
        return
      }
      if (size > limit) {
        oversized = true
        chunks.length = 0
        return
      }
      chunks.push(buffer)
    }
    const onEnd = () => {
      if (oversized) {
        settle(reject, httpError('BODY_TOO_LARGE', `request body exceeds ${limit} bytes`, 413))
        return
      }
      settle(resolve, Buffer.concat(chunks).toString('utf8'))
    }
    const onError = (error) =>
      settle(
        reject,
        httpError('BODY_READ_FAILED', `cannot read request body: ${errorMessage(error)}`, 400),
      )
    const onAborted = () =>
      settle(reject, httpError('BODY_ABORTED', 'request was aborted before the body was read', 400))
    req.on('data', onData)
    req.on('end', onEnd)
    req.on('error', onError)
    req.on('aborted', onAborted)
  })
}

/** Extract `<endpoint>` from `/dsh-ca/<endpoint>`; null when outside/ambiguous. */
function endpointFromRequest(req) {
  const rawUrl = typeof req?.url === 'string' ? req.url : '/'
  const queryIndex = rawUrl.indexOf('?')
  const pathname = queryIndex >= 0 ? rawUrl.slice(0, queryIndex) : rawUrl
  if (pathname !== CHANNEL && !pathname.startsWith(`${CHANNEL}/`)) return null
  const tail = pathname === CHANNEL ? '' : pathname.slice(CHANNEL.length + 1)
  if (tail.length === 0 || tail.includes('/')) return null
  try {
    return decodeURIComponent(tail)
  } catch {
    return null
  }
}

/** Apply DSH's Host/Origin + browser authentication check. */
function rejectionStatus(connection, req) {
  try {
    const rejection = connection.requestRejection(req)
    if (rejection === 401) return 401
    // Any other truthy rejection is a refusal; unknown values fail closed.
    return rejection ? 403 : 0
  } catch {
    // A throwing trust check must fail closed.
    return 403
  }
}

/**
 * Build the `(req,res)` handler registered on the DSH web server.
 * Owns the full response lifecycle and never throws.
 *
 * @param {any} connection DSH connection service (`requestRejection`)
 * @param {(endpoint: string, payload: unknown, signal: AbortSignal) => Promise<any>} dispatch
 */
function makeHttpHandler(connection, dispatch) {
  return async (req, res) => {
    try {
      const rejection = rejectionStatus(connection, req)
      if (rejection !== 0) {
        writeJson(
          res,
          rejection,
          errorEnvelope(
            rejection === 401 ? 'UNAUTHORIZED' : 'FORBIDDEN',
            rejection === 401
              ? 'authentication is required for the CA plugin channel'
              : 'the DSH connection trust policy rejected this request',
          ),
        )
        return
      }
      if (req?.method !== 'POST') {
        writeJson(res, 405, errorEnvelope('METHOD_NOT_ALLOWED', 'the CA plugin channel only accepts POST'), {
          allow: 'POST',
        })
        return
      }
      const endpoint = endpointFromRequest(req)
      if (!endpoint) {
        writeJson(
          res,
          404,
          errorEnvelope('NOT_FOUND', `no CA plugin endpoint at ${req?.url ?? ''}`),
        )
        return
      }
      if (!RPC_ENDPOINTS.includes(endpoint)) {
        writeJson(
          res,
          404,
          errorEnvelope(
            'UNKNOWN_ENDPOINT',
            `unknown CA plugin endpoint "${endpoint}"; expected one of: ${RPC_ENDPOINTS.join(', ')}`,
            { endpoints: [...RPC_ENDPOINTS] },
          ),
        )
        return
      }
      let rawBody
      try {
        rawBody = await readRequestBody(req)
      } catch (error) {
        writeJson(res, error?.status ?? 400, errorEnvelope(error?.code ?? 'BAD_REQUEST', errorMessage(error)))
        return
      }
      let payload
      if (rawBody.length === 0) {
        payload = {}
      } else {
        try {
          payload = JSON.parse(rawBody)
        } catch {
          writeJson(res, 400, errorEnvelope('INVALID_JSON', 'request body must be valid JSON'))
          return
        }
      }
      const controller = new AbortController()
      const onClose = () => controller.abort()
      if (typeof res.on === 'function') res.on('close', onClose)
      let envelope
      try {
        envelope = await dispatch(endpoint, payload, controller.signal)
      } finally {
        if (typeof res.off === 'function') res.off('close', onClose)
      }
      writeJson(res, statusForEnvelope(envelope), envelope)
    } catch {
      // The transport must never surface an unhandled rejection.
      writeJson(res, 500, errorEnvelope('INTERNAL', 'CA plugin internal error'))
    }
  }
}

/**
 * Register the `/dsh-ca` HTTP channel on the DSH web server.
 *
 * @param {any} ctx Cordis plugin context (hard deps: webServer, connection)
 * @param {{backend: any, tasks: any, explain?: any, config?: Record<string, any>}} deps
 * @returns {() => void} disposer
 */
export function registerRpc(ctx, deps = {}) {
  const { backend, tasks } = deps
  if (!backend || typeof backend.getStatus !== 'function') {
    throw new Error('dsh-ca: registerRpc requires the backend manager (backend-ops)')
  }
  if (!tasks || typeof tasks.list !== 'function') {
    throw new Error('dsh-ca: registerRpc requires the task manager (host-tools)')
  }
  const webServer = ctx?.webServer ?? ctx?.get?.('webServer')
  if (!webServer || typeof webServer.register !== 'function') {
    throw new Error('dsh-ca: registerRpc requires the DSH webServer service (inject "webServer")')
  }
  const connection = ctx?.connection ?? ctx?.get?.('connection')
  if (!connection || typeof connection.requestRejection !== 'function') {
    throw new Error(
      'dsh-ca: registerRpc requires the DSH connection service with requestRejection (inject "connection")',
    )
  }
  const dispatch = makeDispatcher({ ...deps, ctx })
  return registerEffect(ctx, 'dsh-ca: http channel', () => {
    const off = webServer.register({
      kind: 'prefix',
      path: CHANNEL,
      handler: makeHttpHandler(connection, dispatch),
    })
    return () => {
      try {
        const result = typeof off === 'function' ? off() : undefined
        if (result && typeof result.catch === 'function') result.catch(() => {})
      } catch {
        // The route is already gone; nothing left to dispose.
      }
    }
  })
}

/** @param {unknown} value */
export function isPlainRpcObject(value) {
  return isPlainObject(value)
}

/** Expose the payload limits for tests/documentation. */
export const RPC_LIMITS = Object.freeze({
  httpBodyBytes: MAX_HTTP_BODY_BYTES,
  payloadBytes: MAX_PAYLOAD_BYTES,
  caBodyBytes: MAX_CA_BODY_BYTES,
  configPatchBytes: MAX_CONFIG_PATCH_BYTES,
  responseBytes: MAX_RESPONSE_BYTES,
  taskLogLimit: clampInt(2000, 1, 2000, 200),
})
