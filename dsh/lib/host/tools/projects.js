/**
 * Project helpers shared by the native tools and the RPC channel.
 *
 * A CA project binds one source tree to one database (a single `.db` file or a
 * shard directory) and one backend location (local machine or a remote Linux
 * host that runs `ca serve`). The backend manager owns project persistence and
 * field validation; these helpers only normalize its reports for presentation
 * and lookup, and the read paths never throw (status rendering must not fail
 * because project metadata is unavailable).
 */

import { isPlainObject } from '../backend/util.js'

/** Fields exposed in `status.project`. */
export const PROJECT_SUMMARY_FIELDS = Object.freeze([
  'id',
  'name',
  'backend',
  'sourceRoot',
  'db',
  'dbDir',
])

/** @param {unknown} value */
function stringOrNull(value) {
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** True when this backend manager exposes the project model. */
export function supportsProjects(backend) {
  return Boolean(backend && typeof backend.listProjects === 'function')
}

/** Normalize one project for status/tool output (never throws). */
export function summarizeProject(project) {
  if (!isPlainObject(project)) return null
  const summary = {}
  for (const field of PROJECT_SUMMARY_FIELDS) {
    summary[field] = field === 'id' || field === 'name' || field === 'backend' || field === 'sourceRoot'
      ? stringOrNull(project[field])
      : project[field] === null || project[field] === undefined
        ? null
        : stringOrNull(project[field])
  }
  return summary
}

/**
 * Read the project list from the backend manager.
 *
 * @param {any} backend
 * @returns {Promise<{projects: Record<string, any>[], activeProjectId: string|null} | null>}
 *   null when the manager build does not expose the project model; BackendError
 *   propagates so RPC callers can normalize it.
 */
export async function fetchProjects(backend) {
  if (!supportsProjects(backend)) return null
  const result = await backend.listProjects()
  if (!isPlainObject(result)) {
    return { projects: [], activeProjectId: null }
  }
  const projects = Array.isArray(result.projects)
    ? result.projects.filter((project) => isPlainObject(project))
    : []
  const activeProjectId =
    typeof result.activeProjectId === 'string' && result.activeProjectId.length > 0
      ? result.activeProjectId
      : null
  return { projects, activeProjectId }
}

/**
 * Resolve the active project summary for status rendering. Never throws: any
 * manager/transport problem degrades to `null`.
 *
 * @param {any} backend
 * @returns {Promise<ReturnType<typeof summarizeProject> | null>}
 */
export async function fetchActiveProject(backend) {
  if (!backend) return null
  if (typeof backend.getActiveProject === 'function') {
    try {
      const project = await backend.getActiveProject()
      if (isPlainObject(project)) return summarizeProject(project)
    } catch {
      // Fall through to the list lookup.
    }
  }
  try {
    const list = await fetchProjects(backend)
    if (!list) return null
    if (list.activeProjectId !== null) {
      const byId = list.projects.find((project) => project.id === list.activeProjectId)
      if (byId) return summarizeProject(byId)
    }
    const flagged = list.projects.find((project) => project.active === true)
    return flagged ? summarizeProject(flagged) : null
  } catch {
    return null
  }
}

/**
 * Find an existing project by exact id, then exact name, then case-insensitive
 * name. Returns null when nothing matches.
 *
 * @param {Record<string, any>[]} projects
 * @param {unknown} target
 */
export function findProject(projects, target) {
  if (!Array.isArray(projects) || typeof target !== 'string') return null
  const wanted = target.trim()
  if (wanted.length === 0) return null
  const byId = projects.find((project) => project.id === wanted)
  if (byId) return byId
  const byName = projects.find((project) => project.name === wanted)
  if (byName) return byName
  const wantedLower = wanted.toLowerCase()
  return (
    projects.find(
      (project) => typeof project.name === 'string' && project.name.toLowerCase() === wantedLower,
    ) ?? null
  )
}

/** Human one-line database description for a project row. */
export function projectDatabaseLabel(project) {
  const db = stringOrNull(project?.db)
  const dbDir = stringOrNull(project?.dbDir)
  if (db) return `db ${db}`
  if (dbDir) return `dbDir ${dbDir}`
  return 'database unset'
}
