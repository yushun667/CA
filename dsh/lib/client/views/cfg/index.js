/**
 * CA control-flow-graph view for the DSH Client.
 *
 * Ported from the vanilla CA web UI (`src/ca/ui/static/app.js`, branches
 * accordion): the `/api/cfg` payload (`blocks`/`edges`/`branches`) is laid out
 * as a depth-ranked left-to-right SVG. Entry/exit blocks are highlighted, true
 * and false branch edges use different colours and carry `T`/`F` labels, back
 * edges are dashed, and the graph zooms 40–180 % (plus drag-to-pan). Functions
 * without deep analysis show a "deep facts pending" fallback.
 *
 * Public factory: `createCfgGraph(React, bag) -> Component` with props
 * `{symbol, zoom?, onLoaded?}`; the component loads and renders everything
 * including loading/error/empty states.
 */

const CFG_GEOMETRY = {
  NODE_W: 190,
  NODE_H: 66,
  GAP_X: 88,
  GAP_Y: 22,
  PAD: 22,
  MIN_ZOOM: 0.4,
  MAX_ZOOM: 1.8,
  ZOOM_STEP: 0.2,
}

const CFG_CSS = `
.ca-cfg {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  width: 100%;
  min-height: 260px;
  background: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-md, 8px);
  overflow: hidden;
  color: var(--dsw-alias-label-primary, #1a2230);
  font-family: var(--dsw-font-family, system-ui, sans-serif);
}
.ca-cfg-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  background: var(--dsw-alias-bg-layer-2, transparent);
}
.ca-cfg-title { margin: 0; font-size: 13px; font-weight: 600; }
.ca-cfg-sub { color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 11px; }
.ca-cfg-note { color: var(--dsw-alias-label-secondary, #5a6577); font-size: 11.5px; }
.ca-cfg-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-left: auto; }
.ca-cfg-zoom-label {
  min-width: 44px;
  text-align: center;
  font-family: var(--ds-font-family-code, var(--dsw-font-mono, ui-monospace, monospace));
  font-size: 11.5px;
  color: var(--dsw-alias-label-secondary, #5a6577);
}
.ca-cfg-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  color: var(--dsw-alias-label-tertiary, #8a94a5);
  font-size: 11px;
}
.ca-cfg-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.ca-cfg-swatch {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 1.5px solid var(--dsw-alias-border-l3, #bcc6d4);
  border-radius: 3px;
  background: var(--dsw-alias-bg-layer-2, transparent);
}
.ca-cfg-swatch.entry { border-color: var(--dsw-alias-state-success-primary, #157347); background: var(--dsw-alias-state-success-secondary, transparent); }
.ca-cfg-swatch.exit { border-color: var(--dsw-alias-state-error-primary, #b42318); background: var(--dsw-alias-state-error-secondary, transparent); }
.ca-cfg-swatch.true { border-color: var(--dsw-alias-state-success-primary, #157347); background: var(--dsw-alias-state-success-primary, #157347); }
.ca-cfg-swatch.false { border-color: var(--dsw-alias-state-error-primary, #b42318); background: var(--dsw-alias-state-error-primary, #b42318); }
.ca-cfg-swatch.back { border-color: var(--dsw-alias-state-warn-primary, #a15c07); background: var(--dsw-alias-state-warn-primary, #a15c07); }
.ca-cfg-body { position: relative; flex: 1; min-height: 0; }
.ca-cfg-canvas { overflow: auto; max-height: 640px; height: 100%; padding: 10px; cursor: grab; }
.ca-cfg-canvas.dragging { cursor: grabbing; user-select: none; }
.ca-cfg-svg { display: block; }
.ca-cfg-node rect {
  fill: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
  stroke: var(--dsw-alias-border-l3, #bcc6d4);
  stroke-width: 1.2;
}
.ca-cfg-node.entry rect { fill: var(--dsw-alias-state-success-secondary, rgba(21, 115, 71, 0.1)); stroke: var(--dsw-alias-state-success-primary, #157347); stroke-width: 2; }
.ca-cfg-node.exit rect { fill: var(--dsw-alias-state-error-secondary, rgba(180, 35, 24, 0.1)); stroke: var(--dsw-alias-state-error-primary, #b42318); stroke-width: 2; }
.ca-cfg-node.branch rect { stroke: var(--dsw-alias-state-business-primary, #2458d6); stroke-width: 1.6; }
.ca-cfg-node text {
  fill: var(--dsw-alias-label-primary, #1a2230);
  font-family: var(--ds-font-family-code, var(--dsw-font-mono, ui-monospace, monospace));
  font-size: 10.5px;
}
.ca-cfg-node .ca-cfg-node-sub { fill: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 9.5px; }
.ca-cfg-node .ca-cfg-node-condition { fill: var(--dsw-alias-state-business-primary, #2458d6); font-size: 9.5px; }
.ca-cfg-edge path { fill: none; stroke: var(--dsw-alias-label-tertiary, #8a94a5); stroke-width: 1.3; }
.ca-cfg-edge.true path { stroke: var(--dsw-alias-state-success-primary, #157347); }
.ca-cfg-edge.false path { stroke: var(--dsw-alias-state-error-primary, #b42318); }
.ca-cfg-edge.back path { stroke: var(--dsw-alias-state-warn-primary, #a15c07); stroke-dasharray: 5 4; }
.ca-cfg-edge text {
  fill: var(--dsw-alias-label-secondary, #5a6577);
  font-family: var(--ds-font-family-code, var(--dsw-font-mono, ui-monospace, monospace));
  font-size: 9.5px;
  paint-order: stroke;
  stroke: var(--dsw-alias-bg-layer-1, #ffffff);
  stroke-width: 3px;
}
.ca-cfg-edge.true text { fill: var(--dsw-alias-state-success-primary, #157347); }
.ca-cfg-edge.false text { fill: var(--dsw-alias-state-error-primary, #b42318); }
.ca-cfg-marker-normal path { fill: var(--dsw-alias-label-tertiary, #8a94a5); }
.ca-cfg-marker-true path { fill: var(--dsw-alias-state-success-primary, #157347); }
.ca-cfg-marker-false path { fill: var(--dsw-alias-state-error-primary, #b42318); }
.ca-cfg-marker-back path { fill: var(--dsw-alias-state-warn-primary, #a15c07); }
.ca-cfg-callout {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin: 12px;
  padding: 10px 13px;
  border: 1px solid var(--dsw-alias-state-warn-secondary, rgba(161, 92, 7, 0.35));
  border-left: 3px solid var(--dsw-alias-state-warn-primary, #a15c07);
  border-radius: var(--dsw-radius-sm, 6px);
  background: var(--dsw-alias-state-warn-secondary, rgba(161, 92, 7, 0.08));
  color: var(--dsw-alias-state-warn-label, var(--dsw-alias-label-primary, #1a2230));
  font-size: 12.5px;
}
.ca-cfg-status { padding: 24px 12px; text-align: center; color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 12.5px; }
.ca-cfg-status .ca-error, .ca-cfg-status .ca-empty { max-width: 560px; margin: 0 auto; }
.ca-cfg-btn {
  padding: 4px 9px;
  font-size: 11.5px;
  color: var(--dsw-alias-label-primary, #1a2230);
  background: var(--dsw-alias-bg-layer-2, transparent);
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-sm, 6px);
  cursor: pointer;
}
.ca-cfg-btn:disabled { opacity: 0.5; cursor: default; }
`

function cfgClamp(value, min, max) {
  return Math.min(max, Math.max(min, value))
}

function cfgTruncate(value, limit) {
  const text = String(value === undefined || value === null ? '' : value)
  if (text.length <= limit) return text
  return text.slice(0, Math.max(1, limit - 1)) + '…'
}

function cfgNormalizeZoom(value) {
  const zoom = Number(value)
  if (!Number.isFinite(zoom)) return 1
  return cfgClamp(Math.round(zoom * 10) / 10, CFG_GEOMETRY.MIN_ZOOM, CFG_GEOMETRY.MAX_ZOOM)
}

/**
 * Deterministic CFG layout: BFS depth columns from the entry block, then
 * index-sorted rows per column (same scheme as the vanilla UI).
 */
function cfgLayout(data) {
  const nodeW = CFG_GEOMETRY.NODE_W
  const nodeH = CFG_GEOMETRY.NODE_H
  const gapX = CFG_GEOMETRY.GAP_X
  const gapY = CFG_GEOMETRY.GAP_Y
  const pad = CFG_GEOMETRY.PAD
  const blocks = Array.isArray(data && data.blocks) ? data.blocks : []
  const edges = Array.isArray(data && data.edges) ? data.edges : []
  const byId = new Map()
  blocks.forEach((block) => byId.set(block.id, block))
  const outgoing = new Map()
  const incoming = new Map()
  blocks.forEach((block) => {
    outgoing.set(block.id, [])
    incoming.set(block.id, 0)
  })
  edges.forEach((edge) => {
    if (outgoing.has(edge.src)) outgoing.get(edge.src).push(edge)
    if (incoming.has(edge.dst)) incoming.set(edge.dst, incoming.get(edge.dst) + 1)
  })
  let entry = blocks.find((block) => block.is_entry)
  if (!entry) entry = blocks.find((block) => !incoming.get(block.id))
  if (!entry) entry = blocks[0]
  const depth = new Map()
  const queue = []
  if (entry) {
    depth.set(entry.id, 0)
    queue.push(entry.id)
  }
  while (queue.length) {
    const id = queue.shift()
    const next = depth.get(id) + 1
    const outgoingEdges = outgoing.get(id) || []
    for (const edge of outgoingEdges) {
      if (!byId.has(edge.dst) || depth.has(edge.dst)) continue
      depth.set(edge.dst, next)
      queue.push(edge.dst)
    }
  }
  let maxDepth = 0
  depth.forEach((value) => {
    if (value > maxDepth) maxDepth = value
  })
  blocks.forEach((block) => {
    if (!depth.has(block.id)) {
      maxDepth += 1
      depth.set(block.id, maxDepth)
    }
  })
  const columns = new Map()
  blocks.forEach((block) => {
    const column = depth.get(block.id)
    if (!columns.has(column)) columns.set(column, [])
    columns.get(column).push(block)
  })
  const columnKeys = Array.from(columns.keys()).sort((left, right) => left - right)
  columnKeys.forEach((column) => {
    columns.get(column).sort((left, right) => {
      const leftIndex = Number(left.index === undefined || left.index === null ? 0 : left.index)
      const rightIndex = Number(right.index === undefined || right.index === null ? 0 : right.index)
      return (
        leftIndex - rightIndex ||
        String(left.id).localeCompare(String(right.id))
      )
    })
  })
  const positions = new Map()
  let rows = 0
  columnKeys.forEach((column, columnIndex) => {
    const columnBlocks = columns.get(column)
    rows = Math.max(rows, columnBlocks.length)
    columnBlocks.forEach((block, rowIndex) => {
      positions.set(block.id, {
        x: pad + columnIndex * (nodeW + gapX),
        y: pad + rowIndex * (nodeH + gapY),
      })
    })
  })
  const width = pad * 2 + columnKeys.length * nodeW + Math.max(0, columnKeys.length - 1) * gapX
  const height = Math.max(160, pad * 2 + rows * nodeH + Math.max(0, rows - 1) * gapY)

  const renderedEdges = []
  edges.forEach((edge, index) => {
    const from = positions.get(edge.src)
    const to = positions.get(edge.dst)
    if (!from || !to) return
    const back = to.x < from.x || (to.x === from.x && to.y <= from.y)
    const startX = from.x + (back ? 0 : nodeW)
    const startY = from.y + nodeH / 2
    const endX = to.x + (back ? nodeW : 0)
    const endY = to.y + nodeH / 2
    const bend = Math.max(34, Math.abs(endX - startX) * 0.5)
    const control1 = back ? startX - bend : startX + bend
    const control2 = back ? endX + bend : endX - bend
    const path =
      'M ' + startX + ' ' + startY +
      ' C ' + control1 + ' ' + startY + ', ' + control2 + ' ' + endY + ', ' + endX + ' ' + endY
    let kind = 'normal'
    if (edge.label === 'true') kind = 'true'
    else if (edge.label === 'false') kind = 'false'
    else if (back) kind = 'back'
    const label = edge.label === 'true' ? 'T' : edge.label === 'false' ? 'F' : ''
    renderedEdges.push({
      key: 'edge-' + index + '-' + kind,
      kind,
      path,
      label,
      labelX: (startX + endX) / 2,
      labelY: (startY + endY) / 2 - 5,
      title:
        String(edge.src) + ' → ' + String(edge.dst) + (edge.label ? ' (' + String(edge.label) + ')' : ''),
    })
  })

  return {
    width,
    height,
    nodes: blocks.map((block) => ({
      block,
      position: positions.get(block.id),
      isEntry: Boolean(block.is_entry) || Boolean(entry && block.id === entry.id),
      isExit: Boolean(block.is_exit),
      isBranch: Boolean(block.condition),
    })),
    edges: renderedEdges,
  }
}

export function createCfgGraph(React, bag) {
  if (!React || typeof React.createElement !== 'function') {
    throw new TypeError('createCfgGraph(React, bag): React with createElement is required')
  }
  const h = React.createElement
  const ui = (bag && bag.ui) || {}
  const api = (bag && bag.api) || {}
  const i18n = (bag && bag.i18n) || {}
  const useStyles = typeof ui.useStyles === 'function' ? ui.useStyles : function noCfgStyles() {}

  const Button =
    typeof ui.Button === 'function'
      ? ui.Button
      : function FallbackCfgButton(props) {
          return h(
            'button',
            {
              type: 'button',
              className: 'ca-cfg-btn',
              title: props.title,
              disabled: props.disabled ? true : undefined,
              onClick: props.onClick,
            },
            props.children,
          )
        }
  const Spinner =
    typeof ui.Spinner === 'function'
      ? ui.Spinner
      : function FallbackCfgSpinner(props) {
          return h('span', { role: 'status' }, props.label || 'loading…')
        }
  const ErrorBox =
    typeof ui.ErrorBox === 'function'
      ? ui.ErrorBox
      : function FallbackCfgErrorBox(props) {
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
      : function FallbackCfgEmptyState(props) {
          const body = props.body !== undefined ? props.body : props.message
          return h(
            'div',
            { className: 'ca-empty' },
            props.title ? h('div', { className: 'ca-empty-title' }, props.title) : null,
            body ? h('div', { className: 'ca-empty-body' }, body) : null,
          )
        }
  const Badge =
    typeof ui.Badge === 'function'
      ? ui.Badge
      : function FallbackCfgBadge(props) {
          return h('span', { className: 'ca-badge' }, props.children)
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

  function markerId(uid, kind) {
    return 'ca-cfg-arrow-' + uid + '-' + kind
  }

  /**
   * CFG component.
   *
   * @param {object} props
   * @param {string} props.symbol function symbol (required to load)
   * @param {number} [props.zoom] initial zoom (0.4–1.8)
   * @param {(data: object) => void} [props.onLoaded] loaded `/api/cfg` payload
   */
  function CfgGraphView(props) {
    useStyles(CFG_CSS)
    const symbol = props && props.symbol ? String(props.symbol) : ''
    const [zoom, setZoom] = React.useState(cfgNormalizeZoom(props && props.zoom))
    const [reloadToken, setReloadToken] = React.useState(0)
    const [loadState, setLoadState] = React.useState({ loading: false, error: null, data: null })
    const [panning, setPanning] = React.useState(false)
    const wrapRef = React.useRef(null)
    const dragRef = React.useRef(null)
    const loadedCallbackRef = React.useRef(null)
    loadedCallbackRef.current = typeof props.onLoaded === 'function' ? props.onLoaded : null
    const uid = React.useId ? String(React.useId()) : 'cfg' + String(reloadToken)
    const safeUid = uid.replace(/[^A-Za-z0-9_-]/g, '')

    React.useEffect(() => {
      if (!symbol) {
        setLoadState({ loading: false, error: null, data: null })
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
      caGet('/api/cfg', { symbol }, controller ? controller.signal : undefined)
        .then((data) => {
          if (cancelled) return
          const payload = data && typeof data === 'object' ? data : {}
          setLoadState({ loading: false, error: null, data: payload })
          if (loadedCallbackRef.current) loadedCallbackRef.current(payload)
        })
        .catch((error) => {
          if (cancelled) return
          const parts = errorParts(error)
          if (parts.code === 'ABORTED') return
          setLoadState({ loading: false, error: parts, data: null })
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
    }, [symbol, reloadToken])

    const data = loadState.data
    const layout = React.useMemo(() => (data ? cfgLayout(data) : null), [data])
    const blocks = data && Array.isArray(data.blocks) ? data.blocks : []
    const stats = {
      blocks:
        data && data.stats && Number.isFinite(Number(data.stats.blocks)) ? Number(data.stats.blocks) : blocks.length,
      edges:
        data && data.stats && Number.isFinite(Number(data.stats.edges))
          ? Number(data.stats.edges)
          : data && Array.isArray(data.edges)
            ? data.edges.length
            : 0,
      branches:
        data && data.stats && Number.isFinite(Number(data.stats.branches))
          ? Number(data.stats.branches)
          : data && Array.isArray(data.branches)
            ? data.branches.length
            : 0,
    }
    const functionInfo = (data && data.function) || {}
    const functionLabel = String(functionInfo.name || functionInfo.qualified_name || symbol)

    function zoomBy(delta) {
      setZoom((previous) => cfgNormalizeZoom(previous + delta))
    }

    function onCanvasPointerDown(event) {
      if (!event || event.button !== 0) return
      const wrap = wrapRef.current
      if (!wrap) return
      dragRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        left: wrap.scrollLeft,
        top: wrap.scrollTop,
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
      wrap.scrollLeft = drag.left - (event.clientX - drag.startX)
      wrap.scrollTop = drag.top - (event.clientY - drag.startY)
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

    const statsText =
      t(
        'cfg.stats',
        { blocks: stats.blocks, edges: stats.edges, branches: stats.branches },
        '{blocks} blocks · {edges} edges · {branches} branches',
      ) + (data && data.truncated ? ' · ' + t('cfg.truncated', null, 'truncated') : '')

    const legend = h(
      'div',
      { className: 'ca-cfg-legend' },
      h('span', { className: 'ca-cfg-legend-item' }, h('span', { className: 'ca-cfg-swatch entry' }), t('cfg.legendEntry', null, 'entry')),
      h('span', { className: 'ca-cfg-legend-item' }, h('span', { className: 'ca-cfg-swatch exit' }), t('cfg.legendExit', null, 'exit')),
      h('span', { className: 'ca-cfg-legend-item' }, h('span', { className: 'ca-cfg-swatch true' }), t('cfg.legendTrue', null, 'true branch')),
      h('span', { className: 'ca-cfg-legend-item' }, h('span', { className: 'ca-cfg-swatch false' }), t('cfg.legendFalse', null, 'false branch')),
      h('span', { className: 'ca-cfg-legend-item' }, h('span', { className: 'ca-cfg-swatch back' }), t('cfg.legendBack', null, 'back edge')),
    )

    const toolbar = h(
      'div',
      { className: 'ca-cfg-toolbar' },
      h('span', { className: 'ca-cfg-note' }, data ? statsText : ''),
      h(
        Button,
        {
          variant: 'ghost',
          size: 'sm',
          onClick: () => zoomBy(-CFG_GEOMETRY.ZOOM_STEP),
          disabled: zoom <= CFG_GEOMETRY.MIN_ZOOM,
          title: t('cfg.zoomOut', null, 'Zoom out'),
          ariaLabel: t('cfg.zoomOut', null, 'Zoom out'),
        },
        '−',
      ),
      h('span', { className: 'ca-cfg-zoom-label' }, Math.round(zoom * 100) + '%'),
      h(
        Button,
        {
          variant: 'ghost',
          size: 'sm',
          onClick: () => zoomBy(CFG_GEOMETRY.ZOOM_STEP),
          disabled: zoom >= CFG_GEOMETRY.MAX_ZOOM,
          title: t('cfg.zoomIn', null, 'Zoom in'),
          ariaLabel: t('cfg.zoomIn', null, 'Zoom in'),
        },
        '+',
      ),
      h(
        Button,
        {
          variant: 'ghost',
          size: 'sm',
          onClick: () => setZoom(1),
          title: t('cfg.zoomReset', null, 'Reset zoom'),
        },
        t('cfg.zoomReset', null, 'Reset'),
      ),
    )

    let body
    if (loadState.error) {
      body = h(
        'div',
        { className: 'ca-cfg-status' },
        h(ErrorBox, {
          title: t('cfg.error', null, 'Could not load the control-flow graph'),
          error: loadState.error,
          message: loadState.error.message,
          onRetry: () => setReloadToken((value) => value + 1),
        }),
      )
    } else if (loadState.loading && !data) {
      body = h(
        'div',
        { className: 'ca-cfg-status' },
        h(Spinner, { label: t('cfg.loading', null, 'Loading control-flow graph…') }),
      )
    } else if (!symbol) {
      body = h(
        'div',
        { className: 'ca-cfg-status' },
        h(EmptyState, {
          title: t('cfg.title', null, 'Control-flow graph'),
          body: t('cfg.noSymbol', null, 'Select a function to inspect its control-flow graph.'),
        }),
      )
    } else if (!blocks.length) {
      if (data && data.deep_ready === false) {
        body = h(
          'div',
          { className: 'ca-cfg-callout' },
          h('span', null, h('strong', null, t('cfg.emptyTitle', null, 'No control-flow facts indexed.') + ' ')),
          h('span', null, t('cfg.emptyBody', null, 'Branches, basic blocks and CFG edges come from the deep CFG/data-flow pass; run `ca ensure-deep` (or index with deep analysis) to fill them.')),
          h('span', { className: 'ca-spacer' }),
          h(Badge, { tone: 'warn' }, t('badge.deepFactsPending', null, 'deep facts pending')),
        )
      } else {
        body = h(
          'div',
          { className: 'ca-cfg-status' },
          h(EmptyState, {
            title: t('cfg.emptyTitle', null, 'No control-flow facts indexed.'),
            body: t('cfg.emptyBody', null, 'Branches, basic blocks and CFG edges come from the deep CFG/data-flow pass; run `ca ensure-deep` (or index with deep analysis) to fill them.'),
          }),
        )
      }
    } else {
      const defs = h(
        'defs',
        null,
        ['normal', 'true', 'false', 'back'].map((kind) =>
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
              className: 'ca-cfg-marker-' + kind,
            },
            h('path', { d: 'M 0 0 L 10 5 L 0 10 z' }),
          ),
        ),
      )
      body = h(
        'div',
        { className: 'ca-cfg-body' },
        h(
          'div',
          {
            ref: wrapRef,
            className: 'ca-cfg-canvas' + (panning ? ' dragging' : ''),
            onPointerDown: onCanvasPointerDown,
            onPointerMove: onCanvasPointerMove,
            onPointerUp: onCanvasPointerUp,
            onPointerCancel: onCanvasPointerUp,
          },
          h(
            'svg',
            {
              className: 'ca-cfg-svg',
              xmlns: 'http://www.w3.org/2000/svg',
              viewBox: '0 0 ' + layout.width + ' ' + layout.height,
              width: Math.max(1, Math.round(layout.width * zoom)),
              height: Math.max(1, Math.round(layout.height * zoom)),
              role: 'img',
              'aria-label': t('cfg.aria', { name: functionLabel }, 'Control-flow graph for {name}'),
            },
            defs,
            h(
              'g',
              { key: 'edges' },
              layout.edges.map((edge) =>
                h(
                  'g',
                  { key: edge.key, className: 'ca-cfg-edge ' + edge.kind },
                  h('path', { d: edge.path, markerEnd: 'url(#' + markerId(safeUid, edge.kind) + ')' }, h('title', null, edge.title)),
                  edge.label
                    ? h('text', { x: edge.labelX, y: edge.labelY, textAnchor: 'middle' }, edge.label)
                    : null,
                ),
              ),
            ),
            h(
              'g',
              { key: 'nodes' },
              layout.nodes.map((entry) => {
                const block = entry.block
                const position = entry.position
                if (!position) return null
                const classes = ['ca-cfg-node']
                if (entry.isEntry) classes.push('entry')
                if (entry.isExit) classes.push('exit')
                if (entry.isBranch) classes.push('branch')
                const head = '#' + String(block.index === undefined || block.index === null ? 0 : block.index) + ' ' + String(block.kind || '')
                const sub =
                  'L' + String(block.line || 0) +
                  ' · ' + t('cfg.stmtCount', { count: Number(block.statement_count) || 0 }, '{count} stmt')
                const condition = block.condition ? cfgTruncate(block.condition, 26) : ''
                const title =
                  String(block.kind || 'block') +
                  ' #' + String(block.index === undefined || block.index === null ? 0 : block.index) +
                  (block.condition ? ': ' + String(block.condition) : '')
                return h(
                  'g',
                  { key: String(block.id), className: classes.join(' ') },
                  h('title', null, title),
                  h('rect', { x: position.x, y: position.y, width: CFG_GEOMETRY.NODE_W, height: CFG_GEOMETRY.NODE_H, rx: 8 }),
                  h('text', { x: position.x + 12, y: position.y + 19 }, head),
                  h('text', { className: 'ca-cfg-node-sub', x: position.x + 12, y: position.y + 35 }, sub),
                  condition
                    ? h('text', { className: 'ca-cfg-node-condition', x: position.x + 12, y: position.y + 51 }, condition)
                    : null,
                )
              }),
            ),
          ),
        ),
      )
    }

    return h(
      'section',
      { className: 'ca-cfg', 'aria-busy': loadState.loading ? 'true' : 'false' },
      h(
        'div',
        { className: 'ca-cfg-head' },
        h('h3', { className: 'ca-cfg-title' }, t('cfg.title', null, 'Control-flow graph')),
        data && functionInfo.name
          ? h(
              'span',
              { className: 'ca-cfg-sub' },
              String(functionInfo.name) +
                (functionInfo.file
                  ? ' · ' + String(functionInfo.file) + ':' + String(functionInfo.line || 0)
                  : ''),
            )
          : null,
        data && data.deep_ready === false
          ? h(Badge, { tone: 'warn' }, t('badge.deepFactsPending', null, 'deep facts pending'))
          : null,
        toolbar,
      ),
      legend,
      body,
    )
  }

  return CfgGraphView
}

export default createCfgGraph
