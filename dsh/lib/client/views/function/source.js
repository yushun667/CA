/* Function detail view — source viewers.
 *
 * Two viewers share one renderer:
 *  - the complete function source in the collapsible top panel (line numbers,
 *    syntax highlighting, function range highlight, node call-site highlight,
 *    truncation notice, independent scrolling);
 *  - the selected graph node's complete source in the right-hand panel.
 *
 * Source text is rendered as React spans from token objects (see highlight.js);
 * no HTML string is ever produced, so database content cannot inject markup.
 */

import { tokenClass } from "./highlight.js";
import { functionLocation, truncate } from "./data.js";

const LINE_HEIGHT = 18;

export function createSourceViews(React, ctx) {
  const { useEffect, useMemo, useRef } = React;
  const kit = ctx.kit;
  const t = ctx.t;
  const highlightRows = ctx.highlightRows;

  function Badge(props) {
    if (!props.children) return null;
    return React.createElement(
      kit.Badge,
      { tone: props.tone || "neutral", title: props.title },
      props.children
    );
  }

  /**
   * Shared read-only code renderer.
   *
   * props: {lines:[{line,text}], start, functionRange:[a,b],
   *         selectedRange:[a,b], ariaLabel, scrollLine}
   */
  function SourceCode(props) {
    const codeRef = useRef(null);
    const rows = useMemo(() => {
      const sourceRows = Array.isArray(props.lines) ? props.lines : [];
      const startLine =
        sourceRows.length && Number(sourceRows[0] && sourceRows[0].line)
          ? Number(sourceRows[0].line)
          : Number(props.start) || 1;
      return highlightRows(
        sourceRows.map((row) =>
          row && row.text !== undefined && row.text !== null ? row.text : ""
        ),
        startLine
      );
    }, [props.lines, props.start]);

    const scrollTarget = Number(props.scrollLine) || 0;
    const firstLine = rows.length ? rows[0].line : 0;

    useEffect(() => {
      const node = codeRef.current;
      if (!node) return;
      if (!scrollTarget || !firstLine) {
        node.scrollTop = 0;
        return;
      }
      node.scrollTop = Math.max(0, (scrollTarget - firstLine - 2) * LINE_HEIGHT);
    }, [rows, scrollTarget, firstLine]);

    const functionStart = Array.isArray(props.functionRange)
      ? Number(props.functionRange[0]) || 0
      : 0;
    const functionEnd = Array.isArray(props.functionRange)
      ? Number(props.functionRange[1]) || 0
      : 0;
    const selectedStart = Array.isArray(props.selectedRange)
      ? Number(props.selectedRange[0]) || 0
      : 0;
    const selectedEnd = Array.isArray(props.selectedRange)
      ? Number(props.selectedRange[1]) || 0
      : 0;

    const children = rows.map((row) => {
      const inFunction =
        functionStart > 0 && row.line >= functionStart && row.line <= functionEnd;
      const selected =
        selectedStart > 0 &&
        row.line >= selectedStart &&
        row.line <= (selectedEnd || selectedStart);
      const className =
        "ca-fn-code-line" +
        (inFunction ? " is-function" : "") +
        (selected ? " is-selected" : "");
      return React.createElement(
        "div",
        { className, key: row.line },
        React.createElement(
          "span",
          { className: "ca-fn-code-lineno", "aria-hidden": "true" },
          String(row.line)
        ),
        React.createElement(
          "span",
          { className: "ca-fn-code-text" },
          row.tokens.map((token, index) =>
            token.cls
              ? React.createElement(
                  "span",
                  { className: tokenClass(token.cls), key: index },
                  token.text
                )
              : token.text
          )
        )
      );
    });

    return React.createElement(
      "div",
      {
        className: "ca-fn-code" + (props.className ? " " + props.className : ""),
        ref: codeRef,
        tabIndex: 0,
        role: "group",
        "aria-label": props.ariaLabel,
      },
      children.length
        ? children
        : React.createElement(
            "p",
            { className: "ca-fn-node-empty" },
            ctx.t("fn.source.loading")
          )
    );
  }

  /** `Lines a–b of n · function c–d · truncated…` meta line. */
  function rangeNote(data) {
    const info = data || {};
    const parts = [];
    if (Number(info.start) && Number(info.end)) {
      parts.push(
        t("fn.source.range", {
          start: info.start,
          end: info.end,
          total: info.total_lines,
        })
      );
    }
    if (Number(info.function_start) && Number(info.function_end)) {
      parts.push(
        t("fn.source.functionRange", {
          start: info.function_start,
          end: info.function_end,
        })
      );
    }
    const rows = Array.isArray(info.lines) ? info.lines.length : 0;
    if (info.truncated || rows >= 400) {
      parts.push(t("fn.source.truncated", { count: rows }));
    }
    return parts.join(" · ");
  }

  function shardBadge(data) {
    const shard = (data && data.shard) || "";
    if (!shard) return null;
    return React.createElement(
      Badge,
      { tone: "brand" },
      t("fn.badge.shard", { name: shard })
    );
  }

  /**
   * Fixed top panel: the complete source of the opened function.
   *
   * props: {state:{status,data,error}, collapsed, onToggleCollapse,
   *         detail, nodeData, callSite, onRetry}
   */
  function TopSourcePanel(props) {
    const state = props.state || {};
    const collapsed = Boolean(props.collapsed);
    const data = state.data || null;
    const ready = state.status === "ready" && data;

    const collapseLabel = collapsed
      ? t("fn.source.expand")
      : t("fn.source.collapse");
    const actions = [
      React.createElement(
        kit.Button,
        {
          key: "collapse",
          variant: "ghost",
          size: "sm",
          onClick: props.onToggleCollapse,
          title: collapseLabel,
          "aria-expanded": collapsed ? "false" : "true",
        },
        collapsed ? "▸ " + t("fn.source.expand") : "▾ " + t("fn.source.collapse")
      ),
    ];

    const heading = React.createElement(
      "div",
      { className: "ca-fn-source-head" },
      React.createElement(
        "div",
        { className: "ca-fn-source-head-left" },
        React.createElement(
          "h2",
          { className: "ca-fn-source-title" },
          t("fn.source.title")
        ),
        React.createElement(
          Badge,
          { tone: "muted" },
          t("fn.source.readonly")
        )
      ),
      React.createElement("div", { className: "ca-fn-source-actions" }, actions)
    );

    if (state.status === "loading") {
      return React.createElement(
        "section",
        {
          className: "ca-fn-source" + (collapsed ? " is-collapsed" : ""),
          "aria-label": t("fn.source.aria"),
        },
        heading,
        collapsed
          ? null
          : React.createElement(
              "div",
              { className: "ca-fn-loading" },
              React.createElement(kit.Spinner, {
                lean: true,
                label: kit.has("Spinner") ? undefined : t("fn.source.loading"),
              }),
              React.createElement("span", null, t("fn.source.loading"))
            )
      );
    }

    if (state.status === "error") {
      return React.createElement(
        "section",
        {
          className: "ca-fn-source" + (collapsed ? " is-collapsed" : ""),
          "aria-label": t("fn.source.aria"),
        },
        heading,
        collapsed
          ? null
          : React.createElement(
              "div",
              { className: "ca-fn-state-inner", style: { padding: "10px 12px" } },
              React.createElement(kit.ErrorBox, {
                title: t("fn.error.sourceTitle"),
                error: state.error,
                hint: t("fn.error.hint"),
                onRetry: props.onRetry,
                retryLabel: t("fn.error.retry"),
              })
            )
      );
    }

    if (!ready) {
      return React.createElement(
        "section",
        {
          className: "ca-fn-source" + (collapsed ? " is-collapsed" : ""),
          "aria-label": t("fn.source.aria"),
        },
        heading
      );
    }

    const fn = data.function || {};
    let selectedRange = null;
    let nodeHint = "";
    const nodeData = props.nodeData;
    if (nodeData && props.callSite) {
      const callSite = props.callSite;
      const inside =
        callSite.file === data.file &&
        Number(callSite.line) >= Number(data.start) &&
        Number(callSite.line) <= Number(data.end);
      if (inside) {
        selectedRange = [Number(callSite.line), Number(callSite.line)];
        nodeHint = t("fn.source.callSite", {
          file: callSite.file,
          line: callSite.line,
        });
      } else {
        nodeHint = t("fn.source.callSiteOutside", {
          file: callSite.file,
          line: callSite.line,
        });
      }
    } else if (nodeData) {
      const nodeFn = nodeData.function || {};
      const nodeStart = Number(nodeData.function_start) || 0;
      const nodeEnd = Number(nodeData.function_end) || 0;
      if (nodeStart && nodeEnd && nodeStart <= Number(data.end) && nodeEnd >= Number(data.start)) {
        selectedRange = [
          Math.max(nodeStart, Number(data.start)),
          Math.min(nodeEnd, Number(data.end)),
        ];
      } else {
        nodeHint = t("fn.source.nodeOutside", {
          file: nodeData.file || "",
          line: nodeFn.line || 0,
        });
      }
    }

    const meta = React.createElement(
      "div",
      { className: "ca-fn-source-meta" },
      React.createElement(
        "span",
        { className: "ca-fn-mono" },
        functionLocation(fn) || data.file || ""
      ),
      shardBadge(data),
      React.createElement("span", null, rangeNote(data)),
      nodeHint
        ? React.createElement(
            "span",
            { className: "ca-fn-node-hint" },
            nodeHint
          )
        : null
    );

    const truncated = Boolean(data.truncated) ||
      (Array.isArray(data.lines) && data.lines.length >= 400);

    return React.createElement(
      "section",
      {
        className: "ca-fn-source" + (collapsed ? " is-collapsed" : ""),
        "aria-label": t("fn.source.aria"),
      },
      heading,
      meta,
      collapsed
        ? null
        : truncated
        ? React.createElement(
            "div",
            { className: "ca-fn-source-warning" },
            t("fn.source.truncatedNote")
          )
        : null,
      collapsed
        ? null
        : React.createElement(SourceCode, {
            lines: data.lines,
            start: data.start,
            functionRange: [
              Number(data.function_start) || Number(fn.line) || 0,
              Number(data.function_end) || Number(fn.end_line) || 0,
            ],
            selectedRange,
            scrollLine: selectedRange ? selectedRange[0] : 0,
            ariaLabel: t("fn.source.aria"),
          })
    );
  }

  /**
   * Right-hand panel: complete source of the selected graph node.
   *
   * props: {node:{symbol,status,data,error}, callSite, onClose,
   *         onOpenDetail(symbol), onRetry}
   */
  function NodeSourcePanel(props) {
    const node = props.node || {};
    const data = node.status === "ready" ? node.data : null;
    const symbol = node.symbol || "";

    if (!symbol) {
      return React.createElement(
        "section",
        { className: "ca-fn-source", "aria-label": t("fn.node.aria") },
        React.createElement(
          "div",
          { className: "ca-fn-source-head" },
          React.createElement(
            "div",
            { className: "ca-fn-source-head-left" },
            React.createElement(
              "h2",
              { className: "ca-fn-source-title" },
              t("fn.node.title")
            )
          )
        ),
        React.createElement(
          "p",
          { className: "ca-fn-node-empty" },
          t("fn.node.hint")
        )
      );
    }

    const heading = React.createElement(
      "div",
      { className: "ca-fn-source-head" },
      React.createElement(
        "div",
        { className: "ca-fn-source-head-left" },
        React.createElement(
          "h2",
          { className: "ca-fn-source-title" },
          t("fn.node.title")
        ),
        React.createElement(
          Badge,
          { tone: "muted" },
          t("fn.source.readonly")
        )
      ),
      React.createElement(
        "div",
        { className: "ca-fn-source-actions" },
        React.createElement(
          kit.Button,
          {
            variant: "ghost",
            size: "sm",
            onClick: props.onClose,
            title: t("fn.node.close"),
          },
          t("fn.node.close")
        )
      )
    );

    if (node.status === "loading") {
      return React.createElement(
        "section",
        { className: "ca-fn-source", "aria-label": t("fn.node.aria") },
        heading,
        React.createElement(
          "div",
          { className: "ca-fn-loading" },
          React.createElement(kit.Spinner, {
            label: kit.has("Spinner") ? undefined : t("fn.node.loading"),
          }),
          React.createElement("span", null, t("fn.node.loading"))
        )
      );
    }

    if (node.status === "error") {
      return React.createElement(
        "section",
        { className: "ca-fn-source", "aria-label": t("fn.node.aria") },
        heading,
        React.createElement(
          "div",
          { className: "ca-fn-node-actions", style: { display: "block" } },
          React.createElement(kit.ErrorBox, {
            title: t("fn.error.nodeTitle"),
            error: node.error,
            hint: truncate(symbol, 120),
            onRetry: props.onRetry,
            retryLabel: t("fn.error.retry"),
          })
        ),
        React.createElement(
          "div",
          { className: "ca-fn-node-actions" },
          React.createElement(
            kit.Button,
            { variant: "ghost", size: "sm", onClick: props.onClose },
            t("fn.node.close")
          )
        )
      );
    }

    if (!data) {
      return React.createElement(
        "section",
        { className: "ca-fn-source", "aria-label": t("fn.node.aria") },
        heading
      );
    }

    const fn = data.function || {};
    let callRange = null;
    let callHint = "";
    if (props.callSite && props.callSite.role === "caller") {
      const callSite = props.callSite;
      if (
        callSite.file === data.file &&
        Number(callSite.line) >= Number(data.start) &&
        Number(callSite.line) <= Number(data.end)
      ) {
        callRange = [Number(callSite.line), Number(callSite.line)];
        callHint = t("fn.source.callerCallsCurrent", {
          file: callSite.file,
          line: callSite.line,
        });
      }
    }

    const openTarget = fn.id || symbol;
    return React.createElement(
      "section",
      { className: "ca-fn-source", "aria-label": t("fn.node.aria") },
      heading,
      React.createElement(
        "div",
        { className: "ca-fn-source-meta" },
        React.createElement(
          "span",
          { className: "ca-fn-mono" },
          functionLocation(fn) || data.file || ""
        ),
        shardBadge(data),
        React.createElement("span", null, rangeNote(data)),
        callHint
          ? React.createElement(
              "span",
              { className: "ca-fn-node-hint" },
              callHint
            )
          : null
      ),
      React.createElement(
        "div",
        { className: "ca-fn-node-actions" },
        React.createElement(
          kit.Button,
          {
            variant: "secondary",
            size: "sm",
            onClick: () => {
              if (typeof props.onOpenDetail === "function") {
                props.onOpenDetail(String(openTarget));
              }
            },
          },
          t("fn.node.openDetail")
        ),
        React.createElement(
          kit.Button,
          { variant: "ghost", size: "sm", onClick: props.onClose },
          t("fn.node.close")
        )
      ),
      React.createElement(SourceCode, {
        lines: data.lines,
        start: data.start,
        functionRange: [
          Number(data.function_start) || Number(fn.line) || 0,
          Number(data.function_end) || Number(fn.end_line) || 0,
        ],
        selectedRange: callRange,
        scrollLine: callRange ? callRange[0] : 0,
        ariaLabel: t("fn.node.aria"),
      })
    );
  }

  return { SourceCode, TopSourcePanel, NodeSourcePanel, rangeNote };
}
