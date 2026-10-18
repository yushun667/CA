/* CA web UI — vanilla JS, no framework, no network beyond this server. */
"use strict";

(() => {
  // ------------------------------------------------------------------ state
  const SIDEBAR_KEY = "ca.ui.sidebarCollapsed";

  function readStoredFlag(key) {
    try {
      return window.localStorage.getItem(key) === "1";
    } catch (_error) {
      return false;
    }
  }

  function storeFlag(key, value) {
    try {
      window.localStorage.setItem(key, value ? "1" : "0");
    } catch (_error) {
      /* storage can be blocked; the toggle still works for this page */
    }
  }

  const LAYOUT_KEY = "ca.ui.layout";
  const LAYOUT_DEFAULTS = {
    top: null,
    left: null,
    right: null,
    leftCollapsed: false,
    rightCollapsed: false,
  };
  const LAYOUT_MIN = { left: 220, middle: 420, right: 280, top: 140 };

  function defaultLayout() {
    return { ...LAYOUT_DEFAULTS };
  }

  function readStoredLayout() {
    try {
      const raw = window.localStorage.getItem(LAYOUT_KEY);
      if (!raw) return defaultLayout();
      const parsed = JSON.parse(raw);
      const layout = defaultLayout();
      if (parsed && typeof parsed === "object") {
        for (const key of ["top", "left", "right"]) {
          const value = Number(parsed[key]);
          layout[key] =
            Number.isFinite(value) && value > 0 ? Math.round(value) : null;
        }
        layout.leftCollapsed = Boolean(parsed.leftCollapsed);
        layout.rightCollapsed = Boolean(parsed.rightCollapsed);
      }
      return layout;
    } catch (_error) {
      return defaultLayout();
    }
  }

  function saveStoredLayout() {
    try {
      window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(state.layout));
    } catch (_error) {
      /* storage can be blocked; dragging still works for this page */
    }
  }

  const HISTORY_KEY = "ca.ui.fnHistory";
  const HISTORY_LIMIT = 50;

  function readStoredHistory() {
    try {
      const raw = window.sessionStorage.getItem(HISTORY_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter(
          (entry) =>
            entry && typeof entry.symbol === "string" && entry.symbol.trim()
        )
        .map((entry) => ({
          symbol: String(entry.symbol).trim(),
          tab: typeof entry.tab === "string" ? entry.tab : "",
        }))
        .slice(-HISTORY_LIMIT);
    } catch (_error) {
      return [];
    }
  }

  function saveStoredHistory() {
    try {
      window.sessionStorage.setItem(
        HISTORY_KEY,
        JSON.stringify(functionHistory)
      );
    } catch (_error) {
      /* storage can be blocked; the stack still works for this page */
    }
  }

  let functionHistory = readStoredHistory();

  function pushFunctionHistory(entry) {
    const symbol = String((entry && entry.symbol) || "").trim();
    if (!symbol) return;
    const tab = entry && entry.tab ? normalizeTab(entry.tab) : "";
    const top = functionHistory[functionHistory.length - 1];
    if (top && top.symbol === symbol) {
      top.tab = tab;
      saveStoredHistory();
      return;
    }
    functionHistory.push({ symbol, tab });
    if (functionHistory.length > HISTORY_LIMIT) {
      functionHistory = functionHistory.slice(-HISTORY_LIMIT);
    }
    saveStoredHistory();
  }

  function popFunctionHistory() {
    if (!functionHistory.length) return null;
    const entry = functionHistory.pop();
    saveStoredHistory();
    return entry;
  }

  function clearFunctionHistory() {
    functionHistory = [];
    saveStoredHistory();
  }

  const state = {
    health: null,
    results: [],
    resultsQuery: "",
    selectedSymbol: "",
    detail: null,
    callers: [],
    callees: [],
    graph: null,
    graphParams: { direction: "both", depth: 1 },
    tab: "overview",
    route: "welcome",
    stats: null,
    callbacks: null,
    callbacksFilters: { path: "", field: "", module: "" },
    loadingSymbol: "",
    // Fixed source of the opened function (does not follow graph clicks).
    topSource: null,
    topSourceCollapsed: false,
    // Independent source of the graph node selected in the right-hand pane.
    nodeSource: null,
    cfg: null,
    cfgLoading: false,
    cfgError: "",
    cfgZoom: 1,
    explain: null,
    explainLoading: false,
    explainError: "",
    sidebarCollapsed: readStoredFlag(SIDEBAR_KEY),
    layout: readStoredLayout(),
  };

  const els = {};

  // Localized UI text; CA_I18N is provided by i18n.js, loaded before app.js.
  const t = (key, params) =>
    window.CA_I18N ? window.CA_I18N.t(key, params) : key;

  // -------------------------------------------------------------- utilities
  const escapeHtml = (value) =>
    String(value === null || value === undefined ? "" : value).replace(
      /[&<>"']/g,
      (ch) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );

  const number = (value) => {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "0";
    }
    return Number(value).toLocaleString("en-US");
  };

  const bytes = (value) => {
    const size = Number(value || 0);
    if (size < 1024) return `${size} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let scaled = size / 1024;
    let index = 0;
    while (scaled >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    return `${scaled.toFixed(scaled >= 10 ? 0 : 1)} ${units[index]}`;
  };

  const confidence = (value) => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric.toFixed(2) : "—";
  };

  const truncate = (value, max) => {
    const text = String(value === null || value === undefined ? "" : value);
    if (text.length <= max) return text;
    return `${text.slice(0, Math.max(1, max - 1))}…`;
  };

  const location = (row, prefix = "") => {
    if (!row) return "";
    const file = row[`${prefix}file`] || "";
    const line = row[`${prefix}line`] || 0;
    return file ? `${file}:${line}` : "";
  };

  const badge = (text, kind = "") =>
    `<span class="badge${kind ? ` ${kind}` : ""}">${escapeHtml(text)}</span>`;

  const shardBadge = (row) =>
    row && row.shard
      ? badge(t("badge.shard", { name: row.shard }), "shard")
      : "";

  const isCandidate = (row) =>
    Boolean(
      row &&
        (row.candidate === true ||
          row.source === "candidate" ||
          row.source === "call_target")
    );

  const edgeBadge = (row) =>
    isCandidate(row)
      ? badge(t("badge.candidate"), "candidate")
      : badge(t("badge.direct"), "direct");

  const functionLink = (name, id) => {
    const label = name || id;
    if (!label) return '<span class="muted">—</span>';
    const target = id || name;
    return `<button type="button" class="link mono" data-open="${escapeHtml(
      target
    )}">${escapeHtml(label)}</button>`;
  };

  const emptyRow = (columns, message) =>
    `<tr><td colspan="${columns}" class="faint">${escapeHtml(message)}</td></tr>`;

  function table(headers, rows) {
    return `<div class="table-wrap"><table class="data-table"><thead><tr>${headers
      .map((header) => `<th>${escapeHtml(header)}</th>`)
      .join("")}</tr></thead><tbody>${rows.join("") || ""}</tbody></table></div>`;
  }

  function card(label, value, sub = "") {
    return `<div class="card"><span class="label">${escapeHtml(
      label
    )}</span><span class="value">${escapeHtml(value)}</span>${
      sub ? `<span class="sub">${escapeHtml(sub)}</span>` : ""
    }</div>`;
  }

  function metric(label, value, note = "") {
    return `<div class="metric"><div class="metric-label">${escapeHtml(
      label
    )}</div><div class="metric-value">${escapeHtml(value)}</div>${
      note ? `<div class="metric-note">${escapeHtml(note)}</div>` : ""
    }</div>`;
  }

  function setStatus(message, kind = "info") {
    if (!message) {
      els.status.className = "status";
      els.status.textContent = "";
      return;
    }
    els.status.className = `status ${kind}`;
    els.status.innerHTML = `<span>${escapeHtml(message)}</span>`;
  }

  function clearStatus() {
    setStatus("");
  }

  function friendlyError(error) {
    if (!error) return t("error.unknown");
    return error.message || String(error);
  }

  function errorPanel(title, error, hint = "") {
    return `<div class="section"><div class="section-head"><h3>${escapeHtml(
      title
    )}</h3></div><div class="section-body"><p class="empty">${escapeHtml(
      friendlyError(error)
    )}</p>${hint ? `<p class="empty">${escapeHtml(hint)}</p>` : ""}</div></div>`;
  }

  function renderLoading(message) {
    els.view.innerHTML = `<div class="view-loading">${escapeHtml(message)}</div>`;
  }

  // ------------------------------------------------------------------- api
  async function api(path) {
    let response;
    try {
      response = await fetch(path, { headers: { Accept: "application/json" } });
    } catch (error) {
      throw new Error(t("error.unreachable", { message: error.message }));
    }
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    if (!payload || payload.ok !== true) {
      const detail =
        payload && payload.error
          ? `${payload.error.code}: ${payload.error.message}`
          : `HTTP ${response.status}`;
      const wrapped = new Error(detail);
      wrapped.status = response.status;
      throw wrapped;
    }
    return payload.data;
  }

  // ---------------------------------------------------------------- routing
  function parseHash() {
    const raw = (window.location.hash || "").replace(/^#\/?/, "");
    if (!raw) return { name: "welcome" };
    const parts = raw.split("/");
    if (parts[0] === "fn" && parts[1]) {
      let symbol = parts[1];
      try {
        symbol = decodeURIComponent(parts[1]);
      } catch (_error) {
        /* keep the raw segment */
      }
      return { name: "function", symbol, tab: parts[2] || "" };
    }
    if (parts[0] === "stats") return { name: "stats" };
    if (parts[0] === "callbacks") return { name: "callbacks" };
    return { name: "welcome" };
  }

  function navigate(hash, options = {}) {
    if (window.location.hash === hash) return;
    if (options.push) {
      window.history.pushState(null, "", hash);
    } else {
      window.history.replaceState(null, "", hash);
    }
  }

  async function applyRoute() {
    const route = parseHash();
    if (route.name === "function") {
      if (
        route.symbol !== state.selectedSymbol ||
        !state.detail ||
        state.detail.function === null
      ) {
        await openFunction(route.symbol, { tab: route.tab, hash: true });
      } else if ((route.tab ? normalizeTab(route.tab) : null) !== state.tab) {
        setSection(route.tab || null, { hash: true });
      }
    } else if (route.name === "stats") {
      await showStats({ hash: true });
    } else if (route.name === "callbacks") {
      await showCallbacks(undefined, { hash: true });
    } else {
      showWelcome({ hash: true });
    }
  }

  function showView(name) {
    state.route = name;
    els.view.classList.remove("function-view");
    els.welcome.hidden = name !== "welcome";
    els.view.hidden = name === "welcome";
  }

  // ----------------------------------------------------------------- search
  let searchTimer = null;

  function showSearchDropdown() {
    if (els.searchDropdown) els.searchDropdown.hidden = false;
  }

  function hideSearchDropdown() {
    if (els.searchDropdown) els.searchDropdown.hidden = true;
  }
  let nodeClickTimer = null;

  async function runSearch(rawQuery) {
    const query = String(rawQuery || "").trim();
    if (!query) {
      clearStatus();
      return;
    }
    state.resultsQuery = query;
    setStatus(t("status.searching", { query }), "info");
    try {
      const data = await api(
        `/api/search?q=${encodeURIComponent(query)}&limit=100`
      );
      state.results = data.items || [];
      renderResults(data);
      showSearchDropdown();
      clearStatus();
    } catch (error) {
      setStatus(friendlyError(error), "error");
    }
  }

  function renderResults(data) {
    const query = data && data.query ? data.query : state.resultsQuery;
    const rows = (data && data.items) || [];
    els.resultsCount.textContent = rows.length ? number(rows.length) : "";
    els.results.innerHTML = "";
    els.resultsEmpty.hidden = rows.length > 0;
    if (!rows.length) {
      els.resultsEmpty.textContent = query
        ? t("search.noMatches", { query })
        : t("search.startHint");
      return;
    }
    const fragment = document.createDocumentFragment();
    rows.forEach((row) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "result";
      button.dataset.symbol = row.id || row.name || "";
      button.dataset.name = row.name || "";
      const badges = [];
      if (row.is_definition) {
        badges.push(badge(t("badge.definition"), "definition"));
      } else {
        badges.push(badge(t("badge.declaration")));
      }
      if (row.is_static) badges.push(badge(t("badge.static")));
      if (row.shard) {
        badges.push(badge(t("badge.shard", { name: row.shard }), "shard"));
      }
      const meta = [location(row), row.kind || "", row.linkage || ""]
        .filter(Boolean)
        .join(" · ");
      button.innerHTML = `<span class="result-name">${escapeHtml(
        row.name || row.id || "?"
      )} ${badges.join(" ")}</span><span class="result-meta">${escapeHtml(
        meta
      )}</span>`;
      item.appendChild(button);
      fragment.appendChild(item);
    });
    els.results.appendChild(fragment);
    highlightSelectedResult();
  }

  function highlightSelectedResult() {
    const selected = state.detail && state.detail.function;
    if (!selected) return;
    els.results.querySelectorAll(".result").forEach((button) => {
      const matches =
        button.dataset.symbol === selected.id ||
        button.dataset.name === selected.name;
      button.setAttribute("aria-current", matches ? "true" : "false");
    });
  }

  // --------------------------------------------------------------- function
  async function openFunction(symbol, options = {}) {
    const target = String(symbol || "").trim();
    if (!target) return;
    hideSearchDropdown();
    const tab =
      options.tab === undefined
        ? "overview"
        : options.tab
        ? normalizeTab(options.tab)
        : null;
    state.loadingSymbol = target;
    showView("view");
    renderLoading(t("status.loadingFunction", { target }));
    clearStatus();
    try {
      const [detail, callers, callees] = await Promise.all([
        api(`/api/function/${encodeURIComponent(target)}`),
        api(`/api/callers?symbol=${encodeURIComponent(target)}&limit=200`).catch(
          () => null
        ),
        api(`/api/callees?symbol=${encodeURIComponent(target)}&limit=200`).catch(
          () => null
        ),
      ]);
      if (state.loadingSymbol !== target) return;
      state.selectedSymbol = target;
      state.detail = detail;
      state.callers = callers ? callers.items || [] : [];
      state.callees = callees ? callees.items || [] : [];
      state.graph = null;
      state.topSource = null;
      state.nodeSource = null;
      state.cfg = null;
      state.cfgLoading = false;
      state.cfgError = "";
      state.cfgZoom = 1;
      state.explain = null;
      state.explainLoading = false;
      state.explainError = "";
      state.tab = tab;
      state.route = "function";
      if (!options.hash) {
        const base = `#/fn/${encodeURIComponent(target)}`;
        navigate(tab ? `${base}/${encodeURIComponent(tab)}` : base, {
          push: true,
        });
      }
      renderFunction();
      highlightSelectedResult();
    } catch (error) {
      if (state.loadingSymbol !== target) return;
      state.detail = null;
      state.selectedSymbol = target;
      els.view.innerHTML = `<div class="fn-head"><h1 class="fn-title">${escapeHtml(
        target
      )}</h1><p class="fn-sub">${escapeHtml(
        t("detail.subtitle")
      )}</p></div>${errorPanel(
        t("error.loadFunction"),
        error,
        t("error.loadFunctionHint")
      )}`;
      setStatus(friendlyError(error), "error");
    } finally {
      if (state.loadingSymbol === target) state.loadingSymbol = "";
    }
  }

  function functionSignature(fn) {
    if (!fn) return "";
    if (fn.signature) return fn.signature;
    const params = Array.isArray(fn.params) ? fn.params : [];
    const rendered = params
      .map((param) => {
        const type = param.type_spelling || "?";
        return param.name ? `${type} ${param.name}` : type;
      })
      .join(", ");
    return `${fn.return_type || "?"} ${fn.name || "?"}(${rendered})`;
  }

  const TAB_IDS = [
    "overview",
    "callers",
    "callees",
    "branches",
    "returns",
    "resources",
    "findings",
  ];

  function normalizeTab(tab) {
    return TAB_IDS.includes(tab) ? tab : "overview";
  }

  function renderFunction() {
    const detail = state.detail;
    const fn = detail.function || {};
    const shard = detail.shard || fn.shard || "";
    const badges = [
      badge(fn.kind || "function", "info"),
      fn.is_definition
        ? badge(t("badge.definition"), "definition")
        : badge(t("badge.declaration")),
      fn.is_static ? badge(t("badge.static")) : "",
      fn.is_inline ? badge(t("badge.inline")) : "",
      fn.linkage ? badge(fn.linkage) : "",
      shard ? badge(t("badge.shard", { name: shard }), "shard") : "",
      detail.deep_ready
        ? badge(t("badge.deepFacts"), "definition")
        : badge(t("badge.deepFactsPending"), "warning"),
    ].filter(Boolean);
    const tabs = [
      ["overview", t("tab.overview"), null],
      ["branches", t("tab.branches"), (detail.branches || []).length],
      ["returns", t("tab.returns"), (detail.returns || []).length],
      ["resources", t("tab.resources"), (detail.resource_ops || []).length],
      ["findings", t("tab.findings"), (detail.findings || []).length],
      ["explain", t("tab.explain"), null],
    ];
    const accordionHtml = tabs
      .map(([id, label, count]) => {
        const open = state.tab === id;
        return `<section class="accordion-item${open ? " open" : ""}" data-section="${id}">
          <button type="button" class="accordion-header" data-section-toggle="${id}" aria-expanded="${
            open ? "true" : "false"
          }"><span class="accordion-title">${escapeHtml(label)}</span>${
            count === null || count === undefined
              ? ""
              : `<span class="tab-count">${number(count)}</span>`
          }<span class="accordion-chevron" aria-hidden="true">${
            open ? "▾" : "▸"
          }</span></button>
          <div class="accordion-body" id="accordion-body-${id}"${
            open ? "" : " hidden"
          }></div>
        </section>`;
      })
      .join("");
    const backButton = functionHistory.length
      ? `<button type="button" class="ghost" id="fn-back">← ${escapeHtml(
          t("nav.backToPrev")
        )}</button>`
      : "";
    els.view.classList.add("function-view");
    els.view.innerHTML = `
      <div class="fn-page" id="fn-page">
        <header class="fn-head" id="fn-head">
          <div class="fn-head-top">
            <div class="fn-head-main">
              <h1 class="fn-title">${escapeHtml(fn.name || state.selectedSymbol)} ${badges.join(
                " "
              )}</h1>
              <p class="fn-sub">${escapeHtml(
                [fn.qualified_name, location(fn)].filter(Boolean).join(" · ")
              )}</p>
            </div>
            <div class="fn-head-actions">
              ${backButton}
              <button type="button" class="ghost" id="toggle-left" aria-pressed="false" title="${escapeHtml(
                t("layout.toggleLeft")
              )}">◧</button>
              <button type="button" class="ghost" id="toggle-right" aria-pressed="false" title="${escapeHtml(
                t("layout.toggleRight")
              )}">◨</button>
              <button type="button" class="ghost" id="layout-reset" title="${escapeHtml(
                t("layout.reset")
              )}">${escapeHtml(t("layout.reset"))}</button>
            </div>
          </div>
          <section class="fn-source" id="fn-source" aria-label="${escapeHtml(
            t("source.aria")
          )}"></section>
        </header>
        <div class="splitter splitter-h" id="split-source" role="separator" aria-orientation="horizontal" aria-valuemin="140" tabindex="0"></div>
        <div class="fn-workspace" id="fn-workspace">
          <aside class="fn-sidebar" aria-label="${escapeHtml(t("nav.sections"))}">
            <div class="accordion" role="tablist">${accordionHtml}</div>
          </aside>
          <div class="splitter splitter-v" data-split="left" role="separator" aria-orientation="vertical" aria-valuemin="220" tabindex="0"></div>
          <section class="fn-graph" id="fn-graph" aria-label="${escapeHtml(
            t("tab.graph")
          )}"></section>
          <div class="splitter splitter-v" data-split="right" role="separator" aria-orientation="vertical" aria-valuemin="280" tabindex="0"></div>
          <aside class="fn-panel" id="fn-panel" role="tabpanel" aria-label="${escapeHtml(
            t("nav.details")
          )}"></aside>
        </div>
      </div>`;
    const key = currentFunctionKey();
    if (!state.topSource || state.topSource.symbol !== key) {
      state.topSource = null;
      loadTopSource(key);
    } else {
      renderTopSource();
    }
    applyLayout();
    renderGraphSurface();
    renderAccordionContent();
    renderTabPanel();
  }

  function renderAccordion() {
    els.view.querySelectorAll(".accordion-item[data-section]").forEach((item) => {
      const open = item.dataset.section === state.tab;
      item.classList.toggle("open", open);
      const header = item.querySelector(".accordion-header");
      if (header) header.setAttribute("aria-expanded", String(open));
      const chevron = item.querySelector(".accordion-chevron");
      if (chevron) chevron.textContent = open ? "▾" : "▸";
      const body = item.querySelector(".accordion-body");
      if (body) body.hidden = !open;
    });
  }

  function setSection(tab, options = {}) {
    if (!state.detail) return;
    const next = tab ? normalizeTab(tab) : null;
    state.tab = options.toggle && next === state.tab ? null : next;
    if (!options.hash) {
      const base = `#/fn/${encodeURIComponent(state.selectedSymbol)}`;
      navigate(state.tab ? `${base}/${encodeURIComponent(state.tab)}` : base);
    }
    renderAccordion();
    renderAccordionContent();
  }

  function renderAccordionContent() {
    if (!state.tab) return;
    const body = document.getElementById(`accordion-body-${state.tab}`);
    if (!body) return;
    switch (state.tab) {
      case "explain":
        renderExplainSection(body);
        break;
      case "branches":
        renderCfgSection(body);
        break;
      case "returns":
        renderReturns(body);
        break;
      case "resources":
        renderResources(body);
        break;
      case "findings":
        renderFindings(body);
        break;
      default:
        renderOverview(body);
    }
  }

  function renderTabPanel() {
    const panel = document.getElementById("fn-panel");
    if (!panel) return;
    if (!state.detail) return;
    if (state.nodeSource) {
      renderNodeSourceContent(panel);
      return;
    }
    panel.innerHTML = `<section class="section"><div class="source-head"><div class="source-head-left"><h2>${escapeHtml(
      t("source.nodeTitle")
    )}</h2></div></div><p class="empty">${escapeHtml(
      t("source.nodeHint")
    )}</p></section>`;
  }

  function renderOverview(panel) {
    const detail = state.detail;
    const fn = detail.function || {};
    const sections = [];
    if (!detail.deep_ready) {
      sections.push(
        `<div class="callout"><span><strong>${escapeHtml(
          t("detail.deepPendingTitle")
        )}</strong> ${escapeHtml(
          t("detail.deepPendingBody")
        )} <code>ca ensure-deep --db DB --repo REPO --compdb COMPDB --symbols ${escapeHtml(
          fn.name || state.selectedSymbol
        )}</code> ${escapeHtml(t("detail.deepPendingAction"))}</span></div>`
      );
    }
    const shard = detail.shard || fn.shard || "";
    const fields = [
      [t("field.file"), location(fn) || "—"],
      [
        t("field.range"),
        fn.line ? `${fn.line}:${fn.col || 0} – ${fn.end_line || "?"}` : "—",
      ],
      [t("field.returnType"), fn.return_type || "—"],
      [t("field.signature"), functionSignature(fn)],
      [t("field.linkage"), fn.linkage || "—"],
      [t("field.storageClass"), fn.storage_class || "—"],
      [
        t("field.declaredIn"),
        shard
          ? t("common.shardName", { name: shard })
          : t("common.singleDatabase"),
      ],
      [t("field.usr"), fn.usr || "—"],
      [t("field.tuId"), fn.tu_id || "—"],
      [t("field.functionId"), fn.id || "—"],
      [t("field.parentId"), fn.parent_id || "—"],
    ];
    const params = Array.isArray(fn.params) ? fn.params : [];
    if (params.length) {
      fields.push([
        t("field.parameters"),
        params
          .map(
            (param) =>
              `${param.type_spelling || "?"}${
                param.name ? ` ${param.name}` : ""
              }${param.default_value ? ` = ${param.default_value}` : ""}`
          )
          .join(", "),
      ]);
    }
    const grid = fields
      .map(
        ([label, value]) =>
          `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`
      )
      .join("");
    const metrics = [
      metric(t("metric.branches"), number((detail.branches || []).length)),
      metric(t("metric.assignments"), number((detail.assignments || []).length)),
      metric(t("metric.returns"), number((detail.returns || []).length)),
      metric(t("metric.resources"), number((detail.resource_ops || []).length)),
      metric(t("metric.findings"), number((detail.findings || []).length)),
      metric(t("metric.callers"), number(state.callers.length)),
      metric(t("metric.callees"), number(state.callees.length)),
    ].join("");
    sections.push(`<section class="section"><div class="section-head"><h3>${escapeHtml(
      t("detail.sectionFunction")
    )}</h3>
      <span class="note">${escapeHtml(t("detail.sectionFunctionNote"))}</span></div>
      <div class="section-body" style="padding: 4px 16px 16px"><dl class="detail-grid">${grid}</dl></div>
    </section>`);
    sections.push(`<div class="metrics">${metrics}</div>`);
    sections.push(assignmentsSection(detail.assignments || []).join(""));
    panel.innerHTML = sections.join("");
  }

  function assignmentsSection(rows) {
    if (!rows.length) {
      return [
        `<section class="section"><div class="section-head"><h3>${escapeHtml(
          t("section.assignments")
        )}</h3><span class="note">${escapeHtml(
          t("note.assignmentsEmpty")
        )}</span></div></section>`,
      ];
    }
    const body = rows
      .map(
        (row) =>
          `<tr><td class="mono">${escapeHtml(row.lhs || "")}</td><td class="mono">${escapeHtml(
            row.operator || "="
          )}</td><td class="mono">${escapeHtml(row.rhs || "")}</td><td>${escapeHtml(
            row.type_spelling || ""
          )}</td><td>${row.is_decl_init ? badge(t("badge.declInit"), "info") : ""}</td><td class="mono muted">${escapeHtml(
            location(row)
          )}</td><td>${shardBadge(row)}</td></tr>`
      )
      .join("");
    return [
      `<section class="section"><div class="section-head"><h3>${escapeHtml(
        t("section.assignments")
      )}</h3><span class="note">${escapeHtml(
        t("note.assignmentsIndexed", { count: number(rows.length) })
      )}</span></div><div class="section-body">${table(
        [
          t("table.lhs"),
          t("table.op"),
          t("table.rhs"),
          t("table.type"),
          t("table.kind"),
          t("table.location"),
          t("table.shard"),
        ],
        [body]
      )}</div></section>`,
    ];
  }

  function renderEdges(panel, kind) {
    const rows = kind === "callers" ? state.callers : state.callees;
    const title = kind === "callers" ? t("tab.callers") : t("tab.callees");
    const emptyMessage =
      kind === "callers" ? t("empty.callers") : t("empty.callees");
    const body = rows.length
      ? rows
          .map((row) => {
            const name = kind === "callers" ? row.caller_name : row.callee_name;
            const id = kind === "callers" ? row.caller_id : row.callee_id;
            const file = kind === "callers" ? row.caller_file : row.callee_file;
            const line = kind === "callers" ? row.caller_line : row.callee_line;
            return `<tr>
              <td>${functionLink(name, id)}</td>
              <td class="mono muted">${escapeHtml(
                file ? `${file}:${line || 0}` : "—"
              )}</td>
              <td class="mono muted">${escapeHtml(location(row, "call_") || "—")}</td>
              <td>${escapeHtml(row.kind || "")}</td>
              <td class="num">${isCandidate(row) ? confidence(row.confidence) : "1.00"}</td>
              <td>${edgeBadge(row)} ${shardBadge(row)}</td>
            </tr>`;
          })
          .join("")
      : emptyRow(6, emptyMessage);
    panel.innerHTML = `<section class="section"><div class="section-head"><h3>${escapeHtml(
      title
    )}</h3><span class="note">${escapeHtml(
      t("note.edgeCount", { count: number(rows.length) })
    )}</span></div><div class="section-body">${table(
      [
        t("table.function"),
        t("table.location"),
        t("table.callSite"),
        t("table.kind"),
        t("table.confidence"),
        t("table.edge"),
      ],
      [body]
    )}</div></section>`;
  }

  function factsTable(panel, title, note, headers, rows, emptyMessage) {
    const body = rows.length ? rows : emptyRow(headers.length, emptyMessage);
    panel.innerHTML = `<section class="section"><div class="section-head"><h3>${escapeHtml(
      title
    )}</h3><span class="note">${escapeHtml(note)}</span></div><div class="section-body">${table(
      headers,
      [body]
    )}</div></section>`;
  }


  function renderReturns(panel) {
    const rows = state.detail.returns || [];
    const body = rows
      .map(
        (row) => `<tr><td class="mono">${escapeHtml(row.expr || "—")}</td>
          <td>${row.is_implicit ? badge(t("badge.implicit"), "warning") : badge(t("badge.explicit"))}</td>
          <td class="mono muted">${escapeHtml(location(row))}</td>
          <td class="mono faint">${escapeHtml(truncate(row.block_id || "", 26))}</td>
          <td>${shardBadge(row)}</td></tr>`
      )
      .join("");
    factsTable(
      panel,
      t("tab.returns"),
      t("note.returnCount", { count: number(rows.length) }),
      [
        t("table.expression"),
        t("table.kind"),
        t("table.location"),
        t("table.block"),
        t("table.shard"),
      ],
      body,
      t("empty.returns")
    );
  }

  function renderResources(panel) {
    const rows = state.detail.resource_ops || [];
    const body = rows
      .map(
        (row) => `<tr><td>${badge(row.op || t("table.op"), row.op === "acquire" ? "warning" : "definition")}</td>
          <td>${escapeHtml(row.resource_kind || "")}</td>
          <td class="mono">${escapeHtml(row.api_name || "")}</td>
          <td class="mono">${escapeHtml(row.target || "")}</td>
          <td class="mono muted">${escapeHtml(row.matcher || "")}</td>
          <td class="mono muted">${escapeHtml(location(row))}</td>
          <td>${shardBadge(row)}</td></tr>`
      )
      .join("");
    factsTable(
      panel,
      t("metric.resources"),
      t("note.resourceCount", { count: number(rows.length) }),
      [
        t("table.op"),
        t("table.resource"),
        t("table.api"),
        t("table.target"),
        t("table.matcher"),
        t("table.location"),
        t("table.shard"),
      ],
      body,
      t("empty.resources")
    );
  }

  function renderFindings(panel) {
    const rows = state.detail.findings || [];
    const body = rows
      .map((row) => {
        const severity = String(row.severity || "info");
        const kind =
          severity === "error"
            ? "danger"
            : severity === "warning"
            ? "warning"
            : "info";
        return `<tr><td>${badge(severity, kind)}</td><td>${escapeHtml(
          row.kind || ""
        )}</td><td>${escapeHtml(row.resource_kind || "")}</td>
          <td class="mono">${escapeHtml(row.message || "")}</td>
          <td class="mono muted">${escapeHtml(location(row))}</td>
          <td>${shardBadge(row)}</td></tr>`;
      })
      .join("");
    factsTable(
      panel,
      t("tab.findings"),
      t("note.findingCount", { count: number(rows.length) }),
      [
        t("table.severity"),
        t("table.kind"),
        t("table.resource"),
        t("table.message"),
        t("table.location"),
        t("table.shard"),
      ],
      body,
      t("empty.findings")
    );
  }

  // ----------------------------------------------------------------- graph
  function currentLanguage() {
    if (window.CA_I18N && typeof window.CA_I18N.getLanguage === "function") {
      return window.CA_I18N.getLanguage();
    }
    return "en";
  }

  async function loadExplain(force = false) {
    const symbol = state.selectedSymbol;
    if (!symbol || state.explainLoading) return;
    const language = currentLanguage();
    state.explainLoading = true;
    state.explainError = "";
    if (force || (state.explain && state.explain.language !== language)) {
      state.explain = null;
    }
    renderAccordionContent();
    try {
      const data = await api(
        `/api/explain?symbol=${encodeURIComponent(
          symbol
        )}&lang=${encodeURIComponent(language)}`
      );
      if (state.selectedSymbol !== symbol) return;
      state.explain = data;
    } catch (error) {
      if (state.selectedSymbol !== symbol) return;
      state.explainError = friendlyError(error);
    } finally {
      if (state.selectedSymbol === symbol) {
        state.explainLoading = false;
        if (state.tab === "explain") renderAccordionContent();
      }
    }
  }

  function renderExplainSection(panel) {
    const title = `<h3>${escapeHtml(t("tab.explain"))}</h3>`;
    if (state.explainError) {
      panel.innerHTML = `<section class="section"><div class="section-head">${title}</div><div class="graph-empty">${escapeHtml(
        `${t("explain.error")}: ${state.explainError}`
      )}</div><div class="source-actions"><button type="button" class="ghost" id="explain-refresh">${escapeHtml(
        t("explain.refresh")
      )}</button></div></section>`;
      return;
    }
    const data = state.explain;
    const language = currentLanguage();
    if (state.explainLoading || !data || data.language !== language) {
      panel.innerHTML = `<section class="section"><div class="section-head">${title}<span class="note">${escapeHtml(
        t("explain.loading")
      )}</span></div><div class="view-loading">${escapeHtml(
        t("explain.loading")
      )}</div></section>`;
      if (!state.explainLoading) loadExplain();
      return;
    }
    const providerBadge =
      data.provider === "llm"
        ? badge(data.model || t("explain.ai"), "definition")
        : badge(t("explain.heuristic"), "warning");
    const facts = data.facts_summary || {};
    const meta = t("explain.meta", {
      provider: data.provider === "llm" ? t("explain.ai") : t("explain.heuristic"),
      ms: Math.round(Number(data.elapsed_ms) || 0),
    });
    const fallback = data.fallback_reason
      ? `<p class="empty">${escapeHtml(
          t("explain.fallback", { message: data.fallback_reason })
        )}</p>`
      : "";
    const disclaimer =
      data.provider === "heuristic"
        ? `<p class="empty">${escapeHtml(t("explain.disclaimer"))}</p>`
        : "";
    const sourceInfo = facts.source || {};
    const sourceNote = sourceInfo.available
      ? `${sourceInfo.file}:${sourceInfo.line} · ${sourceInfo.line_count} ${t(
          "explain.sourceLines"
        )}`
      : t("explain.sourceUnavailable");
    panel.innerHTML = `<section class="section">
      <div class="section-head">${title}<div class="explain-actions">${providerBadge}<button type="button" class="ghost" id="explain-refresh">${escapeHtml(
        t("explain.refresh")
      )}</button><button type="button" class="ghost" id="explain-copy">${escapeHtml(
        t("explain.copy")
      )}</button></div></div>
      <div class="explain-meta">${escapeHtml(meta)} · ${escapeHtml(sourceNote)}</div>
      ${fallback}${disclaimer}
      <pre class="explain-body">${escapeHtml(data.explanation || "")}</pre>
    </section>`;
  }

  async function copyExplain() {
    if (!state.explain || !state.explain.explanation) return;
    const text = state.explain.explanation;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const helper = document.createElement("textarea");
        helper.value = text;
        document.body.appendChild(helper);
        helper.select();
        document.execCommand("copy");
        document.body.removeChild(helper);
      }
      setStatus(t("explain.copied"), "ok");
    } catch (error) {
      setStatus(friendlyError(error), "error");
    }
  }

  function renderGraphSurface() {
    const container = document.getElementById("fn-graph");
    if (!container) return;
    const parameters = state.graphParams;
    const data = state.graph;
    const graphNote = data
      ? [
          t("graph.nodeCount", { count: number((data.nodes || []).length) }),
          t("graph.edgeCount", { count: number((data.edges || []).length) }),
          data.truncated ? t("graph.truncated") : "",
          (data.cycles || []).length
            ? t("graph.cycleCount", { count: number(data.cycles.length) })
            : "",
        ]
          .filter(Boolean)
          .join(" · ")
      : t("common.loading");
    container.innerHTML = `<section class="section graph-section">
      <div class="section-head"><h3>${escapeHtml(
        t("section.callGraph")
      )}</h3><span class="note">${escapeHtml(
        t("section.callGraphNote")
      )}</span></div>
      <div class="graph-controls">
        <label>${escapeHtml(t("graph.direction"))}
          <select name="graph-direction">
            <option value="both"${
              parameters.direction === "both" ? " selected" : ""
            }>${escapeHtml(t("graph.both"))}</option>
            <option value="forward"${
              parameters.direction === "forward" ? " selected" : ""
            }>${escapeHtml(t("graph.forward"))}</option>
            <option value="reverse"${
              parameters.direction === "reverse" ? " selected" : ""
            }>${escapeHtml(t("graph.reverse"))}</option>
          </select>
        </label>
        <label>${escapeHtml(t("graph.depth"))}
          <select name="graph-depth">
            <option value="1"${Number(parameters.depth) === 1 ? " selected" : ""}>1</option>
            <option value="2"${Number(parameters.depth) === 2 ? " selected" : ""}>2</option>
            <option value="3"${Number(parameters.depth) === 3 ? " selected" : ""}>3</option>
            <option value="4"${Number(parameters.depth) === 4 ? " selected" : ""}>4</option>
            <option value="6"${Number(parameters.depth) === 6 ? " selected" : ""}>6</option>
          </select>
        </label>
        <button type="button" class="ghost" id="graph-refresh">${escapeHtml(
          t("graph.rebuild")
        )}</button>
        <span class="note" id="graph-note">${escapeHtml(graphNote)}</span>
      </div>
      <div class="graph-legend">
        <span class="legend-item"><span class="legend-line"></span> ${escapeHtml(
          t("graph.legendDirect")
        )}</span>
        <span class="legend-item"><span class="legend-line candidate"></span> ${escapeHtml(
          t("graph.legendCandidate")
        )}</span>
        <span class="legend-item"><span class="legend-line cycle"></span> ${escapeHtml(
          t("graph.legendCycle")
        )}</span>
        <span class="legend-item">${escapeHtml(t("graph.legendClick"))}</span>
      </div>
      <div class="graph-wrap" id="graph-wrap">
        <div class="view-loading">${escapeHtml(t("status.buildingGraph"))}</div>
      </div>
    </section>`;
    if (data) {
      drawGraph(data);
    } else {
      loadGraph();
    }
  }

  // ------------------------------------------------- top source (fixed)
  function layoutIsResizable() {
    return window.innerWidth >= 1100;
  }

  function clampLayout(layout) {
    const page = document.getElementById("fn-page");
    if (!page) return;
    const viewportHeight =
      window.innerHeight || document.documentElement.clientHeight || 800;
    if (layout.top) {
      layout.top = Math.round(
        Math.min(
          Math.max(LAYOUT_MIN.top, layout.top),
          Math.max(LAYOUT_MIN.top, viewportHeight * 0.7)
        )
      );
    }
    const workspace = document.getElementById("fn-workspace");
    const workspaceWidth =
      (workspace && workspace.clientWidth) || window.innerWidth;
    if (layout.left) {
      const rightPx = layout.right || Math.round(workspaceWidth * 0.26);
      const maxLeft = Math.max(
        LAYOUT_MIN.left,
        workspaceWidth - rightPx - LAYOUT_MIN.middle - 16
      );
      layout.left = Math.round(
        Math.min(Math.max(LAYOUT_MIN.left, layout.left), maxLeft)
      );
    }
    if (layout.right) {
      const leftPx = layout.left || Math.round(workspaceWidth * 0.22);
      const maxRight = Math.max(
        LAYOUT_MIN.right,
        workspaceWidth - leftPx - LAYOUT_MIN.middle - 16
      );
      layout.right = Math.round(
        Math.min(Math.max(LAYOUT_MIN.right, layout.right), maxRight)
      );
    }
  }

  function applyLayout() {
    const page = document.getElementById("fn-page");
    const workspace = document.getElementById("fn-workspace");
    if (!page || !workspace) return;
    const layout = state.layout;
    clampLayout(layout);
    page.style.setProperty("--top-h", layout.top ? `${layout.top}px` : "32vh");
    workspace.style.setProperty(
      "--left-w",
      layout.left ? `${layout.left}px` : "22%"
    );
    workspace.style.setProperty(
      "--right-w",
      layout.right ? `${layout.right}px` : "26%"
    );
    workspace.classList.toggle(
      "left-collapsed",
      Boolean(layout.leftCollapsed)
    );
    workspace.classList.toggle(
      "right-collapsed",
      Boolean(layout.rightCollapsed)
    );
    page.classList.toggle("resizable", layoutIsResizable());
    const toggleLeft = document.getElementById("toggle-left");
    if (toggleLeft) {
      toggleLeft.setAttribute("aria-pressed", String(layout.leftCollapsed));
      toggleLeft.setAttribute("title", t("layout.toggleLeft"));
    }
    const toggleRight = document.getElementById("toggle-right");
    if (toggleRight) {
      toggleRight.setAttribute("aria-pressed", String(layout.rightCollapsed));
      toggleRight.setAttribute("title", t("layout.toggleRight"));
    }
    const resettable = document.getElementById("layout-reset");
    if (resettable) resettable.setAttribute("title", t("layout.reset"));
    const topSplitter = document.getElementById("split-source");
    if (topSplitter) {
      topSplitter.hidden = !layoutIsResizable();
      topSplitter.setAttribute("title", t("layout.resizeSource"));
      topSplitter.setAttribute(
        "aria-valuenow",
        String(
          Math.round(
            (document.getElementById("fn-head") || page).getBoundingClientRect()
              .height || 0
          )
        )
      );
      topSplitter.setAttribute(
        "aria-valuemax",
        String(Math.round((window.innerHeight || 800) * 0.7))
      );
    }
    const sidebar = workspace.querySelector(".fn-sidebar");
    const panel = workspace.querySelector(".fn-panel");
    for (const splitter of workspace.querySelectorAll(".splitter-v")) {
      const isLeft = splitter.dataset.split === "left";
      splitter.hidden = !layoutIsResizable();
      splitter.setAttribute(
        "title",
        t(isLeft ? "layout.resizeLeft" : "layout.resizeRight")
      );
      const current = isLeft ? sidebar : panel;
      splitter.setAttribute(
        "aria-valuenow",
        String(Math.round(current ? current.getBoundingClientRect().width : 0))
      );
      splitter.setAttribute("aria-valuemin", isLeft ? "220" : "280");
    }
  }

  function toggleColumn(side) {
    const key = side === "left" ? "leftCollapsed" : "rightCollapsed";
    state.layout[key] = !state.layout[key];
    saveStoredLayout();
    applyLayout();
  }

  function resetLayout() {
    state.layout = defaultLayout();
    saveStoredLayout();
    applyLayout();
  }

  function ensureRightColumnExpanded() {
    if (!state.layout.rightCollapsed) return;
    state.layout.rightCollapsed = false;
    saveStoredLayout();
    applyLayout();
  }

  function beginSplitterDrag(kind, event) {
    if (!layoutIsResizable()) return;
    if (event.button !== undefined && event.button !== 0) return;
    const workspace = document.getElementById("fn-workspace");
    const page = document.getElementById("fn-page");
    const head = document.getElementById("fn-head");
    if (!workspace || !page) return;
    event.preventDefault();
    const startX = event.clientX;
    const startY = event.clientY;
    const sidebar = workspace.querySelector(".fn-sidebar");
    const panel = workspace.querySelector(".fn-panel");
    const startLeft = sidebar ? sidebar.getBoundingClientRect().width : 0;
    const startRight = panel ? panel.getBoundingClientRect().width : 0;
    const startTop = head ? head.getBoundingClientRect().height : 0;
    const workspaceWidth = workspace.getBoundingClientRect().width;
    const onMove = (moveEvent) => {
      const deltaX = moveEvent.clientX - startX;
      const deltaY = moveEvent.clientY - startY;
      const layout = state.layout;
      if (kind === "top") {
        layout.top = Math.round(
          Math.min(
            Math.max(LAYOUT_MIN.top, startTop + deltaY),
            Math.max(LAYOUT_MIN.top, window.innerHeight * 0.7)
          )
        );
      } else if (kind === "left") {
        const rightPx = layout.right || startRight;
        const maxLeft = Math.max(
          LAYOUT_MIN.left,
          workspaceWidth - rightPx - LAYOUT_MIN.middle - 16
        );
        layout.left = Math.round(
          Math.min(Math.max(LAYOUT_MIN.left, startLeft + deltaX), maxLeft)
        );
      } else {
        const leftPx = layout.left || startLeft;
        const maxRight = Math.max(
          LAYOUT_MIN.right,
          workspaceWidth - leftPx - LAYOUT_MIN.middle - 16
        );
        layout.right = Math.round(
          Math.min(Math.max(LAYOUT_MIN.right, startRight - deltaX), maxRight)
        );
      }
      applyLayout();
    };
    const onUp = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.body.classList.remove("resizing");
      saveStoredLayout();
    };
    document.body.classList.add("resizing");
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  }

  function nudgeSplitter(kind, delta) {
    if (!layoutIsResizable()) return;
    const layout = state.layout;
    const workspace = document.getElementById("fn-workspace");
    const head = document.getElementById("fn-head");
    if (kind === "top") {
      const current =
        layout.top ||
        (head ? head.getBoundingClientRect().height : 0) ||
        Math.round(window.innerHeight * 0.32);
      layout.top = Math.round(current + delta);
    } else if (kind === "left") {
      const sidebar = workspace && workspace.querySelector(".fn-sidebar");
      const current =
        layout.left ||
        (sidebar ? sidebar.getBoundingClientRect().width : 0) ||
        LAYOUT_MIN.left;
      layout.left = Math.round(current + delta);
    } else {
      const panel = workspace && workspace.querySelector(".fn-panel");
      const current =
        layout.right ||
        (panel ? panel.getBoundingClientRect().width : 0) ||
        LAYOUT_MIN.right;
      layout.right = Math.round(current - delta);
    }
    applyLayout();
    saveStoredLayout();
  }

  function resetSplitter(kind) {
    if (kind === "top") state.layout.top = null;
    else state.layout[kind] = null;
    saveStoredLayout();
    applyLayout();
  }

  function splitterKind(splitter) {
    return splitter.id === "split-source" ? "top" : splitter.dataset.split;
  }


  function currentFunctionKey() {
    const fn = state.detail && state.detail.function;
    return (fn && (fn.id || fn.name)) || state.selectedSymbol;
  }

  function codeLinesHtml(data, selectedRange = null) {
    const rows = data.lines || [];
    const texts = rows.map((row) => row.text);
    const highlighter = window.CA_HIGHLIGHT;
    const highlighted = highlighter
      ? highlighter.highlightLines(texts)
      : texts.map((text) => escapeHtml(text));
    return rows
      .map((row, index) => {
        const inFunction =
          row.line >= data.function_start && row.line <= data.function_end;
        const selected =
          selectedRange &&
          row.line >= selectedRange.start &&
          row.line <= selectedRange.end;
        return `<div class="source-line${inFunction ? " in-function" : ""}${
          selected ? " selected-node" : ""
        }"><span class="lineno">${number(
          row.line
        )}</span><span class="line-text">${highlighted[index] || ""}</span></div>`;
      })
      .join("");
  }

  function sourceRangeNote(data) {
    return [
      t("source.range", {
        start: data.start,
        end: data.end,
        total: data.total_lines,
      }),
      t("source.functionRange", {
        start: data.function_start,
        end: data.function_end,
      }),
      data.truncated ? t("source.truncated", { count: (data.lines || []).length }) : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }

  function renderTopSource() {
    const shell = document.getElementById("fn-source");
    if (!shell) return;
    const isCollapsed = Boolean(state.topSourceCollapsed);
    shell.classList.toggle("collapsed", isCollapsed);
    const collapseButton = `<button type="button" class="ghost" id="source-collapse" aria-expanded="${!isCollapsed}">${escapeHtml(
      t(isCollapsed ? "source.expand" : "source.collapse")
    )}</button>`;
    const heading = `<div class="source-head"><div class="source-head-left"><h2>${escapeHtml(
      t("source.title")
    )}</h2><span class="badge">${escapeHtml(t("source.readonly"))}</span></div><div class="source-actions">${collapseButton}</div></div>`;
    const value = state.topSource;
    if (!value || value.loading) {
      shell.innerHTML = `${heading}${
        isCollapsed
          ? ""
          : `<div class="view-loading">${escapeHtml(t("source.loading"))}</div>`
      }`;
      return;
    }
    if (value.error) {
      shell.innerHTML = `${heading}${
        isCollapsed
          ? ""
          : `<div class="graph-empty">${escapeHtml(
              `${t("source.error")}: ${value.error}`
            )}</div>`
      }`;
      return;
    }
    const data = value.data || {};
    const functionInfo = data.function || {};
    // A graph node selected in the right panel: highlight its line range in the
    // fixed top source when the window covers it, otherwise show a hint.
    const node = state.nodeSource && state.nodeSource.data;
    let selectedRange = null;
    let nodeHint = "";
    if (node) {
      const nodeStart = node.function_start;
      const nodeEnd = node.function_end;
      if (nodeStart <= data.end && nodeEnd >= data.start) {
        selectedRange = {
          start: Math.max(nodeStart, data.start),
          end: Math.min(nodeEnd, data.end),
        };
      } else {
        nodeHint = t("source.nodeOutside", {
          file: node.file,
          line: (node.function && node.function.line) || 0,
        });
      }
    }
    const meta = `<div class="source-meta"><span class="mono">${escapeHtml(
      `${data.file}:${functionInfo.line || 0}`
    )}</span>${
      data.shard ? badge(t("badge.shard", { name: data.shard }), "shard") : ""
    }<span>${escapeHtml(sourceRangeNote(data))}</span>${
      nodeHint
        ? `<span class="source-node-hint">${escapeHtml(nodeHint)}</span>`
        : ""
    }</div>`;
    shell.innerHTML = `${heading}${meta}${
      isCollapsed
        ? ""
        : `<div class="source-code top-source-code" tabindex="0">${codeLinesHtml(
            data,
            selectedRange
          )}</div>`
    }`;
    const code = shell.querySelector(".source-code");
    if (code) {
      if (selectedRange) {
        code.scrollTop = Math.max(
          0,
          (selectedRange.start - data.start - 2) * 18
        );
      } else {
        code.scrollTop = 0;
      }
    }
  }

  async function loadTopSource(symbol) {
    if (!symbol) return;
    state.topSource = { symbol, data: null, loading: true, error: "" };
    renderTopSource();
    try {
      const data = await api(
        `/api/source?symbol=${encodeURIComponent(symbol)}&context=0`
      );
      if (!state.topSource || state.topSource.symbol !== symbol) return;
      state.topSource = { symbol, data, loading: false, error: "" };
    } catch (error) {
      if (!state.topSource || state.topSource.symbol !== symbol) return;
      state.topSource = {
        symbol,
        data: null,
        loading: false,
        error: friendlyError(error),
      };
    }
    renderTopSource();
  }

  function toggleTopSource() {
    state.topSourceCollapsed = !state.topSourceCollapsed;
    renderTopSource();
  }

  // ------------------------------------ graph node source (right pane)
  function markSelectedGraphNode(symbol) {
    els.view.querySelectorAll(".graph-node[data-source-open]").forEach((node) => {
      node.classList.toggle(
        "selected",
        Boolean(symbol) && node.dataset.sourceOpen === symbol
      );
    });
  }

  async function loadNodeSource(symbol) {
    if (!symbol) return;
    state.nodeSource = { symbol, data: null, loading: true, error: "" };
    markSelectedGraphNode(symbol);
    ensureRightColumnExpanded();
    renderTabPanel();
    try {
      const data = await api(
        `/api/source?symbol=${encodeURIComponent(symbol)}&context=0`
      );
      if (!state.nodeSource || state.nodeSource.symbol !== symbol) return;
      state.nodeSource = { symbol, data, loading: false, error: "" };
    } catch (error) {
      if (!state.nodeSource || state.nodeSource.symbol !== symbol) return;
      state.nodeSource = {
        symbol,
        data: null,
        loading: false,
        error: friendlyError(error),
      };
    }
    renderTabPanel();
    renderTopSource();
  }

  function clearNodeSource() {
    state.nodeSource = null;
    markSelectedGraphNode("");
    renderTabPanel();
    renderTopSource();
  }

  function showNodeSource(symbol) {
    loadNodeSource(symbol);
  }

  function jumpToFunction(symbol) {
    const target = String(symbol || "").trim();
    if (!target) return;
    const current = currentFunctionKey();
    if (current && target !== current) {
      pushFunctionHistory({ symbol: current, tab: state.tab });
    }
    openFunction(target);
  }

  function goBackToPreviousFunction() {
    const entry = popFunctionHistory();
    if (!entry) return;
    openFunction(entry.symbol, { tab: entry.tab || null });
  }

  function renderNodeSourceContent(panel) {
    const heading = `<div class="source-head"><div class="source-head-left"><h2>${escapeHtml(
      t("source.nodeTitle")
    )}</h2><span class="badge">${escapeHtml(t("source.readonly"))}</span></div></div>`;
    const value = state.nodeSource;
    if (!value) {
      panel.innerHTML = `<section class="section">${heading}<p class="empty">${escapeHtml(
        t("source.nodeHint")
      )}</p></section>`;
      return;
    }
    if (value.loading) {
      panel.innerHTML = `<section class="section">${heading}<div class="view-loading">${escapeHtml(
        t("source.loading")
      )}</div></section>`;
      return;
    }
    if (value.error) {
      panel.innerHTML = `<section class="section">${heading}<div class="graph-empty">${escapeHtml(
        `${t("source.error")}: ${value.error}`
      )}</div><div class="source-actions"><button type="button" class="ghost" id="node-source-close">${escapeHtml(
        t("source.clear")
      )}</button></div></section>`;
      return;
    }
    const data = value.data || {};
    const functionInfo = data.function || {};
    const meta = `<div class="source-meta"><span class="mono">${escapeHtml(
      `${data.file}:${functionInfo.line || 0}`
    )}</span>${
      data.shard ? badge(t("badge.shard", { name: data.shard }), "shard") : ""
    }<span>${escapeHtml(sourceRangeNote(data))}</span></div>`;
    panel.innerHTML = `<section class="section">${heading}${meta}
      <div class="source-actions">
        <button type="button" class="ghost" data-jump="${escapeHtml(
          functionInfo.id || value.symbol
        )}">${escapeHtml(t("source.openDetail"))}</button>
        <button type="button" class="ghost" id="node-source-close">${escapeHtml(
          t("source.clear")
        )}</button>
      </div>
      <div class="source-code node-source-code" tabindex="0">${codeLinesHtml(
        data
      )}</div></section>`;
    const code = panel.querySelector(".source-code");
    if (code) code.scrollTop = 0;
  }

  // -------------------------------------------------- CFG (branches tab)
  async function loadCfg() {
    const symbol = state.selectedSymbol;
    if (!symbol || state.cfgLoading) return;
    state.cfgLoading = true;
    state.cfgError = "";
    state.cfg = null;
    renderTabPanel();
    try {
      const cfg = await api(`/api/cfg?symbol=${encodeURIComponent(symbol)}`);
      if (state.selectedSymbol !== symbol) return;
      state.cfg = cfg;
    } catch (error) {
      if (state.selectedSymbol !== symbol) return;
      state.cfgError = friendlyError(error);
    } finally {
      if (state.selectedSymbol === symbol) {
        state.cfgLoading = false;
        if (state.tab === "branches") renderAccordionContent();
      }
    }
  }

  function setCfgZoom(value) {
    state.cfgZoom = Math.min(1.8, Math.max(0.4, Math.round(value * 10) / 10));
    renderTabPanel();
  }

  function renderCfgSection(panel) {
    const title = `<h3>${escapeHtml(t("cfg.title"))}</h3>`;
    if (state.cfgLoading) {
      panel.innerHTML = `<section class="section"><div class="section-head">${title}</div><div class="view-loading">${escapeHtml(
        t("cfg.loading")
      )}</div></section>`;
      return;
    }
    if (state.cfgError) {
      panel.innerHTML = `<section class="section"><div class="section-head">${title}</div><div class="graph-empty">${escapeHtml(
        `${t("cfg.error")}: ${state.cfgError}`
      )}</div></section>`;
      return;
    }
    const data = state.cfg;
    if (!data) {
      panel.innerHTML = `<section class="section"><div class="section-head">${title}</div><div class="view-loading">${escapeHtml(
        t("cfg.loading")
      )}</div></section>`;
      loadCfg();
      return;
    }
    if (!(data.blocks || []).length) {
      panel.innerHTML = `<section class="section"><div class="section-head">${title}<span class="note">${escapeHtml(
        data.deep_ready ? t("cfg.emptyReady") : t("badge.deepFactsPending")
      )}</span></div><div class="section-body" style="padding: 12px 16px"><div class="callout"><span><strong>${escapeHtml(
        t("cfg.emptyTitle")
      )}</strong> ${escapeHtml(
        t("cfg.emptyBody")
      )}</span></div></div></section>`;
      return;
    }
    const zoom = state.cfgZoom;
    const graph = drawCfgSvg(data, zoom);
    const stats =
      t("cfg.stats", {
        blocks: number(data.stats ? data.stats.blocks : (data.blocks || []).length),
        edges: number(data.stats ? data.stats.edges : (data.edges || []).length),
        branches: number(
          data.stats ? data.stats.branches : (data.branches || []).length
        ),
      }) + (data.truncated ? ` · ${t("cfg.truncated")}` : "");
    const legend = [
      ["entry", t("cfg.legendEntry")],
      ["exit", t("cfg.legendExit")],
      ["true", t("cfg.legendTrue")],
      ["false", t("cfg.legendFalse")],
      ["back", t("cfg.legendBack")],
    ]
      .map(
        ([kind, label]) =>
          `<span class="legend-item"><span class="cfg-swatch ${kind}"></span>${escapeHtml(
            label
          )}</span>`
      )
      .join("");
    panel.innerHTML = `<section class="section">
      <div class="section-head">${title}
        <div class="cfg-toolbar">
          <span class="note">${escapeHtml(stats)}</span>
          <button type="button" class="ghost" id="cfg-zoom-out" title="${escapeHtml(
            t("cfg.zoomOut")
          )}">−</button>
          <span class="cfg-zoom-label">${Math.round(zoom * 100)}%</span>
          <button type="button" class="ghost" id="cfg-zoom-in" title="${escapeHtml(
            t("cfg.zoomIn")
          )}">+</button>
          <button type="button" class="ghost" id="cfg-zoom-reset">${escapeHtml(
            t("cfg.zoomReset")
          )}</button>
        </div>
      </div>
      <div class="cfg-legend">${legend}</div>
      <div class="cfg-wrap">${graph}</div>
    </section>`;
  }

  const CFG_NODE_W = 190;
  const CFG_NODE_H = 66;
  const CFG_GAP_X = 88;
  const CFG_GAP_Y = 22;
  const CFG_PAD = 22;

  function drawCfgSvg(data, zoom) {
    const blocks = data.blocks || [];
    const edges = data.edges || [];
    const byId = new Map();
    blocks.forEach((block) => byId.set(block.id, block));
    const outgoing = new Map();
    const incoming = new Map();
    blocks.forEach((block) => {
      outgoing.set(block.id, []);
      incoming.set(block.id, 0);
    });
    edges.forEach((edge) => {
      if (outgoing.has(edge.src)) outgoing.get(edge.src).push(edge);
      if (incoming.has(edge.dst)) incoming.set(edge.dst, incoming.get(edge.dst) + 1);
    });
    let entry = blocks.find((block) => block.is_entry);
    if (!entry) entry = blocks.find((block) => !incoming.get(block.id));
    if (!entry) entry = blocks[0];
    const depth = new Map();
    const queue = [];
    if (entry) {
      depth.set(entry.id, 0);
      queue.push(entry.id);
    }
    while (queue.length) {
      const id = queue.shift();
      const next = depth.get(id) + 1;
      for (const edge of outgoing.get(id) || []) {
        if (!byId.has(edge.dst) || depth.has(edge.dst)) continue;
        depth.set(edge.dst, next);
        queue.push(edge.dst);
      }
    }
    let maxDepth = 0;
    depth.forEach((value) => {
      if (value > maxDepth) maxDepth = value;
    });
    blocks.forEach((block) => {
      if (!depth.has(block.id)) {
        maxDepth += 1;
        depth.set(block.id, maxDepth);
      }
    });
    const columns = new Map();
    blocks.forEach((block) => {
      const column = depth.get(block.id);
      if (!columns.has(column)) columns.set(column, []);
      columns.get(column).push(block);
    });
    const depths = [...columns.keys()].sort((left, right) => left - right);
    depths.forEach((column) => {
      columns.get(column).sort((left, right) => {
        const leftIndex = Number(left.index ?? 0);
        const rightIndex = Number(right.index ?? 0);
        return leftIndex - rightIndex || String(left.id).localeCompare(String(right.id));
      });
    });
    const positions = new Map();
    let rows = 0;
    depths.forEach((column, columnIndex) => {
      const columnBlocks = columns.get(column);
      rows = Math.max(rows, columnBlocks.length);
      columnBlocks.forEach((block, rowIndex) => {
        positions.set(block.id, {
          x: CFG_PAD + columnIndex * (CFG_NODE_W + CFG_GAP_X),
          y: CFG_PAD + rowIndex * (CFG_NODE_H + CFG_GAP_Y),
        });
      });
    });
    const width =
      CFG_PAD * 2 +
      depths.length * CFG_NODE_W +
      Math.max(0, depths.length - 1) * CFG_GAP_X;
    const height = Math.max(
      160,
      CFG_PAD * 2 +
        rows * CFG_NODE_H +
        Math.max(0, rows - 1) * CFG_GAP_Y
    );
    const edgeHtml = edges
      .map((edge) => {
        const from = positions.get(edge.src);
        const to = positions.get(edge.dst);
        if (!from || !to) return "";
        const back =
          to.x < from.x || (to.x === from.x && to.y <= from.y);
        const startX = from.x + (back ? 0 : CFG_NODE_W);
        const startY = from.y + CFG_NODE_H / 2;
        const endX = to.x + (back ? CFG_NODE_W : 0);
        const endY = to.y + CFG_NODE_H / 2;
        const bend = Math.max(34, Math.abs(endX - startX) * 0.5);
        const control1 = back ? startX - bend : startX + bend;
        const control2 = back ? endX + bend : endX - bend;
        const path = `M ${startX} ${startY} C ${control1} ${startY}, ${control2} ${endY}, ${endX} ${endY}`;
        const kind =
          edge.label === "true"
            ? "true"
            : edge.label === "false"
            ? "false"
            : back
            ? "back"
            : "normal";
        const label =
          edge.label === "true" ? "T" : edge.label === "false" ? "F" : "";
        const title = `${edge.src} → ${edge.dst}${
          edge.label ? ` (${edge.label})` : ""
        }`;
        return `<g class="cfg-edge ${kind}"><path d="${path}" marker-end="url(#cfg-arrow-${kind})"><title>${escapeHtml(
          title
        )}</title></path>${
          label
            ? `<text x="${(startX + endX) / 2}" y="${
                (startY + endY) / 2 - 5
              }" text-anchor="middle">${label}</text>`
            : ""
        }</g>`;
      })
      .join("");
    const nodeHtml = blocks
      .map((block) => {
        const position = positions.get(block.id);
        if (!position) return "";
        const classes = ["cfg-node"];
        if (block.is_entry || (entry && block.id === entry.id)) classes.push("entry");
        if (block.is_exit) classes.push("exit");
        if (block.condition) classes.push("branch");
        const head = `#${block.index ?? 0} ${block.kind || ""}`;
        const sub = `L${block.line || 0} · ${block.statement_count || 0} stmt`;
        const condition = block.condition
          ? truncate(block.condition, 26)
          : "";
        return `<g class="${classes.join(" ")}"><title>${escapeHtml(
          `${block.kind || "block"} #${block.index ?? 0}${
            block.condition ? `: ${block.condition}` : ""
          }`
        )}</title><rect x="${position.x}" y="${position.y}" width="${CFG_NODE_W}" height="${CFG_NODE_H}" rx="8"></rect><text x="${
          position.x + 12
        }" y="${position.y + 19}">${escapeHtml(head)}</text><text class="cfg-sub" x="${
          position.x + 12
        }" y="${position.y + 35}">${escapeHtml(sub)}</text>${
          condition
            ? `<text class="cfg-condition" x="${
                position.x + 12
              }" y="${position.y + 51}">${escapeHtml(condition)}</text>`
            : ""
        }</g>`;
      })
      .join("");
    const defs = `<defs>
      <marker id="cfg-arrow-normal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>
      <marker id="cfg-arrow-true" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>
      <marker id="cfg-arrow-false" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>
      <marker id="cfg-arrow-back" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>
    </defs>`;
    const svg = `<svg class="cfg-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${Math.round(
      width * zoom
    )}" height="${Math.round(
      height * zoom
    )}" role="img" aria-label="${escapeHtml(
      t("cfg.aria", {
        name:
          (data.function && (data.function.name || data.function.id)) ||
          state.selectedSymbol,
      })
    )}">${defs}${edgeHtml}${nodeHtml}</svg>`;
    return svg;
  }


  async function loadGraph() {
    const symbol = state.selectedSymbol;
    if (!symbol) return;
    const parameters = state.graphParams;
    setStatus(t("status.buildingGraph"), "info");
    try {
      const data = await api(
        `/api/graph?symbol=${encodeURIComponent(symbol)}&direction=${
          parameters.direction
        }&depth=${parameters.depth}&limit=500`
      );
      if (state.selectedSymbol !== symbol) return;
      state.graph = data;
      clearStatus();
      renderGraphSurface();
    } catch (error) {
      setStatus(friendlyError(error), "error");
      const wrap = document.getElementById("graph-wrap");
      if (wrap) {
        wrap.innerHTML = `<div class="graph-empty">${escapeHtml(
          friendlyError(error)
        )}</div>`;
      }
    }
  }

  function drawGraph(data) {
    const wrap = document.getElementById("graph-wrap");
    if (!wrap) return;
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    if (!nodes.length) {
      wrap.innerHTML = `<div class="graph-empty">${escapeHtml(
        t("empty.graph")
      )}</div>`;
      return;
    }
    const NODE_W = 196;
    const NODE_H = 42;
    const GAP_X = 74;
    const GAP_Y = 16;
    const PAD = 18;
    const nodeKey = (node) =>
      node.id ? `id:${node.id}` : `name:${node.name || ""}`;
    const byId = new Map();
    const byName = new Map();
    nodes.forEach((node) => {
      if (node.id) byId.set(node.id, node);
      if (node.name && !byName.has(node.name)) byName.set(node.name, node);
    });
    const resolve = (id, name) =>
      (id && byId.get(id)) || (name && byName.get(name)) || null;
    // Direction-aware columns: callers (reverse walk) sit left of the root,
    // callees (forward walk) sit right. "both" therefore shows a caller column,
    // the root, and a callee column instead of collapsing every neighbour to
    // the right (which made forward and reverse look identical).
    const columns = new Map();
    const columnOf = (node) => {
      const depthValue = Number.isFinite(Number(node.depth))
        ? Number(node.depth)
        : 0;
      if (depthValue === 0) return 0;
      const itemDirection = String(node.direction || "");
      if (itemDirection === "reverse") return -depthValue;
      return depthValue; // forward or unknown
    };
    nodes.forEach((node) => {
      const column = columnOf(node);
      if (!columns.has(column)) columns.set(column, []);
      columns.get(column).push(node);
    });
    const depths = [...columns.keys()].sort((a, b) => a - b);
    const minColumn = Math.min(0, ...depths);
    const maxColumn = Math.max(0, ...depths);
    depths.forEach((column) => {
      columns
        .get(column)
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || "")));
    });
    const columnCount = maxColumn - minColumn + 1;
    const maxRows = Math.max(...depths.map((column) => columns.get(column).length));
    const width = PAD * 2 + columnCount * NODE_W + (columnCount - 1) * GAP_X;
    const height = Math.max(150, PAD * 2 + maxRows * NODE_H + (maxRows - 1) * GAP_Y);
    const positions = new Map();
    depths.forEach((column) => {
      columns.get(column).forEach((node, rowIndex) => {
        positions.set(nodeKey(node), {
          x: PAD + (column - minColumn) * (NODE_W + GAP_X),
          y: PAD + rowIndex * (NODE_H + GAP_Y),
          node,
        });
      });
    });

    const edgeSvg = [];
    edges.forEach((edge) => {
      const source = resolve(edge.caller_id, edge.caller_name);
      const target = resolve(edge.callee_id, edge.callee_name);
      if (!source || !target) return;
      const from = positions.get(nodeKey(source));
      const to = positions.get(nodeKey(target));
      if (!from || !to) return;
      const candidate = isCandidate(edge);
      const cycle = edge.cycle === true;
      const forward = to.x >= from.x;
      const x1 = forward ? from.x + NODE_W : from.x;
      const x2 = forward ? to.x : to.x + NODE_W;
      const y1 = from.y + NODE_H / 2;
      const y2 = to.y + NODE_H / 2;
      const bend = Math.max(36, Math.abs(x2 - x1) * 0.55);
      const c1 = forward ? x1 + bend : x1 - bend;
      const c2 = forward ? x2 - bend : x2 + bend;
      const path = `M ${x1} ${y1} C ${c1} ${y1}, ${c2} ${y2}, ${x2} ${y2}`;
      const marker = cycle ? "arrow-cycle" : candidate ? "arrow-candidate" : "arrow";
      edgeSvg.push(
        `<g class="graph-edge${candidate ? " candidate" : ""}${
          cycle ? " cycle" : ""
        }"><path d="${path}" marker-end="url(#${marker})"><title>${escapeHtml(
          `${edge.caller_name || edge.caller_id} → ${
            edge.callee_name || edge.callee_id
          }${candidate ? ` (${edge.kind || "candidate"} ${confidence(edge.confidence)})` : ""}`
        )}</title></path>${
          candidate
            ? `<text x="${(x1 + x2) / 2}" y="${
                (y1 + y2) / 2 - 5
              }" text-anchor="middle">${escapeHtml(
                `${edge.kind || "candidate"} ${confidence(edge.confidence)}`
              )}</text>`
            : ""
        }</g>`
      );
    });

    const nodeSvg = [];
    depths.forEach((depthValue) => {
      columns.get(depthValue).forEach((node) => {
        const position = positions.get(nodeKey(node));
        const isRoot = Number(node.depth) === 0;
        const sub = [
          location(node),
          node.shard
            ? t("common.shardName", { name: node.shard })
            : "",
        ]
          .filter(Boolean)
          .join(" · ");
        nodeSvg.push(
          `<g class="graph-node${isRoot ? " root" : ""}" data-source-open="${escapeHtml(
            node.id || node.name || ""
          )}" role="button" tabindex="0"><rect x="${position.x}" y="${
            position.y
          }" width="${NODE_W}" height="${NODE_H}" rx="7"></rect><text x="${
            position.x + 11
          }" y="${position.y + 17}">${escapeHtml(
            truncate(node.name || node.id || "?", 26)
          )}</text><text class="node-sub" x="${position.x + 11}" y="${
            position.y + 31
          }">${escapeHtml(truncate(sub, 34))}</text><title>${escapeHtml(
            `${node.name || node.id}${sub ? ` — ${sub}` : ""}`
          )}</title></g>`
        );
      });
    });

    const defs = `<defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>
      <marker id="arrow-candidate" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>
      <marker id="arrow-cycle" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>
    </defs>`;
    wrap.innerHTML = `<svg class="graph-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="${escapeHtml(
      t("graph.aria", { root: data.root || "" })
    )}">${defs}${edgeSvg.join("")}${nodeSvg.join("")}</svg>`;
    markSelectedGraphNode(
      (state.nodeSource && state.nodeSource.symbol) || ""
    );
  }

  function applyGraphControls() {
    const controls = els.view.querySelector(".graph-controls");
    if (!controls) return;
    const direction = controls.querySelector('[name="graph-direction"]');
    const depth = controls.querySelector('[name="graph-depth"]');
    state.graphParams.direction = direction ? direction.value : "both";
    state.graphParams.depth = depth ? Number(depth.value) : 1;
    state.graph = null;
    renderGraphSurface();
  }

  // ----------------------------------------------------------------- stats
  async function showStats(options = {}) {
    showView("view");
    renderLoading(t("status.loadingStats"));
    setStatus(t("status.loadingStats"), "info");
    if (!options.hash) navigate("#/stats", { push: true });
    try {
      const data = await api("/api/stats");
      state.stats = data;
      renderStats(data);
      clearStatus();
    } catch (error) {
      setStatus(friendlyError(error), "error");
      els.view.innerHTML = `<div class="fn-head"><h1 class="fn-title">${escapeHtml(
        t("stats.title")
      )}</h1></div>${errorPanel(
        t("error.loadStats"),
        error
      )}`;
    }
  }

  function renderStats(data) {
    const tables = data.tables || {};
    const database = data.database || {};
    const callbacks = data.callbacks || null;
    const cards = [
      card(
        t("stats.cardSize"),
        bytes(data.db_bytes),
        database.federated
          ? t("stats.shardsSub", {
              count: number((database.shards || []).length),
            })
          : t("common.singleDatabase")
      ),
      card(t("stats.cardFunctions"), number(tables.functions), t("stats.indexedFunctions")),
      card(t("stats.cardCalls"), number(tables.calls), t("stats.callSites")),
      card(t("stats.cardBranches"), number(tables.branches), t("stats.branchConditions")),
      card(
        t("stats.cardSchema"),
        String(data.schema_version || "—"),
        `SQLite ${data.sqlite_version || "?"}`
      ),
    ];
    if (callbacks) {
      cards.push(
        card(
          t("stats.cardCallbacks"),
          number(callbacks.registrations),
          t("stats.linkedCandidates", {
            count: number(callbacks.linked_call_targets),
          })
        )
      );
    }
    const skipped = database.skipped || [];
    const skippedHtml = skipped.length
      ? `<div class="callout"><span><strong>${escapeHtml(
          t("error.shardSkipped", { count: skipped.length })
        )}</strong> ${escapeHtml(
          skipped
            .map((item) => `${item.path || "?"}: ${item.error || "unreadable"}`)
            .join("; ")
        )}</span></div>`
      : "";
    const tableRows = Object.entries(tables)
      .sort((a, b) => Number(b[1]) - Number(a[1]) || a[0].localeCompare(b[0]))
      .map(
        ([name, count]) =>
          `<tr><td class="mono">${escapeHtml(name)}</td><td class="num">${number(
            count
          )}</td></tr>`
      )
      .join("");
    const shardRows = (data.shards || [])
      .map((shard) => {
        const shardTables = shard.tables || {};
        return `<tr><td class="mono">${escapeHtml(shard.shard || "?")}</td>
          <td class="num">${bytes(shard.db_bytes)}</td>
          <td class="num">${escapeHtml(String(shard.schema_version || "—"))}</td>
          <td class="num">${number(shardTables.functions)}</td>
          <td class="num">${number(shardTables.calls)}</td>
          <td class="num">${number(shardTables.callback_registrations)}</td></tr>`;
      })
      .join("");
    const callbackKindRows = callbacks
      ? Object.entries(callbacks.by_kind || {})
          .map(
            ([kind, count]) =>
              `<tr><td class="mono">${escapeHtml(kind)}</td><td class="num">${number(
                count
              )}</td></tr>`
          )
          .join("")
      : "";
    const databasePaths = (database.paths || [])
      .map((path) => `<code>${escapeHtml(path)}</code>`)
      .join("<br>");
    const sections = [
      `<div class="fn-head"><h1 class="fn-title">${escapeHtml(t("stats.title"))}</h1>
        <p class="fn-sub">${escapeHtml(
          database.federated
            ? t("stats.subFederated", {
                count: (database.shards || []).length,
              })
            : t("common.singleDatabase")
        )}</p></div>`,
      `<div class="cards">${cards.join("")}</div>`,
      skippedHtml,
      `<section class="section"><div class="section-head"><h3>${escapeHtml(
        t("section.sources")
      )}</h3><span class="note">${escapeHtml(
        t("section.sourcesNote")
      )}</span></div><div class="section-body" style="padding:12px 16px">${databasePaths}</div></section>`,
      `<section class="section"><div class="section-head"><h3>${escapeHtml(
        t("section.tableRows")
      )}</h3><span class="note">${escapeHtml(
        t("section.tableRowsNote")
      )}</span></div><div class="section-body"><div class="table-wrap">${table(
        [t("table.table"), t("table.rows")],
        [tableRows]
      )}</div></div></section>`,
    ];
    if (shardRows) {
      sections.push(
        `<section class="section"><div class="section-head"><h3>${escapeHtml(
          t("section.shards")
        )}</h3><span class="note">${escapeHtml(
          t("section.shardsNote")
        )}</span></div><div class="section-body">${table(
          [
            t("table.shard"),
            t("table.size"),
            t("table.schema"),
            t("table.functions"),
            t("table.calls"),
            t("table.callbacks"),
          ],
          [shardRows]
        )}</div></section>`
      );
    }
    if (callbacks) {
      sections.push(
        `<section class="section"><div class="section-head"><h3>${escapeHtml(
          t("section.callbacks")
        )}</h3><span class="note">${escapeHtml(
          t("section.callbacksNote")
        )}</span></div><div class="section-body">${table(
          [t("table.registrationKind"), t("table.count")],
          [
            callbackKindRows ||
              emptyRow(2, t("empty.callbackKinds")),
          ]
        )}</div></section>`
      );
    }
    els.view.innerHTML = sections.join("");
  }

  // ------------------------------------------------------------- callbacks
  async function showCallbacks(filters, options = {}) {
    if (filters) {
      state.callbacksFilters = {
        path: filters.path ?? state.callbacksFilters.path,
        field: filters.field ?? state.callbacksFilters.field,
        module: filters.module ?? state.callbacksFilters.module,
      };
    }
    const active = state.callbacksFilters;
    showView("view");
    renderLoading(t("status.loadingCallbacks"));
    if (!options.hash) navigate("#/callbacks", { push: true });
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (active.path) params.set("path", active.path);
      if (active.field) params.set("field", active.field);
      if (active.module) params.set("module", active.module);
      const [data, stats] = await Promise.all([
        api(`/api/callbacks?${params.toString()}`),
        api("/api/stats").catch(() => null),
      ]);
      state.callbacks = data;
      if (stats) state.stats = stats;
      renderCallbacks(data, stats);
      clearStatus();
    } catch (error) {
      setStatus(friendlyError(error), "error");
      els.view.innerHTML = `<div class="fn-head"><h1 class="fn-title">${escapeHtml(
        t("callbacks.title")
      )}</h1></div>${errorPanel(
        t("error.loadCallbacks"),
        error
      )}`;
    }
  }

  function renderCallbacks(data, stats) {
    const rows = data.items || [];
    const totals = (stats && stats.callbacks) || {};
    const cards = [
      card(
        t("callbacks.cardRegistrations"),
        number(totals.registrations ?? rows.length),
        t("callbacks.functionPointerFacts")
      ),
      card(
        t("callbacks.cardTargets"),
        number(totals.targets),
        t("callbacks.distinctTargets")
      ),
      card(
        t("callbacks.cardPaths"),
        number(totals.object_paths),
        t("callbacks.pathsDesc")
      ),
      card(
        t("callbacks.cardLinked"),
        number(totals.linked_call_targets),
        t("callbacks.linkedDesc")
      ),
    ];
    const filtersHtml = `<form class="filters" id="callbacks-filters">
      <input name="path" type="search" placeholder="${escapeHtml(
        t("callbacks.filterPath")
      )}" value="${escapeHtml(
        state.callbacksFilters.path
      )}" aria-label="${escapeHtml(t("callbacks.ariaPath"))}">
      <input name="field" type="search" placeholder="${escapeHtml(
        t("callbacks.filterField")
      )}" value="${escapeHtml(
        state.callbacksFilters.field
      )}" aria-label="${escapeHtml(t("callbacks.ariaField"))}">
      <input name="module" type="search" placeholder="${escapeHtml(
        t("callbacks.filterModule")
      )}" value="${escapeHtml(
        state.callbacksFilters.module
      )}" aria-label="${escapeHtml(t("callbacks.ariaModule"))}">
      <button type="submit" class="primary">${escapeHtml(
        t("callbacks.apply")
      )}</button>
      <button type="button" class="ghost" id="callbacks-clear">${escapeHtml(
        t("callbacks.clear")
      )}</button>
    </form>`;
    const body = rows.length
      ? rows
          .map((row) => {
            const evidence = (row.evidence || []).join(" | ");
            return `<tr>
              <td class="mono" title="${escapeHtml(evidence)}">${escapeHtml(
                row.object_path || "—"
              )}</td>
              <td>${functionLink(row.target_function_name, row.target_function_id)}</td>
              <td>${escapeHtml(row.registration_kind || "")}</td>
              <td class="num">${confidence(row.confidence)}</td>
              <td class="mono muted">${escapeHtml(
                row.file ? `${row.file}:${row.line || 0}` : "—"
              )}</td>
              <td>${escapeHtml(row.module || "")}</td>
              <td class="mono faint">${escapeHtml(row.registrar_function_id || "")}</td>
              <td>${shardBadge(row)}</td>
            </tr>`;
          })
          .join("")
      : emptyRow(8, t("empty.callbacks"));
    const note = [
      data.federated ? t("common.federated") : t("common.singleDatabase"),
      t("note.rowsShown", { count: number(data.count) }),
      t("note.callbackCandidates"),
    ].join(" · ");
    els.view.innerHTML = [
      `<div class="fn-head"><h1 class="fn-title">${escapeHtml(
        t("callbacks.title")
      )}</h1>
        <p class="fn-sub">${escapeHtml(t("callbacks.subtitle"))}</p></div>`,
      `<div class="cards">${cards.join("")}</div>`,
      `<section class="section"><div class="section-head"><h3>${escapeHtml(
        t("callbacks.cardRegistrations")
      )}</h3><span class="note">${escapeHtml(
        note
      )}</span></div>${filtersHtml}<div class="section-body"><div class="table-wrap">${table(
        [
          t("table.objectPath"),
          t("table.target"),
          t("table.kind"),
          t("table.confidence"),
          t("table.location"),
          t("table.module"),
          t("table.registrar"),
          t("table.shard"),
        ],
        [body]
      )}</div></div></section>`,
    ].join("");
  }

  // --------------------------------------------------------------- welcome
  function renderWelcomeCards(health) {
    if (!health) return;
    const database = health.database || {};
    const counts = database.counts || {};
    const cards = [
      card(
        t("stats.cardFunctions"),
        number(counts.functions),
        database.federated
          ? t("common.federatedShards", {
              count: (database.shards || []).length,
            })
          : t("common.singleDatabase")
      ),
      card(t("stats.cardCalls"), number(counts.calls), t("stats.callSites")),
      card(
        t("stats.cardCallbacks"),
        number(counts.callback_registrations),
        t("welcome.registrationFacts")
      ),
      card(
        t("stats.cardSize"),
        bytes(database.db_bytes),
        `Schema ${database.schema_version || "?"}`
      ),
    ];
    els.welcomeCards.innerHTML = cards.join("");
    const errorBox = document.getElementById("welcome-error");
    if (!health.db_ok) {
      errorBox.hidden = false;
      errorBox.textContent = t("error.dbUnavailable", {
        message:
          (health.error && health.error.message) || t("error.unknown"),
      });
    } else {
      errorBox.hidden = true;
    }
  }

  function renderDbInfo(health) {
    if (!health) {
      els.dbInfo.textContent = t("db.unavailable");
      return;
    }
    const database = health.database || {};
    const counts = database.counts || {};
    const pieces = [
      badge(
        database.federated ? t("common.federated") : t("common.singleDb"),
        database.federated ? "shard" : "info"
      ),
      badge(
        health.status === "ok"
          ? t("status.ok")
          : health.status === "degraded"
          ? t("status.degraded")
          : String(health.status || t("error.unknown")),
        health.db_ok ? "definition" : "danger"
      ),
      `<span>${escapeHtml(
        t("common.functionCount", { count: number(counts.functions) })
      )}</span>`,
      `<span>${escapeHtml(
        t("common.callCount", { count: number(counts.calls) })
      )}</span>`,
      `<span>${bytes(database.db_bytes)}</span>`,
    ];
    if (database.federated) {
      pieces.push(
        `<span class="badge shard">${escapeHtml(
          (database.shards || []).join(", ")
        )}</span>`
      );
    }
    els.dbInfo.innerHTML = pieces.join(" ");
    els.footerDb.textContent = (database.paths || []).join(" · ");
  }

  function showWelcome(options = {}) {
    showView("welcome");
    if (!options.hash) navigate("#/", { push: true });
  }

  async function loadHealth() {
    try {
      const health = await api("/api/health");
      state.health = health;
      renderDbInfo(health);
      renderWelcomeCards(health);
      if (!health.db_ok) {
        setStatus(
          t("error.dbUnavailable", {
            message:
              (health.error && health.error.message) || t("error.unknown"),
          }),
          "error"
        );
      }
    } catch (error) {
      renderDbInfo(null);
      setStatus(
        t("error.unreachableServer", { message: friendlyError(error) }),
        "error"
      );
    }
  }

  // ---------------------------------------------------------------- events
  function onViewClick(event) {
    const sourceNode = event.target.closest(".graph-node[data-source-open]");
    if (sourceNode && sourceNode.dataset.sourceOpen) {
      event.preventDefault();
      const symbol = sourceNode.dataset.sourceOpen;
      window.clearTimeout(nodeClickTimer);
      nodeClickTimer = window.setTimeout(() => {
        showNodeSource(symbol);
      }, 250);
      return;
    }
    if (event.target.closest("#sidebar-toggle")) {
      toggleColumn("left");
      return;
    }
    if (event.target.closest("#toggle-left")) {
      toggleColumn("left");
      return;
    }
    if (event.target.closest("#toggle-right")) {
      toggleColumn("right");
      return;
    }
    if (event.target.closest("#layout-reset")) {
      resetLayout();
      return;
    }
    if (event.target.closest("#explain-refresh")) {
      loadExplain(true);
      return;
    }
    if (event.target.closest("#explain-copy")) {
      copyExplain();
      return;
    }
    if (event.target.closest("#source-collapse")) {
      toggleTopSource();
      return;
    }
    if (event.target.closest("#node-source-close")) {
      clearNodeSource();
      return;
    }
    if (event.target.closest("#fn-back")) {
      goBackToPreviousFunction();
      return;
    }
    const jumpButton = event.target.closest("[data-jump]");
    if (jumpButton && jumpButton.dataset.jump) {
      event.preventDefault();
      jumpToFunction(jumpButton.dataset.jump);
      return;
    }
    if (event.target.closest("#cfg-zoom-in")) {
      setCfgZoom(state.cfgZoom + 0.2);
      return;
    }
    if (event.target.closest("#cfg-zoom-out")) {
      setCfgZoom(state.cfgZoom - 0.2);
      return;
    }
    if (event.target.closest("#cfg-zoom-reset")) {
      setCfgZoom(1);
      return;
    }
    const openButton = event.target.closest("[data-open]");
    if (openButton && openButton.dataset.open) {
      event.preventDefault();
      openFunction(openButton.dataset.open);
      return;
    }
    const sectionToggle = event.target.closest(
      ".accordion-header[data-section-toggle]"
    );
    if (sectionToggle) {
      setSection(sectionToggle.dataset.sectionToggle, { toggle: true });
      return;
    }
    if (event.target.closest("#graph-refresh")) {
      applyGraphControls();
      return;
    }
    if (event.target.closest("#callbacks-clear")) {
      state.callbacksFilters = { path: "", field: "", module: "" };
      showCallbacks();
    }
  }

  function onViewDoubleClick(event) {
    const sourceNode = event.target.closest(".graph-node[data-source-open]");
    if (!sourceNode || !sourceNode.dataset.sourceOpen) return;
    event.preventDefault();
    window.clearTimeout(nodeClickTimer);
    nodeClickTimer = null;
    jumpToFunction(sourceNode.dataset.sourceOpen);
  }

  function onViewChange(event) {
    if (event.target.closest(".graph-controls")) {
      applyGraphControls();
    }
  }

  function onViewSubmit(event) {
    const form = event.target.closest("#callbacks-filters");
    if (!form) return;
    event.preventDefault();
    const data = new FormData(form);
    state.callbacksFilters = {
      path: String(data.get("path") || ""),
      field: String(data.get("field") || ""),
      module: String(data.get("module") || ""),
    };
    showCallbacks();
  }

  function renderLocalizedView() {
    if (state.health) {
      renderDbInfo(state.health);
      renderWelcomeCards(state.health);
    }
    if (state.resultsQuery) {
      renderResults({ query: state.resultsQuery, items: state.results });
    } else {
      els.resultsEmpty.textContent = t("search.startHint");
    }
    if (state.route === "function" && state.detail) {
      renderFunction();
    } else if (state.route === "stats" && state.stats) {
      renderStats(state.stats);
    } else if (state.route === "callbacks" && state.callbacks) {
      renderCallbacks(state.callbacks, state.stats);
    }
    clearStatus();
  }

  function bindEvents() {
    els.searchForm.addEventListener("submit", (event) => {
      event.preventDefault();
      runSearch(els.searchInput.value);
    });
    els.searchInput.addEventListener("input", () => {
      const value = els.searchInput.value;
      window.clearTimeout(searchTimer);
      if (!value.trim()) {
        state.results = [];
        state.resultsQuery = "";
        els.results.innerHTML = "";
        els.resultsCount.textContent = "";
        els.resultsEmpty.hidden = false;
        els.resultsEmpty.textContent = t("search.startHint");
        hideSearchDropdown();
        return;
      }
      searchTimer = window.setTimeout(() => runSearch(value), 320);
    });
    els.results.addEventListener("click", (event) => {
      const button = event.target.closest(".result");
      if (!button) return;
      hideSearchDropdown();
      openFunction(button.dataset.symbol || button.dataset.name);
    });
    els.statsButton.addEventListener("click", () => showStats());
    els.callbacksButton.addEventListener("click", () => showCallbacks());
    els.brand.addEventListener("click", (event) => {
      event.preventDefault();
      showWelcome();
    });
    els.view.addEventListener("click", onViewClick);
    els.view.addEventListener("dblclick", onViewDoubleClick);
    els.view.addEventListener("pointerdown", (event) => {
      const splitter = event.target.closest(".splitter");
      if (!splitter) return;
      beginSplitterDrag(splitterKind(splitter), event);
    });
    els.view.addEventListener("dblclick", (event) => {
      const splitter = event.target.closest(".splitter");
      if (!splitter) return;
      event.preventDefault();
      resetSplitter(splitterKind(splitter));
    });
    window.addEventListener("resize", () => applyLayout());
    els.view.addEventListener("change", onViewChange);
    els.view.addEventListener("submit", onViewSubmit);
    els.view.addEventListener("keydown", (event) => {
      const splitter = event.target.closest(".splitter");
      if (splitter) {
        const kind = splitterKind(splitter);
        let delta = 0;
        if (kind === "top" && event.key === "ArrowUp") delta = -16;
        if (kind === "top" && event.key === "ArrowDown") delta = 16;
        if (kind === "left" && event.key === "ArrowLeft") delta = -16;
        if (kind === "left" && event.key === "ArrowRight") delta = 16;
        if (kind === "right" && event.key === "ArrowLeft") delta = 16;
        if (kind === "right" && event.key === "ArrowRight") delta = -16;
        if (delta) {
          event.preventDefault();
          nudgeSplitter(kind, delta);
        }
        return;
      }
      if (
        (event.key === "Enter" || event.key === " ") &&
        event.target.classList &&
        event.target.classList.contains("graph-node")
      ) {
        event.preventDefault();
        if (event.target.dataset.sourceOpen) {
          showNodeSource(event.target.dataset.sourceOpen);
        }
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".search-wrap")) hideSearchDropdown();
    });
    window.addEventListener("hashchange", () => {
      applyRoute().catch((error) => setStatus(friendlyError(error), "error"));
    });
    document.addEventListener("ca:languagechange", () => {
      renderLocalizedView();
    });
    document.addEventListener("keydown", (event) => {
      if (
        event.altKey &&
        (event.key === "ArrowLeft" || event.key === "Backspace")
      ) {
        event.preventDefault();
        goBackToPreviousFunction();
        return;
      }
      const tag = (event.target.tagName || "").toLowerCase();
      const typing = tag === "input" || tag === "select" || tag === "textarea";
      if (event.key === "/" && !typing) {
        event.preventDefault();
        els.searchInput.focus();
        els.searchInput.select();
      } else if (event.key === "Escape") {
        if (typing) {
          els.searchInput.value = "";
          els.searchInput.blur();
          state.results = [];
          els.results.innerHTML = "";
          els.resultsCount.textContent = "";
          els.resultsEmpty.hidden = false;
          hideSearchDropdown();
          clearStatus();
        }
      }
    });
  }

  // ------------------------------------------------------------------ init
  async function init() {
    els.searchForm = document.getElementById("search-form");
    els.searchInput = document.getElementById("search-input");
    els.results = document.getElementById("results");
    els.resultsCount = document.getElementById("results-count");
    els.searchDropdown = document.getElementById("search-dropdown");
    els.resultsEmpty = document.getElementById("results-empty");
    els.status = document.getElementById("status");
    els.welcome = document.getElementById("welcome");
    els.welcomeCards = document.getElementById("welcome-cards");
    els.view = document.getElementById("view");
    els.dbInfo = document.getElementById("db-info");
    els.footerDb = document.getElementById("footer-db");
    els.statsButton = document.getElementById("stats-button");
    els.callbacksButton = document.getElementById("callbacks-button");
    els.brand = document.getElementById("brand");
    bindEvents();
    await loadHealth();
    await applyRoute();
  }

  // Small read-only hooks so tests can exercise the jump history without a DOM.
  window.CA_UI_TEST = {
    pushFunctionHistory,
    popFunctionHistory,
    clearFunctionHistory,
    historyLength: () => functionHistory.length,
    historyEntries: () => functionHistory.map((entry) => ({ ...entry })),
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
