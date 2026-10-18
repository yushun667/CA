/**
 * CA protocol constants and parsers shared by the local and remote backends.
 *
 * Keeping the readiness-line regex and version parser in one place means the
 * local `ca serve` path and the remote-over-SSH path can never drift apart.
 */

/**
 * stderr line printed by `ca serve` once the HTTP server is bound:
 * `[serve] CA backend listening on http://127.0.0.1:8765 (db=...; federated=no; ...)`
 */
export const READY_LINE_RE = /\[serve\] CA backend listening on (https?:\/\/[^\s)]+)/;

/**
 * Extract the URL (and numeric port) from a readiness line.
 * @param {string} line
 * @returns {{url: string, port: number|null}|null}
 */
export function parseReadyLine(line) {
  const match = READY_LINE_RE.exec(String(line ?? ''));
  if (!match) return null;
  const url = match[1];
  try {
    const parsed = new URL(url);
    const port = Number(parsed.port) || (parsed.protocol === 'https:' ? 443 : 80);
    return { url, port: Number.isFinite(port) ? port : null };
  } catch {
    return { url, port: null };
  }
}

/**
 * Parse `ca version --json` output (may be pretty-printed). Returns null when
 * the text does not contain a JSON object.
 * @param {string} text
 */
export function parseCaVersionJson(text) {
  const raw = String(text ?? '').trim();
  if (!raw) return null;
  const start = raw.indexOf('{');
  const end = raw.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    const parsed = JSON.parse(raw.slice(start, end + 1));
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

/** Format a CA version record (`ca version --json`) for logs/reports. */
export function describeCaVersion(record) {
  if (!record || typeof record !== 'object') return null;
  const version = typeof record.version === 'string' ? record.version : null;
  const schema = record.schema_version !== undefined ? String(record.schema_version) : null;
  if (!version) return null;
  return schema ? `ca-analyzer ${version} (schema ${schema})` : `ca-analyzer ${version}`;
}
