/**
 * CA symbol search page (`#/search`).
 *
 * The page is the default landing view: a single search box with a floating
 * result dropdown (the same interaction as the standalone CA web UI),
 * keyboard navigation (`ArrowUp`/`ArrowDown` + `Enter`, `Esc` closes/clears)
 * and a `/` shortcut that focuses the box from anywhere on the page.
 * Selecting a result navigates to `#/fn/<symbol>`; the function detail page is
 * owned by the ui-detail view module.
 */

const SEARCH_CSS = `
.ca-search-page { gap: 14px; overflow: auto; padding: 18px; align-items: center; }
.ca-search-inner { width: 100%; max-width: 880px; display: flex; flex-direction: column; gap: 14px; }
.ca-search-head { display: flex; flex-direction: column; gap: 2px; }
.ca-search-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
.ca-search-sub { margin: 0; font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-search-wrap { position: relative; width: 100%; z-index: 20; }
.ca-search-box { display: flex; align-items: center; gap: 8px; padding: 4px 6px 4px 10px; border: 1px solid var(--dsw-alias-border-l3, #d0d7de); border-radius: var(--dsw-radius-lg, 10px); background: var(--dsw-alias-bg-layer-1, #ffffff); transition: border-color 120ms ease, box-shadow 120ms ease; }
.ca-search-box:focus-within { border-color: var(--dsw-alias-state-business-primary, #2f6feb); box-shadow: 0 0 0 1px var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-search-box > .ca-icon-search { color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-search-box .ca-input { flex: 1 1 auto; min-height: 32px; padding: 4px 2px; border: 0; background: transparent; box-shadow: none; font-size: 14px; }
.ca-search-box .ca-input:focus { box-shadow: none; }
.ca-search-kbd { display: inline-flex; align-items: center; gap: 4px; padding: 0 6px; border: 1px solid var(--dsw-alias-border-l3, #d0d7de); border-radius: var(--dsw-radius-xs, 4px); color: var(--dsw-alias-label-tertiary, #8b949e); font-size: 11px; font-family: var(--ds-font-family-code, ui-monospace, monospace); white-space: nowrap; }
.ca-search-dropdown { position: absolute; left: 0; right: 0; top: calc(100% + 6px); max-height: min(62vh, 560px); overflow: auto; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-lg, 10px); background: var(--dsw-alias-bg-layer-1, #ffffff); box-shadow: var(--dsw-elevation-panel, 0 10px 30px rgba(0, 0, 0, 0.16)); }
.ca-search-dropdown-head { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; gap: 8px; padding: 6px 12px; border-bottom: 1px solid var(--dsw-alias-border-l2, #e5e7eb); background: var(--dsw-alias-bg-layer-1, #ffffff); color: var(--dsw-alias-label-tertiary, #8b949e); font-size: 11.5px; }
.ca-search-result { display: flex; flex-direction: column; gap: 2px; width: 100%; padding: 7px 12px; border: 0; border-bottom: 1px solid var(--dsw-alias-border-l1, #eaeef2); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.ca-search-result:last-child { border-bottom: 0; }
.ca-search-result:hover, .ca-search-result-active { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.09)); }
.ca-search-result-active { box-shadow: inset 2px 0 0 var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-search-result-name { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 13px; font-weight: 500; overflow-wrap: anywhere; }
.ca-search-result-meta { font-size: 11.5px; color: var(--dsw-alias-label-tertiary, #8b949e); font-family: var(--ds-font-family-code, ui-monospace, monospace); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ca-search-empty { padding: 18px 14px; text-align: center; color: var(--dsw-alias-label-secondary, #57606a); font-size: 12.5px; }
.ca-search-welcome { display: flex; flex-direction: column; gap: 12px; }
.ca-search-lede { margin: 0; max-width: 760px; color: var(--dsw-alias-label-secondary, #57606a); font-size: 12.5px; line-height: 1.6; }
.ca-search-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
.ca-search-card { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; padding: 12px 14px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-lg, 10px); background: var(--dsw-alias-bg-layer-1, #ffffff); color: inherit; text-align: left; cursor: pointer; font: inherit; min-width: 0; }
.ca-search-card:hover { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.07)); border-color: var(--dsw-alias-border-l3, #d0d7de); }
.ca-search-card-title { display: flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; }
.ca-search-card-body { font-size: 12px; color: var(--dsw-alias-label-secondary, #57606a); line-height: 1.45; }
.ca-search-hint { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 11.5px; color: var(--dsw-alias-label-tertiary, #8b949e); }
`

const SHORTCUTS = [
  { page: 'stats', icon: 'stats', titleKey: 'search.shortcutStats', titleFallback: 'Database statistics', bodyKey: 'search.shortcutStatsBody', bodyFallback: 'Table counts, database size, schema and shards.' },
  { page: 'callbacks', icon: 'callbacks', titleKey: 'search.shortcutCallbacks', titleFallback: 'Callback registrations', bodyKey: 'search.shortcutCallbacksBody', bodyFallback: 'Function-pointer registrations across translation units.' },
  { page: 'graph', icon: 'graph', titleKey: 'search.shortcutGraph', titleFallback: 'Call graph', bodyKey: 'search.shortcutGraphBody', bodyFallback: 'Traverse callers, callees and candidate edges.' },
  { page: 'tasks', icon: 'tasks', titleKey: 'search.shortcutTasks', titleFallback: 'Long-running tasks', bodyKey: 'search.shortcutTasksBody', bodyFallback: 'Run indexing, updates and deep analysis.' },
  { page: 'backend', icon: 'backend', titleKey: 'search.shortcutBackend', titleFallback: 'Backend settings', bodyKey: 'search.shortcutBackendBody', bodyFallback: 'Configure and start the CA backend.' },
]

function isEditableTarget(target) {
  if (!target || typeof target !== 'object') return false
  const tag = typeof target.tagName === 'string' ? target.tagName.toLowerCase() : ''
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true
  return Boolean(target.isContentEditable)
}

function locationOf(row) {
  if (!row || typeof row !== 'object') return ''
  const file = typeof row.file === 'string' ? row.file : ''
  const line = row.line === undefined || row.line === null ? '' : String(row.line)
  return file ? `${file}:${line}` : ''
}

/**
 * Create the search page component.
 *
 * @param {object} React React namespace.
 * @param {{ ui: object, api: object, i18n: object }} bag
 */
export function createSearchPage(React, bag) {
  const { ui, api, i18n } = bag || {}
  if (!ui || !api || !i18n) throw new TypeError('createSearchPage(React, { ui, api, i18n })')
  const h = React.createElement
  const t = (key, vars, fallback) => i18n.t(key, vars, fallback)

  function SearchPage(props) {
    ui.useStyles(SEARCH_CSS)
    const navigate = props && props.navigate
    const initialQuery = props && props.route && props.route.params && props.route.params.q ? String(props.route.params.q) : ''

    const [query, setQuery] = React.useState(initialQuery)
    const [items, setItems] = React.useState([])
    const [phase, setPhase] = React.useState('idle')
    const [error, setError] = React.useState(null)
    const [open, setOpen] = React.useState(Boolean(initialQuery))
    const [active, setActive] = React.useState(-1)

    const inputRef = React.useRef(null)
    const wrapRef = React.useRef(null)
    const resultsRef = React.useRef(null)
    const itemRefs = React.useRef([])
    const timerRef = React.useRef(null)
    const abortRef = React.useRef(null)
    const lastQueryRef = React.useRef('')

    const resultItems = items

    function openSymbol(item) {
      if (!item) return
      const symbol = item.id || item.name || item.qualified_name
      if (!symbol) return
      setOpen(false)
      if (typeof navigate === 'function') navigate({ page: 'function', symbol: String(symbol) })
    }

    function runSearch(text) {
      const trimmed = String(text || '').trim()
      if (abortRef.current) {
        abortRef.current.abort()
        abortRef.current = null
      }
      if (!trimmed) {
        setItems([])
        setPhase('idle')
        setError(null)
        setOpen(false)
        setActive(-1)
        return
      }
      const controller = typeof AbortController === 'function' ? new AbortController() : null
      abortRef.current = controller
      lastQueryRef.current = trimmed
      setPhase('loading')
      setError(null)
      api
        .caGet('/api/search', {
          query: { q: trimmed, limit: 100 },
          signal: controller ? controller.signal : undefined,
        })
        .then((data) => {
          if (controller && controller.signal.aborted) return
          if (lastQueryRef.current !== trimmed) return
          const rows = data && Array.isArray(data.items) ? data.items : []
          setItems(rows)
          setPhase('ready')
          setOpen(true)
          setActive(rows.length > 0 ? 0 : -1)
        })
        .catch((requestError) => {
          if (controller && controller.signal.aborted) return
          const apiError = requestError && requestError.name === 'ApiError' ? requestError : null
          if (apiError && apiError.code === 'ABORTED') return
          setItems([])
          setPhase('error')
          setError(requestError)
          setOpen(true)
          setActive(-1)
        })
        .finally(() => {
          if (abortRef.current === controller) abortRef.current = null
        })
    }

    // Debounced search on query changes.
    React.useEffect(() => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null
        runSearch(query)
      }, 180)
      return () => {
        if (timerRef.current !== null) {
          window.clearTimeout(timerRef.current)
          timerRef.current = null
        }
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [query])

    // Abort in-flight request on unmount.
    React.useEffect(
      () => () => {
        if (abortRef.current) {
          abortRef.current.abort()
          abortRef.current = null
        }
      },
      [],
    )

    // Focus the search box when the page opens.
    React.useEffect(() => {
      if (inputRef.current && typeof inputRef.current.focus === 'function') {
        inputRef.current.focus()
      }
    }, [])

    // Global `/` shortcut (ignored while typing in another control) + Esc.
    React.useEffect(() => {
      const onKeyDown = (event) => {
        if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return
        if (event.key === '/' && !isEditableTarget(event.target)) {
          event.preventDefault()
          if (inputRef.current && typeof inputRef.current.focus === 'function') inputRef.current.focus()
          return
        }
        if (event.key === 'Escape') {
          setOpen(false)
        }
      }
      window.addEventListener('keydown', onKeyDown)
      return () => window.removeEventListener('keydown', onKeyDown)
    }, [])

    // Close the dropdown when clicking outside this page's search wrapper.
    React.useEffect(() => {
      const onPointerDown = (event) => {
        const wrap = wrapRef.current
        if (!wrap) return
        if (event.target instanceof Node && !wrap.contains(event.target)) setOpen(false)
      }
      document.addEventListener('pointerdown', onPointerDown)
      return () => document.removeEventListener('pointerdown', onPointerDown)
    }, [])

    // Keep the active row visible.
    React.useEffect(() => {
      const node = itemRefs.current[active]
      if (node && typeof node.scrollIntoView === 'function') {
        node.scrollIntoView({ block: 'nearest' })
      }
    }, [active])

    function moveActive(delta) {
      if (resultItems.length === 0) return
      setOpen(true)
      setActive((current) => {
        const base = current < 0 ? (delta > 0 ? -1 : 0) : current
        const next = base + delta
        if (next < 0) return resultItems.length - 1
        if (next >= resultItems.length) return 0
        return next
      })
    }

    const onInputKeyDown = (event) => {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        moveActive(1)
      } else if (event.key === 'ArrowUp') {
        event.preventDefault()
        moveActive(-1)
      } else if (event.key === 'Enter') {
        event.preventDefault()
        if (!open) {
          runSearch(query)
          return
        }
        const selected = active >= 0 && active < resultItems.length ? resultItems[active] : resultItems[0]
        openSymbol(selected)
      } else if (event.key === 'Escape') {
        event.preventDefault()
        if (open) setOpen(false)
        else if (query) {
          setQuery('')
          setItems([])
          setPhase('idle')
          setOpen(false)
        }
      }
    }

    const dropdownOpen = open && query.trim().length > 0
    const count = resultItems.length

    const results = dropdownOpen
      ? h(
          'div',
          {
            className: 'ca-search-dropdown',
            id: 'ca-search-results',
            role: 'listbox',
            'aria-label': t('nav.results', null, 'Results'),
            ref: resultsRef,
          },
          h(
            'div',
            { className: 'ca-search-dropdown-head' },
            h(
              'span',
              null,
              query.trim()
                ? t('search.resultsFor', { query: query.trim() }, `Results for “${query.trim()}”`)
                : t('nav.results', null, 'Results'),
            ),
            count > 0 ? h(ui.Badge, { tone: 'neutral' }, String(count)) : null,
            phase === 'loading' ? h(ui.Spinner, { size: 12 }) : null,
            h('span', { className: 'ca-spacer' }),
            h('span', null, t('search.hint', null, '↑ ↓ move · Enter open · Esc close')),
          ),
          phase === 'error'
            ? h(ui.ErrorBox, {
                title: t('common.error', null, 'Error'),
                error,
                retryLabel: t('common.retry', null, 'Retry'),
                onRetry: () => runSearch(query),
              })
            : null,
          phase !== 'error' && count === 0 && phase !== 'loading'
            ? h(
                'div',
                { className: 'ca-search-empty' },
                t('search.noMatches', { query: query.trim() }, `No symbols match “${query.trim()}”.`),
              )
            : null,
          phase === 'loading' && count === 0
            ? h('div', { className: 'ca-search-empty' }, t('search.loading', null, 'Searching…'))
            : null,
          resultItems.map((item, index) => {
            const activeRow = index === active
            const name = item.name || item.id || '?'
            return h(
              'button',
              {
                key: item.id || `${item.name || 'row'}-${index}`,
                type: 'button',
                role: 'option',
                'aria-selected': activeRow ? 'true' : 'false',
                className: `ca-search-result${activeRow ? ' ca-search-result-active' : ''}`,
                ref: (node) => {
                  itemRefs.current[index] = node
                },
                onMouseEnter: () => setActive(index),
                onClick: () => openSymbol(item),
                title: item.qualified_name || item.id || name,
              },
              h(
                'span',
                { className: 'ca-search-result-name' },
                h('span', null, name),
                item.is_definition
                  ? h(ui.Badge, { tone: 'success' }, t('badge.definition', null, 'definition'))
                  : h(ui.Badge, { tone: 'muted' }, t('badge.declaration', null, 'declaration')),
                item.is_static ? h(ui.Badge, { tone: 'neutral' }, t('badge.static', null, 'static')) : null,
                item.shard
                  ? h(ui.Badge, { tone: 'info' }, t('badge.shard', { name: item.shard }, `shard ${item.shard}`))
                  : null,
              ),
              h(
                'span',
                { className: 'ca-search-result-meta' },
                [locationOf(item), item.kind || '', item.linkage || ''].filter(Boolean).join(' · ') || '—',
              ),
            )
          }),
        )
      : null

    const welcome = !query.trim()
      ? h(
          'div',
          { className: 'ca-search-welcome' },
          h(
            'p',
            { className: 'ca-search-lede' },
            t(
              'search.welcomeBody',
              null,
              'CA analyzes operating-system source code from its real build: exact compile arguments, Clang AST, CFG and data-flow facts stored in SQLite. Search a symbol above or open a view below.',
            ),
          ),
          h(
            'div',
            { className: 'ca-search-cards' },
            SHORTCUTS.map((shortcut) =>
              h(
                'button',
                {
                  key: shortcut.page,
                  type: 'button',
                  className: 'ca-search-card',
                  onClick: () => {
                    if (typeof navigate === 'function') navigate({ page: shortcut.page })
                  },
                },
                h(
                  'span',
                  { className: 'ca-search-card-title' },
                  h(ui.Icon, { name: shortcut.icon, size: 14 }),
                  t(shortcut.titleKey, null, shortcut.titleFallback),
                ),
                h(
                  'span',
                  { className: 'ca-search-card-body' },
                  t(shortcut.bodyKey, null, shortcut.bodyFallback),
                ),
              ),
            ),
          ),
        )
      : null

    return h(
      'div',
      { className: 'ca-page ca-search-page' },
      h(
        'div',
        { className: 'ca-search-inner' },
        h(
          'div',
          { className: 'ca-search-head' },
          h('h1', { className: 'ca-search-title' }, t('search.title', null, 'Symbol search')),
          h(
            'p',
            { className: 'ca-search-sub' },
            t(
              'search.subtitle',
              null,
              'Search functions, types and declarations across every translation unit.',
            ),
          ),
        ),
        h(
          'div',
          { className: 'ca-search-wrap', ref: wrapRef },
          h(
            'div',
            { className: 'ca-search-box' },
            h(ui.Icon, { name: 'search', size: 16, className: 'ca-icon-search' }),
            h(ui.Input, {
              value: query,
              onChange: (event) => setQuery(event.target.value),
              onKeyDown: onInputKeyDown,
              placeholder: t(
                'search.placeholder',
                null,
                'Search functions by name, qualified name or id substring…',
              ),
              ariaLabel: t('search.label', null, 'Search functions'),
              inputRef,
              autoFocus: true,
              type: 'text',
              autoComplete: 'off',
            }),
            phase === 'loading' ? h(ui.Spinner, { size: 14 }) : null,
            query
              ? h(
                  ui.IconButton,
                  {
                    label: t('search.kbdClear', null, 'clear'),
                    title: t('search.kbdClear', null, 'clear'),
                    size: 24,
                    onClick: () => {
                      setQuery('')
                      if (inputRef.current && typeof inputRef.current.focus === 'function') {
                        inputRef.current.focus()
                      }
                    },
                  },
                  h(ui.Icon, { name: 'close', size: 13 }),
                )
              : h('span', { className: 'ca-search-kbd', 'aria-hidden': 'true' }, '/'),
            h(
              'span',
              { className: 'ca-search-kbd', 'aria-hidden': 'true' },
              '↵',
            ),
          ),
          results,
        ),
        welcome,
        h(
          'div',
          { className: 'ca-search-hint' },
          h('span', null, t('search.focusHint', null, 'Press / to focus search')),
          h('span', { className: 'ca-spacer' }),
          h('span', null, t('footer.text', null, 'CA web UI · read-only · loopback only')),
        ),
      ),
    )
  }

  SearchPage.displayName = 'CaSearchPage'
  return SearchPage
}

export default createSearchPage
