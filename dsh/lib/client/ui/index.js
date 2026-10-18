/**
 * CA DSH client UI primitives.
 *
 * `createUI(React, ctx?)` returns a small component kit plus a stylesheet
 * bridge. Everything is plain React + CSS custom properties: no Harness React
 * components are imported, colors/spacing/radii come from the `--dsw-*` theme
 * tokens, and class names are prefixed with `ca-` so they cannot clash with the
 * host application.
 *
 * Style ownership: `createUI` injects one <style data-ca-ui> element into
 * document.head (outside document.body) through `ctx.effect` when a context is
 * given, so plugin unload removes it. Views/pages may add their own CSS with
 * `ui.useStyles(css)` / `ui.injectCss(css)`; those blocks are reference-counted
 * and removed when their last owner unmounts.
 *
 * All primitives expose keyboard semantics: buttons are native <button>s,
 * tabs follow the tablist pattern with arrow-key navigation, splitters are
 * focusable separators with arrow-key resizing, and every control accepts an
 * `ariaLabel` prop where visible text is not enough.
 */

const STYLE_ATTR = 'data-ca-ui'

/** Injected once per plugin instance; kept free of host imports. */
const BASE_CSS = `
.ca-app {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: var(--dsw-alias-bg-base, #ffffff);
  color: var(--dsw-alias-label-primary, #1f2328);
  font-family: var(--dsw-font-family, system-ui, -apple-system, "Segoe UI", sans-serif);
  font-size: var(--dsw-font-base-16-font-size, 14px);
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}
.ca-app *, .ca-app *::before, .ca-app *::after { box-sizing: border-box; }
.ca-app :focus-visible {
  outline-width: var(--dsw-focus-ring-width, 2px);
  outline-style: solid;
  outline-color: var(--dsw-focus-ring-color, #4176e6);
  outline-offset: 1px;
}
.ca-mono { font-family: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, monospace); }
.ca-muted { color: var(--dsw-alias-label-secondary, #57606a); }
.ca-faint { color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ca-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.ca-row-wrap { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.ca-col { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.ca-grow { flex: 1 1 auto; min-width: 0; }
.ca-spacer { flex: 1 1 auto; }
.ca-hr { height: 1px; background: var(--dsw-alias-border-l2, #e5e7eb); border: 0; margin: 0; }

/* -------------------------------------------------------------- buttons */
.ca-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 30px;
  padding: 5px 12px;
  border: 1px solid transparent;
  border-radius: var(--dsw-radius-sm, 6px);
  background: transparent;
  color: var(--dsw-alias-label-primary, #1f2328);
  font: inherit;
  line-height: 1.2;
  white-space: nowrap;
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
}
.ca-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.ca-btn-sm { min-height: 24px; padding: 2px 8px; font-size: 0.92em; }
.ca-btn-md { min-height: 30px; }
.ca-btn-primary { background: var(--dsw-alias-button-primary-fill, #2f6feb); color: var(--dsw-alias-label-primary-inverted, #ffffff); }
.ca-btn-primary:hover:not(:disabled) { background: var(--dsw-alias-button-primary-hover, #1f5fd8); }
.ca-btn-secondary { background: var(--dsw-alias-bg-layer-1, transparent); border-color: var(--dsw-alias-border-l3, #d0d7de); color: var(--dsw-alias-label-primary, #1f2328); }
.ca-btn-secondary:hover:not(:disabled) { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.1)); }
.ca-btn-ghost { color: var(--dsw-alias-label-secondary, #57606a); }
.ca-btn-ghost:hover:not(:disabled) { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.1)); color: var(--dsw-alias-label-primary, #1f2328); }
.ca-btn-ghost.ca-btn-active { background: var(--dsw-alias-button-ghost-active-fill, rgba(127, 127, 127, 0.14)); color: var(--dsw-alias-label-primary, #1f2328); }
.ca-btn-danger { color: var(--dsw-alias-state-error-primary, #d1242f); border-color: var(--dsw-alias-border-l3, #d0d7de); }
.ca-btn-danger:hover:not(:disabled) { background: var(--dsw-alias-interactive-bg-hover-danger, rgba(209, 36, 47, 0.1)); }
.ca-icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  min-width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--dsw-radius-sm, 6px);
  background: transparent;
  color: var(--dsw-alias-label-secondary, #57606a);
  cursor: pointer;
}
.ca-icon-btn:hover:not(:disabled) { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.1)); color: var(--dsw-alias-label-primary, #1f2328); }
.ca-icon-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.ca-icon-btn.ca-icon-btn-active { background: var(--dsw-alias-button-ghost-active-fill, rgba(127, 127, 127, 0.14)); color: var(--dsw-alias-label-primary, #1f2328); }

/* --------------------------------------------------------------- inputs */
.ca-input, .ca-select, .ca-textarea {
  width: 100%;
  min-height: 30px;
  padding: 5px 9px;
  border: 1px solid var(--dsw-alias-border-l3, #d0d7de);
  border-radius: var(--dsw-radius-sm, 6px);
  background: var(--dsw-alias-bg-layer-1, #ffffff);
  color: var(--dsw-alias-label-primary, #1f2328);
  font: inherit;
  transition: border-color 120ms ease, box-shadow 120ms ease;
}
.ca-textarea { resize: vertical; line-height: 1.5; }
.ca-select { cursor: pointer; }
.ca-input::placeholder, .ca-textarea::placeholder { color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-input:focus, .ca-select:focus, .ca-textarea:focus {
  border-color: var(--dsw-alias-state-business-primary, #2f6feb);
  box-shadow: 0 0 0 1px var(--dsw-alias-state-business-primary, #2f6feb);
  outline: none;
}
.ca-input:disabled, .ca-select:disabled, .ca-textarea:disabled { opacity: 0.6; cursor: not-allowed; }
.ca-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.ca-field-label { font-size: 12px; font-weight: 600; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-field-hint { font-size: 11.5px; color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-field-error { font-size: 11.5px; color: var(--dsw-alias-state-error-primary, #d1242f); }

/* ---------------------------------------------------------------- cards */
.ca-card {
  border: 1px solid var(--dsw-alias-border-l2, #e5e7eb);
  border-radius: var(--dsw-radius-lg, 10px);
  background: var(--dsw-alias-bg-layer-1, #ffffff);
  min-width: 0;
}
.ca-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 12px 16px 0; }
.ca-card-head-main { min-width: 0; }
.ca-card-title { margin: 0; font-size: 14.5px; font-weight: 600; color: var(--dsw-alias-label-primary, #1f2328); }
.ca-card-sub { margin: 2px 0 0; font-size: 12px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-card-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.ca-card-body { padding: 12px 16px 16px; }
.ca-card-body-tight { padding: 8px 16px 12px; }
.ca-section { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.ca-section-head { display: flex; align-items: center; gap: 8px; min-height: 24px; }
.ca-section-title { margin: 0; font-size: 13px; font-weight: 600; color: var(--dsw-alias-label-primary, #1f2328); }
.ca-section-note { font-size: 12px; color: var(--dsw-alias-label-tertiary, #8b949e); }

/* --------------------------------------------------------------- tables */
.ca-table-wrap { width: 100%; overflow: auto; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); }
.ca-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.ca-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: 7px 10px;
  border-bottom: 1px solid var(--dsw-alias-border-l2, #e5e7eb);
  background: var(--dsw-alias-bg-layer-2, #f6f8fa);
  color: var(--dsw-alias-label-secondary, #57606a);
  font-weight: 600;
  text-align: left;
  white-space: nowrap;
}
.ca-table td { padding: 6px 10px; border-bottom: 1px solid var(--dsw-alias-border-l1, #eaeef2); vertical-align: top; }
.ca-table tbody tr:last-child td { border-bottom: 0; }
.ca-table tbody tr:hover td { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.07)); }
.ca-table tbody tr.ca-table-row-selected td { background: var(--dsw-alias-button-ghost-active-fill, rgba(127, 127, 127, 0.13)); }
.ca-table-clickable tbody tr { cursor: pointer; }
.ca-table-dense th, .ca-table-dense td { padding: 3px 8px; }
.ca-table-empty td { color: var(--dsw-alias-label-tertiary, #8b949e); font-style: italic; padding: 14px 10px; }
.ca-table-cell-right { text-align: right; }
.ca-table-cell-center { text-align: center; }

/* ----------------------------------------------------------------- tabs */
.ca-tabs { display: flex; align-items: stretch; gap: 2px; border-bottom: 1px solid var(--dsw-alias-border-l2, #e5e7eb); overflow-x: auto; scrollbar-width: thin; }
.ca-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 12px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--dsw-alias-label-secondary, #57606a);
  font: inherit;
  cursor: pointer;
  white-space: nowrap;
}
.ca-tab:hover:not(:disabled) { color: var(--dsw-alias-label-primary, #1f2328); background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.07)); }
.ca-tab:disabled { opacity: 0.5; cursor: not-allowed; }
.ca-tab-active { color: var(--dsw-alias-label-primary, #1f2328); border-bottom-color: var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-tab-count { padding: 0 6px; border-radius: 999px; background: var(--dsw-alias-bg-layer-3, rgba(127, 127, 127, 0.14)); color: var(--dsw-alias-label-secondary, #57606a); font-size: 11px; line-height: 16px; }

/* --------------------------------------------------------------- badges */
.ca-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 7px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  line-height: 17px;
  white-space: nowrap;
}
.ca-badge-neutral { background: var(--dsw-alias-bg-layer-3, rgba(127, 127, 127, 0.14)); color: var(--dsw-alias-label-secondary, #57606a); }
.ca-badge-muted { background: transparent; border-color: var(--dsw-alias-border-l3, #d0d7de); color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-badge-info { background: var(--dsw-alias-bg-mask-3, rgba(65, 118, 230, 0.14)); color: var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-badge-success { background: var(--dsw-alias-bg-mask-2, rgba(26, 127, 55, 0.14)); color: var(--dsw-alias-state-success-primary, #1a7f37); }
.ca-badge-warn { background: var(--dsw-alias-bg-mask-2, rgba(154, 103, 0, 0.14)); color: var(--dsw-alias-state-warn-label, #9a6700); }
.ca-badge-error { background: var(--dsw-alias-bg-mask-1, rgba(209, 36, 47, 0.14)); color: var(--dsw-alias-state-error-primary, #d1242f); }
.ca-badge-brand { background: var(--dsw-alias-brand-primary, #2f6feb); color: var(--dsw-alias-label-primary-inverted, #ffffff); }

/* ------------------------------------------------------- spinner/states */
.ca-spinner { display: inline-block; border-radius: 50%; border: 2px solid var(--dsw-alias-border-l3, #d0d7de); border-top-color: var(--dsw-alias-state-business-primary, #2f6feb); animation: ca-spin 0.8s linear infinite; }
@keyframes ca-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .ca-spinner { animation-duration: 2.4s; } }
.ca-spinner-label { display: inline-flex; align-items: center; gap: 8px; color: var(--dsw-alias-label-secondary, #57606a); font-size: 12.5px; }
.ca-error {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--dsw-alias-state-error-secondary, rgba(209, 36, 47, 0.4));
  border-radius: var(--dsw-radius-md, 8px);
  background: var(--dsw-alias-bg-layer-1, #ffffff);
  color: var(--dsw-alias-label-primary, #1f2328);
  font-size: 12.5px;
  min-width: 0;
}
.ca-error-head { display: flex; align-items: center; gap: 8px; font-weight: 600; color: var(--dsw-alias-state-error-primary, #d1242f); }
.ca-error-msg { white-space: pre-wrap; overflow-wrap: anywhere; }
.ca-error-code { font-family: var(--ds-font-family-code, ui-monospace, monospace); font-size: 11px; color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-error-hint { color: var(--dsw-alias-label-secondary, #57606a); }
.ca-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; padding: 28px 16px; text-align: center; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-empty-icon { color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-empty-title { font-size: 13.5px; font-weight: 600; color: var(--dsw-alias-label-primary, #1f2328); }
.ca-empty-body { font-size: 12.5px; max-width: 460px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-empty-action { margin-top: 6px; }

/* ------------------------------------------------------------- codeview */
.ca-code {
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--dsw-alias-border-l2, #e5e7eb);
  border-radius: var(--dsw-radius-md, 8px);
  background: var(--dsw-alias-markdown-code-block, #f6f8fa);
  font-family: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: var(--dsh-code-font-size, 12px);
  line-height: 1.55;
}
.ca-code-head { display: flex; align-items: center; gap: 8px; padding: 5px 10px; border-bottom: 1px solid var(--dsw-alias-border-l2, #e5e7eb); background: var(--dsw-alias-markdown-code-block-banner, transparent); color: var(--dsw-alias-label-secondary, #57606a); font-family: var(--dsw-font-family, system-ui, sans-serif); font-size: 12px; }
.ca-code-file { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ca-code-scroll { overflow: auto; min-height: 0; }
.ca-code-body { display: table; min-width: 100%; }
.ca-code-line { display: table-row; }
.ca-code-gutter {
  display: table-cell;
  padding: 0 10px 0 12px;
  border-right: 1px solid var(--dsw-alias-border-l1, #eaeef2);
  color: var(--dsw-alias-label-tertiary, #8b949e);
  text-align: right;
  white-space: nowrap;
  user-select: none;
}
.ca-code-text { display: table-cell; padding: 0 12px; white-space: pre; tab-size: 4; }
.ca-code-line-hl .ca-code-gutter, .ca-code-line-hl .ca-code-text { background: var(--dsw-alias-bg-document-selection, rgba(65, 118, 230, 0.16)); }
.ca-tok-keyword { color: var(--dsw-alias-link, #0550ae); font-weight: 600; }
.ca-tok-type { color: var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-tok-comment { color: var(--dsw-alias-label-tertiary, #8b949e); font-style: italic; }
.ca-tok-string { color: var(--dsw-alias-state-success-primary, #1a7f37); }
.ca-tok-number { color: var(--dsw-alias-state-warn-primary, #9a6700); }
.ca-tok-preproc { color: var(--dsw-alias-label-secondary, #57606a); }
.ca-tok-function { color: var(--dsw-alias-label-primary, #1f2328); }

/* ------------------------------------------------------------- splitter */
.ca-splitter { position: relative; flex: 0 0 auto; padding: 0; border: 0; background: transparent; touch-action: none; }
.ca-splitter-h { width: 7px; cursor: col-resize; }
.ca-splitter-v { height: 7px; cursor: row-resize; }
.ca-splitter::after { content: ""; position: absolute; border-radius: 2px; background: var(--dsw-alias-border-l2, #e5e7eb); }
.ca-splitter-h::after { top: 0; bottom: 0; left: 3px; width: 1px; }
.ca-splitter-v::after { left: 0; right: 0; top: 3px; height: 1px; }
.ca-splitter:hover::after, .ca-splitter-active::after, .ca-splitter:focus-visible::after { background: var(--dsw-alias-state-business-primary, #2f6feb); }

/* ----------------------------------------------------------- misc utils */
.ca-kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 0; font-size: 12.5px; }
.ca-kv dt { color: var(--dsw-alias-label-secondary, #57606a); white-space: nowrap; }
.ca-kv dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.ca-pre { margin: 0; padding: 10px 12px; overflow: auto; max-height: 420px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-markdown-code-block, #f6f8fa); font-family: var(--ds-font-family-code, ui-monospace, monospace); font-size: 11.5px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.ca-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.ca-page { position: relative; display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; overflow: hidden; }
.ca-page-scroll { overflow: auto; }
.ca-page-pad { padding: 16px; gap: 14px; }
.ca-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.ca-visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
`

// ---------------------------------------------------------------------------
// stylesheet bridge
// ---------------------------------------------------------------------------

/** @type {{ el: HTMLStyleElement|null, blocks: Map<string, number>, order: string[] }} */
const styleStore = { el: null, blocks: new Map(), order: [] }

function canUseDocument() {
  return typeof document !== 'undefined' && document !== null && typeof document.createElement === 'function'
}

function ensureStyleElement() {
  if (!canUseDocument()) return null
  if (styleStore.el && styleStore.el.isConnected) return styleStore.el
  const existing = document.head ? document.head.querySelector(`style[${STYLE_ATTR}]`) : null
  if (existing) {
    styleStore.el = existing
    return existing
  }
  const element = document.createElement('style')
  element.setAttribute(STYLE_ATTR, 'ca')
  element.setAttribute('data-plugin', '@local/dsh-ca')
  if (document.head) document.head.appendChild(element)
  styleStore.el = element
  return element
}

function renderStyles() {
  const element = ensureStyleElement()
  if (!element) return
  const parts = []
  for (const key of styleStore.order) {
    // `blocks` maps each CSS string to its reference count; the key is the CSS
    // itself. (Pushing the value here wrote the count, e.g. "1", into the
    // <style> element, so no plugin stylesheet ever applied.)
    if (styleStore.blocks.has(key)) parts.push(key)
  }
  element.textContent = parts.join('\n')
}

/**
 * Add a CSS block owned by the caller. The block is removed when the returned
 * disposer runs (idempotent). Identical CSS strings are shared.
 */
export function injectCss(css) {
  if (typeof css !== 'string' || css.length === 0) return () => {}
  let key = css
  if (styleStore.blocks.has(key)) {
    styleStore.blocks.set(key, (styleStore.blocks.get(key) || 0) + 1)
  } else {
    styleStore.blocks.set(key, 1)
    styleStore.order.push(key)
    renderStyles()
  }
  let disposed = false
  return () => {
    if (disposed) return
    disposed = true
    const count = styleStore.blocks.get(key) || 0
    if (count > 1) {
      styleStore.blocks.set(key, count - 1)
      return
    }
    styleStore.blocks.delete(key)
    styleStore.order = styleStore.order.filter((entry) => entry !== key)
    renderStyles()
  }
}

/**
 * Inject the base stylesheet once. With a plugin context the style element is
 * owned by `ctx.effect`, so plugin unload removes it.
 */
export function attachStyles(ctx) {
  if (ctx && typeof ctx.effect === 'function') {
    ctx.effect(() => injectCss(BASE_CSS), 'ca: ui styles')
    return
  }
  injectCss(BASE_CSS)
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/** Join truthy class names. */
export function cx(...parts) {
  const out = []
  for (const part of parts) {
    if (!part) continue
    if (typeof part === 'string') out.push(part)
    else if (Array.isArray(part)) {
      const nested = cx(...part)
      if (nested) out.push(nested)
    } else if (typeof part === 'object') {
      for (const [name, enabled] of Object.entries(part)) if (enabled) out.push(name)
    }
  }
  return out.join(' ')
}

function ariaLabelProps(props) {
  const out = {}
  if (props && typeof props.ariaLabel === 'string' && props.ariaLabel) out['aria-label'] = props.ariaLabel
  return out
}

function clampNumber(value, min, max, fallback) {
  const number = Number(value)
  if (!Number.isFinite(number)) return fallback
  const lo = Number.isFinite(min) ? min : -Infinity
  const hi = Number.isFinite(max) ? max : Infinity
  return Math.min(hi, Math.max(lo, Math.round(number)))
}

function normalizeTone(tone) {
  switch (tone) {
    case 'warning':
      return 'warn'
    case 'danger':
      return 'error'
    case 'brand':
    case 'info':
    case 'success':
    case 'warn':
    case 'error':
    case 'muted':
    case 'neutral':
      return tone
    default:
      return 'neutral'
  }
}

const ICON_PATHS = {
  ca: ['M4 5h16v14H4z', 'M8 9h8M8 12h8M8 15h5'],
  search: ['M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14z', 'M16.5 16.5 21 21'],
  stats: ['M5 20V10M12 20V4M19 20v-7'],
  callbacks: ['M7 7h10a3 3 0 0 1 3 3v0a3 3 0 0 1-3 3h-4l-4 4v-4H7a3 3 0 0 1-3-3v0a3 3 0 0 1 3-3z'],
  graph: ['M5 6a2 2 0 1 1 4 0 2 2 0 0 1-4 0z', 'M15 6a2 2 0 1 1 4 0 2 2 0 0 1-4 0z', 'M10 18a2 2 0 1 1 4 0 2 2 0 0 1-4 0z', 'M9 6h6M8 8l2 8M16 8l-2 8'],
  tasks: ['M5 6h14M5 12h14M5 18h9'],
  backend: ['M4 5h16v5H4z', 'M4 14h16v5H4z', 'M8 7.5h.01M8 16.5h.01'],
  close: ['M6 6l12 12M18 6 6 18'],
  'chevron-down': ['M6 9l6 6 6-6'],
  'chevron-right': ['M9 6l6 6-6 6'],
  refresh: ['M20 12a8 8 0 1 1-2.3-5.6', 'M20 4v5h-5'],
  play: ['M8 5l11 7-11 7z'],
  stop: ['M7 7h10v10H7z'],
  check: ['M5 12l5 5L20 7'],
  warning: ['M12 4 22 20H2z', 'M12 9v5M12 17h.01'],
  info: ['M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z', 'M12 11v6M12 7h.01'],
  copy: ['M9 9h11v11H9z', 'M5 15V5h10'],
  external: ['M14 4h6v6', 'M20 4l-9 9', 'M18 14v5H5V6h5'],
  settings: ['M12 9a3 3 0 1 1 0 6 3 3 0 0 1 0-6z', 'M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.3 1a7 7 0 0 0-1.7-1L14.5 3h-5l-.4 2.6a7 7 0 0 0-1.7 1l-2.3-1-2 3.4 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 1.7 1l.4 2.6h5l.4-2.6a7 7 0 0 0 1.7-1l2.3 1 2-3.4-2-1.5c.06-.33.1-.66.1-1z'],
  menu: ['M4 6h16M4 12h16M4 18h16'],
  folder: ['M3 6.5h6l2 2h10v9H3z'],
}

// ---------------------------------------------------------------------------
// primitives
// ---------------------------------------------------------------------------

/**
 * Build the primitive kit.
 *
 * @param {object} React React namespace supplied by the Client module table.
 * @param {object} [ctx] DSH Client plugin context; when present the base
 *   stylesheet is registered as an owned effect.
 */
export function createUI(React, ctx) {
  if (!React || typeof React.createElement !== 'function') {
    throw new TypeError('createUI(React): React with createElement is required')
  }
  const h = React.createElement

  if (ctx) attachStyles(ctx)
  else {
    // Lazy fallback for tests/standalone rendering: inject on first component mount.
    let injected = false
    const ensureInjected = () => {
      if (injected) return
      injected = true
      injectCss(BASE_CSS)
    }
    ctx = { effect: undefined, __ensureInjected: ensureInjected }
  }

  const ensureInjected = ctx && typeof ctx.__ensureInjected === 'function' ? ctx.__ensureInjected : () => {}

  function useStyles(css) {
    React.useEffect(() => {
      ensureInjected()
      const dispose = injectCss(css)
      return dispose
      // css identity is expected to be a module-level constant
    }, [css])
  }

  function Icon(props) {
    const size = Number(props.size) > 0 ? Number(props.size) : 16
    const name = Object.prototype.hasOwnProperty.call(ICON_PATHS, props.name) ? props.name : 'ca'
    const paths = ICON_PATHS[name]
    const children = paths.map((d, index) =>
      h('path', {
        key: index,
        d,
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth: 1.6,
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
      }),
    )
    return h(
      'svg',
      {
        viewBox: '0 0 24 24',
        width: size,
        height: size,
        className: cx('ca-icon', props.className),
        'aria-hidden': props.title ? undefined : true,
        role: props.title ? 'img' : undefined,
        focusable: 'false',
        style: Object.assign({ display: 'block', flex: '0 0 auto' }, props.style || {}),
      },
      props.title ? h('title', null, props.title) : null,
      children,
    )
  }

  function Button(props) {
    const variant = ['primary', 'secondary', 'ghost', 'danger'].includes(props.variant) ? props.variant : 'secondary'
    const size = props.size === 'sm' ? 'sm' : 'md'
    return h(
      'button',
      Object.assign(
        {
          type: props.type || 'button',
          className: cx('ca-btn', `ca-btn-${variant}`, `ca-btn-${size}`, props.active && 'ca-btn-active', props.className),
          disabled: props.disabled ? true : undefined,
          title: props.title,
          style: props.style,
          onClick: props.onClick,
        },
        ariaLabelProps(props),
        props['aria-expanded'] !== undefined ? { 'aria-expanded': props['aria-expanded'] } : null,
        props['aria-controls'] ? { 'aria-controls': props['aria-controls'] } : null,
      ),
      props.children,
    )
  }

  function IconButton(props) {
    const size = Number(props.size) > 0 ? Number(props.size) : 28
    return h(
      'button',
      Object.assign(
        {
          type: 'button',
          className: cx('ca-icon-btn', props.active && 'ca-icon-btn-active', props.className),
          disabled: props.disabled ? true : undefined,
          title: props.title || props.label,
          onClick: props.onClick,
          style: Object.assign({ width: size, minWidth: size, height: size }, props.style || {}),
        },
        ariaLabelProps({ ariaLabel: props.label }),
        props['aria-pressed'] !== undefined ? { 'aria-pressed': props['aria-pressed'] } : null,
        props['aria-expanded'] !== undefined ? { 'aria-expanded': props['aria-expanded'] } : null,
      ),
      props.children,
    )
  }

  function Input(props) {
    return h(
      'input',
      Object.assign(
        {
          type: props.type || 'text',
          className: cx('ca-input', props.className),
          value: props.value === undefined || props.value === null ? '' : props.value,
          placeholder: props.placeholder,
          disabled: props.disabled ? true : undefined,
          autoFocus: props.autoFocus ? true : undefined,
          spellCheck: props.spellCheck,
          autoComplete: props.autoComplete || 'off',
          ref: props.inputRef,
          onChange: props.onChange,
          onKeyDown: props.onKeyDown,
          onFocus: props.onFocus,
          onBlur: props.onBlur,
          name: props.name,
          title: props.title,
          style: props.style,
        },
        ariaLabelProps(props),
      ),
    )
  }

  function Select(props) {
    const options = Array.isArray(props.options) ? props.options : []
    return h(
      'select',
      Object.assign(
        {
          className: cx('ca-select', props.className),
          value: props.value === undefined || props.value === null ? '' : props.value,
          disabled: props.disabled ? true : undefined,
          title: props.title,
          style: props.style,
          onChange: (event) => {
            if (typeof props.onChange === 'function') props.onChange(event.target.value)
          },
        },
        ariaLabelProps(props),
      ),
      options.map((option, index) =>
        h(
          'option',
          {
            key: option.value !== undefined ? String(option.value) : `option-${index}`,
            value: option.value !== undefined ? String(option.value) : '',
            disabled: option.disabled ? true : undefined,
          },
          option.label === undefined ? String(option.value) : option.label,
        ),
      ),
    )
  }

  function TextArea(props) {
    return h(
      'textarea',
      Object.assign(
        {
          className: cx('ca-textarea', props.className),
          value: props.value === undefined || props.value === null ? '' : props.value,
          rows: props.rows || 4,
          placeholder: props.placeholder,
          disabled: props.disabled ? true : undefined,
          spellCheck: props.spellCheck === undefined ? false : props.spellCheck,
          onChange: props.onChange,
          onKeyDown: props.onKeyDown,
          title: props.title,
          style: props.style,
        },
        ariaLabelProps(props),
      ),
    )
  }

  function Card(props) {
    const body = h(
      'div',
      { className: cx('ca-card-body', props.padded === false && 'ca-card-body-tight') },
      props.children,
    )
    if (!props.title && !props.subtitle && !props.actions) {
      return h('section', { className: cx('ca-card', props.className) }, body)
    }
    return h(
      'section',
      { className: cx('ca-card', props.className) },
      h(
        'div',
        { className: 'ca-card-head' },
        h(
          'div',
          { className: 'ca-card-head-main' },
          props.title ? h('h3', { className: 'ca-card-title' }, props.title) : null,
          props.subtitle ? h('p', { className: 'ca-card-sub' }, props.subtitle) : null,
        ),
        props.actions ? h('div', { className: 'ca-card-actions' }, props.actions) : null,
      ),
      body,
    )
  }

  function Section(props) {
    return h(
      'section',
      { className: cx('ca-section', props.className) },
      props.title || props.actions
        ? h(
            'div',
            { className: 'ca-section-head' },
            props.title ? h('h3', { className: 'ca-section-title' }, props.title) : null,
            props.note ? h('span', { className: 'ca-section-note' }, props.note) : null,
            h('span', { className: 'ca-spacer' }),
            props.actions || null,
          )
        : null,
      props.children,
    )
  }

  function Toolbar(props) {
    return h('div', { className: cx('ca-toolbar', props.className) }, props.children)
  }

  function Table(props) {
    const columns = Array.isArray(props.columns) ? props.columns : []
    const rows = Array.isArray(props.rows) ? props.rows : []
    const dense = Boolean(props.dense)
    const head = h(
      'thead',
      null,
      h(
        'tr',
        null,
        columns.map((column, index) =>
          h(
            'th',
            {
              key: column.key !== undefined ? String(column.key) : `col-${index}`,
              style: column.width ? { width: column.width } : undefined,
              className: cx(
                column.align === 'right' && 'ca-table-cell-right',
                column.align === 'center' && 'ca-table-cell-center',
              ),
              scope: 'col',
            },
            column.label === undefined ? String(column.key ?? '') : column.label,
          ),
        ),
      ),
    )
    const body = h(
      'tbody',
      null,
      rows.length === 0
        ? h(
            'tr',
            { className: 'ca-table-empty' },
            h(
              'td',
              { colSpan: Math.max(1, columns.length) },
              props.empty === undefined || props.empty === null ? '—' : props.empty,
            ),
          )
        : rows.map((row, rowIndex) => {
            const key =
              typeof props.rowKey === 'function'
                ? props.rowKey(row, rowIndex)
                : row && row.id !== undefined
                  ? String(row.id)
                  : `row-${rowIndex}`
            const rowClass =
              typeof props.rowClassName === 'function' ? props.rowClassName(row, rowIndex) : props.rowClassName
            return h(
              'tr',
              {
                key,
                onClick: typeof props.onRowClick === 'function' ? () => props.onRowClick(row, rowIndex) : undefined,
                'aria-selected': props.selectedKey !== undefined && key === String(props.selectedKey) ? 'true' : undefined,
                className:
                  cx(
                    props.onRowClick && 'ca-table-row-clickable',
                    rowClass,
                    props.selectedKey !== undefined && key === String(props.selectedKey) && 'ca-table-row-selected',
                  ) || undefined,
              },
              columns.map((column, columnIndex) => {
                let content
                if (typeof column.render === 'function') content = column.render(row, rowIndex)
                else if (row && typeof row === 'object') content = row[column.key]
                else content = undefined
                return h(
                  'td',
                  {
                    key: column.key !== undefined ? String(column.key) : `cell-${columnIndex}`,
                    className: cx(
                      column.align === 'right' && 'ca-table-cell-right',
                      column.align === 'center' && 'ca-table-cell-center',
                    ),
                  },
                  content === undefined || content === null || content === '' ? '—' : content,
                )
              }),
            )
          }),
    )
    return h(
      'div',
      { className: cx('ca-table-wrap', props.className) },
      h(
        'table',
        { className: cx('ca-table', dense && 'ca-table-dense', props.onRowClick && 'ca-table-clickable') },
        props.caption ? h('caption', { className: 'ca-visually-hidden' }, props.caption) : null,
        head,
        body,
      ),
    )
  }

  function Tabs(props) {
    const items = Array.isArray(props.items) ? props.items : []
    const value = props.value
    const onKeyDown = (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
      const enabled = items.filter((item) => !item.disabled)
      if (enabled.length === 0) return
      const currentIndex = enabled.findIndex((item) => item.id === value)
      const delta = event.key === 'ArrowRight' ? 1 : -1
      const next = enabled[(currentIndex + delta + enabled.length) % enabled.length]
      if (next && typeof props.onChange === 'function') {
        event.preventDefault()
        props.onChange(next.id)
      }
    }
    const ids = items.map((item) => String(item.id))
    return h(
      'div',
      Object.assign(
        { className: cx('ca-tabs', props.className), role: 'tablist', onKeyDown },
        ariaLabelProps(props),
      ),
      items.map((item) => {
        const id = String(item.id)
        const active = item.id === value
        const tabId = `ca-tab-${id.replace(/[^A-Za-z0-9_-]/g, '_')}`
        return h(
          'button',
          {
            key: id,
            id: tabId,
            type: 'button',
            role: 'tab',
            className: cx('ca-tab', active && 'ca-tab-active'),
            'aria-selected': active ? 'true' : 'false',
            'aria-controls': props.panelId ? `${props.panelId}-${id}` : undefined,
            tabIndex: active ? 0 : -1,
            disabled: item.disabled ? true : undefined,
            title: item.title,
            onClick: () => {
              if (typeof props.onChange === 'function') props.onChange(item.id)
            },
          },
          item.label === undefined ? id : item.label,
          item.count !== undefined && item.count !== null
            ? h('span', { className: 'ca-tab-count' }, String(item.count))
            : null,
        )
      }),
      null,
    )
  }

  function Badge(props) {
    return h(
      'span',
      {
        className: cx('ca-badge', `ca-badge-${normalizeTone(props.tone)}`, props.className),
        title: props.title,
        style: props.style,
      },
      props.children,
    )
  }

  function Spinner(props) {
    const size = Number(props.size) > 0 ? Number(props.size) : 16
    const spinner = h('span', {
      className: 'ca-spinner',
      style: { width: size, height: size },
      role: props.label ? undefined : 'presentation',
      'aria-hidden': props.label ? undefined : true,
    })
    if (!props.label) return spinner
    return h('span', { className: 'ca-spinner-label', role: 'status' }, spinner, h('span', null, props.label))
  }

  function ErrorBox(props) {
    const error = props.error !== undefined ? props.error : props.message
    const code =
      error && typeof error === 'object' && typeof error.code === 'string' ? error.code : ''
    const message =
      error === undefined || error === null
        ? ''
        : typeof error === 'string'
          ? error
          : typeof error.message === 'string' && error.message
            ? error.message
            : String(error)
    const title = props.title || 'Error'
    return h(
      'div',
      { className: cx('ca-error', props.className), role: 'alert' },
      h(
        'div',
        { className: 'ca-error-head' },
        h(Icon, { name: 'warning', size: 14 }),
        h('span', null, title),
      ),
      message ? h('div', { className: 'ca-error-msg' }, message) : null,
      code ? h('div', { className: 'ca-error-code' }, code) : null,
      props.hint ? h('div', { className: 'ca-error-hint' }, props.hint) : null,
      typeof props.onRetry === 'function'
        ? h(
            'div',
            { className: 'ca-row' },
            h(Button, { variant: 'secondary', size: 'sm', onClick: props.onRetry }, props.retryLabel || 'Retry'),
          )
        : null,
    )
  }

  function EmptyState(props) {
    const body = props.body !== undefined ? props.body : props.message
    return h(
      'div',
      { className: cx('ca-empty', props.className) },
      props.icon ? h('div', { className: 'ca-empty-icon' }, props.icon) : null,
      props.title ? h('div', { className: 'ca-empty-title' }, props.title) : null,
      body ? h('div', { className: 'ca-empty-body' }, body) : null,
      props.action ? h('div', { className: 'ca-empty-action' }, props.action) : null,
    )
  }

  function CodeView(props) {
    const rawLines = Array.isArray(props.lines) ? props.lines : []
    const startLine = Number.isFinite(Number(props.startLine)) ? Number(props.startLine) : 1
    const highlight = Array.isArray(props.highlight) ? props.highlight : null
    const hlStart = highlight ? Number(highlight[0]) : NaN
    const hlEnd = highlight ? Number(highlight[1]) : NaN
    const head =
      props.title || props.file || props.actions
        ? h(
            'div',
            { className: 'ca-code-head' },
            props.title ? h('span', null, props.title) : null,
            props.file ? h('span', { className: 'ca-code-file ca-mono' }, props.file) : null,
            h('span', { className: 'ca-spacer' }),
            props.actions || null,
          )
        : null
    const body = h(
      'div',
      { className: 'ca-code-body' },
      rawLines.map((raw, index) => {
        const entry = typeof raw === 'string' ? { text: raw } : raw || {}
        const lineNumber = Number.isFinite(Number(entry.line)) ? Number(entry.line) : startLine + index
        const highlighted =
          Number.isFinite(hlStart) && Number.isFinite(hlEnd) && lineNumber >= hlStart && lineNumber <= hlEnd
        let content
        if (Array.isArray(entry.tokens)) {
          content = entry.tokens.map((token, tokenIndex) =>
            h(
              'span',
              {
                key: tokenIndex,
                className: token && token.cls ? `ca-tok-${token.cls}` : undefined,
              },
              token && token.text !== undefined ? String(token.text) : '',
            ),
          )
        } else {
          content = entry.text === undefined || entry.text === null ? '' : String(entry.text)
        }
        return h(
          'div',
          {
            key: lineNumber,
            className: cx('ca-code-line', highlighted && 'ca-code-line-hl'),
          },
          h('div', { className: 'ca-code-gutter' }, String(lineNumber)),
          h('div', { className: 'ca-code-text' }, content),
        )
      }),
    )
    return h(
      'div',
      Object.assign(
        { className: cx('ca-code', props.className) },
        ariaLabelProps(props.ariaLabel ? { ariaLabel: props.ariaLabel } : {}),
      ),
      head,
      h(
        'div',
        {
          className: 'ca-code-scroll',
          style: props.maxHeight ? { maxHeight: typeof props.maxHeight === 'number' ? `${props.maxHeight}px` : props.maxHeight } : undefined,
        },
        body,
      ),
    )
  }

  function Splitter(props) {
    const horizontal = props.orientation !== 'vertical'
    const min = Number.isFinite(Number(props.min)) ? Number(props.min) : 0
    const max = props.max === undefined || props.max === null || !Number.isFinite(Number(props.max)) ? Infinity : Number(props.max)
    const step = Number(props.step) > 0 ? Number(props.step) : 16
    const [dragging, setDragging] = React.useState(false)
    const dragState = React.useRef(null)

    React.useEffect(() => {
      if (!dragging) return undefined
      const onMove = (event) => {
        const state = dragState.current
        if (!state) return
        const current = horizontal ? event.clientX : event.clientY
        const delta = current - state.start
        if (delta === 0) return
        const next = clampNumber(state.startValue + delta, min, max, state.startValue)
        state.last = next
        if (typeof props.onChange === 'function') props.onChange(next)
      }
      const onUp = () => {
        dragState.current = null
        setDragging(false)
      }
      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp)
      window.addEventListener('pointercancel', onUp)
      return () => {
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerup', onUp)
        window.removeEventListener('pointercancel', onUp)
      }
    }, [dragging, horizontal, min, max, props])

    const onPointerDown = (event) => {
      if (event.button !== 0) return
      event.preventDefault()
      dragState.current = {
        start: horizontal ? event.clientX : event.clientY,
        startValue: Number.isFinite(Number(props.value)) ? Number(props.value) : 0,
      }
      setDragging(true)
    }
    const onKeyDown = (event) => {
      const decrease = horizontal ? 'ArrowLeft' : 'ArrowUp'
      const increase = horizontal ? 'ArrowRight' : 'ArrowDown'
      if (event.key !== decrease && event.key !== increase) return
      event.preventDefault()
      const current = Number.isFinite(Number(props.value)) ? Number(props.value) : 0
      const next = clampNumber(current + (event.key === decrease ? -step : step), min, max, current)
      if (typeof props.onChange === 'function') props.onChange(next)
    }
    return h(
      'div',
      {
        role: 'separator',
        tabIndex: 0,
        'aria-orientation': horizontal ? 'vertical' : 'horizontal',
        'aria-valuenow': Number.isFinite(Number(props.value)) ? Number(props.value) : undefined,
        'aria-valuemin': Number.isFinite(min) ? min : undefined,
        'aria-valuemax': Number.isFinite(max) ? max : undefined,
        'aria-label': props.label || props.ariaLabel || (horizontal ? 'Resize panel' : 'Resize source panel'),
        title: props.label || props.ariaLabel,
        className: cx('ca-splitter', horizontal ? 'ca-splitter-h' : 'ca-splitter-v', dragging && 'ca-splitter-active', props.className),
        onPointerDown,
        onKeyDown,
        onDoubleClick: () => {
          if (typeof props.onReset === 'function') props.onReset()
        },
      },
      null,
    )
  }

  function Field(props) {
    return h(
      'label',
      { className: cx('ca-field', props.className) },
      props.label
        ? h(
            'span',
            { className: 'ca-field-label' },
            props.label,
            props.required ? ' *' : null,
            props.optional ? h('span', { className: 'ca-faint' }, ' (optional)') : null,
          )
        : null,
      props.children,
      props.hint ? h('span', { className: 'ca-field-hint' }, props.hint) : null,
      props.error ? h('span', { className: 'ca-field-error' }, props.error) : null,
    )
  }

  function KV(props) {
    const rows = Array.isArray(props.rows) ? props.rows : []
    return h(
      'dl',
      { className: cx('ca-kv', props.className) },
      rows.map((row, index) =>
        h(
          React.Fragment,
          { key: row.key !== undefined ? String(row.key) : `kv-${index}` },
          h('dt', null, row.label === undefined ? '' : row.label),
          h('dd', null, row.value === undefined || row.value === null || row.value === '' ? '—' : row.value),
        ),
      ),
    )
  }

  function Pre(props) {
    return h(
      'pre',
      { className: cx('ca-pre', props.className), style: props.style },
      typeof props.children === 'string' || typeof props.children === 'number'
        ? String(props.children)
        : props.children,
    )
  }

  return {
    React,
    h,
    cx,
    injectCss,
    attachStyles,
    useStyles,
    Icon,
    Button,
    IconButton,
    Input,
    Select,
    TextArea,
    Card,
    Section,
    Toolbar,
    Table,
    Tabs,
    Badge,
    Spinner,
    ErrorBox,
    EmptyState,
    CodeView,
    Splitter,
    Field,
    KV,
    Pre,
    IconPaths: ICON_PATHS,
    BaseCss: BASE_CSS,
  }
}

export default createUI
