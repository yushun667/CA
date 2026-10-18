/**
 * CA DSH client shell: the full-page panel registered into the Harness
 * `main` slot (key `ca`).
 *
 * Responsibilities:
 *   - hash routing (`#/search`, `#/fn/<symbol>/<tab>`, `#/stats`,
 *     `#/callbacks`, `#/graph`, `#/tasks`, `#/backend`), with pushState
 *     navigation plus hashchange/popstate handling for browser back/forward;
 *   - the top bar (brand, navigation, variant selector, connection + backend
 *     status, language switch);
 *   - a page registry filled by the Lead-owned `lib/client/index.js`;
 *   - initial loading, RPC-unreachable, and "backend not configured/stopped"
 *     states, steering the user to `#/backend` when nothing is configured.
 *
 * Page components are created by their own factories
 * (`createXPage(React, { ui, api, i18n })`) and receive `{ route, navigate }`.
 */

import { toApiError } from '../api.js'

/** Main-slot key and locale namespace used by this plugin. */
export const PANEL_ID = 'ca'
export const NS = 'ca'

/** Pages that have a route even when no explicit entry is registered. */
export const KNOWN_PAGES = Object.freeze(['search', 'function', 'stats', 'callbacks', 'graph', 'tasks', 'backend'])

const STATUS_POLL_MS = 8000

// ---------------------------------------------------------------------------
// routing
// ---------------------------------------------------------------------------

function safeDecode(value) {
  try {
    return decodeURIComponent(value)
  } catch (_error) {
    return value
  }
}

function splitHash(rawHash) {
  const raw = String(rawHash || '')
  const withoutHash = raw.startsWith('#') ? raw.slice(1) : raw
  const queryIndex = withoutHash.indexOf('?')
  const pathPart = queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash
  const queryPart = queryIndex >= 0 ? withoutHash.slice(queryIndex + 1) : ''
  const params = {}
  if (queryPart) {
    for (const pair of queryPart.split('&')) {
      if (!pair) continue
      const equals = pair.indexOf('=')
      const key = safeDecode(equals >= 0 ? pair.slice(0, equals) : pair)
      const value = equals >= 0 ? safeDecode(pair.slice(equals + 1)) : ''
      if (key) params[key] = value
    }
  }
  return { pathPart: pathPart.replace(/^\/+/, '').replace(/\/+$/, ''), params }
}

/**
 * Parse a location hash into a route object.
 * @param {string} rawHash hash including the leading `#` (optional)
 */
export function parseHash(rawHash) {
  const { pathPart, params } = splitHash(rawHash)
  const segments = pathPart ? pathPart.split('/') : []
  const page = segments[0] || 'search'
  const raw = String(rawHash || '')
  const base = { page, path: pathPart, params, raw, hash: raw }
  if (page === 'fn' && segments[1]) {
    return Object.assign({}, base, {
      page: 'function',
      symbol: safeDecode(segments[1]),
      tab: segments[2] ? safeDecode(segments[2]) : '',
    })
  }
  if (page === 'search' && segments.length === 1) return Object.assign({}, base, { page: 'search' })
  if (['stats', 'callbacks', 'graph', 'tasks', 'backend'].includes(page)) {
    return Object.assign({}, base, { page })
  }
  if (segments.length === 0) return Object.assign({}, base, { page: 'search' })
  return Object.assign({}, base, { page: 'notfound', unknown: safeDecode(page) })
}

/** Turn a route object into a location hash. */
export function routeToHash(route) {
  if (typeof route === 'string') {
    const text = route.trim()
    if (!text) return '#/search'
    if (text.startsWith('#')) return text
    if (text.startsWith('/')) return `#${text}`
    if (text.includes('/')) return `#/${text}`
    return `#/${text}`
  }
  if (!route || typeof route !== 'object') return '#/search'
  const page = route.page || route.name || 'search'
  const params = route.params || route.query
  let hash
  if (page === 'function') {
    const symbol = route.symbol === undefined || route.symbol === null ? '' : String(route.symbol)
    if (!symbol) return '#/search'
    hash = `#/fn/${encodeURIComponent(symbol)}`
    if (route.tab) hash += `/${encodeURIComponent(String(route.tab))}`
  } else {
    hash = `#/${page}`
  }
  if (params && typeof params === 'object') {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      search.set(key, String(value))
    }
    const suffix = search.toString()
    if (suffix) hash += `?${suffix}`
  }
  return hash
}

function currentHash() {
  if (typeof window === 'undefined' || !window.location) return ''
  return window.location.hash || ''
}

// ---------------------------------------------------------------------------
// sidebar glyph
// ---------------------------------------------------------------------------

/**
 * Build the glyph component registered into `sidebar.panellist`.
 * The host passes `{ size, active }`; we only draw the mark.
 */
export function createPanelIcon(React) {
  const h = React.createElement
  function CaPanelIcon(props) {
    const size = props && Number.isFinite(Number(props.size)) ? Number(props.size) : 18
    const active = Boolean(props && props.active)
    return h(
      'svg',
      {
        viewBox: '0 0 24 24',
        width: size,
        height: size,
        'aria-hidden': true,
        focusable: 'false',
        style: { display: 'block', color: active ? 'currentColor' : 'inherit' },
      },
      h('rect', {
        x: 3.5,
        y: 4.5,
        width: 17,
        height: 15,
        rx: 3,
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth: 1.6,
      }),
      h('path', {
        d: 'M7.5 9.5h6M7.5 12.5h9M7.5 15.5h4.5',
        stroke: 'currentColor',
        strokeWidth: 1.6,
        strokeLinecap: 'round',
      }),
    )
  }
  CaPanelIcon.displayName = 'CaPanelIcon'
  return CaPanelIcon
}

// ---------------------------------------------------------------------------
// page error boundary
// ---------------------------------------------------------------------------

/**
 * Isolate page crashes so one broken view cannot blank the whole CA panel
 * (the harness logs "slot entry crashed" and drops the entire main entry).
 * The top bar and navigation stay usable; a retry remounts the page.
 */
function createPageErrorBoundary(React, ui, t) {
  class PageErrorBoundary extends React.Component {
    constructor(props) {
      super(props)
      this.state = { error: null }
      this.reset = this.reset.bind(this)
    }

    static getDerivedStateFromError(error) {
      return { error }
    }

    componentDidCatch(error, info) {
      console.error('[ca] page crashed:', error, info)
    }

    reset() {
      this.setState({ error: null })
    }

    render() {
      if (this.state.error) {
        return React.createElement(
          'div',
          { className: 'ca-static-fill' },
          React.createElement(ui.ErrorBox, {
            title: t('shell.pageError', null, 'This page failed to render'),
            error: this.state.error,
            hint: t('shell.pageErrorHint', null, 'Switch to another view and back, or retry the page.'),
            retryLabel: t('common.retry', null, 'Retry'),
            onRetry: this.reset,
          }),
        )
      }
      return this.props.children
    }
  }
  PageErrorBoundary.displayName = 'CaPageErrorBoundary'
  return PageErrorBoundary
}

// ---------------------------------------------------------------------------
// shell styles
// ---------------------------------------------------------------------------

const SHELL_CSS = `
.ca-main { position: relative; display: flex; flex-direction: column; flex: 1 1 auto; min-height: 0; overflow: hidden; }
.ca-topbar { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; min-height: 48px; padding: 0 12px; border-bottom: 1px solid var(--dsw-alias-border-l2, #e5e7eb); background: var(--dsw-alias-bg-layer-1, #ffffff); }
.ca-brand { display: inline-flex; align-items: center; gap: 9px; padding: 4px 6px; border: 0; background: transparent; border-radius: var(--dsw-radius-sm, 6px); color: var(--dsw-alias-label-primary, #1f2328); cursor: pointer; text-align: left; }
.ca-brand:hover { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.1)); }
.ca-brand-mark { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: var(--dsw-radius-sm, 6px); background: var(--dsw-alias-brand-primary, #2f6feb); color: var(--dsw-alias-label-primary-inverted, #ffffff); font-size: 11px; font-weight: 700; letter-spacing: 0.02em; }
.ca-brand-text { display: flex; flex-direction: column; line-height: 1.15; min-width: 0; }
.ca-brand-title { font-size: 13.5px; font-weight: 650; white-space: nowrap; }
.ca-brand-sub { font-size: 10.5px; color: var(--dsw-alias-label-tertiary, #8b949e); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 280px; }
.ca-topnav { display: flex; align-items: center; gap: 2px; min-width: 0; overflow-x: auto; scrollbar-width: none; }
.ca-topnav::-webkit-scrollbar { display: none; }
.ca-topbar-right { display: flex; align-items: center; gap: 8px; margin-left: auto; min-width: 0; }
.ca-topbar-sep { width: 1px; height: 18px; background: var(--dsw-alias-border-l2, #e5e7eb); }
.ca-status-pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: 999px; background: transparent; color: var(--dsw-alias-label-secondary, #57606a); font-size: 11.5px; cursor: pointer; white-space: nowrap; }
.ca-status-pill:hover { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.1)); }
.ca-status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--dsw-alias-state-idle-primary, #8b949e); flex: 0 0 auto; }
.ca-status-dot-running { background: var(--dsw-alias-state-success-primary, #1a7f37); }
.ca-status-dot-starting { background: var(--dsw-alias-state-warn-primary, #9a6700); }
.ca-status-dot-error { background: var(--dsw-alias-state-error-primary, #d1242f); }
.ca-status-dot-offline { background: var(--dsw-alias-state-error-primary, #d1242f); }
.ca-lang-switch { display: inline-flex; align-items: center; padding: 2px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-sm, 6px); }
.ca-lang-switch .ca-btn { min-height: 20px; padding: 0 7px; font-size: 11.5px; border-radius: calc(var(--dsw-radius-sm, 6px) - 2px); }
.ca-lang-switch .ca-btn-active { background: var(--dsw-alias-button-ghost-active-fill, rgba(127, 127, 127, 0.14)); }
.ca-variant { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.ca-variant-label { font-size: 11.5px; color: var(--dsw-alias-label-tertiary, #8b949e); white-space: nowrap; }
.ca-variant .ca-select { width: auto; min-width: 140px; max-width: 280px; min-height: 26px; padding: 2px 8px; font-size: 12px; }
.ca-banner { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; padding: 6px 14px; border-bottom: 1px solid var(--dsw-alias-border-l2, #e5e7eb); background: var(--dsw-alias-bg-layer-2, #f6f8fa); color: var(--dsw-alias-label-secondary, #57606a); font-size: 12.5px; }
.ca-banner-warn { color: var(--dsw-alias-state-warn-label, #9a6700); }
.ca-banner-error { color: var(--dsw-alias-state-error-primary, #d1242f); }
.ca-banner-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ca-loading-fill { display: flex; align-items: center; justify-content: center; flex: 1 1 auto; min-height: 0; }
.ca-static-fill { display: flex; align-items: center; justify-content: center; flex: 1 1 auto; min-height: 0; padding: 24px; }
.ca-static-fill > * { max-width: 560px; width: 100%; }
@media (max-width: 1080px) {
  .ca-brand-sub { display: none; }
  .ca-variant-label { display: none; }
}
@media (max-width: 860px) {
  .ca-topnav .ca-btn { padding: 4px 8px; }
  .ca-status-pill .ca-status-text { display: none; }
}
`

// ---------------------------------------------------------------------------
// shell factory
// ---------------------------------------------------------------------------

/**
 * Create the CA shell.
 *
 * @param {object} React React namespace from the module table.
 * @param {{ ui: object, api: object, i18n: object, layout?: object }} bag
 * @returns {{ App: Function, registerPage: Function, navigate: Function, getRoute: Function, hasPage: Function, parseHash: Function }}
 */
export function createShell(React, bag) {
  if (!React || typeof React.createElement !== 'function') {
    throw new TypeError('createShell(React, bag): React with createElement is required')
  }
  const { ui, api, i18n } = bag || {}
  if (!ui || !api || !i18n) throw new TypeError('createShell(React, { ui, api, i18n })')
  const h = React.createElement

  const pages = new Map()
  const listeners = new Set()

  let state = {
    route: parseHash(currentHash()),
    connected: null, // null = unknown yet
    statusLoading: true,
    statusError: null,
    status: null, // full status RPC value { backend, tasks, model, counts }
    backend: null, // status.backend
    counts: null,
    variantError: null,
    language: i18n.lang,
  }

  function getSnapshot() {
    return state
  }

  function publish(patch) {
    state = Object.assign({}, state, patch)
    for (const fn of [...listeners]) {
      try {
        fn(state)
      } catch (error) {
        console.error('[ca] shell subscriber crashed:', error)
      }
    }
  }

  function subscribe(fn) {
    if (typeof fn !== 'function') return () => {}
    listeners.add(fn)
    return () => listeners.delete(fn)
  }

  function setRoute(route) {
    if (!route || route === state.route) return
    publish({ route })
  }

  /** Navigate to a route (string hash or object). Defaults to pushState. */
  function navigate(target, options) {
    const settings = options && typeof options === 'object' ? options : {}
    const hash = routeToHash(target)
    const replace = settings.replace === true
    const targetRoute = parseHash(hash)
    if (typeof window !== 'undefined' && window.history && window.location) {
      const same = currentHash() === hash
      if (!same) {
        try {
          if (replace) window.history.replaceState(null, '', hash)
          else window.history.pushState(null, '', hash)
        } catch (_error) {
          window.location.hash = hash
        }
      }
      publish({ route: targetRoute })
    } else {
      publish({ route: targetRoute })
    }
  }

  function getRoute() {
    return state.route
  }

  /** Register one page component under `name`; returns an unregister function. */
  function registerPage(name, component) {
    if (typeof name !== 'string' || name.length === 0) {
      throw new TypeError('registerPage(name, component): name must be a non-empty string')
    }
    if (typeof component !== 'function' && (typeof component !== 'object' || component === null)) {
      throw new TypeError(`registerPage(${name}): component must be a React component`)
    }
    pages.set(name, component)
    if (typeof component === 'function') {
      try {
        if (!component.displayName) component.displayName = `Ca${name[0].toUpperCase()}${name.slice(1)}Page`
      } catch (_error) {
        /* displayName is cosmetic */
      }
    }
    return () => {
      if (pages.get(name) === component) pages.delete(name)
    }
  }

  function hasPage(name) {
    return pages.has(name)
  }

  // -- status polling ------------------------------------------------------- //

  let statusAbort = null
  let statusTimer = null
  let statusInFlight = false

  async function refreshStatus(options) {
    const settings = options && typeof options === 'object' ? options : {}
    if (statusInFlight && !settings.force) return
    statusInFlight = true
    if (statusAbort) statusAbort.abort()
    const controller = typeof AbortController === 'function' ? new AbortController() : null
    statusAbort = controller
    try {
      const value = await api.getStatus(controller ? controller.signal : undefined)
      if (controller && controller.signal.aborted) return
      publish({
        connected: true,
        statusLoading: false,
        statusError: null,
        status: value || null,
        backend: value && value.backend ? value.backend : null,
        counts: value && value.counts ? value.counts : null,
      })
      if (value && value.backend && value.backend.mode && value.backend.mode !== 'unconfigured') {
        // Keep the variant catalog fresh once the backend can answer.
        api.variant.refresh({ signal: controller ? controller.signal : undefined }).catch(() => {})
      }
    } catch (error) {
      if (controller && controller.signal.aborted) return
      const apiError = toApiError(error)
      if (apiError.code === 'ABORTED') return
      const transport = apiError.transport || apiError.code === 'TRANSPORT' || apiError.code === 'RPC_UNAVAILABLE'
      publish({
        connected: transport ? false : state.connected,
        statusLoading: false,
        statusError: apiError,
        backend: transport ? state.backend : state.backend,
      })
    } finally {
      statusInFlight = false
      statusAbort = null
    }
  }

  function startPolling() {
    stopPolling()
    refreshStatus({ force: true })
    if (typeof window !== 'undefined') {
      statusTimer = window.setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        refreshStatus()
      }, STATUS_POLL_MS)
    }
  }

  function stopPolling() {
    if (statusTimer !== null && typeof window !== 'undefined') {
      window.clearInterval(statusTimer)
      statusTimer = null
    }
    if (statusAbort) {
      statusAbort.abort()
      statusAbort = null
    }
  }

  // -- variant switching ---------------------------------------------------- //

  async function switchVariant(variantId) {
    if (!variantId || variantId === activeVariantId()) return
    try {
      publish({ variantError: null })
      await api.variant.setActive(variantId)
      publish({ variantError: null })
    } catch (error) {
      const apiError = toApiError(error)
      if (apiError.code === 'ABORTED') return
      publish({ variantError: apiError })
    }
  }

  function activeVariantId() {
    const variantState = api.variant.get()
    const registry = variantState && variantState.registry
    if (!registry) return ''
    return registry.active_variant_id || ''
  }

  function variantOptions() {
    const variantState = api.variant.get()
    const registry = variantState && variantState.registry
    if (!registry || !Array.isArray(registry.variants)) return []
    return registry.variants.map((variant) => {
      const parts = [variant.name, variant.product, variant.board, variant.version].filter(
        (part) => typeof part === 'string' && part.length > 0,
      )
      return {
        value: variant.id,
        label: parts.length ? parts.join(' · ') : String(variant.id),
        disabled: !variant.db_exists,
        title: variant.db_path || undefined,
      }
    })
  }

  const translate = (key, vars, fallback) => i18n.t(key, vars, fallback)
  const PageErrorBoundary = createPageErrorBoundary(React, ui, translate)

  // -- top bar -------------------------------------------------------------- //

  const NAV_ITEMS = [
    { page: 'search', icon: 'search', label: 'shell.nav.search', fallback: 'Search' },
    { page: 'stats', icon: 'stats', label: 'shell.nav.stats', fallback: 'Stats' },
    { page: 'callbacks', icon: 'callbacks', label: 'shell.nav.callbacks', fallback: 'Callbacks' },
    { page: 'graph', icon: 'graph', label: 'shell.nav.graph', fallback: 'Graph' },
    { page: 'tasks', icon: 'tasks', label: 'shell.nav.tasks', fallback: 'Tasks' },
    { page: 'backend', icon: 'backend', label: 'shell.nav.backend', fallback: 'Backend' },
  ]

  function backendStateTone(backend) {
    if (!backend || typeof backend !== 'object') return 'idle'
    if (backend.state === 'running') return 'running'
    if (backend.state === 'starting') return 'starting'
    if (backend.state === 'error') return 'error'
    if (backend.mode === 'unconfigured') return 'idle'
    return 'idle'
  }

  function backendStateLabel(backend) {
    if (!backend || typeof backend !== 'object') return translate('shell.initializing', null, 'Connecting…')
    if (backend.mode === 'unconfigured') {
      return translate('shell.backendState.unconfigured', null, 'unconfigured')
    }
    const key = `shell.backendState.${backend.state || 'stopped'}`
    return translate(key, null, backend.state || 'stopped')
  }

  function TopBar() {
    const variantState = api.variant.get()
    const registry = variantState && variantState.registry
    const options = variantOptions()
    const backend = state.backend
    const runningTasks =
      state.counts && Number(state.counts.running) > 0 ? Number(state.counts.running) : 0
    const route = state.route
    const showVariant = Boolean(registry && registry.available && options.length > 0)
    return h(
      'header',
      { className: 'ca-topbar', role: 'banner' },
      h(
        'button',
        {
          type: 'button',
          className: 'ca-brand',
          onClick: () => navigate({ page: 'search' }),
          title: translate('shell.title', null, 'CA source explorer'),
        },
        h('span', { className: 'ca-brand-mark', 'aria-hidden': 'true' }, 'CA'),
        h(
          'span',
          { className: 'ca-brand-text' },
          h('span', { className: 'ca-brand-title' }, translate('shell.title', null, 'CA source explorer')),
          h(
            'span',
            { className: 'ca-brand-sub' },
            translate('shell.subtitle', null, 'real compile args → Clang AST → CFG / data-flow → SQLite'),
          ),
        ),
      ),
      h(
        'nav',
        { className: 'ca-topnav', 'aria-label': translate('shell.nav', null, 'Navigation') },
        NAV_ITEMS.map((item) => {
          const active =
            route.page === item.page || (item.page === 'search' && route.page === 'function')
          return h(
            ui.Button,
            {
              key: item.page,
              variant: 'ghost',
              size: 'sm',
              active,
              'aria-expanded': undefined,
              onClick: () => navigate({ page: item.page }),
              title: translate(item.label, null, item.fallback),
              ariaLabel: translate(item.label, null, item.fallback),
            },
            h(ui.Icon, { name: item.icon, size: 14 }),
            h(
              'span',
              { className: 'ca-visually-hidden' },
              translate(item.label, null, item.fallback),
            ),
          )
        }),
      ),
      h('span', { className: 'ca-spacer' }),
      h(
        'div',
        { className: 'ca-topbar-right' },
        showVariant
          ? h(
              'div',
              { className: 'ca-variant' },
              h(
                'span',
                { className: 'ca-variant-label' },
                translate('shell.variant', null, 'Variant'),
              ),
              h(ui.Select, {
                value: registry.active_variant_id || '',
                options,
                ariaLabel: translate('shell.variant', null, 'Variant'),
                title: registry.active_variant && registry.active_variant.db_path,
                onChange: (value) => {
                  void switchVariant(value)
                },
              }),
            )
          : null,
        h(
          'button',
          {
            type: 'button',
            className: 'ca-status-pill',
            onClick: () => navigate({ page: 'backend' }),
            title: backend && backend.baseUrl ? backend.baseUrl : translate('shell.nav.backend', null, 'Backend'),
            'aria-label': `${translate('shell.backendStatus', null, 'Backend')}: ${backendStateLabel(backend)}`,
          },
          h('span', {
            className: `ca-status-dot ${
              state.connected === false
                ? 'ca-status-dot-offline'
                : backendStateTone(backend) === 'running'
                  ? 'ca-status-dot-running'
                  : backendStateTone(backend) === 'starting'
                    ? 'ca-status-dot-starting'
                    : backendStateTone(backend) === 'error'
                      ? 'ca-status-dot-error'
                      : ''
            }`,
            'aria-hidden': 'true',
          }),
          h(
            'span',
            { className: 'ca-status-text' },
            state.connected === false
              ? translate('shell.disconnected', null, 'disconnected')
              : backendStateLabel(backend),
          ),
          runningTasks > 0
            ? h(
                ui.Badge,
                { tone: 'info' },
                translate('shell.tasksRunning', { count: runningTasks }, `${runningTasks} running`),
              )
            : null,
        ),
        h('span', { className: 'ca-topbar-sep', 'aria-hidden': 'true' }),
        h(
          'div',
          { className: 'ca-lang-switch', role: 'group', 'aria-label': translate('shell.langSwitch', null, 'Language') },
          ['zh', 'en'].map((lang) =>
            h(
              ui.Button,
              {
                key: lang,
                variant: 'ghost',
                size: 'sm',
                active: state.language === lang,
                'aria-pressed': state.language === lang ? 'true' : 'false',
                onClick: () => {
                  i18n.setLang(lang)
                },
                title: lang === 'zh' ? '中文' : 'English',
                ariaLabel: lang === 'zh' ? '中文' : 'English',
              },
              lang === 'zh' ? translate('shell.langZh', null, '中文') : translate('shell.langEn', null, 'EN'),
            ),
          ),
        ),
      ),
    )
  }

  // -- state renders -------------------------------------------------------- //

  function LoadingState() {
    return h(
      'div',
      { className: 'ca-loading-fill' },
      h(ui.Spinner, {
        size: 20,
        label: translate('shell.initializing', null, 'Connecting to the CA plugin host…'),
      }),
    )
  }

  function UnreachableState() {
    return h(
      'div',
      { className: 'ca-static-fill' },
      h(ui.ErrorBox, {
        title: translate('shell.rpcUnavailable', null, 'The CA plugin host is not reachable.'),
        error: state.statusError,
        hint: translate('shell.initializingHint', null, 'The panel appears once the DSH host answers.'),
        retryLabel: translate('shell.retry', null, 'Retry'),
        onRetry: () => {
          startPolling()
        },
      }),
    )
  }

  function UnconfiguredState() {
    return h(
      'div',
      { className: 'ca-static-fill' },
      h(ui.EmptyState, {
        icon: h(ui.Icon, { name: 'backend', size: 28 }),
        title: translate('shell.banner.unconfiguredTitle', null, 'CA backend is not configured'),
        body: translate(
          'shell.banner.unconfiguredBody',
          null,
          'Choose a local database or a remote SSH host, then start the backend.',
        ),
        action: h(
          ui.Button,
          { variant: 'primary', onClick: () => navigate({ page: 'backend' }) },
          translate('shell.banner.action', null, 'Open backend settings'),
        ),
      }),
    )
  }

  function StoppedBanner() {
    const backend = state.backend
    if (!backend || backend.mode === 'unconfigured') return null
    if (backend.state === 'running' || backend.state === 'starting') return null
    return h(
      'div',
      { className: 'ca-banner ca-banner-warn', role: 'status' },
      h(ui.Icon, { name: 'warning', size: 13 }),
      h(
        'span',
        { className: 'ca-banner-text' },
        backend.state === 'error' && backend.error
          ? `${translate('shell.banner.title', null, 'CA backend is not running')}: ${backend.error}`
          : translate('shell.banner.body', null, 'Start the backend to search and browse the index.'),
      ),
      h('span', { className: 'ca-spacer' }),
      h(
        ui.Button,
        { variant: 'ghost', size: 'sm', onClick: () => navigate({ page: 'backend' }) },
        translate('shell.banner.action', null, 'Open backend settings'),
      ),
    )
  }

  function VariantErrorBanner() {
    if (!state.variantError) return null
    return h(
      'div',
      { className: 'ca-banner ca-banner-error', role: 'alert' },
      h(ui.Icon, { name: 'warning', size: 13 }),
      h('span', { className: 'ca-banner-text' }, state.variantError.message),
      h('span', { className: 'ca-spacer' }),
      h(
        ui.Button,
        {
          variant: 'ghost',
          size: 'sm',
          onClick: () => publish({ variantError: null }),
        },
        translate('common.close', null, 'Close'),
      ),
    )
  }

  function PageBody() {
    const route = state.route
    const backend = state.backend
    const unconfigured = Boolean(backend && backend.mode === 'unconfigured')
    if (route.page === 'notfound') {
      return h(
        'div',
        { className: 'ca-static-fill' },
        h(ui.EmptyState, {
          title: translate('shell.pageNotFound', { page: route.unknown || '' }, 'Unknown page.'),
          body: translate('shell.pageNotFoundBody', null, 'Use the navigation above to open a view.'),
          action: h(
            ui.Button,
            { variant: 'secondary', onClick: () => navigate({ page: 'search' }) },
            translate('shell.nav.search', null, 'Search'),
          ),
        }),
      )
    }
    const Page = pages.get(route.page)
    if (!Page) {
      if (unconfigured && route.page !== 'backend') return h(UnconfiguredState)
      return h(
        'div',
        { className: 'ca-static-fill' },
        h(ui.EmptyState, {
          title: translate('shell.noPage', null, 'This page is not registered yet.'),
          body: `${route.page}`,
        }),
      )
    }
    if (unconfigured && route.page !== 'backend') return h(UnconfiguredState)
    const routeKey = `${route.page}:${route.symbol || ''}:${route.tab || ''}`
    return h(PageErrorBoundary, { key: routeKey }, h(Page, { route, navigate }))
  }

  function App() {
    ui.useStyles(SHELL_CSS)

    const [tick, setTick] = React.useState(0)
    const forceRender = React.useCallback(() => setTick((value) => value + 1), [])

    // subscribe to shell store + language changes + variant store
    React.useEffect(() => {
      const unsubscribeShell = subscribe(() => forceRender())
      const unsubscribeI18n = i18n.subscribe(() => {
        publish({ language: i18n.lang })
        forceRender()
      })
      const unsubscribeVariant = api.variant.subscribe(() => forceRender())
      return () => {
        unsubscribeShell()
        unsubscribeI18n()
        unsubscribeVariant()
      }
    }, [forceRender])

    // route + status lifecycle
    React.useEffect(() => {
      const onHashChange = () => publish({ route: parseHash(currentHash()) })
      const onPopState = () => publish({ route: parseHash(currentHash()) })
      if (typeof window !== 'undefined') {
        window.addEventListener('hashchange', onHashChange)
        window.addEventListener('popstate', onPopState)
      }
      startPolling()
      return () => {
        if (typeof window !== 'undefined') {
          window.removeEventListener('hashchange', onHashChange)
          window.removeEventListener('popstate', onPopState)
        }
        stopPolling()
      }
    }, [])

    void tick

    const initial = state.statusLoading && state.connected === null && !state.statusError
    let body
    if (initial) body = h(LoadingState)
    else if (state.connected === false && state.statusError) body = h(UnreachableState)
    else body = h(PageBody)

    return h(
      'div',
      { className: 'ca-app' },
      h(TopBar),
      h(StoppedBanner),
      h(VariantErrorBanner),
      h('main', { className: 'ca-main' }, body),
    )
  }

  App.displayName = 'CaApp'

  return {
    App,
    registerPage,
    hasPage,
    navigate,
    getRoute,
    parseHash,
    routeToHash,
    subscribe,
    getSnapshot,
    refreshStatus,
  }
}

export default createShell
