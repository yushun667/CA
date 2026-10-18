/* CA function detail page (DSH client plugin).
 *
 * Ported from the CA web UI function page (src/ca/ui/static/app.js):
 *  - fixed top panel with the complete function source (line numbers, syntax
 *    highlighting, function range highlight, collapsible, independent scroll);
 *  - left accordion with overview / branches (embedded CFG) / returns /
 *    resources / findings and count badges;
 *  - middle call graph (ui-graph component) and right graph-node source panel;
 *  - three resizable panes with persisted sizes (`ca.ui.layout`), narrow-screen
 *    stacking, session jump history (`ca.ui.fnHistory`) and the
 *    Alt+Left / Alt+Backspace shortcuts.
 *
 * Data comes from the authenticated `/api/*` passthrough through
 * `bag.api.caRequest`; the hash route (`#/fn/<symbol>/<tab>`) is owned by the
 * shell, which passes `{route, navigate}` to this page.
 */

import { createCallGraph } from "../graph/index.js";
import { createCfgGraph } from "../cfg/index.js";
import { createExplainPanel } from "../explain/index.js";
import { createKit } from "./ui-kit.js";
import { createT } from "./i18n-keys.js";
import { FUNCTION_CSS } from "./styles.js";
import { createSplitter } from "./splitter.js";
import { createSourceViews } from "./source.js";
import { createAccordion } from "./accordion.js";
import { highlightRows } from "./highlight.js";
import {
  asArray,
  callSiteForNode,
  createAbortController,
  createRequester,
  errorKind,
  functionKey,
  functionLocation,
  isAbortError,
  normalizeTab,
  requestDetail,
  requestSource,
  shardOf,
  TAB_IDS,
  TAB_LABEL_KEYS,
} from "./data.js";
import {
  clampLayout,
  defaultLayout,
  LAYOUT_MIN,
  layoutStyle,
  readStoredLayout,
  saveStoredLayout,
} from "./layout.js";
import {
  loadHistory,
  popEntry,
  pushEntry,
  saveHistory,
} from "./history.js";

const REMEMBERED_TAB_KEY = "ca.ui.fn.accordion";

const noop = () => {};

function localStore() {
  try {
    if (typeof window === "undefined" || !window.localStorage) return null;
    return window.localStorage;
  } catch (_error) {
    return null;
  }
}

function readRememberedTab() {
  const store = localStore();
  if (!store) return "";
  try {
    return normalizeTab(store.getItem(REMEMBERED_TAB_KEY));
  } catch (_error) {
    return "";
  }
}

function saveRememberedTab(tab) {
  const store = localStore();
  if (!store) return;
  try {
    store.setItem(REMEMBERED_TAB_KEY, normalizeTab(tab));
  } catch (_error) {
    /* storage can be blocked; the accordion still works for this page */
  }
}

function viewportSize() {
  if (typeof window === "undefined") return { width: 0, height: 0 };
  return {
    width: Number(window.innerWidth) || 0,
    height: Number(window.innerHeight) || 0,
  };
}

function prefersNarrow() {
  if (typeof window === "undefined") return false;
  try {
    if (typeof window.matchMedia === "function") {
      return window.matchMedia("(max-width: 1099px)").matches;
    }
  } catch (_error) {
    /* matchMedia can be unavailable in tests */
  }
  return viewportSize().width > 0 && viewportSize().width < 1100;
}

function variantIdOf(state) {
  if (!state || typeof state !== "object") return "";
  const registry = state.registry;
  const id =
    registry && typeof registry === "object"
      ? registry.active_variant_id || (registry.active_variant && registry.active_variant.id)
      : "";
  return id === null || id === undefined ? "" : String(id);
}

function activeVariantId(store) {
  if (!store) return null;
  try {
    const id = variantIdOf(store.get());
    return id || null;
  } catch (_error) {
    return null;
  }
}

function measureNode(ref) {
  const node = ref && ref.current;
  if (!node) return 0;
  try {
    if (typeof node.getBoundingClientRect === "function") {
      return Math.round(node.getBoundingClientRect().width);
    }
    return Math.round(Number(node.clientWidth) || 0);
  } catch (_error) {
    return 0;
  }
}

function measureHeight(ref) {
  const node = ref && ref.current;
  if (!node) return 0;
  try {
    if (typeof node.getBoundingClientRect === "function") {
      return Math.round(node.getBoundingClientRect().height);
    }
    return Math.round(Number(node.clientHeight) || 0);
  } catch (_error) {
    return 0;
  }
}

/**
 * Create the function-detail page component.
 *
 * `bag` is the shell-provided `{ui, api, i18n}`; the returned component takes
 * `{route, navigate}` where route is `{page:'function', symbol, tab}`.
 */
export function createFunctionPage(React, bag) {
  const ctx = bag || {};
  const ui = ctx.ui || {};
  const api = ctx.api || {};
  const t = createT(ctx.i18n);
  const kit = createKit(React, ui, t);
  const request = createRequester(ctx);
  const Splitter = createSplitter(React);
  const sourceViews = createSourceViews(React, { kit, t, highlightRows });
  const accordion = createAccordion(React, { kit, t, highlightRows });
  const CallGraph = createCallGraph(React, bag);
  const CfgGraph = createCfgGraph(React, bag);
  const ExplainPanel = createExplainPanel(React, bag);
  const stylesHook = typeof ui.useStyles === "function" ? ui.useStyles : null;

  const { useState, useEffect, useRef } = React;

  function Badge(props) {
    if (
      props.children === null ||
      props.children === undefined ||
      props.children === ""
    ) {
      return null;
    }
    return React.createElement(
      kit.Badge,
      { tone: props.tone || "neutral", title: props.title },
      props.children
    );
  }

  function countFor(id, detail) {
    if (!detail) return null;
    switch (id) {
      case "branches":
        return asArray(detail.branches).length;
      case "returns":
        return asArray(detail.returns).length;
      case "resources":
        return asArray(detail.resource_ops).length;
      case "findings":
        return asArray(detail.findings).length;
      default:
        return null;
    }
  }

  return function FunctionPage(props) {
    (stylesHook || noop)(FUNCTION_CSS);

    const route = (props && props.route) || {};
    const navigate =
      props && typeof props.navigate === "function" ? props.navigate : noop;
    const symbol = String(route.symbol || "").trim();

    const [detailState, setDetailState] = useState({
      status: "loading",
      symbol,
      data: null,
      error: null,
    });
    const [sourceState, setSourceState] = useState({
      status: "loading",
      symbol,
      data: null,
      error: null,
    });
    const [nodeState, setNodeState] = useState({
      symbol: "",
      status: "idle",
      data: null,
      error: null,
    });
    const [selectedNode, setSelectedNode] = useState("");
    const [graphData, setGraphData] = useState(null);
    const [tab, setTab] = useState(() => normalizeTab(route.tab) || readRememberedTab());
    const [layout, setLayout] = useState(readStoredLayout);
    const [history, setHistory] = useState(loadHistory);
    const [sourceCollapsed, setSourceCollapsed] = useState(false);
    const [refreshKey, setRefreshKey] = useState(0);
    const [, setViewportTick] = useState(0);
    const [narrow, setNarrow] = useState(prefersNarrow);

    const headRef = useRef(null);
    const workspaceRef = useRef(null);
    const sidebarRef = useRef(null);
    const panelRef = useRef(null);
    const lastRouteRef = useRef({ symbol: route.symbol, tab: route.tab });
    const historyRef = useRef(history);
    historyRef.current = history;

    /* ------------------------------------------------------- data loading */

    useEffect(() => {
      const target = symbol;
      const controller = createAbortController();
      const signal = controller ? controller.signal : undefined;
      let alive = true;
      if (!target) {
        setDetailState({
          status: "error",
          symbol: "",
          data: null,
          error: { code: "NOT_FOUND", message: "No function symbol in route" },
        });
        setSourceState({ status: "idle", symbol: "", data: null, error: null });
        return undefined;
      }
      setDetailState({ status: "loading", symbol: target, data: null, error: null });
      setSourceState({ status: "loading", symbol: target, data: null, error: null });
      setSelectedNode("");
      setNodeState({ symbol: "", status: "idle", data: null, error: null });
      setGraphData(null);

      requestDetail(request, target, signal).then(
        (data) => {
          if (!alive) return;
          setDetailState({ status: "ready", symbol: target, data, error: null });
        },
        (error) => {
          if (!alive || isAbortError(error)) return;
          setDetailState({ status: "error", symbol: target, data: null, error });
        }
      );
      requestSource(request, target, signal).then(
        (data) => {
          if (!alive) return;
          setSourceState({ status: "ready", symbol: target, data, error: null });
        },
        (error) => {
          if (!alive || isAbortError(error)) return;
          setSourceState({ status: "error", symbol: target, data: null, error });
        }
      );
      return function cancel() {
        alive = false;
        if (controller) {
          try {
            controller.abort();
          } catch (_error) {
            /* already aborted */
          }
        }
      };
    }, [symbol, refreshKey]);

    useEffect(() => {
      const target = String(selectedNode || "").trim();
      if (!target) {
        setNodeState({ symbol: "", status: "idle", data: null, error: null });
        return undefined;
      }
      const controller = createAbortController();
      const signal = controller ? controller.signal : undefined;
      let alive = true;
      setNodeState({ symbol: target, status: "loading", data: null, error: null });
      requestSource(request, target, signal).then(
        (data) => {
          if (!alive) return;
          setNodeState({ symbol: target, status: "ready", data, error: null });
        },
        (error) => {
          if (!alive || isAbortError(error)) return;
          setNodeState({ symbol: target, status: "error", data: null, error });
        }
      );
      return function cancel() {
        alive = false;
        if (controller) {
          try {
            controller.abort();
          } catch (_error) {
            /* already aborted */
          }
        }
      };
    }, [selectedNode, refreshKey]);

    /* -------------------------------------------- route / state persistence */

    useEffect(() => {
      const previous = lastRouteRef.current;
      if (previous.symbol === route.symbol && previous.tab === route.tab) {
        // Mount and no-op re-renders: keep the remembered/optimistic section.
        return;
      }
      lastRouteRef.current = { symbol: route.symbol, tab: route.tab };
      setTab(normalizeTab(route.tab));
    }, [route.tab, route.symbol]);

    useEffect(() => {
      saveRememberedTab(tab);
    }, [tab]);

    useEffect(() => {
      saveStoredLayout(layout);
    }, [layout]);

    useEffect(() => {
      function onResize() {
        const size = viewportSize();
        setNarrow(prefersNarrow());
        setViewportTick((tick) => tick + 1);
        setLayout((current) =>
          clampLayout(current, {
            viewportWidth: size.width,
            viewportHeight: size.height,
            workspaceWidth:
              (workspaceRef.current && workspaceRef.current.clientWidth) ||
              Math.max(0, size.width - 40),
          })
        );
      }
      if (typeof window === "undefined") return undefined;
      window.addEventListener("resize", onResize);
      onResize();
      return function cleanup() {
        window.removeEventListener("resize", onResize);
      };
    }, []);

    /* -------------------------------------------- variant switch + shortcut */

    useEffect(() => {
      // The shell's top-bar selector switches the backend's active variant;
      // refresh this page's data when the active variant id actually changes
      // (the variant store also notifies on periodic registry polls).
      const store = api.variant;
      let dispose = null;
      let lastVariantId = activeVariantId(store);
      if (store && typeof store.subscribe === "function") {
        try {
          dispose = store.subscribe(function onVariantChange(next) {
            const nextId = variantIdOf(next);
            if (lastVariantId !== null && nextId && nextId !== lastVariantId) {
              lastVariantId = nextId;
              setRefreshKey((key) => key + 1);
              return;
            }
            lastVariantId = nextId || lastVariantId;
          });
        } catch (_error) {
          /* the shell may not expose the store; the page still works */
        }
      }
      function onVariantEvent() {
        setRefreshKey((key) => key + 1);
      }
      if (typeof window !== "undefined") {
        window.addEventListener("ca:variantchange", onVariantEvent);
      }
      return function cleanup() {
        if (typeof dispose === "function") dispose();
        if (typeof window !== "undefined") {
          window.removeEventListener("ca:variantchange", onVariantEvent);
        }
      };
    }, []);

    function goBack() {
      const list = historyRef.current || [];
      if (!list.length) return;
      const result = popEntry(list);
      if (!result.entry) return;
      setHistory(result.list);
      saveHistory(result.list);
      setSelectedNode("");
      navigate({
        page: "function",
        symbol: result.entry.symbol,
        tab: result.entry.tab || "",
      });
    }

    const goBackRef = useRef(noop);
    goBackRef.current = goBack;

    useEffect(() => {
      function onKeyDown(event) {
        if (!event || !event.altKey) return;
        if (event.key !== "ArrowLeft" && event.key !== "Backspace") return;
        const list = historyRef.current || [];
        if (!list.length) return;
        if (typeof event.preventDefault === "function") event.preventDefault();
        goBackRef.current();
      }
      if (typeof window === "undefined") return undefined;
      window.addEventListener("keydown", onKeyDown);
      return function cleanup() {
        window.removeEventListener("keydown", onKeyDown);
      };
    }, []);

    /* ------------------------------------------------------------- handlers */

    function currentViewport() {
      const size = viewportSize();
      const node = workspaceRef.current;
      return {
        viewportWidth: size.width,
        viewportHeight: size.height,
        workspaceWidth:
          (node && node.clientWidth) || Math.max(0, size.width - 40),
      };
    }

    function updateLayout(patch) {
      setLayout((current) =>
        clampLayout(Object.assign({}, current, patch), currentViewport())
      );
    }

    function toggleColumn(side) {
      setLayout((current) => {
        const key = side === "left" ? "leftCollapsed" : "rightCollapsed";
        const next = Object.assign({}, current);
        next[key] = !current[key];
        return next;
      });
    }

    function ensureRightExpanded() {
      setLayout((current) =>
        current.rightCollapsed
          ? Object.assign({}, current, { rightCollapsed: false })
          : current
      );
    }

    function handleSelectNode(nodeSymbol) {
      const next = String(nodeSymbol || "").trim();
      setSelectedNode(next);
      if (next) ensureRightExpanded();
    }

    function jumpToFunction(target) {
      const nextSymbol = String(target || "").trim();
      if (!nextSymbol) return;
      const rootKey = functionKey(detailState.data, symbol);
      if (rootKey && String(rootKey) !== nextSymbol) {
        const nextHistory = pushEntry(historyRef.current, {
          symbol: String(rootKey),
          tab: tab || "",
        });
        setHistory(nextHistory);
        saveHistory(nextHistory);
      }
      setSelectedNode("");
      setNodeState({ symbol: "", status: "idle", data: null, error: null });
      navigate({ page: "function", symbol: nextSymbol, tab: "overview" });
    }

    function handleOpenDetail(target) {
      jumpToFunction(target);
    }

    function retry() {
      setRefreshKey((key) => key + 1);
    }

    function toggleSection(id) {
      if (!detailState.data) return;
      const normalized = normalizeTab(id);
      const next = tab === normalized ? "" : normalized;
      setTab(next);
      navigate({ page: "function", symbol, tab: next }, { replace: true });
    }

    /* --------------------------------------------------------------- render */

    const detail = detailState.status === "ready" ? detailState.data : null;
    const fn = (detail && detail.function) || {};
    const shard = shardOf(detail);
    const selectedNodeData =
      nodeState.status === "ready" && nodeState.symbol === selectedNode
        ? nodeState.data
        : null;
    const callSite = selectedNode
      ? callSiteForNode(detail, graphData, selectedNode)
      : null;
    const graphSymbol = detail ? functionKey(detail, symbol) : symbol;
    const isState = detailState.status !== "ready";
    const topHeight = layout.top || measureHeight(headRef) || 0;
    const leftWidth = layout.left || measureNode(sidebarRef) || 0;
    const rightWidth = layout.right || measureNode(panelRef) || 0;
    const viewport = viewportSize();
    const workspaceWidth =
      (workspaceRef.current && workspaceRef.current.clientWidth) ||
      Math.max(0, viewport.width - 40);
    const maxLeftWidth = Math.max(
      LAYOUT_MIN.left,
      workspaceWidth -
        (layout.right || Math.round(workspaceWidth * 0.26)) -
        LAYOUT_MIN.middle -
        16
    );
    const maxRightWidth = Math.max(
      LAYOUT_MIN.right,
      workspaceWidth -
        (layout.left || Math.round(workspaceWidth * 0.22)) -
        LAYOUT_MIN.middle -
        16
    );
    const maxTopHeight = Math.max(
      LAYOUT_MIN.top,
      Math.round((viewport.height || 800) * 0.7)
    );

    const badges = [];
    if (detail) {
      badges.push(
        React.createElement(
          Badge,
          { key: "kind", tone: "neutral" },
          fn.kind || "function"
        )
      );
      badges.push(
        fn.is_definition
          ? React.createElement(
              Badge,
              { key: "def", tone: "success" },
              t("fn.badge.definition")
            )
          : React.createElement(
              Badge,
              { key: "def", tone: "muted" },
              t("fn.badge.declaration")
            )
      );
      if (fn.is_static) {
        badges.push(
          React.createElement(
            Badge,
            { key: "static", tone: "muted" },
            t("fn.badge.static")
          )
        );
      }
      if (fn.is_inline) {
        badges.push(
          React.createElement(
            Badge,
            { key: "inline", tone: "muted" },
            t("fn.badge.inline")
          )
        );
      }
      if (fn.linkage) {
        badges.push(
          React.createElement(
            Badge,
            { key: "linkage", tone: "neutral" },
            fn.linkage
          )
        );
      }
      if (shard) {
        badges.push(
          React.createElement(
            Badge,
            { key: "shard", tone: "brand" },
            t("fn.badge.shard", { name: shard })
          )
        );
      }
      badges.push(
        detail.deep_ready
          ? React.createElement(
              Badge,
              { key: "deep", tone: "success" },
              t("fn.badge.deepFacts")
            )
          : React.createElement(
              Badge,
              { key: "deep", tone: "warn" },
              t("fn.badge.deepFactsPending")
            )
      );
    }

    const title = fn.name || symbol || t("fn.pageAria");
    const subtitle =
      [fn.qualified_name, functionLocation(fn)].filter(Boolean).join(" · ") ||
      symbol;

    const backButton = history.length
      ? React.createElement(
          kit.Button,
          {
            key: "back",
            variant: "ghost",
            size: "sm",
            onClick: goBack,
            title: t("fn.back"),
          },
          "← " + t("fn.back")
        )
      : null;

    const headerActions = React.createElement(
      "div",
      { className: "ca-fn-head-actions" },
      backButton,
      React.createElement(
        kit.IconButton,
        {
          key: "toggle-left",
          label: t("layout.toggleLeft"),
          title: t("layout.toggleLeft"),
          active: layout.leftCollapsed,
          onClick: () => toggleColumn("left"),
          "aria-pressed": layout.leftCollapsed ? "true" : "false",
        },
        "◧"
      ),
      React.createElement(
        kit.IconButton,
        {
          key: "toggle-right",
          label: t("layout.toggleRight"),
          title: t("layout.toggleRight"),
          active: layout.rightCollapsed,
          onClick: () => toggleColumn("right"),
          "aria-pressed": layout.rightCollapsed ? "true" : "false",
        },
        "◨"
      ),
      React.createElement(
        kit.Button,
        {
          variant: "ghost",
          size: "sm",
          onClick: () => setLayout(defaultLayout()),
          title: t("layout.reset"),
        },
        t("layout.reset")
      )
    );

    const header = React.createElement(
      "header",
      {
        className:
          "ca-fn-head" +
          (sourceCollapsed || isState ? " is-collapsed" : ""),
        ref: headRef,
      },
      React.createElement(
        "div",
        { className: "ca-fn-head-top" },
        React.createElement(
          "div",
          { className: "ca-fn-head-main" },
          React.createElement(
            "h1",
            { className: "ca-fn-title" },
            React.createElement(
              "span",
              { className: "ca-fn-title-name" },
              title
            ),
            badges.length
              ? React.createElement("span", { className: "ca-fn-badges" }, badges)
              : null
          ),
          subtitle
            ? React.createElement("p", { className: "ca-fn-sub" }, subtitle)
            : null
        ),
        headerActions
      ),
      detail
        ? React.createElement(sourceViews.TopSourcePanel, {
            state: sourceState,
            collapsed: sourceCollapsed,
            onToggleCollapse: () => setSourceCollapsed((value) => !value),
            detail,
            nodeData: selectedNodeData,
            callSite,
            onRetry: retry,
          })
        : null
    );

    function stateBody() {
      if (detailState.status === "loading") {
        return React.createElement(
          "div",
          { className: "ca-fn-state" },
          React.createElement(
            "div",
            { className: "ca-fn-loading" },
            React.createElement(kit.Spinner, {
              label: kit.has("Spinner")
                ? undefined
                : t("fn.loading", { target: symbol }),
            }),
            React.createElement(
              "span",
              null,
              t("fn.loading", { target: symbol })
            ),
            React.createElement(
              "span",
              { className: "ca-fn-note" },
              t("fn.loadingHint")
            )
          )
        );
      }
      const error = detailState.error;
      const kind = errorKind(error);
      const retryButton = React.createElement(
        kit.Button,
        { variant: "secondary", onClick: retry, title: t("fn.error.retry") },
        t("fn.error.retry")
      );
      if (kind === "notFound") {
        return React.createElement(
          "div",
          { className: "ca-fn-state" },
          React.createElement(
            "div",
            { className: "ca-fn-state-inner" },
            React.createElement(kit.EmptyState, {
              title: t("fn.error.notFoundTitle"),
              body: t("fn.error.notFoundBody", {
                symbol: symbol || t("fn.common.none"),
              }),
              action: retryButton,
            })
          )
        );
      }
      if (kind === "dbUnavailable") {
        return React.createElement(
          "div",
          { className: "ca-fn-state" },
          React.createElement(
            "div",
            { className: "ca-fn-state-inner" },
            React.createElement(kit.ErrorBox, {
              title: t("fn.error.dbUnavailableTitle"),
              error,
              hint: t("fn.error.dbUnavailableBody"),
              onRetry: retry,
              retryLabel: t("fn.error.retry"),
            })
          )
        );
      }
      return React.createElement(
        "div",
        { className: "ca-fn-state" },
        React.createElement(
          "div",
          { className: "ca-fn-state-inner" },
          React.createElement(kit.ErrorBox, {
            title: t("fn.error.title"),
            error,
            hint: t("fn.error.hint"),
            onRetry: retry,
            retryLabel: t("fn.error.retry"),
          })
        )
      );
    }

    const accordionItems = detail
      ? TAB_IDS.map((id) => ({
          id,
          label: t(TAB_LABEL_KEYS[id]),
          count: countFor(id, detail),
        }))
      : [];

    const accordionNode = detail
      ? React.createElement(
          accordion.Accordion,
          {
            items: accordionItems,
            openId: tab || "",
            onToggle: toggleSection,
          },
          tab
            ? React.createElement(accordion.SectionBody, {
                id: tab,
                detail,
                symbol,
                refreshKey,
                CfgGraph,
                ExplainPanel,
              })
            : null
        )
      : null;

    const topSplitter =
      !isState && !narrow && !sourceCollapsed
        ? React.createElement(Splitter, {
            kind: "top",
            orientation: "horizontal",
            value: topHeight,
            min: LAYOUT_MIN.top,
            max: maxTopHeight,
            label: t("layout.resizeSource"),
            disabled: narrow,
            measure: () => measureHeight(headRef),
            onResize: (next) => updateLayout({ top: next }),
            onReset: () => updateLayout({ top: null }),
          })
        : null;

    const leftSplitter =
      !isState && !narrow && !layout.leftCollapsed
        ? React.createElement(Splitter, {
            kind: "left",
            orientation: "vertical",
            value: leftWidth,
            min: LAYOUT_MIN.left,
            max: maxLeftWidth,
            label: t("layout.resizeLeft"),
            disabled: narrow,
            measure: () => measureNode(sidebarRef),
            onResize: (next) => updateLayout({ left: next }),
            onReset: () => updateLayout({ left: null }),
          })
        : null;

    const rightSplitter =
      !isState && !narrow && !layout.rightCollapsed
        ? React.createElement(Splitter, {
            kind: "right",
            orientation: "vertical",
            invert: true,
            value: rightWidth,
            min: LAYOUT_MIN.right,
            max: maxRightWidth,
            label: t("layout.resizeRight"),
            disabled: narrow,
            measure: () => measureNode(panelRef),
            onResize: (next) => updateLayout({ right: next }),
            onReset: () => updateLayout({ right: null }),
          })
        : null;

    const workspace = React.createElement(
      "div",
      {
        className:
          "ca-fn-workspace" +
          (layout.leftCollapsed ? " is-left-collapsed" : "") +
          (layout.rightCollapsed ? " is-right-collapsed" : ""),
        ref: workspaceRef,
      },
      React.createElement(
        "aside",
        {
          className: "ca-fn-sidebar",
          ref: sidebarRef,
          "aria-label": t("layout.aria.sections"),
        },
        accordionNode
      ),
      leftSplitter,
      React.createElement(
        "section",
        { className: "ca-fn-graph", "aria-label": t("layout.aria.graph") },
        detail
          ? React.createElement(CallGraph, {
              key: graphSymbol + ":" + refreshKey,
              symbol: graphSymbol,
              onSelectNode: handleSelectNode,
              onOpenDetail: handleOpenDetail,
              onGraphData: setGraphData,
            })
          : null
      ),
      rightSplitter,
      React.createElement(
        "aside",
        {
          className: "ca-fn-panel",
          ref: panelRef,
          "aria-label": t("layout.aria.details"),
        },
        detail
          ? React.createElement(sourceViews.NodeSourcePanel, {
              node: nodeState,
              callSite,
              onClose: () => setSelectedNode(""),
              onOpenDetail: handleOpenDetail,
              onRetry: retry,
            })
          : null
      )
    );

    return React.createElement(
      "div",
      {
        className: "ca-fn-page" + (isState ? " is-state" : ""),
        style: layoutStyle(layout),
        "data-ca-view": "function",
        role: "region",
        "aria-label": t("fn.pageAria"),
      },
      header,
      topSplitter,
      isState ? stateBody() : workspace,
      stylesHook
        ? null
        : React.createElement(
            "style",
            { key: "ca-fn-styles" },
            FUNCTION_CSS
          )
    );
  };
}

export default createFunctionPage;
