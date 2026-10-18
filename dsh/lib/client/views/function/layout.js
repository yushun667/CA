/* Function detail view — persisted three-pane layout state.
 *
 * Sizes/columns are stored in localStorage under `ca.ui.layout` exactly like
 * the CA web UI:
 *   { top: number|null, left: number|null, right: number|null,
 *     leftCollapsed: boolean, rightCollapsed: boolean }
 * `null` means "use the stylesheet default" (top 32vh, left 22 %, right 26 %).
 */

export const LAYOUT_KEY = "ca.ui.layout";
export const LAYOUT_DEFAULTS = Object.freeze({
  top: null,
  left: null,
  right: null,
  leftCollapsed: false,
  rightCollapsed: false,
});
export const LAYOUT_MIN = Object.freeze({
  top: 140,
  left: 220,
  middle: 420,
  right: 280,
});

export function defaultLayout() {
  return {
    top: null,
    left: null,
    right: null,
    leftCollapsed: false,
    rightCollapsed: false,
  };
}

function positiveNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.round(number) : null;
}

function storage() {
  try {
    if (typeof window === "undefined" || !window.localStorage) return null;
    return window.localStorage;
  } catch (_error) {
    return null;
  }
}

export function readStoredLayout() {
  const layout = defaultLayout();
  const store = storage();
  if (!store) return layout;
  try {
    const raw = store.getItem(LAYOUT_KEY);
    if (!raw) return layout;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return layout;
    layout.top = positiveNumber(parsed.top);
    layout.left = positiveNumber(parsed.left);
    layout.right = positiveNumber(parsed.right);
    layout.leftCollapsed = Boolean(parsed.leftCollapsed);
    layout.rightCollapsed = Boolean(parsed.rightCollapsed);
  } catch (_error) {
    return defaultLayout();
  }
  return layout;
}

export function saveStoredLayout(layout) {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(LAYOUT_KEY, JSON.stringify(layout || defaultLayout()));
  } catch (_error) {
    /* storage can be blocked; dragging still works for this page */
  }
}

function clamp(value, min, max) {
  return Math.round(Math.min(Math.max(value, min), Math.max(min, max)));
}

/**
 * Clamp stored/dragged sizes to the current viewport/workspace.
 *
 * `viewport` = `{viewportWidth, viewportHeight, workspaceWidth}`; any missing
 * value makes that dimension unconstrained. Returns the same object when
 * nothing changed (so callers can avoid needless re-renders).
 */
export function clampLayout(layout, viewport) {
  const source = layout || defaultLayout();
  const bounds = viewport || {};
  const viewportHeight = Number(bounds.viewportHeight) || 0;
  const workspaceWidth = Number(bounds.workspaceWidth) || 0;
  const next = {
    top: source.top,
    left: source.left,
    right: source.right,
    leftCollapsed: Boolean(source.leftCollapsed),
    rightCollapsed: Boolean(source.rightCollapsed),
  };

  if (next.top !== null && viewportHeight > 0) {
    next.top = clamp(
      next.top,
      LAYOUT_MIN.top,
      Math.max(LAYOUT_MIN.top, viewportHeight * 0.7)
    );
  }
  if (workspaceWidth > 0) {
    const leftPx = next.left || Math.round(workspaceWidth * 0.22);
    const rightPx = next.right || Math.round(workspaceWidth * 0.26);
    const maxLeft = Math.max(
      LAYOUT_MIN.left,
      workspaceWidth - rightPx - LAYOUT_MIN.middle - 16
    );
    const maxRight = Math.max(
      LAYOUT_MIN.right,
      workspaceWidth - leftPx - LAYOUT_MIN.middle - 16
    );
    if (next.left !== null) {
      next.left = clamp(next.left, LAYOUT_MIN.left, maxLeft);
    }
    if (next.right !== null) {
      next.right = clamp(next.right, LAYOUT_MIN.right, maxRight);
    }
  }

  const unchanged =
    next.top === source.top &&
    next.left === source.left &&
    next.right === source.right &&
    next.leftCollapsed === Boolean(source.leftCollapsed) &&
    next.rightCollapsed === Boolean(source.rightCollapsed);
  return unchanged ? source : next;
}

/** CSS custom properties consumed by the function-view stylesheet. */
export function layoutStyle(layout) {
  const value = layout || defaultLayout();
  return {
    "--ca-fn-top-h": value.top ? value.top + "px" : "32vh",
    "--ca-fn-left-w": value.left ? value.left + "px" : "22%",
    "--ca-fn-right-w": value.right ? value.right + "px" : "26%",
  };
}
