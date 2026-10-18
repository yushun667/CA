/* Function detail view — left accordion (overview/branches/returns/resources/
 * findings), ported from the CA web UI.
 *
 * Only one section is expanded at a time; the header row shows a count badge,
 * the body scrolls inside the sidebar and the optional AI explanation panel is
 * mounted at the bottom of the overview content.
 */

import {
  asArray,
  formatNumber,
  functionKey,
  functionLocation,
  functionSignature,
  location,
  shardOf,
  truncate,
} from "./data.js";

export function createAccordion(React, ctx) {
  const kit = ctx.kit;
  const t = ctx.t;

  function Badge(props) {
    if (props.children === null || props.children === undefined || props.children === "") {
      return null;
    }
    return React.createElement(
      kit.Badge,
      { tone: props.tone || "neutral", title: props.title },
      props.children
    );
  }

  function shardBadge(row) {
    if (!row || !row.shard) return null;
    return React.createElement(
      Badge,
      { tone: "brand" },
      t("fn.badge.shard", { name: row.shard })
    );
  }

  function Mono(props) {
    return React.createElement(
      "span",
      { className: "ca-fn-mono" + (props.muted ? " ca-fn-muted" : "") },
      props.children
    );
  }

  function note(props) {
    return React.createElement(
      "span",
      { className: "ca-fn-note" },
      props.children
    );
  }

  function Section(props) {
    return React.createElement(
      "section",
      { className: "ca-fn-section" },
      React.createElement(
        "div",
        { className: "ca-fn-section-head" },
        React.createElement(
          "h3",
          null,
          props.title
        ),
        props.note ? note({ children: props.note }) : null
      ),
      React.createElement(
        "div",
        { className: "ca-fn-section-body" },
        props.children
      )
    );
  }

  function DataTable(props) {
    return React.createElement(
      "div",
      { className: "ca-fn-table-wrap" },
      React.createElement(kit.Table, {
        columns: props.columns,
        rows: props.rows,
        empty: props.empty,
        rowKey: props.rowKey,
      })
    );
  }

  /* ------------------------------------------------------------- overview */

  function deepPendingCallout(detail) {
    const fn = detail.function || {};
    const target = fn.name || "";
    return React.createElement(
      "div",
      { className: "ca-fn-callout" },
      React.createElement("strong", null, t("fn.deep.pendingTitle") + " "),
      React.createElement("span", null, t("fn.deep.pendingBody") + " "),
      React.createElement(
        "code",
        null,
        "ca ensure-deep --db DB --repo REPO --compdb COMPDB --symbols " + target
      ),
      React.createElement("span", null, " " + t("fn.deep.pendingAction"))
    );
  }

  function OverviewContent(props) {
    const detail = props.detail || {};
    const fn = detail.function || {};
    const shard = shardOf(detail);
    const params = asArray(fn.params);

    const fields = [
      [t("fn.field.file"), functionLocation(fn) || t("fn.common.none")],
      [
        t("fn.field.range"),
        fn.line
          ? String(fn.line) + ":" + String(fn.col || 0) + " – " + String(fn.end_line || "?")
          : t("fn.common.none"),
      ],
      [t("fn.field.returnType"), fn.return_type || t("fn.common.none")],
      [t("fn.field.signature"), functionSignature(fn) || t("fn.common.none")],
      [t("fn.field.linkage"), fn.linkage || t("fn.common.none")],
      [t("fn.field.storageClass"), fn.storage_class || t("fn.common.none")],
      [
        t("fn.field.declaredIn"),
        shard
          ? t("fn.common.shardName", { name: shard })
          : t("fn.common.singleDatabase"),
      ],
      [t("fn.field.usr"), fn.usr || t("fn.common.none")],
      [t("fn.field.tuId"), fn.tu_id || t("fn.common.none")],
      [t("fn.field.functionId"), fn.id || t("fn.common.none")],
      [t("fn.field.parentId"), fn.parent_id || t("fn.common.none")],
      [
        t("fn.field.deepReady"),
        detail.deep_ready
          ? t("fn.field.deepReadyYes")
          : t("fn.field.deepReadyNo"),
      ],
    ];
    if (params.length) {
      fields.push([
        t("fn.field.parameters"),
        params
          .map(
            (param) =>
              (param.type_spelling || "?") +
              (param.name ? " " + param.name : "") +
              (param.default_value ? " = " + param.default_value : "")
          )
          .join(", "),
      ]);
    }

    const grid = React.createElement(
      "dl",
      { className: "ca-fn-grid" },
      fields.map((entry, index) =>
        React.createElement(
          "div",
          { key: index },
          React.createElement("dt", null, entry[0]),
          React.createElement("dd", null, entry[1])
        )
      )
    );

    const metricItems = [
      [t("fn.metric.branches"), asArray(detail.branches).length],
      [t("fn.metric.assignments"), asArray(detail.assignments).length],
      [t("fn.metric.returns"), asArray(detail.returns).length],
      [t("fn.metric.resources"), asArray(detail.resource_ops).length],
      [t("fn.metric.findings"), asArray(detail.findings).length],
    ];
    const metrics = React.createElement(
      "div",
      { className: "ca-fn-metrics" },
      metricItems.map((entry, index) =>
        React.createElement(
          "div",
          { className: "ca-fn-metric", key: index },
          React.createElement(
            "div",
            { className: "ca-fn-metric-label" },
            entry[0]
          ),
          React.createElement(
            "div",
            { className: "ca-fn-metric-value" },
            formatNumber(entry[1])
          )
        )
      )
    );

    const assignments = asArray(detail.assignments);
    const assignmentColumns = [
      { key: "lhs", label: t("fn.table.lhs"), render: (row) => Mono({ children: row.lhs || "" }) },
      {
        key: "operator",
        label: t("fn.table.op"),
        render: (row) => Mono({ children: row.operator || "=" }),
      },
      { key: "rhs", label: t("fn.table.rhs"), render: (row) => Mono({ children: row.rhs || "" }) },
      { key: "type_spelling", label: t("fn.table.type"), render: (row) => row.type_spelling || "" },
      {
        key: "is_decl_init",
        label: t("fn.table.kind"),
        render: (row) =>
          row.is_decl_init
            ? React.createElement(
                Badge,
                { tone: "info" },
                t("fn.badge.declInit")
              )
            : "",
      },
      {
        key: "location",
        label: t("fn.table.location"),
        render: (row) => Mono({ muted: true, children: location(row) || t("fn.common.none") }),
      },
      {
        key: "shard",
        label: t("fn.table.shard"),
        render: (row) => shardBadge(row) || "",
      },
    ];

    const signature = React.createElement(
      "pre",
      { className: "ca-fn-signature" },
      functionSignature(fn) || t("fn.common.none")
    );

    const explain = props.ExplainPanel
      ? React.createElement(
          "div",
          { className: "ca-fn-explain" },
          React.createElement(props.ExplainPanel, {
            symbol: functionKey(detail, props.symbol),
          })
        )
      : null;

    return React.createElement(
      "div",
      null,
      detail.deep_ready === false ? deepPendingCallout(detail) : null,
      React.createElement(
        Section,
        {
          title: t("fn.section.function"),
          note: t("fn.section.functionNote"),
        },
        signature,
        grid,
        metrics
      ),
      React.createElement(
        Section,
        {
          title: t("fn.section.assignments"),
          note: assignments.length
            ? t("fn.note.assignmentsIndexed", { count: formatNumber(assignments.length) })
            : t("fn.note.assignmentsEmpty"),
        },
        React.createElement(DataTable, {
          columns: assignmentColumns,
          rows: assignments,
          empty: t("fn.empty.assignments"),
          rowKey: (row, index) => index,
        })
      ),
      explain
    );
  }

  /* -------------------------------------------------------------- branches */

  function BranchesContent(props) {
    const CfgGraph = props.CfgGraph;
    if (!CfgGraph) {
      return React.createElement(
        kit.EmptyState,
        {
          title: t("fn.tab.branches"),
          body: t("fn.empty.branches"),
        }
      );
    }
    return React.createElement(
      "div",
      { className: "ca-fn-cfg", style: { minHeight: "280px", display: "flex" } },
      React.createElement(CfgGraph, {
        key: functionKey(props.detail, props.symbol) + ":" + (props.refreshKey || 0),
        symbol: functionKey(props.detail, props.symbol),
      })
    );
  }

  /* ---------------------------------------------------------------- facts */

  function ReturnsContent(props) {
    const rows = asArray(props.detail.returns);
    const columns = [
      {
        key: "expr",
        label: t("fn.table.expression"),
        render: (row) => Mono({ children: row.expr || t("fn.common.none") }),
      },
      {
        key: "is_implicit",
        label: t("fn.table.kind"),
        render: (row) =>
          row.is_implicit
            ? React.createElement(Badge, { tone: "warn" }, t("fn.badge.implicit"))
            : React.createElement(Badge, { tone: "neutral" }, t("fn.badge.explicit")),
      },
      {
        key: "location",
        label: t("fn.table.location"),
        render: (row) => Mono({ muted: true, children: location(row) || t("fn.common.none") }),
      },
      {
        key: "block_id",
        label: t("fn.table.block"),
        render: (row) => Mono({ muted: true, children: truncate(row.block_id || "", 26) }),
      },
      { key: "shard", label: t("fn.table.shard"), render: (row) => shardBadge(row) || "" },
    ];
    return React.createElement(DataTable, {
      columns,
      rows,
      empty: t("fn.empty.returns"),
      rowKey: (row, index) => index,
    });
  }

  function ResourcesContent(props) {
    const rows = asArray(props.detail.resource_ops);
    const columns = [
      {
        key: "op",
        label: t("fn.table.op"),
        render: (row) =>
          React.createElement(
            Badge,
            { tone: row.op === "acquire" ? "warn" : "success" },
            row.op || t("fn.common.none")
          ),
      },
      {
        key: "resource_kind",
        label: t("fn.table.resource"),
        render: (row) => Mono({ children: row.resource_kind || "" }),
      },
      {
        key: "api_name",
        label: t("fn.table.api"),
        render: (row) => Mono({ children: row.api_name || "" }),
      },
      {
        key: "target",
        label: t("fn.table.target"),
        render: (row) => Mono({ children: row.target || "" }),
      },
      {
        key: "matcher",
        label: t("fn.table.matcher"),
        render: (row) => Mono({ muted: true, children: row.matcher || "" }),
      },
      {
        key: "location",
        label: t("fn.table.location"),
        render: (row) => Mono({ muted: true, children: location(row) || t("fn.common.none") }),
      },
      { key: "shard", label: t("fn.table.shard"), render: (row) => shardBadge(row) || "" },
    ];
    return React.createElement(DataTable, {
      columns,
      rows,
      empty: t("fn.empty.resources"),
      rowKey: (row, index) => index,
    });
  }

  function FindingsContent(props) {
    const rows = asArray(props.detail.findings);
    const severityTone = (severity) => {
      if (severity === "error") return "error";
      if (severity === "warning") return "warn";
      return "info";
    };
    const columns = [
      {
        key: "severity",
        label: t("fn.table.severity"),
        render: (row) =>
          React.createElement(
            Badge,
            { tone: severityTone(String(row.severity || "info")) },
            String(row.severity || "info")
          ),
      },
      { key: "kind", label: t("fn.table.kind"), render: (row) => row.kind || "" },
      {
        key: "resource_kind",
        label: t("fn.table.resource"),
        render: (row) => Mono({ children: row.resource_kind || "" }),
      },
      {
        key: "message",
        label: t("fn.table.message"),
        render: (row) => Mono({ children: row.message || "" }),
      },
      {
        key: "location",
        label: t("fn.table.location"),
        render: (row) => Mono({ muted: true, children: location(row) || t("fn.common.none") }),
      },
      { key: "shard", label: t("fn.table.shard"), render: (row) => shardBadge(row) || "" },
    ];
    return React.createElement(DataTable, {
      columns,
      rows,
      empty: t("fn.empty.findings"),
      rowKey: (row, index) => index,
    });
  }

  /* ------------------------------------------------------------ containers */

  function Accordion(props) {
    const items = Array.isArray(props.items) ? props.items : [];
    return React.createElement(
      "div",
      { className: "ca-fn-accordion", role: "presentation" },
      items.map((item) => {
        const open = props.openId === item.id;
        return React.createElement(
          "section",
          {
            className: "ca-fn-accordion-item" + (open ? " is-open" : ""),
            key: item.id,
            "data-section": item.id,
          },
          React.createElement(
            "button",
            {
              type: "button",
              className: "ca-fn-accordion-header",
              "aria-expanded": open ? "true" : "false",
              "aria-controls": "ca-fn-section-body",
              "data-section-toggle": item.id,
              onClick: () => props.onToggle(item.id),
            },
            React.createElement(
              "span",
              { className: "ca-fn-accordion-title" },
              item.label
            ),
            item.count !== null && item.count !== undefined
              ? React.createElement(
                  "span",
                  { className: "ca-fn-count" },
                  formatNumber(item.count)
                )
              : null,
            React.createElement(
              "span",
              { className: "ca-fn-chevron", "aria-hidden": "true" },
              open ? "▾" : "▸"
            )
          ),
          open
            ? React.createElement(
                "div",
                {
                  className: "ca-fn-accordion-body",
                  id: "ca-fn-section-body",
                  role: "region",
                  "aria-label": item.label,
                },
                props.children
              )
            : null
        );
      })
    );
  }

  /** Body for the open section; only the active section is rendered. */
  function SectionBody(props) {
    const common = {
      detail: props.detail,
      symbol: props.symbol,
      refreshKey: props.refreshKey || 0,
      CfgGraph: props.CfgGraph,
      ExplainPanel: props.ExplainPanel,
    };
    switch (props.id) {
      case "branches":
        return React.createElement(BranchesContent, common);
      case "returns":
        return React.createElement(ReturnsContent, common);
      case "resources":
        return React.createElement(ResourcesContent, common);
      case "findings":
        return React.createElement(FindingsContent, common);
      case "overview":
      default:
        return React.createElement(OverviewContent, common);
    }
  }

  return { Accordion, SectionBody, OverviewContent, BranchesContent };
}
