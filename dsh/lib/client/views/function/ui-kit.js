/* Function detail view — bag.ui primitives with self-drawn fallbacks.
 *
 * The shell owns the real primitives (`bag.ui`); this module only wraps the
 * small set the function page needs so a missing/renamed primitive degrades to
 * a plain element with the `ca-fn-` stylesheet instead of crashing the page.
 * Fallbacks never touch host DOM and use theme tokens from the view CSS.
 */

function classes() {
  const list = [];
  for (let index = 0; index < arguments.length; index += 1) {
    const value = arguments[index];
    if (value) list.push(value);
  }
  return list.join(" ");
}

function passThrough(props, omit) {
  const rest = {};
  const skip = omit || [];
  for (const key of Object.keys(props || {})) {
    if (skip.indexOf(key) >= 0) continue;
    rest[key] = props[key];
  }
  return rest;
}

export function createKit(React, ui, t) {
  const base = ui || {};
  const translate = typeof t === "function" ? t : (key) => key;

  const FallbackButton = (props) => {
    const variant = props.variant || "secondary";
    const size = props.size === "sm" ? "sm" : "md";
    const rest = passThrough(props, [
      "variant",
      "size",
      "ariaLabel",
      "children",
      "type",
    ]);
    return React.createElement(
      "button",
      Object.assign({}, rest, {
        type: props.type || "button",
        "aria-label": props["aria-label"] || props.ariaLabel || undefined,
        className: classes(
          "ca-fn-btn",
          "ca-fn-btn-" + variant,
          "ca-fn-btn-" + size,
          props.className
        ),
        disabled: Boolean(props.disabled),
      }),
      props.children
    );
  };

  const FallbackIconButton = (props) => {
    const rest = passThrough(props, [
      "label",
      "title",
      "size",
      "active",
      "children",
    ]);
    return React.createElement(
      "button",
      Object.assign({}, rest, {
        type: "button",
        "aria-label": props["aria-label"] || props.label,
        title: props.title || props.label,
        "aria-pressed": props.active ? "true" : undefined,
        className: classes(
          "ca-fn-btn",
          "ca-fn-btn-ghost",
          "ca-fn-btn-icon",
          props.active ? "is-active" : "",
          props.className
        ),
        disabled: Boolean(props.disabled),
      }),
      props.children
    );
  };

  const FallbackBadge = (props) => {
    const rest = passThrough(props, ["tone", "title", "children", "className"]);
    return React.createElement(
      "span",
      Object.assign({}, rest, {
        className: classes(
          "ca-fn-badge",
          "ca-fn-badge-" + (props.tone || "neutral"),
          props.className
        ),
        title: props.title,
      }),
      props.children
    );
  };

  const FallbackSpinner = (props) => {
    const label = props.label || translate("fn.loading", { target: "" });
    return React.createElement(
      "span",
      {
        className: "ca-fn-spinner",
        role: "status",
        "aria-label": label,
        title: label,
      },
      React.createElement("span", {
        className: "ca-fn-spinner-ring",
        "aria-hidden": "true",
      })
    );
  };

  const FallbackErrorBox = (props) => {
    const error = props.error;
    const info =
      error && typeof error === "object"
        ? {
            code: error.code ? String(error.code) : "",
            message: error.message ? String(error.message) : String(error),
          }
        : { code: "", message: error ? String(error) : "" };
    return React.createElement(
      "div",
      { className: "ca-fn-error-box", role: "alert" },
      React.createElement(
        "p",
        { className: "ca-fn-error-title" },
        props.title || translate("fn.error.title")
      ),
      React.createElement("p", { className: "ca-fn-error-message" }, info.message),
      info.code
        ? React.createElement(
            "p",
            { className: "ca-fn-error-code" },
            info.code
          )
        : null,
      props.hint
        ? React.createElement("p", { className: "ca-fn-error-hint" }, props.hint)
        : null,
      typeof props.onRetry === "function"
        ? React.createElement(
            "button",
            {
              type: "button",
              className: "ca-fn-btn ca-fn-btn-secondary ca-fn-btn-sm",
              onClick: props.onRetry,
            },
            props.retryLabel || translate("fn.error.retry")
          )
        : null
    );
  };

  const FallbackEmptyState = (props) =>
    React.createElement(
      "div",
      { className: "ca-fn-empty-state" },
      props.icon || null,
      React.createElement(
        "p",
        { className: "ca-fn-empty-title" },
        props.title
      ),
      props.body
        ? React.createElement("p", { className: "ca-fn-empty-body" }, props.body)
        : null,
      props.action || null
    );

  const FallbackTable = (props) => {
    const columns = Array.isArray(props.columns) ? props.columns : [];
    const rows = Array.isArray(props.rows) ? props.rows : [];
    const empty =
      props.empty !== undefined && props.empty !== null
        ? props.empty
        : translate("fn.common.none");
    return React.createElement(
      "div",
      { className: "ca-fn-table-wrap" },
      React.createElement(
        "table",
        { className: "ca-fn-table" },
        React.createElement(
          "thead",
          null,
          React.createElement(
            "tr",
            null,
            columns.map((column, index) =>
              React.createElement(
                "th",
                {
                  key: column.key || String(index),
                  className: column.align ? "is-" + column.align : undefined,
                  style: column.width ? { width: column.width } : undefined,
                },
                column.label
              )
            )
          )
        ),
        React.createElement(
          "tbody",
          null,
          rows.length
            ? rows.map((row, rowIndex) => {
                const key =
                  typeof props.rowKey === "function"
                    ? props.rowKey(row, rowIndex)
                    : rowIndex;
                return React.createElement(
                  "tr",
                  { key },
                  columns.map((column, columnIndex) => {
                    let value;
                    if (typeof column.render === "function") {
                      value = column.render(row, rowIndex);
                    } else {
                      value = row ? row[column.key] : "";
                    }
                    return React.createElement(
                      "td",
                      {
                        key: column.key || String(columnIndex),
                        className: classes(
                          column.align ? "is-" + column.align : "",
                          column.mono && typeof column.render !== "function"
                            ? "ca-fn-mono"
                            : ""
                        ),
                      },
                      value === null || value === undefined ? "" : value
                    );
                  })
                );
              })
            : React.createElement(
                "tr",
                null,
                React.createElement(
                  "td",
                  {
                    className: "ca-fn-table-empty",
                    colSpan: Math.max(1, columns.length),
                  },
                  empty
                )
              )
        )
      )
    );
  };

  return {
    has: (name) => Boolean(base[name]),
    Button: base.Button || FallbackButton,
    IconButton: base.IconButton || FallbackIconButton,
    Badge: base.Badge || FallbackBadge,
    Spinner: base.Spinner || FallbackSpinner,
    ErrorBox: base.ErrorBox || FallbackErrorBox,
    EmptyState: base.EmptyState || FallbackEmptyState,
    Table: base.Table || FallbackTable,
  };
}
