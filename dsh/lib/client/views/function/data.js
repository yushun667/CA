/* Function detail view — data helpers shared by the page and its subviews. */

export const TAB_IDS = Object.freeze([
  "overview",
  "branches",
  "returns",
  "resources",
  "findings",
]);

export const TAB_LABEL_KEYS = Object.freeze({
  overview: "fn.tab.overview",
  branches: "fn.tab.branches",
  returns: "fn.tab.returns",
  resources: "fn.tab.resources",
  findings: "fn.tab.findings",
});

/** Normalize a hash tab segment: unknown/missing values collapse everything. */
export function normalizeTab(tab) {
  const value = String(tab || "").trim();
  return TAB_IDS.indexOf(value) >= 0 ? value : "";
}

export function asArray(value) {
  return Array.isArray(value) ? value : [];
}

export function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0";
  try {
    return number.toLocaleString("en-US");
  } catch (_error) {
    return String(number);
  }
}

export function truncate(value, max) {
  const text = String(value === null || value === undefined ? "" : value);
  if (text.length <= max) return text;
  return text.slice(0, Math.max(1, max - 1)) + "…";
}

/** `file:line` for a fact row; optional prefix reads prefixed columns. */
export function location(row, prefix) {
  if (!row) return "";
  const keyPrefix = prefix || "";
  const file = row[keyPrefix + "file"] || "";
  const line = row[keyPrefix + "line"] || 0;
  return file ? file + ":" + line : "";
}

/** `file:line` of a function record (or the symbol fallback). */
export function functionLocation(fn) {
  if (!fn) return "";
  const file = fn.file || "";
  const line = fn.line || 0;
  return file ? file + ":" + line : "";
}

/** Signature text: the indexed signature, or one rebuilt from the parameters. */
export function functionSignature(fn) {
  if (!fn) return "";
  if (fn.signature) return String(fn.signature);
  const params = asArray(fn.params);
  const rendered = params
    .map((param) => {
      const type = param.type_spelling || "?";
      return param.name ? type + " " + param.name : type;
    })
    .join(", ");
  return (fn.return_type || "?") + " " + (fn.name || "?") + "(" + rendered + ")";
}

/** Root key used for history entries and graph call-site matching. */
export function functionKey(detail, fallback) {
  const fn = detail && detail.function;
  return (fn && (fn.id || fn.name)) || fallback || "";
}

/** Canonical id of the opened function (id wins over name). */
export function functionId(detail, fallback) {
  const fn = detail && detail.function;
  return (fn && fn.id) || fallback || "";
}

export function shardOf(detail) {
  if (!detail) return "";
  return detail.shard || (detail.function && detail.function.shard) || "";
}

function looseEqual(left, right) {
  if (left === null || left === undefined || right === null || right === undefined) {
    return false;
  }
  return String(left) === String(right);
}

/**
 * Where the currently selected graph node appears relative to the opened
 * function, using the graph edges (call-site highlighting relies on this).
 *
 * Returns `null`, or `{file, line, self, role}` with role
 * `self|callee|caller` (`callee`: the opened function calls the node).
 */
export function callSiteForNode(detail, graph, symbol) {
  const fn = detail && detail.function;
  const rootId = fn && (fn.id || fn.name);
  const target = String(symbol || "").trim();
  if (!target || !rootId) return null;
  if (looseEqual(target, rootId) || looseEqual(target, fn.name)) {
    return {
      file: fn.file || "",
      line: fn.line || 0,
      self: true,
      role: "self",
    };
  }
  const edges = asArray(graph && graph.edges);
  const direct = edges.find(
    (edge) =>
      looseEqual(edge.caller_id, rootId) && looseEqual(edge.callee_id, target)
  );
  if (direct) {
    return {
      file: direct.call_file || "",
      line: direct.call_line || 0,
      self: false,
      role: "callee",
    };
  }
  const reverse = edges.find(
    (edge) =>
      looseEqual(edge.caller_id, target) && looseEqual(edge.callee_id, rootId)
  );
  if (reverse) {
    return {
      file: reverse.call_file || "",
      line: reverse.call_line || 0,
      self: false,
      role: "caller",
    };
  }
  return null;
}

/** `{code, message}` of any thrown value (ApiError or plain Error). */
export function errorInfo(error) {
  if (error === null || error === undefined) {
    return { code: "", message: "" };
  }
  const code = error.code ? String(error.code) : "";
  let message = "";
  if (typeof error === "string") {
    message = error;
  } else if (error.message) {
    message = String(error.message);
  } else {
    try {
      message = JSON.stringify(error);
    } catch (_jsonError) {
      message = String(error);
    }
  }
  return { code, message };
}

export function errorKind(error) {
  const code = errorInfo(error).code;
  if (code === "NOT_FOUND") return "notFound";
  if (code === "DB_UNAVAILABLE") return "dbUnavailable";
  return "other";
}

export function isAbortError(error) {
  if (!error) return false;
  if (error.name === "AbortError") return true;
  return errorInfo(error).code === "ABORTED";
}

function unwrapEnvelope(value) {
  if (value && typeof value === "object") {
    if (value.ok === false && value.error) {
      const error = new Error(
        value.error.message || value.error.code || "CA request failed"
      );
      error.code = value.error.code || "INTERNAL";
      error.details = value.error.details;
      throw error;
    }
    if (value.ok === true && Object.prototype.hasOwnProperty.call(value, "data")) {
      return value.data;
    }
  }
  return value;
}

/**
 * Create the narrow CA request helper the function view needs.
 *
 * Transport: `bag.api.caRequest({method, path, query, body, signal})` (unary
 * authenticated RPC); the result is expected to be already unwrapped data, but
 * an envelope is accepted and unwrapped defensively.
 */
export function createRequester(bag) {
  const api = bag && bag.api ? bag.api : {};
  function request(path, options) {
    const opts = options || {};
    try {
      if (typeof api.caRequest === "function") {
        return Promise.resolve(
          api.caRequest({
            method: opts.method || "GET",
            path,
            query: opts.query,
            body: opts.body,
            signal: opts.signal,
          })
        ).then(unwrapEnvelope);
      }
      if (typeof api.rpc === "function") {
        return Promise.resolve(
          api.rpc(
            "ca.request",
            { method: opts.method || "GET", path, query: opts.query, body: opts.body },
            opts.signal
          )
        ).then(unwrapEnvelope);
      }
      if (typeof api.request === "function") {
        return Promise.resolve(
          api.request({
            method: opts.method || "GET",
            path,
            query: opts.query,
            body: opts.body,
            signal: opts.signal,
          })
        ).then(unwrapEnvelope);
      }
    } catch (error) {
      return Promise.reject(error);
    }
    return Promise.reject(new Error("CA request API is not available"));
  }
  return request;
}

export function requestDetail(request, symbol, signal) {
  return request("/api/function/" + encodeURIComponent(symbol), { signal });
}

export function requestSource(request, symbol, signal) {
  return request(
    "/api/source?symbol=" + encodeURIComponent(symbol) + "&context=0",
    { signal }
  );
}

export function requestCfg(request, symbol, signal) {
  return request("/api/cfg?symbol=" + encodeURIComponent(symbol), { signal });
}

/** AbortController when the client runtime offers one. */
export function createAbortController() {
  if (typeof AbortController === "function") {
    try {
      return new AbortController();
    } catch (_error) {
      return null;
    }
  }
  return null;
}
