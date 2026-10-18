/* Function detail view stylesheet.
 *
 * Injected once through `bag.ui.useStyles` (or a local <style> fallback) by the
 * function page. Every class is `ca-fn-` prefixed; colors come from DSH theme
 * tokens with `color-mix(currentColor …)` fallbacks so an unknown token name
 * still yields a readable surface in both light and dark themes.
 */

export const FUNCTION_CSS = `
.ca-fn-page {
  /* DSH theme tokens; the color-mix fallbacks keep unknown/missing tokens
     readable in both light and dark themes. */
  --ca-fn-border: var(--dsw-alias-border-l2, color-mix(in srgb, currentColor 16%, transparent));
  --ca-fn-border-strong: var(--dsw-alias-border-l3, color-mix(in srgb, currentColor 28%, transparent));
  --ca-fn-surface: var(--dsw-alias-bg-layer-1, color-mix(in srgb, currentColor 4%, transparent));
  --ca-fn-surface-soft: var(--dsw-alias-bg-layer-2, color-mix(in srgb, currentColor 7%, transparent));
  --ca-fn-muted: var(--dsw-alias-label-secondary, color-mix(in srgb, currentColor 72%, transparent));
  --ca-fn-faint: var(--dsw-alias-label-tertiary, color-mix(in srgb, currentColor 52%, transparent));
  --ca-fn-accent: var(--dsw-alias-brand-primary, var(--dsw-alias-state-business-primary, #4f7cff));
  --ca-fn-accent-soft: color-mix(in srgb, var(--ca-fn-accent) 14%, transparent);
  --ca-fn-ok: var(--dsw-alias-state-success-primary, #1f8a4c);
  --ca-fn-ok-soft: color-mix(in srgb, var(--ca-fn-ok) 14%, transparent);
  --ca-fn-warn: var(--dsw-alias-state-warn-primary, #b57900);
  --ca-fn-warn-soft: color-mix(in srgb, var(--ca-fn-warn) 16%, transparent);
  --ca-fn-danger: var(--dsw-alias-state-error-primary, #c53f3f);
  --ca-fn-danger-soft: color-mix(in srgb, var(--ca-fn-danger) 14%, transparent);
  --ca-fn-radius: var(--dsw-radius-md, 8px);
  --ca-fn-radius-sm: var(--dsw-radius-sm, 6px);
  --ca-fn-mono: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  --ca-fn-line: 18px;
  --ca-fn-syn-keyword: #9a3fa3;
  --ca-fn-syn-type: #1b6b8f;
  --ca-fn-syn-string: #a4442a;
  --ca-fn-syn-number: #1f7a4d;
  --ca-fn-syn-comment: #7a8699;
  --ca-fn-syn-preproc: #8a5a00;
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  min-height: 0;
  color: inherit;
  font: inherit;
}
.ca-fn-page, .ca-fn-page *, .ca-fn-page *::before, .ca-fn-page *::after {
  box-sizing: border-box;
}
.ca-fn-page :focus-visible {
  outline: 2px solid var(--ca-fn-accent);
  outline-offset: 1px;
}

/* ------------------------------------------------------------ top header */
.ca-fn-head {
  flex: 0 0 var(--ca-fn-top-h, 32vh);
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 8px 10px 6px;
  border: 1px solid var(--ca-fn-border);
  border-radius: var(--ca-fn-radius);
  background: var(--ca-fn-surface);
}
.ca-fn-head.is-collapsed,
.ca-fn-page.is-state .ca-fn-head {
  flex: 0 0 auto;
}
.ca-fn-head-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.ca-fn-head-main { min-width: 0; }
.ca-fn-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  margin: 0;
  font-family: var(--ca-fn-mono);
  font-size: 20px;
  font-weight: 650;
  letter-spacing: -0.2px;
  overflow-wrap: anywhere;
}
.ca-fn-sub {
  margin: 3px 0 0;
  color: var(--ca-fn-muted);
  font-family: var(--ca-fn-mono);
  font-size: 12.5px;
  overflow-wrap: anywhere;
}
.ca-fn-head-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.ca-fn-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

/* --------------------------------------------------- loading/error states */
.ca-fn-state {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 32px 20px;
  overflow: auto;
}
.ca-fn-state-inner {
  width: 100%;
  max-width: 680px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.ca-fn-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 40px 16px;
  color: var(--ca-fn-muted);
  font-size: 13px;
  text-align: center;
}

/* ----------------------------------------------------------- workspace */
.ca-fn-workspace {
  display: flex;
  align-items: stretch;
  flex: 1;
  min-height: 0;
  width: 100%;
}
.ca-fn-sidebar {
  flex: 0 0 var(--ca-fn-left-w, 22%);
  min-width: 0;
  min-height: 0;
  overflow: auto;
  padding: 8px 6px;
  border-right: 1px solid var(--ca-fn-border);
  scrollbar-width: thin;
}
.ca-fn-workspace.is-left-collapsed > .ca-fn-sidebar { display: none; }
.ca-fn-workspace.is-right-collapsed > .ca-fn-panel { display: none; }
.ca-fn-graph {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 8px 6px;
}
.ca-fn-graph > * {
  flex: 1;
  min-height: 0;
  min-width: 0;
}
.ca-fn-panel {
  flex: 0 0 var(--ca-fn-right-w, 26%);
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: auto;
  padding: 8px 6px;
  border-left: 1px solid var(--ca-fn-border);
  scrollbar-width: thin;
}

/* -------------------------------------------------------------- splitters */
.ca-fn-splitter {
  flex: none;
  position: relative;
  z-index: 6;
  background: var(--ca-fn-border);
  touch-action: none;
  border: 0;
  padding: 0;
}
.ca-fn-splitter:hover,
.ca-fn-splitter:focus-visible {
  background: var(--ca-fn-accent);
}
.ca-fn-splitter:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px var(--ca-fn-accent-soft);
}
.ca-fn-splitter-horizontal {
  flex: 0 0 6px;
  width: 100%;
  margin: 2px 0;
  cursor: row-resize;
}
.ca-fn-splitter-vertical {
  flex: 0 0 6px;
  cursor: col-resize;
}
.ca-fn-splitter.is-disabled {
  cursor: default;
  opacity: 0.4;
}
.ca-fn-splitter-grip { display: none; }

/* ------------------------------------------------------------- accordion */
.ca-fn-accordion {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ca-fn-accordion-item {
  border: 1px solid var(--ca-fn-border);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-surface);
  overflow: hidden;
}
.ca-fn-accordion-item.is-open { border-color: var(--ca-fn-accent); }
.ca-fn-accordion-header {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: 0;
  background: var(--ca-fn-surface-soft);
  color: inherit;
  padding: 8px 10px;
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}
.ca-fn-accordion-header:hover,
.ca-fn-accordion-item.is-open .ca-fn-accordion-header {
  background: var(--ca-fn-accent-soft);
  color: var(--ca-fn-accent);
}
.ca-fn-accordion-title {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.ca-fn-count {
  flex: none;
  padding: 0 6px;
  border-radius: 999px;
  border: 1px solid var(--ca-fn-border);
  background: var(--ca-fn-surface);
  color: var(--ca-fn-faint);
  font-family: var(--ca-fn-mono);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
}
.ca-fn-chevron {
  flex: none;
  color: var(--ca-fn-faint);
  font-size: 9px;
}
.ca-fn-accordion-body {
  max-height: 52vh;
  overflow: auto;
  padding: 8px;
  border-top: 1px solid var(--ca-fn-border);
  scrollbar-width: thin;
}

/* --------------------------------------------------------------- sections */
.ca-fn-section {
  border: 1px solid var(--ca-fn-border);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-surface);
  overflow: hidden;
}
.ca-fn-section + .ca-fn-section { margin-top: 10px; }
.ca-fn-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 12px;
  border-bottom: 1px solid var(--ca-fn-border);
  background: var(--ca-fn-surface-soft);
}
.ca-fn-section-head h3 {
  margin: 0;
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ca-fn-muted);
}
.ca-fn-section-body { padding: 10px 12px; }
.ca-fn-note { color: var(--ca-fn-faint); font-size: 11.5px; }
.ca-fn-signature {
  margin: 0 0 10px;
  padding: 8px 12px;
  border: 1px solid var(--ca-fn-border);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-surface-soft);
  font-family: var(--ca-fn-mono);
  font-size: 12.5px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.ca-fn-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 8px 18px;
  margin: 0;
}
.ca-fn-grid > div { min-width: 0; }
.ca-fn-grid dt {
  margin: 0;
  color: var(--ca-fn-faint);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.ca-fn-grid dd {
  margin: 1px 0 0;
  font-family: var(--ca-fn-mono);
  font-size: 12.5px;
  overflow-wrap: anywhere;
}
.ca-fn-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 8px;
  margin-top: 10px;
}
.ca-fn-metric {
  border: 1px solid var(--ca-fn-border);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-surface-soft);
  padding: 8px 10px;
}
.ca-fn-metric-label {
  color: var(--ca-fn-faint);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.ca-fn-metric-value {
  font-family: var(--ca-fn-mono);
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.ca-fn-callout {
  border: 1px solid var(--ca-fn-warn);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-warn-soft);
  padding: 10px 12px;
  margin-bottom: 10px;
  font-size: 12.5px;
  line-height: 1.5;
}
.ca-fn-callout code {
  font-family: var(--ca-fn-mono);
  background: var(--ca-fn-surface-soft);
  border: 1px solid var(--ca-fn-border);
  border-radius: 4px;
  padding: 1px 4px;
  overflow-wrap: anywhere;
}
.ca-fn-explain { margin-top: 12px; }

/* ------------------------------------------------------------ code viewer */
.ca-fn-source {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin-top: 6px;
  border: 1px solid var(--ca-fn-border);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-surface);
  overflow: hidden;
}
.ca-fn-source.is-collapsed .ca-fn-code { display: none; }
.ca-fn-source-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  padding: 7px 12px;
  border-bottom: 1px solid var(--ca-fn-border);
  background: var(--ca-fn-surface-soft);
}
.ca-fn-source-head-left {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.ca-fn-source-title {
  margin: 0;
  font-size: 12px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--ca-fn-muted);
}
.ca-fn-source-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.ca-fn-source-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 7px 12px 0;
  color: var(--ca-fn-muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.ca-fn-source-meta .ca-fn-mono { font-family: var(--ca-fn-mono); }
.ca-fn-source-note {
  padding: 6px 12px 8px;
  color: var(--ca-fn-faint);
  font-size: 11.5px;
  border-bottom: 1px solid var(--ca-fn-border);
}
.ca-fn-source-warning {
  margin: 8px 12px 0;
  padding: 6px 10px;
  border: 1px solid var(--ca-fn-warn);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-warn-soft);
  color: inherit;
  font-size: 11.5px;
}
.ca-fn-node-hint {
  padding: 1px 8px;
  border: 1px solid var(--ca-fn-accent);
  border-radius: 999px;
  background: var(--ca-fn-accent-soft);
  color: var(--ca-fn-accent);
  font-size: 11px;
}
.ca-fn-code {
  flex: 1;
  min-height: 80px;
  overflow: auto;
  padding: 4px 0;
  font-family: var(--ca-fn-mono);
  font-size: 12px;
  line-height: var(--ca-fn-line);
  scrollbar-width: thin;
}
.ca-fn-code-line {
  display: flex;
  gap: 10px;
  padding-right: 12px;
  white-space: pre;
  min-width: max-content;
}
.ca-fn-code-lineno {
  flex: none;
  width: 52px;
  text-align: right;
  padding-right: 6px;
  margin-right: 4px;
  border-right: 1px solid var(--ca-fn-border);
  color: var(--ca-fn-faint);
  user-select: none;
  font-variant-numeric: tabular-nums;
}
.ca-fn-code-text { color: inherit; }
.ca-fn-code-line.is-function { background: var(--ca-fn-accent-soft); }
.ca-fn-code-line.is-function .ca-fn-code-lineno {
  color: var(--ca-fn-accent);
  font-weight: 600;
}
.ca-fn-code-line.is-selected { background: var(--ca-fn-warn-soft); box-shadow: inset 3px 0 var(--ca-fn-warn); }
.ca-fn-code-line.is-selected .ca-fn-code-lineno {
  color: var(--ca-fn-warn);
  font-weight: 700;
}
.ca-fn-tok-keyword { color: var(--ca-fn-syn-keyword); font-weight: 600; }
.ca-fn-tok-type { color: var(--ca-fn-syn-type); }
.ca-fn-tok-string { color: var(--ca-fn-syn-string); }
.ca-fn-tok-number { color: var(--ca-fn-syn-number); }
.ca-fn-tok-comment { color: var(--ca-fn-syn-comment); font-style: italic; }
.ca-fn-tok-preproc { color: var(--ca-fn-syn-preproc); }

/* -------------------------------------------------- node source panel */
.ca-fn-node-empty {
  padding: 14px 12px;
  color: var(--ca-fn-faint);
  font-size: 12.5px;
  line-height: 1.5;
}
.ca-fn-node-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 12px;
}
.ca-fn-panel .ca-fn-source { margin-top: 0; }

/* -------------------------------------------------------------- tables */
.ca-fn-table-wrap {
  overflow: auto;
  max-height: 320px;
  border: 1px solid var(--ca-fn-border);
  border-radius: var(--ca-fn-radius-sm);
  scrollbar-width: thin;
}
.ca-fn-table {
  border-collapse: collapse;
  width: 100%;
  font-size: 12px;
}
.ca-fn-table th,
.ca-fn-table td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--ca-fn-border);
  text-align: left;
  vertical-align: top;
}
.ca-fn-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--ca-fn-surface-soft);
  color: var(--ca-fn-faint);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
}
.ca-fn-table tbody tr:last-child td { border-bottom: 0; }
.ca-fn-table .is-right { text-align: right; }
.ca-fn-table .is-center { text-align: center; }
.ca-fn-table-empty,
.ca-fn-table td.ca-fn-table-empty {
  color: var(--ca-fn-faint);
  padding: 12px;
}
.ca-fn-mono { font-family: var(--ca-fn-mono); }
.ca-fn-muted { color: var(--ca-fn-muted); }
.ca-fn-faint { color: var(--ca-fn-faint); }

/* ------------------------------------------------- fallback primitives */
.ca-fn-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 5px 11px;
  border: 1px solid var(--ca-fn-border-strong);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-surface-soft);
  color: inherit;
  font: inherit;
  font-size: 12px;
  font-weight: 550;
  line-height: 1.4;
  cursor: pointer;
  white-space: nowrap;
}
.ca-fn-btn:hover { border-color: var(--ca-fn-accent); color: var(--ca-fn-accent); }
.ca-fn-btn:disabled { opacity: 0.5; cursor: default; }
.ca-fn-btn-sm { padding: 3px 8px; font-size: 11.5px; }
.ca-fn-btn-ghost { background: transparent; }
.ca-fn-btn-primary {
  background: var(--ca-fn-accent);
  border-color: var(--ca-fn-accent);
  color: #fff;
}
.ca-fn-btn-danger {
  background: var(--ca-fn-danger);
  border-color: var(--ca-fn-danger);
  color: #fff;
}
.ca-fn-btn-icon { padding: 3px 8px; }
.ca-fn-btn.is-active { background: var(--ca-fn-accent-soft); border-color: var(--ca-fn-accent); color: var(--ca-fn-accent); }
.ca-fn-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 8px;
  border: 1px solid var(--ca-fn-border-strong);
  border-radius: 999px;
  background: var(--ca-fn-surface-soft);
  color: var(--ca-fn-muted);
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}
.ca-fn-badge-info { border-color: var(--ca-fn-accent); background: var(--ca-fn-accent-soft); color: var(--ca-fn-accent); }
.ca-fn-badge-brand { border-color: var(--ca-fn-accent); background: var(--ca-fn-accent-soft); color: var(--ca-fn-accent); font-family: var(--ca-fn-mono); }
.ca-fn-badge-success { border-color: var(--ca-fn-ok); background: var(--ca-fn-ok-soft); color: var(--ca-fn-ok); }
.ca-fn-badge-warn { border-color: var(--ca-fn-warn); background: var(--ca-fn-warn-soft); color: var(--ca-fn-warn); }
.ca-fn-badge-error { border-color: var(--ca-fn-danger); background: var(--ca-fn-danger-soft); color: var(--ca-fn-danger); }
.ca-fn-badge-muted { color: var(--ca-fn-muted); }
.ca-fn-spinner { display: inline-flex; align-items: center; justify-content: center; }
.ca-fn-spinner-ring {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--ca-fn-border-strong);
  border-top-color: var(--ca-fn-accent);
  animation: ca-fn-spin 0.8s linear infinite;
}
@keyframes ca-fn-spin { to { transform: rotate(360deg); } }
.ca-fn-error-box {
  border: 1px solid var(--ca-fn-danger);
  border-radius: var(--ca-fn-radius-sm);
  background: var(--ca-fn-danger-soft);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ca-fn-error-title { margin: 0; color: var(--ca-fn-danger); font-weight: 700; font-size: 13px; }
.ca-fn-error-message { margin: 0; font-size: 12.5px; overflow-wrap: anywhere; }
.ca-fn-error-code { margin: 0; font-family: var(--ca-fn-mono); font-size: 11px; color: var(--ca-fn-faint); }
.ca-fn-error-hint { margin: 0; color: var(--ca-fn-muted); font-size: 12px; line-height: 1.5; }
.ca-fn-empty-state {
  border: 1px dashed var(--ca-fn-border-strong);
  border-radius: var(--ca-fn-radius-sm);
  padding: 18px 16px;
  text-align: center;
  color: var(--ca-fn-muted);
}
.ca-fn-empty-title { margin: 0 0 4px; font-weight: 650; font-size: 13.5px; color: inherit; }
.ca-fn-empty-body { margin: 0; font-size: 12.5px; line-height: 1.55; }

/* ------------------------------------------------------------ responsive */
@media (max-width: 1099px) {
  .ca-fn-head { flex: 0 0 auto; max-height: 45vh; }
  .ca-fn-workspace {
    flex-direction: column;
    overflow: auto;
  }
  .ca-fn-sidebar {
    flex: 0 0 auto;
    width: 100%;
    max-height: none;
    border-right: 0;
    border-bottom: 1px solid var(--ca-fn-border);
  }
  .ca-fn-graph {
    flex: 0 0 auto;
    height: 62vh;
    min-height: 360px;
    border-bottom: 1px solid var(--ca-fn-border);
  }
  .ca-fn-panel {
    flex: 0 0 auto;
    width: 100%;
    max-height: 72vh;
    border-left: 0;
  }
  .ca-fn-splitter { display: none; }
  .ca-fn-accordion-body { max-height: none; }
}

/* --------------------------------------------- dark-theme syntax palette */
@media (prefers-color-scheme: dark) {
  .ca-fn-page {
    --ca-fn-syn-keyword: #e0a6f0;
    --ca-fn-syn-type: #7fd2f2;
    --ca-fn-syn-string: #f0a48c;
    --ca-fn-syn-number: #8fe0b0;
    --ca-fn-syn-comment: #7d8a9c;
    --ca-fn-syn-preproc: #f0c46b;
  }
}
.dark .ca-fn-page,
[data-theme="dark"] .ca-fn-page,
[data-color-scheme="dark"] .ca-fn-page {
  --ca-fn-syn-keyword: #e0a6f0;
  --ca-fn-syn-type: #7fd2f2;
  --ca-fn-syn-string: #f0a48c;
  --ca-fn-syn-number: #8fe0b0;
  --ca-fn-syn-comment: #7d8a9c;
  --ca-fn-syn-preproc: #f0c46b;
}
`;

export default FUNCTION_CSS;
