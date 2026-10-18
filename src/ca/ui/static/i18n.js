/* CA web UI localization — central dictionary + static/dynamic translation.
 *
 * Usage from app.js:
 *   const t = window.CA_I18N.t;
 *   t("tab.callers")                    -> "Callers" / "调用者"
 *   t("status.searching", { query })    -> fills {query}
 *
 * The module:
 *  - detects the language from ?lang=zh|en, then localStorage, then
 *    navigator.languages (Chinese browser -> zh, otherwise English);
 *  - translates the static shell through data-i18n / data-i18n-placeholder /
 *    data-i18n-title / data-i18n-aria-label attributes;
 *  - persists an explicit choice in localStorage and updates <html lang>;
 *  - dispatches "ca:languagechange" so app.js can re-render the current view;
 *  - keeps a MutationObserver so newly inserted data-i18n nodes are translated.
 *
 * Data is never translated: function names, file paths, code snippets,
 * signatures and raw field values stay exactly as indexed.
 */
"use strict";

(() => {
  const STORAGE_KEY = "ca.ui.lang";
  const DEFAULT_LANGUAGE = "en";
  const SUPPORTED_LANGUAGES = ["en", "zh"];

  const en = {
    // application shell
    "app.title": "CA source explorer",
    "app.subtitle": "real compile args → Clang AST → CFG / data-flow → SQLite",
    "lang.switchLabel": "Language",
    "search.label": "Search functions",
    "search.placeholder": "Search functions by name, qualified name or id substring…",
    "search.button": "Search",
    "search.kbdSearch": "search",
    "search.kbdClear": "clear",
    "nav.views": "Views",
    "nav.results": "Results",
    "nav.searchResults": "Search results",
    "nav.details": "Details",
    "button.stats": "Stats",
    "button.callbacks": "Callbacks",
    "footer.text": "CA web UI · read-only · loopback only",
    // connection / status
    "db.connecting": "connecting…",
    "db.unavailable": "database unavailable",
    "status.ok": "ok",
    "status.degraded": "degraded",
    "status.searching": "Searching for “{query}”…",
    "status.loadingFunction": "Loading {target}…",
    "status.loadingStats": "Loading statistics…",
    "status.loadingCallbacks": "Loading callback registrations…",
    "status.buildingGraph": "Building call graph…",
    // common words / counts
    "common.loading": "loading…",
    "common.singleDatabase": "single database",
    "common.singleDb": "single db",
    "common.federated": "federated",
    "common.federatedShards": "{count} federated shards",
    "common.shardCount": "{count} shards",
    "common.shardName": "shard {name}",
    "common.functionCount": "{count} functions",
    "common.callCount": "{count} calls",
    "common.expandNone": "—",
    // search results
    "search.noMatches": "No symbols match “{query}”. Try a shorter substring, e.g. “proc_”.",
    "search.startHint": "Search for a function to begin. Substring matches work too, e.g. proc_ or sys_.",
    // badges
    "badge.definition": "definition",
    "badge.declaration": "declaration",
    "badge.static": "static",
    "badge.inline": "inline",
    "badge.deepFacts": "deep facts",
    "badge.deepFactsPending": "deep facts pending",
    "badge.shard": "shard {name}",
    "badge.candidate": "candidate",
    "badge.direct": "direct",
    "badge.negated": "negated",
    "badge.implicit": "implicit",
    "badge.explicit": "explicit",
    "badge.declInit": "decl-init",
    // errors
    "error.unknown": "Unknown error",
    "error.unreachable": "Cannot reach the CA server: {message}",
    "error.unreachableServer": "Cannot reach the CA UI server: {message}",
    "error.dbUnavailable": "Database unavailable: {message}",
    "error.loadFunction": "Could not load this function",
    "error.loadFunctionHint": "Check the symbol name, or search for it above. The database must be a CA index built by `ca index`.",
    "error.loadStats": "Could not load statistics",
    "error.loadCallbacks": "Could not load callback registrations",
    "error.shardSkipped": "{count} shard(s) skipped.",
    // function detail
    "detail.subtitle": "Function detail",
    "detail.deepPendingTitle": "Deep facts pending.",
    "detail.deepPendingBody": "This index is base/L0 or the function has not been deep-analyzed, so branches, assignments, returns and resource operations may be empty. Run",
    "detail.deepPendingAction": "to compute them on demand.",
    "detail.sectionFunction": "Function",
    "detail.sectionFunctionNote": "location, signature and indexed facts",
    "field.file": "File",
    "field.range": "Range",
    "field.returnType": "Return type",
    "field.linkage": "Linkage",
    "field.storageClass": "Storage class",
    "field.declaredIn": "Declared in",
    "field.usr": "USR",
    "field.tuId": "TU id",
    "field.functionId": "Function id",
    "field.parentId": "Parent id",
    "field.parameters": "Parameters",
    "metric.branches": "Branches",
    "metric.assignments": "Assignments",
    "metric.returns": "Returns",
    "metric.resources": "Resources",
    "metric.findings": "Findings",
    "metric.callers": "Callers",
    "metric.callees": "Callees",
    // tabs
    "tab.overview": "Overview",
    "tab.callers": "Callers",
    "tab.callees": "Callees",
    "tab.graph": "Call graph",
    "tab.branches": "Branches",
    "tab.returns": "Returns",
    "tab.resources": "Resources",
    "tab.findings": "Findings",
    // sections / notes
    "section.assignments": "Assignments",
    "section.callGraph": "Call graph",
    "section.callGraphNote": "SVG · read-only traversal of merged calls + candidates",
    "section.callbacks": "Callbacks",
    "section.callbacksNote": "cross-TU registration facts",
    "section.sources": "Sources",
    "section.sourcesNote": "read-only SQLite files backing this UI",
    "section.tableRows": "Table rows",
    "section.tableRowsNote": "all CA tables",
    "section.shards": "Shards",
    "section.shardsNote": "per-database provenance",
    "note.assignmentsEmpty": "no assignment facts indexed",
    "note.assignmentsIndexed": "{count} indexed",
    "note.edgeCount": "{count} edges · direct calls and callback candidates",
    "note.branchCount": "{count} branch conditions",
    "note.returnCount": "{count} return facts",
    "note.resourceCount": "{count} acquire/release operations",
    "note.findingCount": "{count} data-flow findings",
    "note.rowsShown": "{count} rows shown",
    "note.callbackCandidates": "candidate links come from the cross-TU callback pass",
    // tables
    "table.function": "Function",
    "table.location": "Location",
    "table.callSite": "Call site",
    "table.kind": "Kind",
    "table.edge": "Edge",
    "table.condition": "Condition",
    "table.trueBlock": "True block",
    "table.falseBlock": "False block",
    "table.shard": "Shard",
    "table.expression": "Expression",
    "table.block": "Block",
    "table.op": "Op",
    "table.resource": "Resource",
    "table.api": "API",
    "table.target": "Target",
    "table.matcher": "Matcher",
    "table.severity": "Severity",
    "table.message": "Message",
    "table.lhs": "LHS",
    "table.rhs": "RHS",
    "table.type": "Type",
    "table.objectPath": "Object path",
    "table.module": "Module",
    "table.registrar": "Registrar",
    "table.registrationKind": "Registration kind",
    "table.table": "Table",
    "table.rows": "Rows",
    "table.size": "Size",
    "table.schema": "Schema",
    "table.functions": "Functions",
    "table.calls": "Calls",
    "table.callbacks": "Callbacks",
    "table.count": "Count",
    // empty states
    "empty.callers": "No callers indexed for this function. Candidate callback edges appear after `ca resolve-callbacks`.",
    "empty.callees": "No callees indexed for this function. Candidate callback edges appear after `ca resolve-callbacks`.",
    "empty.branches": "No branch facts indexed for this function (common in base/L0 databases).",
    "empty.returns": "No return facts indexed for this function (common in base/L0 databases).",
    "empty.resources": "No resource operations indexed for this function.",
    "empty.findings": "No findings reported for this function.",
    "empty.callbacks": "No callback registrations match. Run `ca resolve-callbacks` on the database to compute them.",
    "empty.callbackKinds": "No callback registrations in this database.",
    "empty.graph": "No call-graph nodes found for this symbol. The graph is built from indexed calls and cross-TU callback candidates; a base database may have no edges.",
    // call graph
    "graph.direction": "Direction",
    "graph.depth": "Depth",
    "graph.both": "both",
    "graph.forward": "callees (forward)",
    "graph.reverse": "callers (reverse)",
    "graph.rebuild": "Rebuild",
    "graph.nodeCount": "{count} nodes",
    "graph.edgeCount": "{count} edges",
    "graph.truncated": "truncated",
    "graph.cycleCount": "{count} cycles",
    "graph.legendDirect": "direct call",
    "graph.legendCandidate": "indirect callback candidate (purple dashed; possible target)",
    "graph.legendVirtual": "virtual candidate (teal dashed; possible target)",
    "graph.legendTargetSet": "dashed edges = the set of possible targets at this call site",
    "graph.legendCycle": "cycle",
    "graph.legendClick": "click a node to view its source on the right",
    "nav.sections": "Sections",
    "nav.backToPrev": "Back to previous function",
    "layout.reset": "Reset layout",
    "layout.toggleLeft": "Show or hide the sections sidebar",
    "layout.toggleRight": "Show or hide the node source panel",
    "layout.resizeSource": "Drag to resize the source area (double-click resets)",
    "layout.resizeLeft": "Drag to resize the sidebar (double-click resets)",
    "layout.resizeRight": "Drag to resize the node source panel (double-click resets)",
    "conn.connecting": "Connecting…",
    "conn.online": "Connected",
    "conn.reconnecting": "Reconnecting…",
    "conn.offline": "Disconnected",
    "conn.retry": "click to retry",
    "registry.variant": "Variant",
    "registry.aria": "Workspace variant",
    "registry.workspace": "Workspace",
    "registry.projects": "{count} projects",
    "registry.projectOne": "1 project",
    "registry.db": "db",
    "registry.noDatabase": "no database built",
    "registry.noDatabaseHint": "This variant has no database yet",
    "registry.switching": "Switching to {name}…",
    "registry.switched": "Switched to {name}",
    "registry.error": "Cannot switch variant: {message}",
    "registry.resume": "Recently used: {name}",
    "registry.notFound": "Variant not found",
    "sidebar.collapse": "Collapse sections",
    "sidebar.expand": "Expand sections",
    "source.nodeTitle": "Node source",
    "source.nodeHint": "Click a node in the graph to view that function's complete source here.",
    "source.nodeOutside": "Node is at {file}:{line}",
    "source.callSite": "Call site {file}:{line}",
    "source.callSiteOutside": "Call site is in {file}:{line}",
    "source.callerCallsCurrent": "This function calls the current function at {file}:{line}",
    "source.collapse": "Collapse source",
    "source.expand": "Expand source",
    "source.readonly": "read-only",
    "field.signature": "Signature",
    "cfg.title": "Control-flow graph",
    "cfg.loading": "Loading control-flow graph…",
    "cfg.error": "Could not load the control-flow graph",
    "cfg.emptyTitle": "No control-flow facts indexed.",
    "cfg.emptyBody": "Branches, basic blocks and CFG edges come from the deep CFG/data-flow pass; run `ca ensure-deep` (or index with deep analysis) to fill them.",
    "cfg.emptyReady": "deep facts ready",
    "cfg.zoomIn": "Zoom in",
    "cfg.zoomOut": "Zoom out",
    "cfg.zoomReset": "Reset zoom",
    "cfg.legendEntry": "entry",
    "cfg.legendExit": "exit",
    "cfg.legendTrue": "true branch",
    "cfg.legendFalse": "false branch",
    "cfg.legendBack": "back edge",
    "cfg.stats": "{blocks} blocks · {edges} edges · {branches} branches",
    "cfg.aria": "Control-flow graph for {name}",
    "cfg.truncated": "truncated",
    "source.title": "Source",
    "source.hint": "Click a node in the graph to view its source here.",
    "source.loading": "Loading source…",
    "source.error": "Could not load source",
    "source.openDetail": "Open function detail",
    "source.clear": "Close",
    "source.range": "Lines {start}–{end} of {total}",
    "source.functionRange": "function {start}–{end}",
    "source.context": "{count} context lines",
    "source.truncated": "truncated to {count} lines",
    "source.aria": "Function source",
    "graph.aria": "Call graph for {root}",
    // stats
    "stats.title": "Database statistics",
    "stats.subFederated": "federated · {count} shards",
    "stats.cardSize": "Database size",
    "stats.cardFunctions": "Functions",
    "stats.cardCalls": "Calls",
    "stats.cardBranches": "Branches",
    "stats.cardSchema": "Schema",
    "stats.cardCallbacks": "Callback registrations",
    "stats.indexedFunctions": "indexed functions",
    "stats.callSites": "call sites",
    "stats.branchConditions": "branch conditions",
    "stats.linkedCandidates": "{count} linked candidates",
    "stats.shardsSub": "{count} shards",
    // welcome
    "welcome.title": "Explore a CA index",
    "welcome.lede": "CA analyzes operating-system source code from its real build: exact compile arguments, Clang AST, CFG and data-flow facts stored in SQLite. This local page is a read-only view over that database.",
    "welcome.tipsTitle": "What you can inspect",
    "welcome.tip1Title": "Symbol search",
    "welcome.tip1Body": "exact names and substring matches across every translation unit.",
    "welcome.tip2Title": "Function detail",
    "welcome.tip2Body": "signature, source location, branches, assignments, return paths and resource operations.",
    "welcome.tip3Title": "Call graph",
    "welcome.tip3Body": "SVG traversal with direct edges and candidate target sets (dashed).",
    "welcome.tip4Title": "Callers / callees",
    "welcome.tip4Body": "merged direct calls and cross-TU callback candidates.",
    "welcome.tip5Title": "Stats / callbacks",
    "welcome.tip5Body": "table counts, shard provenance, and function-pointer registration facts.",
    "welcome.registrationFacts": "registration facts",
    // callbacks page
    "callbacks.title": "Callback registrations",
    "callbacks.subtitle": "Function-pointer registration facts extracted from the AST across translation units.",
    "callbacks.cardRegistrations": "Registrations",
    "callbacks.cardTargets": "Targets",
    "callbacks.cardPaths": "Object paths",
    "callbacks.cardLinked": "Linked candidates",
    "callbacks.functionPointerFacts": "function-pointer facts",
    "callbacks.distinctTargets": "distinct callback targets",
    "callbacks.pathsDesc": "globals, fields, table slots",
    "callbacks.linkedDesc": "resolved call_target edges",
    "callbacks.filterPath": "object path prefix, e.g. g_ops.",
    "callbacks.filterField": "field, e.g. read",
    "callbacks.filterModule": "module",
    "callbacks.apply": "Apply filters",
    "callbacks.clear": "Clear",
    "callbacks.ariaPath": "Object path prefix",
    "callbacks.ariaField": "Object field",
    "callbacks.ariaModule": "Module",
  };

  const zh = {
    // application shell
    "app.title": "CA 源码浏览器",
    "app.subtitle": "真实编译参数 → Clang AST → CFG / 数据流 → SQLite",
    "lang.switchLabel": "语言",
    "search.label": "搜索函数",
    "search.placeholder": "按函数名、qualified name 或 id 子串搜索…",
    "search.button": "搜索",
    "search.kbdSearch": "搜索",
    "search.kbdClear": "清空",
    "nav.views": "视图",
    "nav.results": "结果",
    "nav.searchResults": "搜索结果",
    "nav.details": "详情",
    "button.stats": "统计",
    "button.callbacks": "回调",
    "footer.text": "CA Web UI · 只读 · 仅回环地址",
    // connection / status
    "db.connecting": "连接中…",
    "db.unavailable": "数据库不可用",
    "status.ok": "正常",
    "status.degraded": "受限",
    "status.searching": "正在搜索“{query}”…",
    "status.loadingFunction": "正在加载 {target}…",
    "status.loadingStats": "正在加载统计…",
    "status.loadingCallbacks": "正在加载回调注册事实…",
    "status.buildingGraph": "正在构建调用图…",
    // common words / counts
    "common.loading": "加载中…",
    "common.singleDatabase": "单库",
    "common.singleDb": "单库",
    "common.federated": "联邦",
    "common.federatedShards": "{count} 个联邦分片",
    "common.shardCount": "{count} 个分片",
    "common.shardName": "分片 {name}",
    "common.functionCount": "{count} 个函数",
    "common.callCount": "{count} 次调用",
    "common.expandNone": "—",
    // search results
    "search.noMatches": "没有匹配“{query}”的符号。试试更短的子串，例如“proc_”。",
    "search.startHint": "输入函数名开始搜索；也支持子串匹配，例如 proc_ 或 sys_。",
    // badges
    "badge.definition": "定义",
    "badge.declaration": "声明",
    "badge.static": "静态",
    "badge.inline": "内联",
    "badge.deepFacts": "深析事实",
    "badge.deepFactsPending": "深析未生成",
    "badge.shard": "分片 {name}",
    "badge.candidate": "候选",
    "badge.direct": "直接",
    "badge.negated": "取反",
    "badge.implicit": "隐式",
    "badge.explicit": "显式",
    "badge.declInit": "声明初始化",
    // errors
    "error.unknown": "未知错误",
    "error.unreachable": "无法连接 CA 服务：{message}",
    "error.unreachableServer": "无法连接 CA Web UI 服务：{message}",
    "error.dbUnavailable": "数据库不可用：{message}",
    "error.loadFunction": "无法加载该函数",
    "error.loadFunctionHint": "请检查符号名，或在上方搜索。数据库必须是 `ca index` 生成的 CA 索引。",
    "error.loadStats": "无法加载统计",
    "error.loadCallbacks": "无法加载回调注册事实",
    "error.shardSkipped": "跳过了 {count} 个分片。",
    // function detail
    "detail.subtitle": "函数详情",
    "detail.deepPendingTitle": "深析事实尚未生成。",
    "detail.deepPendingBody": "此索引为 base/L0，或该函数尚未做深析，因此分支、赋值、返回与资源操作可能为空。运行",
    "detail.deepPendingAction": "即可按需计算。",
    "detail.sectionFunction": "函数",
    "detail.sectionFunctionNote": "位置、签名与已索引事实",
    "field.file": "文件",
    "field.range": "范围",
    "field.returnType": "返回类型",
    "field.linkage": "链接属性",
    "field.storageClass": "存储类",
    "field.declaredIn": "所在库",
    "field.usr": "USR",
    "field.tuId": "TU id",
    "field.functionId": "函数 id",
    "field.parentId": "父级 id",
    "field.parameters": "参数",
    "metric.branches": "分支",
    "metric.assignments": "赋值",
    "metric.returns": "返回",
    "metric.resources": "资源",
    "metric.findings": "发现",
    "metric.callers": "调用者",
    "metric.callees": "被调用者",
    // tabs
    "tab.overview": "概览",
    "tab.callers": "调用者",
    "tab.callees": "被调用者",
    "tab.graph": "调用图",
    "tab.branches": "分支",
    "tab.returns": "返回",
    "tab.resources": "资源",
    "tab.findings": "发现",
    // sections / notes
    "section.assignments": "赋值",
    "section.callGraph": "调用图",
    "section.callGraphNote": "SVG · 只读遍历合并后的调用与候选边",
    "section.callbacks": "回调",
    "section.callbacksNote": "跨 TU 注册事实",
    "section.sources": "数据源",
    "section.sourcesNote": "支撑本页面的只读 SQLite 文件",
    "section.tableRows": "表行数",
    "section.tableRowsNote": "全部 CA 表",
    "section.shards": "分片",
    "section.shardsNote": "每个数据库的来源",
    "note.assignmentsEmpty": "未索引赋值事实",
    "note.assignmentsIndexed": "已索引 {count} 条",
    "note.edgeCount": "{count} 条边 · 直接调用与回调候选",
    "note.branchCount": "{count} 个分支条件",
    "note.returnCount": "{count} 条返回事实",
    "note.resourceCount": "{count} 个获取/释放操作",
    "note.findingCount": "{count} 条数据流发现",
    "note.rowsShown": "显示 {count} 行",
    "note.callbackCandidates": "候选边来自跨 TU 回调处理",
    // tables
    "table.function": "函数",
    "table.location": "位置",
    "table.callSite": "调用点",
    "table.kind": "类型",
    "table.edge": "边",
    "table.condition": "条件",
    "table.trueBlock": "真分支块",
    "table.falseBlock": "假分支块",
    "table.shard": "分片",
    "table.expression": "表达式",
    "table.block": "基本块",
    "table.op": "操作",
    "table.resource": "资源",
    "table.api": "API",
    "table.target": "目标",
    "table.matcher": "匹配器",
    "table.severity": "严重级别",
    "table.message": "消息",
    "table.lhs": "左值",
    "table.rhs": "右值",
    "table.type": "类型",
    "table.objectPath": "对象路径",
    "table.module": "模块",
    "table.registrar": "注册函数",
    "table.registrationKind": "注册类型",
    "table.table": "表",
    "table.rows": "行数",
    "table.size": "大小",
    "table.schema": "Schema",
    "table.functions": "函数",
    "table.calls": "调用",
    "table.callbacks": "回调",
    "table.count": "数量",
    // empty states
    "empty.callers": "该函数没有已索引的调用者。运行 `ca resolve-callbacks` 后会出现候选回调边。",
    "empty.callees": "该函数没有已索引的被调用者。运行 `ca resolve-callbacks` 后会出现候选回调边。",
    "empty.branches": "该函数没有已索引的分支事实（base/L0 数据库常见）。",
    "empty.returns": "该函数没有已索引的返回事实（base/L0 数据库常见）。",
    "empty.resources": "该函数没有已索引的资源操作。",
    "empty.findings": "该函数没有报告发现。",
    "empty.callbacks": "没有匹配的回调注册事实。可在数据库上运行 `ca resolve-callbacks` 计算。",
    "empty.callbackKinds": "该数据库没有回调注册事实。",
    "empty.graph": "该符号没有调用图节点。调用图来自已索引调用与跨 TU 回调候选；base 数据库可能没有边。",
    // call graph
    "graph.direction": "方向",
    "graph.depth": "深度",
    "graph.both": "双向",
    "graph.forward": "被调用者（正向）",
    "graph.reverse": "调用者（反向）",
    "graph.rebuild": "重新构建",
    "graph.nodeCount": "{count} 个节点",
    "graph.edgeCount": "{count} 条边",
    "graph.truncated": "已截断",
    "graph.cycleCount": "{count} 个环",
    "graph.legendDirect": "直接调用",
    "graph.legendCandidate": "间接回调候选（紫色虚线；可能目标）",
    "graph.legendVirtual": "虚函数候选（青色虚线；可能目标）",
    "graph.legendTargetSet": "虚线 = 当前调用点的可能目标集合",
    "graph.legendCycle": "环",
    "graph.legendClick": "点击节点在右侧查看源码",
    "nav.sections": "内容",
    "nav.backToPrev": "返回上一个函数",
    "layout.reset": "重置布局",
    "layout.toggleLeft": "显示/隐藏左侧栏",
    "layout.toggleRight": "显示/隐藏右侧面板",
    "layout.resizeSource": "拖动调整源码区高度（双击恢复默认）",
    "layout.resizeLeft": "拖动调整左栏宽度（双击恢复默认）",
    "layout.resizeRight": "拖动调整右栏宽度（双击恢复默认）",
    "conn.connecting": "连接中…",
    "conn.online": "已连接",
    "conn.reconnecting": "重连中…",
    "conn.offline": "已断开",
    "conn.retry": "点击重试",
    "registry.variant": "变体",
    "registry.aria": "工作区变体",
    "registry.workspace": "工作区",
    "registry.projects": "{count} 个项目",
    "registry.projectOne": "1 个项目",
    "registry.db": "库",
    "registry.noDatabase": "未建库",
    "registry.noDatabaseHint": "该变体尚未建库",
    "registry.switching": "正在切换到 {name}…",
    "registry.switched": "已切换到 {name}",
    "registry.error": "无法切换变体：{message}",
    "registry.resume": "最近使用：{name}",
    "registry.notFound": "找不到该变体",
    "sidebar.collapse": "收起侧栏",
    "sidebar.expand": "展开侧栏",
    "source.nodeTitle": "节点源码",
    "source.nodeHint": "点击调用图节点，在此查看该函数的完整源码。",
    "source.nodeOutside": "节点位于 {file}:{line}",
    "source.callSite": "调用位置 {file}:{line}",
    "source.callSiteOutside": "调用位置在 {file}:{line}",
    "source.callerCallsCurrent": "本函数在此调用当前函数：{file}:{line}",
    "source.collapse": "收起源码",
    "source.expand": "展开源码",
    "source.readonly": "只读",
    "field.signature": "签名",
    "cfg.title": "控制流图",
    "cfg.loading": "正在加载控制流图…",
    "cfg.error": "无法加载控制流图",
    "cfg.emptyTitle": "没有已索引的控制流事实。",
    "cfg.emptyBody": "分支、基本块与 CFG 边由深析 CFG/数据流 pass 生成；运行 `ca ensure-deep`（或用深析重新索引）补齐。",
    "cfg.emptyReady": "深析已就绪",
    "cfg.zoomIn": "放大",
    "cfg.zoomOut": "缩小",
    "cfg.zoomReset": "重置缩放",
    "cfg.legendEntry": "入口",
    "cfg.legendExit": "出口",
    "cfg.legendTrue": "真分支",
    "cfg.legendFalse": "假分支",
    "cfg.legendBack": "回边",
    "cfg.stats": "{blocks} 个块 · {edges} 条边 · {branches} 个分支",
    "cfg.aria": "{name} 的控制流图",
    "cfg.truncated": "已截断",
    "source.title": "源码",
    "source.hint": "点击调用图中的节点，在此查看源码。",
    "source.loading": "正在加载源码…",
    "source.error": "无法加载源码",
    "source.openDetail": "打开函数详情",
    "source.clear": "关闭",
    "source.range": "第 {start}–{end} 行，共 {total} 行",
    "source.functionRange": "函数 {start}–{end}",
    "source.context": "上下文 {count} 行",
    "source.truncated": "已截断为 {count} 行",
    "source.aria": "函数源码",
    "graph.aria": "{root} 的调用图",
    // stats
    "stats.title": "数据库统计",
    "stats.subFederated": "联邦 · {count} 个分片",
    "stats.cardSize": "数据库大小",
    "stats.cardFunctions": "函数",
    "stats.cardCalls": "调用",
    "stats.cardBranches": "分支",
    "stats.cardSchema": "Schema",
    "stats.cardCallbacks": "回调注册事实",
    "stats.indexedFunctions": "已索引函数",
    "stats.callSites": "调用点",
    "stats.branchConditions": "分支条件",
    "stats.linkedCandidates": "{count} 条已关联候选边",
    "stats.shardsSub": "{count} 个分片",
    // welcome
    "welcome.title": "浏览 CA 索引",
    "welcome.lede": "CA 从真实构建分析操作系统源码：精确编译参数、Clang AST、CFG 与数据流事实都存入 SQLite。本页面是该数据库的本地只读视图。",
    "welcome.tipsTitle": "可以查看的内容",
    "welcome.tip1Title": "符号搜索",
    "welcome.tip1Body": "跨所有翻译单元精确匹配与子串匹配。",
    "welcome.tip2Title": "函数详情",
    "welcome.tip2Body": "签名、源码位置、分支、赋值、返回路径与资源操作。",
    "welcome.tip3Title": "调用图",
    "welcome.tip3Body": "SVG 遍历，直接边与候选目标集合（虚线）。",
    "welcome.tip4Title": "调用者 / 被调用者",
    "welcome.tip4Body": "合并直接调用与跨 TU 回调候选。",
    "welcome.tip5Title": "统计 / 回调",
    "welcome.tip5Body": "表计数、分片来源与函数指针注册事实。",
    "welcome.registrationFacts": "注册事实",
    // callbacks page
    "callbacks.title": "回调注册事实",
    "callbacks.subtitle": "跨翻译单元从 AST 提取的函数指针注册事实。",
    "callbacks.cardRegistrations": "注册事实",
    "callbacks.cardTargets": "目标",
    "callbacks.cardPaths": "对象路径",
    "callbacks.cardLinked": "已关联候选",
    "callbacks.functionPointerFacts": "函数指针事实",
    "callbacks.distinctTargets": "不同回调目标",
    "callbacks.pathsDesc": "全局变量、字段、表槽位",
    "callbacks.linkedDesc": "已解析的 call_target 边",
    "callbacks.filterPath": "对象路径前缀，例如 g_ops.",
    "callbacks.filterField": "字段，例如 read",
    "callbacks.filterModule": "模块",
    "callbacks.apply": "应用过滤",
    "callbacks.clear": "清除",
    "callbacks.ariaPath": "对象路径前缀",
    "callbacks.ariaField": "对象字段",
    "callbacks.ariaModule": "模块",
  };

  const DICTIONARY = { en, zh };

  function isSupported(language) {
    return SUPPORTED_LANGUAGES.includes(language);
  }

  function normalize(language) {
    const value = String(language || "")
      .trim()
      .toLowerCase();
    if (value.startsWith("zh")) return "zh";
    if (value.startsWith("en")) return "en";
    return "";
  }

  function detectLanguage() {
    try {
      const fromUrl = new URLSearchParams(window.location.search).get("lang");
      const normalized = normalize(fromUrl);
      if (normalized) return normalized;
    } catch (_error) {
      /* URLSearchParams is always available in supported browsers */
    }
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (isSupported(stored)) return stored;
    } catch (_error) {
      /* storage can be blocked (private mode, file://); fall through */
    }
    const candidates = Array.isArray(navigator.languages)
      ? navigator.languages
      : [];
    const languages = candidates.length
      ? candidates
      : [navigator.language || navigator.userLanguage || ""];
    for (const language of languages) {
      const normalized = normalize(language);
      if (normalized) return normalized;
    }
    return DEFAULT_LANGUAGE;
  }

  let currentLanguage = detectLanguage();

  function t(key, params) {
    const table = DICTIONARY[currentLanguage] || DICTIONARY[DEFAULT_LANGUAGE];
    let text = table[key];
    if (text === undefined) text = DICTIONARY[DEFAULT_LANGUAGE][key];
    if (text === undefined) return key;
    if (!params) return text;
    return text.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name) =>
      params[name] === undefined || params[name] === null
        ? match
        : String(params[name])
    );
  }

  function translateElement(element) {
    const textKey = element.getAttribute("data-i18n");
    if (textKey) element.textContent = t(textKey);
    const attributes = [
      ["placeholder", "data-i18n-placeholder"],
      ["title", "data-i18n-title"],
      ["aria-label", "data-i18n-aria-label"],
    ];
    for (const [attribute, dataAttribute] of attributes) {
      const key = element.getAttribute(dataAttribute);
      if (key) element.setAttribute(attribute, t(key));
    }
  }

  const TRANSLATABLE =
    "[data-i18n], [data-i18n-placeholder], [data-i18n-title], [data-i18n-aria-label]";

  function translateStatic(root) {
    const scope = root && root.querySelectorAll ? root : document;
    if (scope.matches && scope.matches(TRANSLATABLE)) translateElement(scope);
    scope.querySelectorAll(TRANSLATABLE).forEach(translateElement);
  }

  function updateSwitcher() {
    document.querySelectorAll("[data-lang]").forEach((button) => {
      const active = button.getAttribute("data-lang") === currentLanguage;
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setLanguage(language, options = {}) {
    const next = isSupported(language) ? language : DEFAULT_LANGUAGE;
    currentLanguage = next;
    document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
    document.title = t("app.title");
    translateStatic(document);
    updateSwitcher();
    if (options.persist !== false) {
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch (_error) {
        /* best effort: the switch still works for this page */
      }
    }
    if (!options.silent) {
      document.dispatchEvent(
        new CustomEvent("ca:languagechange", { detail: { language: next } })
      );
    }
  }

  window.CA_I18N = {
    t,
    setLanguage,
    getLanguage: () => currentLanguage,
    detectLanguage,
    languages: SUPPORTED_LANGUAGES,
  };

  function bindSwitcher() {
    document.querySelectorAll("[data-lang]").forEach((button) => {
      button.addEventListener("click", () => {
        setLanguage(button.getAttribute("data-lang"));
      });
    });
    setLanguage(currentLanguage, { persist: false, silent: true });
  }

  // Dynamic HTML produced by app.js is rendered through t() at render time;
  // the observer keeps any data-i18n nodes added later (or by future widgets)
  // in sync when the language changes.
  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType === 1) translateStatic(node);
      }
    }
  });

  function start() {
    bindSwitcher();
    try {
      observer.observe(document.body, { childList: true, subtree: true });
    } catch (_error) {
      /* observe() is best effort; rendering still works */
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
