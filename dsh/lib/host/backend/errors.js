/**
 * Error type shared by every CA backend module.
 *
 * Every failure that crosses a module boundary is a BackendError so callers
 * (RPC layer, tools, task manager) can branch on a stable `code` and show an
 * actionable `message` to the user. `details` is a small JSON-serializable
 * object; it must never contain credentials (SSH args are redacted by the
 * remote module before being attached).
 *
 * Codes used by this package (uppercase, hyphen separated):
 *   BackendError                    generic
 *   DISPOSED / INVALID_ARGUMENT / NOT_CONFIGURED
 *   CONFIG_INVALID / CONFIG_WRITE_FAILED
 *   DISCOVERY_FAILED / REPO_NOT_FOUND / PYTHON_NOT_FOUND / DB_NOT_FOUND
 *   SPAWN_FAILED / EXEC_FAILED
 *   SERVE_FAILED / SERVE_EXITED / HEALTH_TIMEOUT / NOT_READY / NOT_RUNNING
 *   ALREADY_RUNNING / BACKEND_NOT_RUNNING
 *   HTTP_TIMEOUT / HTTP_NETWORK / HTTP_INVALID_JSON / HTTP_STATUS
 *   HTTP_UNAUTHORIZED / HTTP_NOT_FOUND / HTTP_CONFLICT / HTTP_SERVER_ERROR
 *   CANCELLED
 *   SSH_UNREACHABLE / SSH_AUTH_FAILED / SSH_HOST_UNKNOWN / SSH_FAILED
 *   REMOTE_NOT_IMPLEMENTED / PORT_UNAVAILABLE / PROBE_FAILED
 *   INSTALL_STEP_FAILED / INSTALL_FAILED / ALREADY_RUNNING
 */

/** Error carrying a stable machine-readable code plus optional details. */
export class BackendError extends Error {
  /**
   * @param {string} code stable uppercase code (see module comment)
   * @param {string} message actionable, user-facing message
   * @param {Record<string, unknown>} [details] small JSON-serializable extras
   */
  constructor(code, message, details) {
    super(message);
    this.name = 'BackendError';
    this.code = typeof code === 'string' && code.length > 0 ? code : 'INTERNAL';
    if (details !== undefined) this.details = details;
  }

  /** Payload shape used by the RPC layer: `{code,message,details}`. */
  toJSON() {
    const payload = { code: this.code, message: this.message };
    if (this.details !== undefined) payload.details = this.details;
    return payload;
  }
}

/**
 * @param {string} code
 * @param {string} message
 * @param {Record<string, unknown>} [details]
 * @returns {BackendError}
 */
export function backendError(code, message, details) {
  return new BackendError(code, message, details);
}

/** True when `err` is a BackendError (including subclasses). */
export function isBackendError(err) {
  return err instanceof BackendError;
}

/** True for DOMException AbortError, Node ABORT_ERR, or our CANCELLED code. */
export function isAbortError(err) {
  if (!err || typeof err !== 'object') return false;
  if (err.code === 'CANCELLED') return true;
  if (err.name === 'AbortError' || err.code === 'ABORT_ERR') return true;
  return false;
}

/** Human-readable one-line cause description (never throws). */
export function describeCause(err) {
  if (err === undefined || err === null) return 'unknown';
  if (typeof err === 'string') return err;
  const code = err.code ? `${err.code}: ` : '';
  const message = err.message ? String(err.message) : String(err);
  return `${code}${message}`;
}

/**
 * Coerce any thrown value into a BackendError.
 *
 * Already-normalized errors pass through unchanged so their code/details are
 * preserved; abort-like errors become CANCELLED; everything else is wrapped
 * with `fallbackCode` and an optional context prefix.
 *
 * @param {unknown} err
 * @param {string} [fallbackCode]
 * @param {string} [context] short prefix, e.g. "ca serve 启动失败"
 * @returns {BackendError}
 */
export function normalizeError(err, fallbackCode = 'INTERNAL', context) {
  if (err instanceof BackendError) return err;
  if (isAbortError(err)) {
    return new BackendError('CANCELLED', context ? `${context}: 操作已取消` : '操作已取消', {
      cause: describeCause(err),
    });
  }
  const cause = describeCause(err);
  const message = context ? `${context}: ${cause}` : cause;
  return new BackendError(fallbackCode, message, { cause });
}

/** JSON-safe error payload for RPC/tools responses. */
export function toErrorPayload(err) {
  const normalized = normalizeError(err);
  return normalized.toJSON();
}
