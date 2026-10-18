/**
 * Callback registrations page (`#/callbacks`).
 *
 * Ported from the vanilla CA web UI: cross-TU function-pointer registration
 * facts (`/api/callbacks`) with object-path / field / module / registration-kind
 * filters, offset pagination and a target link that opens the function detail
 * page. `object_path` cells carry the raw `evidence` list as a tooltip; the
 * registration/stat cards come from `/api/stats.callbacks`.
 */

const CALLBACKS_CSS = `
.ca-callbacks-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
  color: var(--dsw-alias-label-primary, #1a2230);
}
.ca-callbacks-head { display: flex; align-items: flex-start; gap: 10px; flex-wrap: wrap; flex: 0 0 auto; }
.ca-callbacks-title { margin: 0; font-size: 18px; font-weight: 600; }
.ca-callbacks-sub { margin: 3px 0 0; color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 12px; }
.ca-callbacks-head-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.ca-callbacks-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  flex: 0 0 auto;
}
.ca-callbacks-card {
  padding: 10px 12px;
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-md, 8px);
  background: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
}
.ca-callbacks-card-label {
  display: block;
  color: var(--dsw-alias-label-tertiary, #8a94a5);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.ca-callbacks-card-value {
  display: block;
  margin-top: 3px;
  font-family: var(--ds-font-family-code, ui-monospace, monospace);
  font-size: 19px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.ca-callbacks-card-sub { display: block; color: var(--dsw-alias-label-secondary, #5a6577); font-size: 11px; }
.ca-callbacks-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  padding: 8px 10px;
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-md, 8px);
  background: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
}
.ca-callbacks-filters .ca-input { min-width: 150px; flex: 1 1 150px; }
.ca-callbacks-filters .ca-select { min-width: 130px; }
.ca-callbacks-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-md, 8px);
  background: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
}
.ca-callbacks-note { color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 11.5px; padding: 6px 0; }
.ca-callbacks-pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
  padding: 2px 0;
}
.ca-callbacks-page-info {
  margin-left: auto;
  color: var(--dsw-alias-label-tertiary, #8a94a5);
  font-family: var(--ds-font-family-code, ui-monospace, monospace);
  font-size: 11.5px;
}
.ca-callbacks-mono { font-family: var(--ds-font-family-code, ui-monospace, monospace); overflow-wrap: anywhere; }
.ca-callbacks-target {
  padding: 0;
  border: 0;
  background: none;
  color: var(--dsw-alias-link, #2458d6);
  font-family: var(--ds-font-family-code, ui-monospace, monospace);
  font-size: 12px;
  cursor: pointer;
  text-align: left;
  overflow-wrap: anywhere;
}
.ca-callbacks-target:hover { text-decoration: underline; }
.ca-callbacks-shard {
  display: inline-block;
  padding: 0 6px;
  border: 1px solid var(--dsw-alias-border-l3, #bcc6d4);
  border-radius: 999px;
  color: var(--dsw-alias-label-secondary, #5a6577);
  font-size: 10.5px;
  white-space: nowrap;
}
.ca-callbacks-status { padding: 32px 12px; text-align: center; color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 12.5px; }
.ca-callbacks-status .ca-error { max-width: 560px; margin: 0 auto; }
`

function callbacksFormatCount(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return String(Math.trunc(number)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

const CALLBACKS_EMPTY_FILTERS = { path: '', field: '', module: '', kind: '' }

export function createCallbacksPage(React, bag) {
  if (!React || typeof React.createElement !== 'function') {
    throw new TypeError('createCallbacksPage(React, bag): React with createElement is required')
  }
  const h = React.createElement
  const ui = (bag && bag.ui) || {}
  const api = (bag && bag.api) || {}
  const i18n = (bag && bag.i18n) || {}
  const useStyles = typeof ui.useStyles === 'function' ? ui.useStyles : function noCallbacksStyles() {}

  const Button = typeof ui.Button === 'function' ? ui.Button : function FallbackCallbacksButton(props) {
    return h(
      'button',
      { type: 'button', disabled: props.disabled ? true : undefined, title: props.title, onClick: props.onClick },
      props.children,
    )
  }
  const Input = typeof ui.Input === 'function' ? ui.Input : function FallbackCallbacksInput(props) {
    return h('input', {
      className: 'ca-input',
      value: props.value,
      placeholder: props.placeholder,
      onChange: props.onChange,
      onKeyDown: props.onKeyDown,
    })
  }
  const Select = typeof ui.Select === 'function' ? ui.Select : function FallbackCallbacksSelect(props) {
    const options = Array.isArray(props.options) ? props.options : []
    return h(
      'select',
      {
        className: 'ca-select',
        value: props.value,
        onChange: (event) => {
          if (typeof props.onChange === 'function') props.onChange(event.target.value)
        },
      },
      options.map((option) => h('option', { key: String(option.value), value: String(option.value) }, option.label)),
    )
  }
  const Spinner = typeof ui.Spinner === 'function' ? ui.Spinner : function FallbackCallbacksSpinner(props) {
    return h('span', { role: 'status' }, props.label || 'loading…')
  }
  const ErrorBox = typeof ui.ErrorBox === 'function' ? ui.ErrorBox : function FallbackCallbacksError(props) {
    const error = props.error !== undefined ? props.error : props.message
    const message = error && typeof error === 'object' ? String(error.message || error.code || error) : String(error || '')
    return h('div', { className: 'ca-error', role: 'alert' }, props.title || 'Error', ' — ', message)
  }
  const Table = typeof ui.Table === 'function' ? ui.Table : function FallbackCallbacksTable(props) {
    const columns = Array.isArray(props.columns) ? props.columns : []
    const rows = Array.isArray(props.rows) ? props.rows : []
    return h(
      'table',
      { className: 'ca-table' },
      h('thead', null, h('tr', null, columns.map((column) => h('th', { key: column.key }, column.label)))),
      h(
        'tbody',
        null,
        rows.length
          ? rows.map((row, index) =>
              h(
                'tr',
                { key: index },
                columns.map((column) =>
                  h('td', { key: column.key }, typeof column.render === 'function' ? column.render(row, index) : row[column.key]),
                ),
              ),
            )
          : h('tr', null, h('td', { colSpan: Math.max(1, columns.length) }, props.empty || '—')),
      ),
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
        /* fall through */
      }
    }
    return formatText(fallback || key, vars)
  }

  function caGet(path, query, signal) {
    if (api && typeof api.caGet === 'function') return api.caGet(path, { query, signal })
    if (api && typeof api.caRequest === 'function') return api.caRequest({ method: 'GET', path, query, signal })
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

  const PAGE_SIZE = 100

  function CallbacksPage(props) {
    useStyles(CALLBACKS_CSS)
    const [filters, setFilters] = React.useState(CALLBACKS_EMPTY_FILTERS)
    const [applied, setApplied] = React.useState(CALLBACKS_EMPTY_FILTERS)
    const [offset, setOffset] = React.useState(0)
    const [reloadToken, setReloadToken] = React.useState(0)
    const [statsToken, setStatsToken] = React.useState(0)
    const [stats, setStats] = React.useState(null)
    const [state, setState] = React.useState({ loading: true, error: null, data: null })

    React.useEffect(() => {
      let cancelled = false
      let controller = null
      try {
        if (typeof AbortController === 'function') controller = new AbortController()
      } catch (_error) {
        controller = null
      }
      caGet('/api/stats', null, controller ? controller.signal : undefined)
        .then((data) => {
          if (cancelled) return
          setStats(data && typeof data === 'object' ? data : null)
        })
        .catch(() => {
          if (cancelled) return
          setStats(null)
        })
      return () => {
        cancelled = true
        if (controller) {
          try {
            controller.abort()
          } catch (_error) {
            /* ignore */
          }
        }
      }
    }, [statsToken])

    React.useEffect(() => {
      let cancelled = false
      let controller = null
      try {
        if (typeof AbortController === 'function') controller = new AbortController()
      } catch (_error) {
        controller = null
      }
      const query = { limit: PAGE_SIZE, offset }
      if (applied.path) query.path = applied.path
      if (applied.field) query.field = applied.field
      if (applied.module) query.module = applied.module
      if (applied.kind) query.kind = applied.kind
      setState((previous) => ({ loading: true, error: null, data: previous.data }))
      caGet('/api/callbacks', query, controller ? controller.signal : undefined)
        .then((data) => {
          if (cancelled) return
          setState({ loading: false, error: null, data: data && typeof data === 'object' ? data : {} })
        })
        .catch((error) => {
          if (cancelled) return
          const parts = errorParts(error)
          if (parts.code === 'ABORTED') return
          setState({ loading: false, error: parts, data: null })
        })
      return () => {
        cancelled = true
        if (controller) {
          try {
            controller.abort()
          } catch (_error) {
            /* ignore */
          }
        }
      }
    }, [applied.path, applied.field, applied.module, applied.kind, offset, reloadToken])

    function updateFilter(name, value) {
      setFilters((previous) => {
        const next = Object.assign({}, previous)
        next[name] = value
        return next
      })
    }

    function applyFilters() {
      setOffset(0)
      setApplied({
        path: String(filters.path || '').trim(),
        field: String(filters.field || '').trim(),
        module: String(filters.module || '').trim(),
        kind: String(filters.kind || '').trim(),
      })
    }

    function clearFilters() {
      setFilters(CALLBACKS_EMPTY_FILTERS)
      setApplied(CALLBACKS_EMPTY_FILTERS)
      setOffset(0)
    }

    function submitOnEnter(event) {
      if (!event) return
      if (event.key !== 'Enter') return
      event.preventDefault()
      applyFilters()
    }

    function navigateToFunction(target) {
      const value = String(target || '').trim()
      if (!value) return
      if (props && typeof props.navigate === 'function') {
        props.navigate('#/fn/' + encodeURIComponent(value))
      }
    }

    const data = state.data
    const rows = (data && Array.isArray(data.items) ? data.items : []).slice()
    const totals = stats && stats.callbacks ? stats.callbacks : null
    const federated = Boolean(data && data.federated)
    const kindCounts = (totals && totals.by_kind) || {}
    const kindOptions = [{ value: '', label: t('callbacks.kindAll', null, 'all kinds') }].concat(
      Object.keys(kindCounts)
        .sort()
        .map((kind) => ({ value: kind, label: kind + ' (' + callbacksFormatCount(kindCounts[kind]) + ')' })),
    )

    const cards = [
      {
        key: 'registrations',
        label: t('callbacks.cardRegistrations', null, 'Registrations'),
        value: callbacksFormatCount(totals ? totals.registrations : data ? data.count : undefined),
        sub: t('callbacks.functionPointerFacts', null, 'function-pointer facts'),
      },
      {
        key: 'targets',
        label: t('callbacks.cardTargets', null, 'Targets'),
        value: callbacksFormatCount(totals ? totals.targets : undefined),
        sub: t('callbacks.distinctTargets', null, 'distinct callback targets'),
      },
      {
        key: 'paths',
        label: t('callbacks.cardPaths', null, 'Object paths'),
        value: callbacksFormatCount(totals ? totals.object_paths : undefined),
        sub: t('callbacks.pathsDesc', null, 'globals, fields, table slots'),
      },
      {
        key: 'linked',
        label: t('callbacks.cardLinked', null, 'Linked candidates'),
        value: callbacksFormatCount(totals ? totals.linked_call_targets : undefined),
        sub: t('callbacks.linkedDesc', null, 'resolved call_target edges'),
      },
    ]

    const filtersBar = h(
      'div',
      { className: 'ca-callbacks-filters', role: 'search' },
      h(Input, {
        value: filters.path,
        placeholder: t('callbacks.filterPath', null, 'object path prefix, e.g. g_ops.'),
        ariaLabel: t('callbacks.ariaPath', null, 'Object path prefix'),
        onChange: (event) => updateFilter('path', event && event.target ? event.target.value : ''),
        onKeyDown: submitOnEnter,
      }),
      h(Input, {
        value: filters.field,
        placeholder: t('callbacks.filterField', null, 'field, e.g. read'),
        ariaLabel: t('callbacks.ariaField', null, 'Object field'),
        onChange: (event) => updateFilter('field', event && event.target ? event.target.value : ''),
        onKeyDown: submitOnEnter,
      }),
      h(Input, {
        value: filters.module,
        placeholder: t('callbacks.filterModule', null, 'module'),
        ariaLabel: t('callbacks.ariaModule', null, 'Module'),
        onChange: (event) => updateFilter('module', event && event.target ? event.target.value : ''),
        onKeyDown: submitOnEnter,
      }),
      h(Select, {
        value: filters.kind,
        options: kindOptions,
        ariaLabel: t('callbacks.ariaKind', null, 'Registration kind'),
        onChange: (next) => updateFilter('kind', next),
      }),
      h(Button, { variant: 'primary', size: 'sm', onClick: applyFilters }, t('callbacks.apply', null, 'Apply filters')),
      h(Button, { variant: 'ghost', size: 'sm', onClick: clearFilters }, t('callbacks.clear', null, 'Clear')),
    )

    const evidenceTitle = (row) => {
      const evidence = Array.isArray(row.evidence) ? row.evidence : []
      return evidence
        .map((item) => (typeof item === 'string' ? item : String((item && (item.detail || item.reason || item.evidence)) || '')))
        .filter(Boolean)
        .join(' | ')
    }

    const columns = [
      {
        key: 'object_path',
        label: t('table.objectPath', null, 'Object path'),
        render: (row) =>
          h(
            'span',
            { className: 'ca-callbacks-mono', title: evidenceTitle(row) || undefined },
            row.object_path ? String(row.object_path) : '—',
          ),
      },
      {
        key: 'target',
        label: t('table.target', null, 'Target'),
        render: (row) => {
          const target = row.target_function_id || row.target_function_name
          const label = row.target_function_name || row.target_function_id
          if (!target) return '—'
          return h(
            'button',
            {
              type: 'button',
              className: 'ca-callbacks-target',
              title: t('callbacks.openTarget', null, 'Open the target function detail'),
              onClick: () => navigateToFunction(target),
            },
            String(label),
          )
        },
      },
      {
        key: 'registration_kind',
        label: t('table.registrationKind', null, 'Registration kind'),
        render: (row) => (row.registration_kind ? String(row.registration_kind) : '—'),
      },
      {
        key: 'location',
        label: t('table.location', null, 'Location'),
        render: (row) => {
          const file = row.file ? String(row.file) : ''
          const line = Number(row.line) || 0
          const text = file ? (line ? file + ':' + line : file) : '—'
          return h('span', { className: 'ca-callbacks-mono' }, text)
        },
      },
      {
        key: 'module',
        label: t('table.module', null, 'Module'),
        render: (row) => (row.module ? String(row.module) : '—'),
      },
      {
        key: 'registrar',
        label: t('table.registrar', null, 'Registrar'),
        render: (row) => (row.registrar_function_id ? String(row.registrar_function_id) : '—'),
      },
      {
        key: 'shard',
        label: t('table.shard', null, 'Shard'),
        render: (row) => (row.shard ? h('span', { className: 'ca-callbacks-shard' }, String(row.shard)) : '—'),
      },
    ]

    const note = [
      federated
        ? t('common.federated', null, 'federated')
        : t('common.singleDatabase', null, 'single database'),
      t('note.rowsShown', { count: callbacksFormatCount(rows.length) }, '{count} rows shown'),
      t('note.callbackCandidates', null, 'candidate links come from the cross-TU callback pass'),
    ].join(' · ')

    const from = rows.length ? offset + 1 : 0
    const to = offset + rows.length
    const canPrev = offset > 0 && !state.loading
    const canNext = rows.length >= PAGE_SIZE && !state.loading

    let tableArea
    if (state.error) {
      tableArea = h(
        'div',
        { className: 'ca-callbacks-status' },
        h(ErrorBox, {
          title: t('error.loadCallbacks', null, 'Could not load callback registrations'),
          error: state.error,
          message: state.error.message,
          onRetry: () => setReloadToken((value) => value + 1),
        }),
      )
    } else if (!data && state.loading) {
      tableArea = h('div', { className: 'ca-callbacks-status' }, h(Spinner, { label: t('status.loadingCallbacks', null, 'Loading callback registrations…') }))
    } else {
      tableArea = h(Table, {
        columns,
        rows,
        rowKey: (row, index) => String(row.id || 'row-' + (offset + index)),
        empty: t('empty.callbacks', null, 'No callback registrations match. Run `ca resolve-callbacks` on the database to compute them.'),
        dense: true,
      })
    }

    return h(
      'div',
      { className: 'ca-callbacks-page' },
      h(
        'header',
        { className: 'ca-callbacks-head' },
        h(
          'div',
          null,
          h('h2', { className: 'ca-callbacks-title' }, t('callbacks.title', null, 'Callback registrations')),
          h(
            'p',
            { className: 'ca-callbacks-sub' },
            t(
              'callbacks.subtitle',
              null,
              'Function-pointer registration facts extracted from the AST across translation units.',
            ),
          ),
        ),
        h(
          'div',
          { className: 'ca-callbacks-head-actions' },
          state.loading ? h(Spinner, { label: t('common.loading', null, 'loading…') }) : null,
        ),
      ),
      h(
        'div',
        { className: 'ca-callbacks-cards' },
        cards.map((card) =>
          h(
            'div',
            { className: 'ca-callbacks-card', key: card.key },
            h('span', { className: 'ca-callbacks-card-label' }, card.label),
            h('span', { className: 'ca-callbacks-card-value' }, card.value),
            card.sub ? h('span', { className: 'ca-callbacks-card-sub' }, card.sub) : null,
          ),
        ),
      ),
      filtersBar,
      h('div', { className: 'ca-callbacks-note' }, note),
      h('div', { className: 'ca-callbacks-body' }, tableArea),
      h(
        'div',
        { className: 'ca-callbacks-pager' },
        h(
          Button,
          {
            variant: 'secondary',
            size: 'sm',
            disabled: !canPrev,
            onClick: () => setOffset((value) => Math.max(0, value - PAGE_SIZE)),
            title: t('callbacks.prev', null, 'Previous page'),
          },
          '‹ ' + t('callbacks.prev', null, 'Previous'),
        ),
        h(
          Button,
          {
            variant: 'secondary',
            size: 'sm',
            disabled: !canNext,
            onClick: () => setOffset((value) => value + PAGE_SIZE),
            title: t('callbacks.next', null, 'Next page'),
          },
          t('callbacks.next', null, 'Next') + ' ›',
        ),
        h(
          'span',
          { className: 'ca-callbacks-page-info' },
          t('callbacks.pageInfo', { from, to }, 'rows {from}–{to}'),
        ),
      ),
    )
  }

  return CallbacksPage
}

export default createCallbacksPage
