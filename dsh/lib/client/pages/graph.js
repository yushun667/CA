/**
 * Standalone call-graph page (`#/graph`).
 *
 * Asks for a symbol (or accepts `#/graph?symbol=...`) and embeds the shared
 * call-graph view. The graph view owns direction/depth/rebuild controls and the
 * SVG; this page only supplies the symbol entry point and the node selection
 * actions for a standalone route (double-click / Open detail navigates to the
 * function page).
 */

import { createCallGraph } from '../views/graph/index.js'

const GRAPH_PAGE_CSS = `
.ca-graph-page {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
  min-height: 0;
  color: var(--dsw-alias-label-primary, #1a2230);
}
.ca-graph-page-head { flex: 0 0 auto; }
.ca-graph-page-title { margin: 0; font-size: 18px; font-weight: 600; }
.ca-graph-page-sub {
  margin: 3px 0 0;
  color: var(--dsw-alias-label-tertiary, #8a94a5);
  font-size: 12px;
}
.ca-graph-page-form { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; flex: 0 0 auto; }
.ca-graph-page-form .ca-input { min-width: 260px; max-width: 520px; flex: 1 1 260px; }
.ca-graph-page-selected {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  padding: 6px 10px;
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-sm, 6px);
  background: var(--dsw-alias-bg-layer-2, transparent);
  font-size: 12px;
  color: var(--dsw-alias-label-secondary, #5a6577);
}
.ca-graph-page-selected .ca-mono { font-family: var(--ds-font-family-code, ui-monospace, monospace); }
.ca-graph-page-view { flex: 1 1 auto; min-height: 0; }
.ca-graph-page-empty { flex: 1 1 auto; display: flex; align-items: center; justify-content: center; min-height: 220px; }
`

function graphPageReadRouteSymbol(route) {
  if (!route || typeof route !== 'object') return ''
  const params = route.params && typeof route.params === 'object' ? route.params : {}
  if (params.symbol) return String(params.symbol)
  if (params.s) return String(params.s)
  const raw = String(route.raw || route.path || '')
  const queryStart = raw.indexOf('?')
  if (queryStart >= 0) {
    const query = raw.slice(queryStart + 1)
    const match = /(?:^|&)symbol=([^&]*)/.exec(query)
    if (match) {
      try {
        return decodeURIComponent(match[1])
      } catch (_error) {
        return match[1]
      }
    }
  }
  return ''
}

function graphPageRouteKey(route) {
  if (!route) return ''
  return String(route.raw || '') + '|' + String(route.path || '') + '|' + graphPageReadRouteSymbol(route)
}

export function createGraphPage(React, bag) {
  if (!React || typeof React.createElement !== 'function') {
    throw new TypeError('createGraphPage(React, bag): React with createElement is required')
  }
  const h = React.createElement
  const ui = (bag && bag.ui) || {}
  const i18n = (bag && bag.i18n) || {}
  const useStyles = typeof ui.useStyles === 'function' ? ui.useStyles : function noGraphPageStyles() {}
  const CallGraph = createCallGraph(React, bag)

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
        /* fall through */
      }
    }
    return formatText(fallback || key, vars)
  }

  function fallbackElement(className, text) {
    return h('div', { className: className }, text)
  }

  const Button = typeof ui.Button === 'function' ? ui.Button : function FallbackPageButton(props) {
    return h(
      'button',
      { type: 'button', disabled: props.disabled ? true : undefined, title: props.title, onClick: props.onClick },
      props.children,
    )
  }
  const Input = typeof ui.Input === 'function' ? ui.Input : function FallbackPageInput(props) {
    return h('input', {
      className: 'ca-input',
      value: props.value,
      placeholder: props.placeholder,
      onChange: props.onChange,
      onKeyDown: props.onKeyDown,
    })
  }
  const EmptyState = typeof ui.EmptyState === 'function' ? ui.EmptyState : function FallbackPageEmpty(props) {
    return fallbackElement('ca-empty', [props.title, props.body].filter(Boolean).join(' — '))
  }

  function GraphPage(props) {
    useStyles(GRAPH_PAGE_CSS)
    const routeSymbol = graphPageReadRouteSymbol(props && props.route)
    const [input, setInput] = React.useState(routeSymbol)
    const [symbol, setSymbol] = React.useState(routeSymbol)
    const [selected, setSelected] = React.useState('')
    const lastRouteRef = React.useRef(graphPageRouteKey(props && props.route))

    React.useEffect(() => {
      const nextKey = graphPageRouteKey(props && props.route)
      if (nextKey === lastRouteRef.current) return
      lastRouteRef.current = nextKey
      const nextSymbol = graphPageReadRouteSymbol(props && props.route)
      if (!nextSymbol) return
      setInput(nextSymbol)
      setSymbol(nextSymbol)
      setSelected('')
    }, [graphPageRouteKey(props && props.route)])

    function openSymbol() {
      const next = String(input || '').trim()
      setSelected('')
      setSymbol(next)
    }

    function onInputChange(event) {
      setInput(event && event.target ? event.target.value : '')
    }

    function onInputKeyDown(event) {
      if (!event) return
      if (event.key !== 'Enter') return
      event.preventDefault()
      openSymbol()
    }

    function navigateToFunction(target) {
      const value = String(target || '').trim()
      if (!value) return
      if (props && typeof props.navigate === 'function') {
        props.navigate('#/fn/' + encodeURIComponent(value))
      }
    }

    const form = h(
      'div',
      { className: 'ca-graph-page-form' },
      h(Input, {
        value: input,
        placeholder: t('graph.symbolPlaceholder', null, 'Function name, qualified name or id'),
        ariaLabel: t('graph.symbolLabel', null, 'Function symbol'),
        onChange: onInputChange,
        onKeyDown: onInputKeyDown,
      }),
      h(Button, { variant: 'primary', size: 'sm', onClick: openSymbol }, t('graph.open', null, 'Open')),
      symbol
        ? h(
            Button,
            { variant: 'ghost', size: 'sm', onClick: () => navigateToFunction(symbol) },
            t('source.openDetail', null, 'Open function detail'),
          )
        : null,
    )

    const selectedBar =
      selected && symbol
        ? h(
            'div',
            { className: 'ca-graph-page-selected' },
            h('span', null, t('graph.selectedNode', { name: selected }, 'Selected node: {name}')),
            h(
              Button,
              { variant: 'secondary', size: 'sm', onClick: () => navigateToFunction(selected) },
              t('source.openDetail', null, 'Open function detail'),
            ),
          )
        : null

    let view
    if (symbol) {
      view = h(
        'div',
        { className: 'ca-graph-page-view' },
        h(CallGraph, {
          symbol,
          direction: 'both',
          depth: 2,
          limit: 500,
          onSelectNode: (next) => setSelected(String(next || '')),
          onOpenDetail: (next) => navigateToFunction(next),
        }),
      )
    } else {
      view = h(
        'div',
        { className: 'ca-graph-page-empty' },
        h(EmptyState, {
          title: t('graph.title', null, 'Call graph'),
          body: t(
            'graph.enterSymbol',
            null,
            'Enter a function name (or an id) to build its call graph. Double-click a node to open the function detail.',
          ),
        }),
      )
    }

    return h(
      'div',
      { className: 'ca-graph-page' },
      h(
        'header',
        { className: 'ca-graph-page-head' },
        h('h2', { className: 'ca-graph-page-title' }, t('graph.title', null, 'Call graph')),
        h(
          'p',
          { className: 'ca-graph-page-sub' },
          t(
            'graph.pageSubtitle',
            null,
            'Read-only SVG traversal of merged direct calls and cross-TU callback candidates.',
          ),
        ),
      ),
      form,
      selectedBar,
      view,
    )
  }

  return GraphPage
}

export default createGraphPage
