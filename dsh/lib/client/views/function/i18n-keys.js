/* Function detail view — English defaults for every translation key it uses.
 *
 * `createT(i18n)` prefers the shell dictionary (`bag.i18n.t(key, vars,
 * fallback)`), then the English default in this table, then the key itself.
 * The zh dictionary is owned by ui-shell (locale NS `ca`); the key list here is
 * the contract for it. Keys use the `fn.*` / `layout.*` namespaces.
 */

export const DEFAULTS = Object.freeze({
  // page shell / header
  "fn.pageAria": "Function detail",
  "fn.loading": "Loading {target}…",
  "fn.loadingHint": "Fetching function facts and source…",
  "fn.back": "Back to previous function",
  "fn.history.empty": "No previous function",

  // header badges
  "fn.badge.definition": "definition",
  "fn.badge.declaration": "declaration",
  "fn.badge.static": "static",
  "fn.badge.inline": "inline",
  "fn.badge.deepFacts": "deep facts",
  "fn.badge.deepFactsPending": "deep facts pending",
  "fn.badge.shard": "shard {name}",
  "fn.badge.declInit": "decl-init",
  "fn.badge.implicit": "implicit",
  "fn.badge.explicit": "explicit",

  // overview fields / metrics
  "fn.common.none": "—",
  "fn.common.singleDatabase": "single database",
  "fn.common.shardName": "shard {name}",
  "fn.field.file": "File",
  "fn.field.range": "Range",
  "fn.field.returnType": "Return type",
  "fn.field.signature": "Signature",
  "fn.field.linkage": "Linkage",
  "fn.field.storageClass": "Storage class",
  "fn.field.declaredIn": "Declared in",
  "fn.field.usr": "USR",
  "fn.field.tuId": "TU id",
  "fn.field.functionId": "Function id",
  "fn.field.parentId": "Parent id",
  "fn.field.parameters": "Parameters",
  "fn.field.deepReady": "Deep analysis",
  "fn.field.deepReadyYes": "deep facts ready",
  "fn.field.deepReadyNo": "not deep-analyzed",
  "fn.metric.branches": "Branches",
  "fn.metric.assignments": "Assignments",
  "fn.metric.returns": "Returns",
  "fn.metric.resources": "Resources",
  "fn.metric.findings": "Findings",

  // deep facts callout
  "fn.deep.pendingTitle": "Deep facts pending.",
  "fn.deep.pendingBody":
    "This index is base/L0 or the function has not been deep-analyzed, so branches, assignments, returns and resource operations may be empty. Run",
  "fn.deep.pendingAction": "to compute them on demand.",

  // accordion sections
  "fn.tab.overview": "Overview",
  "fn.tab.branches": "Branches",
  "fn.tab.returns": "Returns",
  "fn.tab.resources": "Resources",
  "fn.tab.findings": "Findings",
  "fn.section.function": "Function",
  "fn.section.functionNote": "location, signature and indexed facts",
  "fn.section.assignments": "Assignments",
  "fn.note.assignmentsEmpty": "no assignment facts indexed",
  "fn.note.assignmentsIndexed": "{count} indexed",
  "fn.note.returnCount": "{count} return facts",
  "fn.note.resourceCount": "{count} acquire/release operations",
  "fn.note.findingCount": "{count} data-flow findings",
  "fn.note.sectionCount": "{count} rows",

  // fact tables
  "fn.table.lhs": "LHS",
  "fn.table.op": "Op",
  "fn.table.rhs": "RHS",
  "fn.table.type": "Type",
  "fn.table.kind": "Kind",
  "fn.table.location": "Location",
  "fn.table.shard": "Shard",
  "fn.table.expression": "Expression",
  "fn.table.block": "Block",
  "fn.table.resource": "Resource",
  "fn.table.api": "API",
  "fn.table.target": "Target",
  "fn.table.matcher": "Matcher",
  "fn.table.severity": "Severity",
  "fn.table.message": "Message",
  "fn.empty.assignments": "No assignment facts indexed for this function.",
  "fn.empty.returns":
    "No return facts indexed for this function (common in base/L0 databases).",
  "fn.empty.resources": "No resource operations indexed for this function.",
  "fn.empty.findings": "No findings reported for this function.",
  "fn.empty.branches":
    "No branch/control-flow facts indexed for this function (common in base/L0 databases).",

  // errors and empty states
  "fn.error.title": "Could not load this function",
  "fn.error.hint":
    "Check the symbol name or search above. The database must be a CA index built by `ca index`.",
  "fn.error.notFoundTitle": "Function not found",
  "fn.error.notFoundBody":
    "No function matching “{symbol}” in the active CA database.",
  "fn.error.dbUnavailableTitle": "Database unavailable",
  "fn.error.dbUnavailableBody":
    "The CA backend has no readable database. Start or configure the backend, then retry.",
  "fn.error.unknown": "Unknown error",
  "fn.error.retry": "Retry",
  "fn.error.sourceTitle": "Could not load source",
  "fn.error.nodeTitle": "Could not load node source",

  // source viewer
  "fn.source.title": "Source",
  "fn.source.readonly": "read-only",
  "fn.source.aria": "Function source",
  "fn.source.collapse": "Collapse source",
  "fn.source.expand": "Expand source",
  "fn.source.loading": "Loading source…",
  "fn.source.error": "Could not load source",
  "fn.source.range": "Lines {start}–{end} of {total}",
  "fn.source.functionRange": "function {start}–{end}",
  "fn.source.truncated": "truncated to {count} lines",
  "fn.source.truncatedNote":
    "The source window is capped at 400 lines around the function.",
  "fn.source.nodeOutside": "Node is at {file}:{line}",
  "fn.source.callSite": "Call site {file}:{line}",
  "fn.source.callSiteOutside": "Call site is in {file}:{line}",
  "fn.source.callerCallsCurrent":
    "This function calls the current function at {file}:{line}",
  "fn.source.fileAt": "{file}:{line}",

  // right-hand graph-node source panel
  "fn.node.title": "Node source",
  "fn.node.readonly": "read-only",
  "fn.node.hint":
    "Click a node in the graph to view that function's complete source here.",
  "fn.node.loading": "Loading source…",
  "fn.node.aria": "Selected graph-node source",
  "fn.node.openDetail": "Open function detail",
  "fn.node.close": "Close",
  "fn.node.selected": "Selected node {symbol}",

  // layout
  "layout.reset": "Reset layout",
  "layout.toggleLeft": "Show or hide the sections sidebar",
  "layout.toggleRight": "Show or hide the node source panel",
  "layout.resizeSource": "Drag to resize the source area (double-click resets)",
  "layout.resizeLeft": "Drag to resize the sidebar (double-click resets)",
  "layout.resizeRight":
    "Drag to resize the node source panel (double-click resets)",
  "layout.aria.sections": "Sections",
  "layout.aria.details": "Details",
  "layout.aria.graph": "Call graph",
  "layout.sidebarCollapsed": "Sections sidebar hidden",
  "layout.panelCollapsed": "Node source panel hidden",

  // optional AI explanation mount point
  "fn.explain.title": "AI explanation",
  "fn.explain.fallback": "AI explanation is not available in this build.",
});

/**
 * Interpolate `{name}` placeholders (same grammar as the CA UI dictionary).
 */
export function formatMessage(template, vars) {
  if (!vars) return String(template);
  return String(template).replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name) =>
    vars[name] === undefined || vars[name] === null ? match : String(vars[name])
  );
}

/**
 * Create the translation function used by the function view.
 *
 * Preference order: shell dictionary → English default → key. The shell is
 * expected to expose `t(key, vars?, fallback?)`; if it only exposes
 * `t(key, vars?)` the English default is still produced locally.
 */
export function createT(i18n) {
  const dict = i18n || {};
  return function t(key, vars) {
    const fallback = Object.prototype.hasOwnProperty.call(DEFAULTS, key)
      ? DEFAULTS[key]
      : key;
    if (typeof dict.t === "function") {
      try {
        const translated = dict.t(key, vars || null, fallback);
        if (translated !== undefined && translated !== null && translated !== "") {
          const text = String(translated);
          // The shell interpolates `{name}` itself; interpolate again only when
          // placeholders survived (e.g. a minimal i18n implementation or a
          // fallback that the dictionary returned verbatim).
          if (vars && /\{[a-zA-Z0-9_]+\}/.test(text)) {
            return formatMessage(text, vars);
          }
          return text;
        }
      } catch (_error) {
        /* a broken dictionary must never break the page */
      }
    }
    return formatMessage(fallback, vars);
  };
}
