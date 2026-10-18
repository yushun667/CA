/**
 * Pure decision helpers for the CA shell.
 *
 * These live outside `shell/index.js` so the routing/lifecycle decisions can be
 * unit-tested without React or a DOM. The shell keeps the same rules in sync:
 * the backend is an implementation detail (never a settings page), so it starts
 * automatically for an active project and the panel lands on projects when
 * there is nothing to show yet.
 */

/**
 * Does the running backend configuration correspond to this project?
 * Guards automatic starts against race conditions during project switches.
 *
 * @param {object|null} backend `status.backend`
 * @param {object|null} project current project
 * @returns {boolean}
 */
export function backendMatchesProject(backend, project) {
  if (!backend || !project) return false
  if (backend.mode !== project.backend) return false
  const wanted = project.db || project.dbDir || null
  if (wanted && backend.db !== wanted) return false
  return true
}

/** Minimum delay between automatic start attempts (ms). */
export const AUTO_START_THROTTLE_MS = 15000

/**
 * Should the shell start the backend on its own?
 *
 * @param {object|null} backend `status.backend`
 * @param {object|null} project current project (`currentProject()`)
 * @param {number} lastStartedAt timestamp of the previous attempt (0 = never)
 * @param {number} now current timestamp
 * @returns {boolean}
 */
export function shouldAutoStartBackend(backend, project, lastStartedAt, now = Date.now()) {
  if (!backend || typeof backend !== 'object') return false
  if (backend.mode === 'unconfigured') return false
  if (!project) return false
  if (backend.state !== 'stopped') return false
  const last = Number(lastStartedAt) || 0
  if (last <= 0) return true // no attempt yet in this page session
  return now - last >= AUTO_START_THROTTLE_MS
}

/**
 * Should the shell switch the landing route to `#/projects`?
 *
 * @param {object} input
 * @param {boolean} input.statusLoading initial status RPC still in flight
 * @param {boolean|null} input.connected status transport state (null = unknown)
 * @param {object|null} input.project current project
 * @param {string} input.routePage current route page
 * @returns {boolean}
 */
export function shouldLandOnProjects({ statusLoading, connected, project, routePage }) {
  if (statusLoading) return false
  if (connected === null || connected === undefined) return false
  if (project) return false
  return routePage !== 'projects'
}

export default shouldAutoStartBackend
