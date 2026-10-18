/**
 * Aggregate exports for the CA backend layer.
 *
 * The host wiring only needs `createBackendManager` + `BackendError`; the
 * remaining exports exist for tests, RPC helpers and the remote module.
 */

export { createBackendManager, BackendError } from './manager.js';
export { isBackendError, normalizeError, toErrorPayload } from './errors.js';
export { createLogger } from './log.js';
export { createHttpClient } from './http.js';
export { createLocalBackend } from './local.js';
export { buildCaArgv, resolveLocalTarget, resolveRuntime } from './discover.js';
