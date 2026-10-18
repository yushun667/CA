/* Function detail view — session-scoped function jump history.
 *
 * Mirrors the CA web UI (`ca.ui.fnHistory`, sessionStorage, deduplicated at the
 * top, capped at 50 entries). Pure list helpers keep the React page simple and
 * testable: `loadHistory`, `saveHistory`, `pushEntry`, `popEntry`.
 */

export const HISTORY_KEY = "ca.ui.fnHistory";
export const HISTORY_LIMIT = 50;

function storage() {
  try {
    if (typeof window === "undefined" || !window.sessionStorage) return null;
    return window.sessionStorage;
  } catch (_error) {
    return null;
  }
}

export function normalizeEntry(entry) {
  if (!entry || typeof entry !== "object") return null;
  const symbol = String(entry.symbol || "").trim();
  if (!symbol) return null;
  const tab = typeof entry.tab === "string" ? entry.tab.trim() : "";
  return { symbol, tab };
}

export function loadHistory() {
  const store = storage();
  if (!store) return [];
  try {
    const raw = store.getItem(HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map(normalizeEntry)
      .filter(Boolean)
      .slice(-HISTORY_LIMIT);
  } catch (_error) {
    return [];
  }
}

export function saveHistory(list) {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(HISTORY_KEY, JSON.stringify(list || []));
  } catch (_error) {
    /* storage can be blocked; the stack still works for this page */
  }
}

/**
 * Push `entry` on top of `list`. The same symbol as the current top only
 * updates its tab (no duplicate entries); the list never exceeds the cap.
 */
export function pushEntry(list, entry) {
  const normalized = normalizeEntry(entry);
  const next = Array.isArray(list) ? list.slice() : [];
  if (!normalized) return next;
  const top = next[next.length - 1];
  if (top && top.symbol === normalized.symbol) {
    next[next.length - 1] = { symbol: top.symbol, tab: normalized.tab };
    return next;
  }
  next.push(normalized);
  return next.length > HISTORY_LIMIT ? next.slice(-HISTORY_LIMIT) : next;
}

/** Pop the most recent entry: `{entry, list}` (`entry` is null when empty). */
export function popEntry(list) {
  const next = Array.isArray(list) ? list.slice() : [];
  if (!next.length) return { entry: null, list: next };
  const entry = next.pop() || null;
  return { entry, list: next };
}
