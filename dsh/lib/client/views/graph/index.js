/**
 * CA call-graph view for the DSH Client.
 *
 * Ported from the vanilla CA web UI (`src/ca/ui/static/app.js`, call-graph
 * section): a deterministic left-to-right column layout over the normalized
 * `/api/graph` payload, rendered as SVG. One direction-aware column scheme:
 * callers (reverse walk) sit left of the root, callees (forward walk) right,
 * so `both` shows a caller column, the root, and a callee column.
 *
 * Edge semantics (same as the existing UI):
 *   - direct call: solid grey;
 *   - indirect callback candidate: purple dashed;
 *   - virtual-dispatch candidate: teal dashed;
 *   - cycle edge: red.
 * Candidate edges are possible target sets; no score or ranking is shown.
 *
 * Public factory: `createCallGraph(React, bag) -> Component` with props
 * `{symbol, direction, depth, limit, onSelectNode, onOpenDetail, onGraphData}`.
 * The component owns its controls (direction, depth, rebuild) and its data
 * loading; `direction`/`depth`/`limit` are initial values only.
 */

/** Node geometry (px) — mirrors the vanilla UI proportions. */
const GRAPH_VIEW = {
  NODE_W: 196,
  NODE_H: 44,
  GAP_X: 76,
  GAP_Y: 16,
  PAD: 18,
  MIN_ZOOM: 0.25,
  MAX_ZOOM: 2.5,
}

const GRAPH_CSS = `
.ca-graph {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 320px;
  background: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-md, 8px);
  overflow: hidden;
  color: var(--dsw-alias-label-primary, #1a2230);
  font-family: var(--dsw-font-family, system-ui, sans-serif);
}
.ca-graph-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  background: var(--dsw-alias-bg-layer-2, transparent);
}
.ca-graph-title { margin: 0; font-size: 13px; font-weight: 600; }
.ca-graph-note { color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 11.5px; }
.ca-graph-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-left: auto; }
.ca-graph-label { display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--dsw-alias-label-secondary, #5a6577); }
.ca-graph-zoom-label {
  min-width: 44px;
  text-align: center;
  font-family: var(--ds-font-family-code, var(--dsw-font-mono, ui-monospace, monospace));
  font-size: 11.5px;
  color: var(--dsw-alias-label-secondary, #5a6577);
}
.ca-graph-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  color: var(--dsw-alias-label-tertiary, #8a94a5);
  font-size: 11px;
}
.ca-graph-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.ca-graph-swatch { display: inline-block; width: 18px; height: 0; border-top: 2px solid var(--dsw-alias-label-tertiary, #8a94a5); }
.ca-graph-swatch.candidate { border-top-color: #a855f7; border-top-style: dashed; }
.ca-graph-swatch.virtual { border-top-color: #14b8a6; border-top-style: dashed; }
.ca-graph-swatch.cycle { border-top-color: var(--dsw-alias-state-error-primary, #b42318); border-top-width: 2px; }
.ca-graph-body { position: relative; flex: 1; min-height: 0; }
.ca-graph-canvas { width: 100%; height: 100%; overflow: auto; padding: 8px; cursor: grab; }
.ca-graph-canvas.dragging { cursor: grabbing; user-select: none; }
.ca-graph-svg { display: block; }
.ca-graph-node { cursor: pointer; outline: none; }
.ca-graph-node rect {
  fill: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
  stroke: var(--dsw-alias-border-l3, #bcc6d4);
  stroke-width: 1.2;
}
.ca-graph-node:hover rect,
.ca-graph-node:focus-visible rect { stroke: var(--dsw-alias-state-business-primary, #2458d6); }
.ca-graph-node.root rect {
  fill: var(--dsw-alias-interactive-bg-hover, rgba(36, 88, 214, 0.08));
  stroke: var(--dsw-alias-state-business-primary, #2458d6);
  stroke-width: 2;
}
.ca-graph-node.selected rect {
  fill: var(--dsw-alias-interactive-bg-active, rgba(36, 88, 214, 0.14));
  stroke: var(--dsw-alias-state-business-primary, #2458d6);
  stroke-width: 2.4;
}
.ca-graph-node text {
  fill: var(--dsw-alias-label-primary, #1a2230);
  font-family: var(--ds-font-family-code, var(--dsw-font-mono, ui-monospace, monospace));
  font-size: 11.5px;
  pointer-events: none;
}
.ca-graph-node .ca-graph-node-sub { fill: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 9.5px; }
.ca-graph-edge path { fill: none; stroke: var(--dsw-alias-label-tertiary, #8a94a5); stroke-width: 1.3; }
.ca-graph-edge.candidate path { stroke: #a855f7; stroke-dasharray: 5 4; }
.ca-graph-edge.virtual path { stroke: #14b8a6; stroke-dasharray: 2 3; }
.ca-graph-edge.cycle path { stroke: var(--dsw-alias-state-error-primary, #b42318); stroke-width: 1.8; }
.ca-graph-marker-direct path { fill: var(--dsw-alias-label-tertiary, #8a94a5); }
.ca-graph-marker-candidate path { fill: #a855f7; }
.ca-graph-marker-virtual path { fill: #14b8a6; }
.ca-graph-marker-cycle path { fill: var(--dsw-alias-state-error-primary, #b42318); }
.ca-graph-status {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 200px;
  height: 100%;
  padding: 16px;
  color: var(--dsw-alias-label-tertiary, #8a94a5);
  font-size: 12.5px;
  text-align: center;
}
.ca-graph-status .ca-empty,
.ca-graph-status .ca-error { width: 100%; max-width: 560px; }
.ca-graph-btn {
  padding: 4px 9px;
  font-size: 11.5px;
  color: var(--dsw-alias-label-primary, #1a2230);
  background: var(--dsw-alias-bg-layer-2, transparent);
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-sm, 6px);
  cursor: pointer;
}
.ca-graph-btn:disabled { opacity: 0.5; cursor: default; }
.ca-graph-select {
  font-size: 11.5px;
  padding: 3px 6px;
  color: var(--dsw-alias-label-primary, #1a2230);
  background: var(--dsw-alias-bg-base, #ffffff);
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-sm, 6px);
}
`

const GRAPH_DEPTHS = [1, 2, 3, 4, 6]

/** Number of digits grouping for counts (locale-independent, stable). */
function graphFormatCount(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '0'
  return String(Math.trunc(number)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

/** Truncate a label, appending an ellipsis only when actually shortened. */
function graphTruncate(value, limit) {
  const text = String(value === undefined || value === null ? '' : value)
  if (text.length <= limit) return text
  return text.slice(0, Math.max(1, limit - 1)) + '…'
}

function graphClamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function graphNormalizeDirection(value) {
  const direction = String(value || '').toLowerCase()
  if (direction === 'forward' || direction === 'callees' || direction === 'callee' || direction === 'out') {
    return 'forward'
  }
  if (direction === 'reverse' || direction === 'callers' || direction === 'caller' || direction === 'in') {
    return 'reverse'
  }
  return 'both'
}

function graphNormalizeDepth(value) {
  const depth = Math.round(Number(value))
  if (!Number.isFinite(depth)) return 2
  return graphClamp(depth, 0, 12)
}

function graphNormalizeLimit(value) {
  const limit = Math.round(Number(value))
  if (!Number.isFinite(limit) || limit <= 0) return 500
  return graphClamp(limit, 1, 5000)
}

/** Stable per-node key: prefer the DB id, fall back to the display name. */
function graphNodeKey(node) {
  if (!node) return ''
  if (node.id) return 'id:' + String(node.id)
  if (node.name) return 'name:' + String(node.name)
  return ''
}

/** Edge classification shared by rendering and the legend. */
function graphEdgeKind(edge) {
  if (!edge) return 'direct'
  if (edge.cycle === true) return 'cycle'
  const source = String(edge.source || '')
  const kind = String(edge.kind || '')
  const candidate = edge.candidate === true || source === 'candidate' || source === 'call_target'
  if (source === 'virtual_candidate' || kind.indexOf('virtual') >= 0) {
    return candidate || source === 'virtual_candidate' ? 'virtual' : 'direct'
  }
  return candidate ? 'candidate' : 'direct'
}

/**
 * Compute the deterministic SVG layout for a `/api/graph` payload.
 * Returns `{width, height, nodes, edges}` where nodes/edges carry positions.
 */
function graphLayout(data) {
  const width = GRAPH_VIEW.NODE_W
  const height = GRAPH_VIEW.NODE_H
  const gapX = GRAPH_VIEW.GAP_X
  const gapY = GRAPH_VIEW.GAP_Y
  const pad = GRAPH_VIEW.PAD
  const nodes = Array.isArray(data && data.nodes) ? data.nodes : []
  const edges = Array.isArray(data && data.edges) ? data.edges : []

  const columns = new Map()
  nodes.forEach((node) => {
    const depthValue = Number.isFinite(Number(node.depth)) ? Number(node.depth) : 0
    const direction = String(node.direction || '')
    let column = 0
    if (depthValue !== 0) column = direction === 'reverse' ? -depthValue : depthValue
    if (!columns.has(column)) columns.set(column, [])
    columns.get(column).push(node)
  })
  const columnKeys = Array.from(columns.keys()).sort((left, right) => left - right)
  columnKeys.forEach((column) => {
    columns.get(column).sort((left, right) => {
      const byName = String(left.name || '').localeCompare(String(right.name || ''))
      if (byName !== 0) return byName
      return String(left.id || '').localeCompare(String(right.id || ''))
    })
  })
  const minColumn = Math.min(0, ...(columnKeys.length ? columnKeys : [0]))
  const maxColumn = Math.max(0, ...(columnKeys.length ? columnKeys : [0]))
  const columnCount = maxColumn - minColumn + 1
  let maxRows = 1
  columnKeys.forEach((column) => {
    maxRows = Math.max(maxRows, columns.get(column).length)
  })
  const svgWidth = pad * 2 + columnCount * width + Math.max(0, columnCount - 1) * gapX
  const svgHeight = Math.max(160, pad * 2 + maxRows * height + Math.max(0, maxRows - 1) * gapY)

  const positions = new Map()
  columnKeys.forEach((column) => {
    columns.get(column).forEach((node, rowIndex) => {
      positions.set(graphNodeKey(node), {
        x: pad + (column - minColumn) * (width + gapX),
        y: pad + rowIndex * (height + gapY),
        node,
      })
    })
  })

  const byId = new Map()
  const byName = new Map()
  nodes.forEach((node) => {
    if (node.id) byId.set(String(node.id), node)
    if (node.name && !byName.has(String(node.name))) byName.set(String(node.name), node)
  })
  function resolve(id, name) {
    if (id && byId.has(String(id))) return byId.get(String(id))
    if (name && byName.has(String(name))) return byName.get(String(name))
    return null
  }

  const renderedEdges = []
  edges.forEach((edge, index) => {
    const source = resolve(edge.caller_id, edge.caller_name)
    const target = resolve(edge.callee_id, edge.callee_name)
    if (!source || !target) return
    const from = positions.get(graphNodeKey(source))
    const to = positions.get(graphNodeKey(target))
    if (!from || !to) return
    const kind = graphEdgeKind(edge)
    const titleParts = [
      String(edge.caller_name || edge.caller_id || '?') + ' → ' + String(edge.callee_name || edge.callee_id || '?'),
    ]
    if (kind === 'candidate' || kind === 'virtual') {
      titleParts.push('(' + String(edge.kind || 'candidate') + '; possible target)')
    } else if (edge.kind === 'direct' || edge.source === 'direct' || edge.source === 'call') {
      titleParts.push('(direct call)')
    }
    let path
    if (from.x === to.x && from.y === to.y) {
      // Self-recursive call: a small loop above the node.
      const cx = from.x + width / 2
      const cy = from.y
      path =
        'M ' + (cx - 14) + ' ' + cy +
        ' C ' + (cx - 30) + ' ' + (cy - 30) + ', ' + (cx + 30) + ' ' + (cy - 30) + ', ' + (cx + 14) + ' ' + cy
    } else {
      const forward = to.x >= from.x
      const x1 = forward ? from.x + width : from.x
      const x2 = forward ? to.x : to.x + width
      const y1 = from.y + height / 2
      const y2 = to.y + height / 2
      const bend = Math.max(36, Math.abs(x2 - x1) * 0.55)
      const c1 = forward ? x1 + bend : x1 - bend
      const c2 = forward ? x2 - bend : x2 + bend
      path = 'M ' + x1 + ' ' + y1 + ' C ' + c1 + ' ' + y1 + ', ' + c2 + ' ' + y2 + ', ' + x2 + ' ' + y2
    }
    renderedEdges.push({
      key: 'edge-' + index + '-' + kind,
      kind,
      path,
      title: titleParts.join(' '),
    })
  })

  return {
    width: svgWidth,
    height: svgHeight,
    nodes: Array.from(positions.values()),
    edges: renderedEdges,
  }
}

export function createCallGraph(React, bag) {
  if (!React || typeof React.createElement !== 'function') {
    throw new TypeError('createCallGraph(React, bag): React with createElement is required')
  }
  const h = React.createElement
  const ui = (bag && bag.ui) || {}
  const api = (bag && bag.api) || {}
  const i18n = (bag && bag.i18n) || {}
  const useStyles = typeof ui.useStyles === 'function' ? ui.useStyles : function noGraphStyles() {}

  const Button =
    typeof ui.Button === 'function'
      ? ui.Button
      : function FallbackButton(props) {
          return h(
            'button',
            {
              type: 'button',
              className: 'ca-graph-btn',
              title: props.title,
              disabled: props.disabled ? true : undefined,
              onClick: props.onClick,
            },
            props.children,
          )
        }
  const Select =
    typeof ui.Select === 'function'
      ? ui.Select
      : function FallbackSelect(props) {
          const options = Array.isArray(props.options) ? props.options : []
          return h(
            'select',
            {
              className: 'ca-graph-select',
              value: props.value,
              title: props.title,
              onChange: (event) => {
                if (typeof props.onChange === 'function') props.onChange(event.target.value)
              },
            },
            options.map((option) =>
              h('option', { key: String(option.value), value: String(option.value) }, option.label),
            ),
          )
        }
  const Spinner =
    typeof ui.Spinner === 'function'
      ? ui.Spinner
      : function FallbackSpinner(props) {
          return h(
            'span',
            { className: 'ca-graph-status', role: 'status' },
            props.label || 'loading…',
          )
        }
  const ErrorBox =
    typeof ui.ErrorBox === 'function'
      ? ui.ErrorBox
      : function FallbackErrorBox(props) {
          const error = props.error !== undefined ? props.error : props.message
          const message =
            error && typeof error === 'object' ? String(error.message || error.code || error) : String(error || '')
          return h(
            'div',
            { className: 'ca-error', role: 'alert' },
            h('div', { className: 'ca-error-head' }, props.title || 'Error'),
            h('div', { className: 'ca-error-msg' }, message),
          )
        }
  const EmptyState =
    typeof ui.EmptyState === 'function'
      ? ui.EmptyState
      : function FallbackEmptyState(props) {
          const body = props.body !== undefined ? props.body : props.message
          return h(
            'div',
            { className: 'ca-empty' },
            props.title ? h('div', { className: 'ca-empty-title' }, props.title) : null,
            body ? h('div', { className: 'ca-empty-body' }, body) : null,
          )
        }

  function formatText(template, vars) {
    if (typeof template !== 'string') return String(template === undefined || template === null ? '' : template)
    if (!vars) return template
    return template.replace(/\{(\w+)\}/g, (match, name) => {
      if (!Object.prototype.hasOwnProperty.call(vars, name)) return match
      const value = vars[name]
      return value === undefined || value === null ? match : String(value)
    })
  }

  function t(key, vars, fallback) {
    if (i18n && typeof i18n.t === 'function') {
      try {
        const value = i18n.t(key, vars || null, fallback)
        if (typeof value === 'string' && value.length > 0 && value !== key) return value
      } catch (_error) {
        /* fall through to the local default */
      }
    }
    return formatText(fallback || key, vars)
  }

  function caGet(path, query, signal) {
    if (api && typeof api.caGet === 'function') return api.caGet(path, { query, signal })
    if (api && typeof api.caRequest === 'function') {
      return api.caRequest({ method: 'GET', path, query, signal })
    }
    return Promise.reject(new Error('CA API is unavailable'))
  }

  function errorParts(error) {
    const code = error && typeof error.code === 'string' ? error.code : ''
    const message =
      error && typeof error.message === 'string' && error.message
        ? error.message
        : error === undefined || error === null
          ? String(error)
          : String(error)
    return { code, message }
  }

  function nodeSub(node) {
    const file = node.file ? String(node.file) : ''
    const line = Number(node.line) || 0
    const location = file ? (line ? file + ':' + line : file) : ''
    const shard = node.shard ? t('common.shardName', { name: node.shard }, 'shard {name}') : ''
    return [location, shard].filter(Boolean).join(' · ')
  }

  function markerId(uid, kind) {
    return 'ca-graph-arrow-' + uid + '-' + kind
  }

  function markerPath() {
    return 'M 0 0 L 10 5 L 0 10 z'
  }

  function renderEdge(edge, uid) {
    return h(
      'g',
      { key: edge.key, className: 'ca-graph-edge ' + edge.kind },
      h('path', { d: edge.path, markerEnd: 'url(#' + markerId(uid, edge.kind) + ')' }, h('title', null, edge.title)),
    )
  }

  function renderNode(entry, selectedKey, onNodeClick, onNodeDoubleClick, onNodeKeyDown) {
    const node = entry.node
    const key = graphNodeKey(node)
    const isRoot = Number(node.depth) === 0
    const selected = selectedKey === key
    const name = String(node.name || node.id || '?')
    const sub = nodeSub(node)
    const className =
      'ca-graph-node' + (isRoot ? ' root' : '') + (selected ? ' selected' : '')
    return h(
      'g',
      {
        key,
        className,
        role: 'button',
        tabIndex: 0,
        'aria-label': name + (sub ? ' — ' + sub : ''),
        onClick: () => onNodeClick(node),
        onDoubleClick: () => onNodeDoubleClick(node),
        onKeyDown: (event) => onNodeKeyDown(node, event),
      },
      h('title', null, name + (sub ? ' — ' + sub : '')),
      h('rect', { x: entry.x, y: entry.y, width: GRAPH_VIEW.NODE_W, height: GRAPH_VIEW.NODE_H, rx: 7 }),
      h('text', { x: entry.x + 11, y: entry.y + 17 }, graphTruncate(name, 26)),
      h('text', { className: 'ca-graph-node-sub', x: entry.x + 11, y: entry.y + 32 }, graphTruncate(sub, 36)),
    )
  }

  /**
   * The call-graph component.
   *
   * @param {object} props
   * @param {string} props.symbol root symbol (required to load data)
   * @param {string} [props.direction] initial `both|forward|reverse`
   * @param {number} [props.depth] initial traversal depth (1–12)
   * @param {number} [props.limit] edge/node limit (default 500)
   * @param {(symbol: string) => void} [props.onSelectNode] single-click (delayed)
   * @param {(symbol: string) => void} [props.onOpenDetail] double-click / open detail
   * @param {(data: object|null) => void} [props.onGraphData] loaded graph payload
   */
  function CallGraph(props) {
    useStyles(GRAPH_CSS)
    const symbol = props && props.symbol ? String(props.symbol) : ''
    const limit = graphNormalizeLimit(props && props.limit)
    const [direction, setDirection] = React.useState(graphNormalizeDirection(props && props.direction))
    const [depth, setDepth] = React.useState(graphNormalizeDepth(props && props.depth))
    const [reloadToken, setReloadToken] = React.useState(0)
    const [loadState, setLoadState] = React.useState({ loading: false, error: null, data: null })
    const [selectedKey, setSelectedKey] = React.useState('')
    const [zoom, setZoom] = React.useState(1)
    const [autoFit, setAutoFit] = React.useState(true)
    const [panning, setPanning] = React.useState(false)
    const uid = React.useId ? String(React.useId()) : 'g' + String(reloadToken)
    const safeUid = uid.replace(/[^A-Za-z0-9_-]/g, '')
    const wrapRef = React.useRef(null)
    const clickTimerRef = React.useRef(null)
    const dragRef = React.useRef(null)
    const dataCallbackRef = React.useRef(null)
    dataCallbackRef.current = typeof props.onGraphData === 'function' ? props.onGraphData : null

    React.useEffect(() => {
      if (props && props.direction !== undefined) setDirection(graphNormalizeDirection(props.direction))
    }, [props && props.direction])

    React.useEffect(() => {
      if (props && props.depth !== undefined) setDepth(graphNormalizeDepth(props.depth))
    }, [props && props.depth])

    React.useEffect(() => () => {
      if (clickTimerRef.current) {
        clearTimeout(clickTimerRef.current)
        clickTimerRef.current = null
      }
    }, [])

    React.useEffect(() => {
      setSelectedKey('')
      setAutoFit(true)
      if (!symbol) {
        setLoadState({ loading: false, error: null, data: null })
        if (dataCallbackRef.current) dataCallbackRef.current(null)
        return undefined
      }
      let cancelled = false
      let controller = null
      try {
        if (typeof AbortController === 'function') controller = new AbortController()
      } catch (_error) {
        controller = null
      }
      setLoadState((previous) => ({ loading: true, error: null, data: previous.data }))
      caGet('/api/graph', { symbol, direction, depth, limit }, controller ? controller.signal : undefined)
        .then((data) => {
          if (cancelled) return
          const payload = data && typeof data === 'object' ? data : {}
          setLoadState({ loading: false, error: null, data: payload })
          if (dataCallbackRef.current) dataCallbackRef.current(payload)
        })
        .catch((error) => {
          if (cancelled) return
          const parts = errorParts(error)
          if (parts.code === 'ABORTED') return
          setLoadState({ loading: false, error: parts, data: null })
          if (dataCallbackRef.current) dataCallbackRef.current(null)
        })
      return () => {
        cancelled = true
        if (controller) {
          try {
            controller.abort()
          } catch (_error) {
            /* ignore abort failures */
          }
        }
      }
    }, [symbol, direction, depth, limit, reloadToken])

    const data = loadState.data
    const layout = React.useMemo(() => (data ? graphLayout(data) : null), [data])
    const layoutRef = React.useRef(null)
    layoutRef.current = layout

    function measureFitZoom() {
      const wrap = wrapRef.current
      const current = layoutRef.current
      if (!wrap || !current || current.width <= 0 || current.height <= 0) return 1
      const availableWidth = Math.max(60, wrap.clientWidth - 20)
      const availableHeight = Math.max(60, wrap.clientHeight - 20)
      return graphClamp(
        Math.min(availableWidth / current.width, availableHeight / current.height),
        GRAPH_VIEW.MIN_ZOOM,
        1.4,
      )
    }

    React.useEffect(() => {
      if (!autoFit || !layout) return undefined
      setZoom(measureFitZoom())
      const wrap = wrapRef.current
      let observer = null
      if (wrap && typeof ResizeObserver === 'function') {
        observer = new ResizeObserver(() => {
          if (!autoFit) return
          setZoom(measureFitZoom())
        })
        try {
          observer.observe(wrap)
        } catch (_error) {
          observer = null
        }
      }
      return () => {
        if (observer) observer.disconnect()
      }
    }, [autoFit, layout])

    const nodes = data && Array.isArray(data.nodes) ? data.nodes : []
    const stats = {
      nodes: data && data.stats && Number.isFinite(Number(data.stats.nodes)) ? Number(data.stats.nodes) : nodes.length,
      edges:
        data && data.stats && Number.isFinite(Number(data.stats.edges))
          ? Number(data.stats.edges)
          : data && Array.isArray(data.edges)
            ? data.edges.length
            : 0,
      cycles: data && Array.isArray(data.cycles) ? data.cycles.length : 0,
      truncated: Boolean(data && data.truncated),
    }

    function symbolOf(node) {
      return node ? String(node.id || node.name || '') : ''
    }

    function emitSelect(node) {
      const selected = symbolOf(node)
      if (!selected) return
      setSelectedKey(graphNodeKey(node))
      if (typeof props.onSelectNode === 'function') props.onSelectNode(selected)
      else if (typeof props.onOpenNode === 'function') props.onOpenNode(selected)
    }

    function clearClickTimer() {
      if (clickTimerRef.current) {
        clearTimeout(clickTimerRef.current)
        clickTimerRef.current = null
      }
    }

    function handleNodeClick(node) {
      clearClickTimer()
      clickTimerRef.current = setTimeout(() => {
        clickTimerRef.current = null
        emitSelect(node)
      }, 250)
    }

    function handleNodeDoubleClick(node) {
      clearClickTimer()
      const selected = symbolOf(node)
      if (!selected) return
      setSelectedKey(graphNodeKey(node))
      if (typeof props.onOpenDetail === 'function') props.onOpenDetail(selected)
    }

    function handleNodeKeyDown(node, event) {
      if (!event) return
      if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return
      event.preventDefault()
      clearClickTimer()
      emitSelect(node)
    }

    function zoomBy(factor) {
      setAutoFit(false)
      setZoom((previous) => graphClamp(previous * factor, GRAPH_VIEW.MIN_ZOOM, GRAPH_VIEW.MAX_ZOOM))
    }

    function resetZoom() {
      setAutoFit(true)
      setZoom(measureFitZoom())
    }

    function onCanvasPointerDown(event) {
      if (!event || event.button !== 0) return
      const target = event.target
      if (target && typeof target.closest === 'function' && target.closest('.ca-graph-node')) return
      const wrap = wrapRef.current
      if (!wrap) return
      dragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: wrap.scrollLeft,
        top: wrap.scrollTop,
        moved: false,
      }
      setPanning(true)
      try {
        if (wrap.setPointerCapture) wrap.setPointerCapture(event.pointerId)
      } catch (_error) {
        /* pointer capture is optional */
      }
    }

    function onCanvasPointerMove(event) {
      const drag = dragRef.current
      const wrap = wrapRef.current
      if (!drag || !wrap) return
      const dx = event.clientX - drag.startX
      const dy = event.clientY - drag.startY
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true
      wrap.scrollLeft = drag.left - dx
      wrap.scrollTop = drag.top - dy
    }

    function onCanvasPointerUp() {
      const wrap = wrapRef.current
      const drag = dragRef.current
      dragRef.current = null
      setPanning(false)
      if (drag && wrap && wrap.releasePointerCapture) {
        try {
          wrap.releasePointerCapture(drag.pointerId)
        } catch (_error) {
          /* ignore */
        }
      }
    }

    const directionOptions = [
      { value: 'both', label: t('graph.both', null, 'both') },
      { value: 'forward', label: t('graph.forward', null, 'callees (forward)') },
      { value: 'reverse', label: t('graph.reverse', null, 'callers (reverse)') },
    ]
    const depthOptions = GRAPH_DEPTHS.map((value) => ({ value: String(value), label: String(value) }))

    let noteText
    if (loadState.loading && !data) noteText = t('status.buildingGraph', null, 'Building call graph…')
    else if (loadState.error) noteText = loadState.error.message
    else if (data) {
      noteText = [
        t('graph.nodeCount', { count: graphFormatCount(stats.nodes) }, '{count} nodes'),
        t('graph.edgeCount', { count: graphFormatCount(stats.edges) }, '{count} edges'),
        stats.truncated ? t('graph.truncated', null, 'truncated') : '',
        stats.cycles
          ? t('graph.cycleCount', { count: graphFormatCount(stats.cycles) }, '{count} cycles')
          : '',
      ]
        .filter(Boolean)
        .join(' · ')
    } else {
      noteText = t('graph.nodeCount', { count: '0' }, '{count} nodes')
    }

    const toolbar = h(
      'div',
      { className: 'ca-graph-toolbar' },
      h(
        'label',
        { className: 'ca-graph-label' },
        h('span', null, t('graph.direction', null, 'Direction')),
        h(Select, {
          value: direction,
          options: directionOptions,
          ariaLabel: t('graph.direction', null, 'Direction'),
          onChange: (next) => setDirection(graphNormalizeDirection(next)),
        }),
      ),
      h(
        'label',
        { className: 'ca-graph-label' },
        h('span', null, t('graph.depth', null, 'Depth')),
        h(Select, {
          value: String(depth),
          options: depthOptions,
          ariaLabel: t('graph.depth', null, 'Depth'),
          onChange: (next) => setDepth(graphNormalizeDepth(next)),
        }),
      ),
      h(
        Button,
        {
          variant: 'secondary',
          size: 'sm',
          onClick: () => setReloadToken((value) => value + 1),
          title: t('graph.rebuild', null, 'Rebuild'),
        },
        t('graph.rebuild', null, 'Rebuild'),
      ),
      h(
        Button,
        {
          variant: 'ghost',
          size: 'sm',
          onClick: () => zoomBy(1 / 1.2),
          title: t('graph.zoomOut', null, 'Zoom out'),
          ariaLabel: t('graph.zoomOut', null, 'Zoom out'),
        },
        '−',
      ),
      h('span', { className: 'ca-graph-zoom-label' }, Math.round(zoom * 100) + '%'),
      h(
        Button,
        {
          variant: 'ghost',
          size: 'sm',
          onClick: () => zoomBy(1.2),
          title: t('graph.zoomIn', null, 'Zoom in'),
          ariaLabel: t('graph.zoomIn', null, 'Zoom in'),
        },
        '+',
      ),
      h(
        Button,
        {
          variant: 'ghost',
          size: 'sm',
          onClick: resetZoom,
          title: t('graph.zoomFit', null, 'Fit to view'),
        },
        t('graph.zoomFit', null, 'Fit'),
      ),
    )

    const legend = h(
      'div',
      { className: 'ca-graph-legend' },
      h(
        'span',
        { className: 'ca-graph-legend-item' },
        h('span', { className: 'ca-graph-swatch direct' }),
        t('graph.legendDirect', null, 'direct call'),
      ),
      h(
        'span',
        { className: 'ca-graph-legend-item' },
        h('span', { className: 'ca-graph-swatch candidate' }),
        t('graph.legendCandidate', null, 'indirect callback candidate (purple dashed; possible target)'),
      ),
      h(
        'span',
        { className: 'ca-graph-legend-item' },
        h('span', { className: 'ca-graph-swatch virtual' }),
        t('graph.legendVirtual', null, 'virtual candidate (teal dashed; possible target)'),
      ),
      h(
        'span',
        { className: 'ca-graph-legend-item' },
        h('span', { className: 'ca-graph-swatch cycle' }),
        t('graph.legendCycle', null, 'cycle'),
      ),
      h('span', { className: 'ca-graph-legend-item' }, t('graph.legendTargetSet', null, 'dashed edges = the set of possible targets at this call site')),
      h('span', { className: 'ca-graph-legend-item' }, t('graph.legendClick', null, 'click a node to view its source on the right')),
    )

    let body
    if (loadState.error) {
      body = h(
        'div',
        { className: 'ca-graph-status' },
        h(ErrorBox, {
          title: t('error.loadGraph', null, 'Could not load the call graph'),
          error: loadState.error,
          message: loadState.error.message,
          onRetry: () => setReloadToken((value) => value + 1),
          retryLabel: t('graph.rebuild', null, 'Rebuild'),
        }),
      )
    } else if (loadState.loading && !data) {
      body = h(
        'div',
        { className: 'ca-graph-status' },
        h(Spinner, { label: t('status.buildingGraph', null, 'Building call graph…') }),
      )
    } else if (!data || nodes.length === 0) {
      body = h(
        'div',
        { className: 'ca-graph-status' },
        h(EmptyState, {
          title: t('section.callGraph', null, 'Call graph'),
          body: t(
            'empty.graph',
            null,
            'No call-graph nodes found for this symbol. The graph is built from indexed calls and cross-TU callback candidates; a base database may have no edges.',
          ),
        }),
      )
    } else {
      const defs = h(
        'defs',
        null,
        ['direct', 'candidate', 'virtual', 'cycle'].map((kind) =>
          h(
            'marker',
            {
              key: kind,
              id: markerId(safeUid, kind),
              viewBox: '0 0 10 10',
              refX: 9,
              refY: 5,
              markerWidth: 7,
              markerHeight: 7,
              orient: 'auto-start-reverse',
              className: 'ca-graph-marker ca-graph-marker-' + kind,
            },
            h('path', { d: markerPath() }),
          ),
        ),
      )
      body = h(
        'div',
        { className: 'ca-graph-body' },
        h(
          'div',
          {
            ref: wrapRef,
            className: 'ca-graph-canvas' + (panning ? ' dragging' : ''),
            onPointerDown: onCanvasPointerDown,
            onPointerMove: onCanvasPointerMove,
            onPointerUp: onCanvasPointerUp,
            onPointerCancel: onCanvasPointerUp,
          },
          h(
            'svg',
            {
              className: 'ca-graph-svg',
              xmlns: 'http://www.w3.org/2000/svg',
              viewBox: '0 0 ' + layout.width + ' ' + layout.height,
              width: Math.max(1, Math.round(layout.width * zoom)),
              height: Math.max(1, Math.round(layout.height * zoom)),
              role: 'img',
              'aria-label': t(
                'graph.aria',
                { root: data.root || symbol },
                'Call graph for {root}',
              ),
            },
            defs,
            h(
              'g',
              { key: 'edges' },
              layout.edges.map((edge) => renderEdge(edge, safeUid)),
            ),
            h(
              'g',
              { key: 'nodes' },
              layout.nodes.map((entry) =>
                renderNode(entry, selectedKey, handleNodeClick, handleNodeDoubleClick, handleNodeKeyDown),
              ),
            ),
          ),
        ),
      )
    }

    return h(
      'section',
      { className: 'ca-graph', 'aria-busy': loadState.loading ? 'true' : 'false' },
      h(
        'div',
        { className: 'ca-graph-head' },
        h('h3', { className: 'ca-graph-title' }, t('section.callGraph', null, 'Call graph')),
        h('span', { className: 'ca-graph-note' }, noteText),
        toolbar,
      ),
      legend,
      body,
    )
  }

  return CallGraph
}

export default createCallGraph
