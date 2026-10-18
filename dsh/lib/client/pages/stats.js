/**
 * Database statistics page (`#/stats`).
 *
 * Ported from the vanilla CA web UI stats view: `/api/stats` provides table row
 * counts, database size, schema/SQLite versions, callback statistics and (in
 * federated mode) per-shard size/schema/counts. The page is read-only and shows
 * every number with the same meaning as the CLI/MCP output.
 */

const STATS_CSS = `
.ca-stats-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
  color: var(--dsw-alias-label-primary, #1a2230);
}
.ca-stats-head { display: flex; align-items: flex-start; gap: 10px; flex-wrap: wrap; flex: 0 0 auto; }
.ca-stats-title { margin: 0; font-size: 18px; font-weight: 600; }
.ca-stats-sub { margin: 3px 0 0; color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 12px; }
.ca-stats-head-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
.ca-stats-scroll { flex: 1 1 auto; min-height: 0; overflow: auto; padding-right: 2px; }
.ca-stats-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.ca-stats-card {
  padding: 10px 12px;
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-md, 8px);
  background: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
}
.ca-stats-card-label {
  display: block;
  color: var(--dsw-alias-label-tertiary, #8a94a5);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.ca-stats-card-value {
  display: block;
  margin-top: 3px;
  font-family: var(--ds-font-family-code, ui-monospace, monospace);
  font-size: 19px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.ca-stats-card-sub { display: block; color: var(--dsw-alias-label-secondary, #5a6577); font-size: 11px; overflow-wrap: anywhere; }
.ca-stats-section {
  margin-bottom: 12px;
  border: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  border-radius: var(--dsw-radius-md, 8px);
  background: var(--dsw-alias-bg-layer-1, var(--dsw-alias-bg-base, #ffffff));
  overflow: hidden;
}
.ca-stats-section-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--dsw-alias-border-l2, #d9dee7);
  background: var(--dsw-alias-bg-layer-2, transparent);
}
.ca-stats-section-head h3 { margin: 0; font-size: 13px; font-weight: 600; }
.ca-stats-section-note { color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 11.5px; }
.ca-stats-section-body { padding: 0; overflow: auto; }
.ca-stats-sources { margin: 0; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; }
.ca-stats-sources code {
  font-family: var(--ds-font-family-code, ui-monospace, monospace);
  font-size: 11.5px;
  color: var(--dsw-alias-label-secondary, #5a6577);
  overflow-wrap: anywhere;
}
.ca-stats-callout {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-bottom: 12px;
  padding: 10px 13px;
  border: 1px solid var(--dsw-alias-state-warn-secondary, rgba(161, 92, 7, 0.35));
  border-left: 3px solid var(--dsw-alias-state-warn-primary, #a15c07);
  border-radius: var(--dsw-radius-sm, 6px);
  background: var(--dsw-alias-state-warn-secondary, rgba(161, 92, 7, 0.08));
  color: var(--dsw-alias-state-warn-label, var(--dsw-alias-label-primary, #1a2230));
  font-size: 12.5px;
}
.ca-stats-status { padding: 40px 12px; text-align: center; color: var(--dsw-alias-label-tertiary, #8a94a5); font-size: 12.5px; }
.ca-stats-status .ca-error { max-width: 560px; margin: 0 auto; }
.ca-stats-mono { font-family: var(--ds-font-family-code, ui-monospace, monospace); }
`

function statsFormatCount(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return String(Math.trunc(number)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function statsFormatBytes(value) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return String(bytes) + ' B'
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = bytes / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return (size >= 10 ? size.toFixed(1) : size.toFixed(2)) + ' ' + units[unit]
}

export function createStatsPage(React, bag) {
  if (!React || typeof React.createElement !== 'function') {
    throw new TypeError('createStatsPage(React, bag): React with createElement is required')
  }
  const h = React.createElement
  const ui = (bag && bag.ui) || {}
  const api = (bag && bag.api) || {}
  const i18n = (bag && bag.i18n) || {}
  const useStyles = typeof ui.useStyles === 'function' ? ui.useStyles : function noStatsStyles() {}

  const Button = typeof ui.Button === 'function' ? ui.Button : function FallbackStatsButton(props) {
    return h(
      'button',
      { type: 'button', disabled: props.disabled ? true : undefined, title: props.title, onClick: props.onClick },
      props.children,
    )
  }
  const Spinner = typeof ui.Spinner === 'function' ? ui.Spinner : function FallbackStatsSpinner(props) {
    return h('span', { role: 'status' }, props.label || 'loading…')
  }
  const ErrorBox = typeof ui.ErrorBox === 'function' ? ui.ErrorBox : function FallbackStatsError(props) {
    const error = props.error !== undefined ? props.error : props.message
    const message = error && typeof error === 'object' ? String(error.message || error.code || error) : String(error || '')
    return h('div', { className: 'ca-error', role: 'alert' }, props.title || 'Error', ' — ', message)
  }
  const Table = typeof ui.Table === 'function' ? ui.Table : function FallbackStatsTable(props) {
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

  function caGet(path, signal) {
    if (api && typeof api.caGet === 'function') return api.caGet(path, { signal })
    if (api && typeof api.caRequest === 'function') return api.caRequest({ method: 'GET', path, signal })
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

  function StatsPage() {
    useStyles(STATS_CSS)
    const [reloadToken, setReloadToken] = React.useState(0)
    const [state, setState] = React.useState({ loading: true, error: null, data: null })

    React.useEffect(() => {
      let cancelled = false
      let controller = null
      try {
        if (typeof AbortController === 'function') controller = new AbortController()
      } catch (_error) {
        controller = null
      }
      setState((previous) => ({ loading: true, error: null, data: previous.data }))
      caGet('/api/stats', controller ? controller.signal : undefined)
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
    }, [reloadToken])

    const data = state.data
    const tables = (data && data.tables) || {}
    const database = (data && data.database) || {}
    const callbacks = (data && data.callbacks) || null
    const federated = Boolean((database && database.federated) || (data && data.federated))
    // `/api/stats` in federated mode returns `shards` as entries with counts;
    // `database.shards` only carries shard basenames, so never use it for rows.
    const shards = data && Array.isArray(data.shards) ? data.shards : []
    const skipped =
      (data && Array.isArray(data.skipped) ? data.skipped : null) ||
      (Array.isArray(database.skipped) ? database.skipped : [])
    const paths = Array.isArray(database.paths) ? database.paths : []

    const header = h(
      'header',
      { className: 'ca-stats-head' },
      h(
        'div',
        null,
        h('h2', { className: 'ca-stats-title' }, t('stats.title', null, 'Database statistics')),
        h(
          'p',
          { className: 'ca-stats-sub' },
          federated
            ? t('stats.subFederated', { count: shards.length }, 'federated · {count} shards')
            : t('common.singleDatabase', null, 'single database'),
        ),
      ),
      h(
        'div',
        { className: 'ca-stats-head-actions' },
        state.loading ? h(Spinner, { label: t('common.loading', null, 'loading…') }) : null,
        h(
          Button,
          { variant: 'secondary', size: 'sm', onClick: () => setReloadToken((value) => value + 1), disabled: state.loading },
          t('stats.refresh', null, 'Refresh'),
        ),
      ),
    )

    if (state.error) {
      return h(
        'div',
        { className: 'ca-stats-page' },
        header,
        h(
          'div',
          { className: 'ca-stats-status' },
          h(ErrorBox, {
            title: t('error.loadStats', null, 'Could not load statistics'),
            error: state.error,
            message: state.error.message,
            onRetry: () => setReloadToken((value) => value + 1),
          }),
        ),
      )
    }
    if (!data) {
      return h('div', { className: 'ca-stats-page' }, header, h('div', { className: 'ca-stats-status' }, h(Spinner, { label: t('status.loadingStats', null, 'Loading statistics…') })))
    }

    const cards = [
      {
        key: 'size',
        label: t('stats.cardSize', null, 'Database size'),
        value: statsFormatBytes(data.db_bytes),
        sub: federated
          ? t('stats.shardsSub', { count: shards.length }, '{count} shards')
          : t('common.singleDatabase', null, 'single database'),
      },
      {
        key: 'functions',
        label: t('stats.cardFunctions', null, 'Functions'),
        value: statsFormatCount(tables.functions),
        sub: t('stats.indexedFunctions', null, 'indexed functions'),
      },
      {
        key: 'calls',
        label: t('stats.cardCalls', null, 'Calls'),
        value: statsFormatCount(tables.calls),
        sub: t('stats.callSites', null, 'call sites'),
      },
      {
        key: 'branches',
        label: t('stats.cardBranches', null, 'Branches'),
        value: statsFormatCount(tables.branches),
        sub: t('stats.branchConditions', null, 'branch conditions'),
      },
      {
        key: 'schema',
        label: t('stats.cardSchema', null, 'Schema'),
        value: String(data.schema_version === undefined || data.schema_version === null ? '—' : data.schema_version),
        sub: 'SQLite ' + String(data.sqlite_version || '?'),
      },
    ]
    if (callbacks) {
      cards.push({
        key: 'callbacks',
        label: t('stats.cardCallbacks', null, 'Callback registrations'),
        value: statsFormatCount(callbacks.registrations),
        sub: t('stats.linkedCandidates', { count: statsFormatCount(callbacks.linked_call_targets) }, '{count} linked candidates'),
      })
    }

    const tableRows = Object.keys(tables)
      .sort((left, right) => {
        const delta = Number(tables[right]) - Number(tables[left])
        return delta !== 0 ? delta : left.localeCompare(right)
      })
      .map((name) => ({ name, count: tables[name] }))

    const shardRows = shards.map((shard) => {
      const shardTables = shard.tables || {}
      return {
        shard: shard.shard || shard.path || '?',
        bytes: shard.db_bytes,
        schema: shard.schema_version,
        functions: shardTables.functions,
        calls: shardTables.calls,
        callbacks: shardTables.callback_registrations,
      }
    })

    const callbackKindRows = callbacks
      ? Object.keys(callbacks.by_kind || {}).map((kind) => ({ kind, count: callbacks.by_kind[kind] }))
      : []

    const sections = []
    sections.push(
      h(
        'div',
        { className: 'ca-stats-cards', key: 'cards' },
        cards.map((card) =>
          h(
            'div',
            { className: 'ca-stats-card', key: card.key },
            h('span', { className: 'ca-stats-card-label' }, card.label),
            h('span', { className: 'ca-stats-card-value' }, card.value),
            card.sub ? h('span', { className: 'ca-stats-card-sub' }, card.sub) : null,
          ),
        ),
      ),
    )
    if (skipped.length) {
      sections.push(
        h(
          'div',
          { className: 'ca-stats-callout', key: 'skipped' },
          h(
            'span',
            null,
            h('strong', null, t('error.shardSkipped', { count: skipped.length }, '{count} shard(s) skipped.')),
            ' ',
            skipped
              .map((item) => String((item && item.path) || '?') + ': ' + String((item && item.error) || 'unreadable'))
              .join('; '),
          ),
        ),
      )
    }
    sections.push(
      h(
        'section',
        { className: 'ca-stats-section', key: 'sources' },
        h(
          'div',
          { className: 'ca-stats-section-head' },
          h('h3', null, t('section.sources', null, 'Sources')),
          h('span', { className: 'ca-stats-section-note' }, t('section.sourcesNote', null, 'read-only SQLite files backing this UI')),
        ),
        h(
          'div',
          { className: 'ca-stats-sources' },
          paths.length
            ? paths.map((path, index) => h('code', { key: index }, String(path)))
            : h('code', null, '—'),
        ),
      ),
    )
    sections.push(
      h(
        'section',
        { className: 'ca-stats-section', key: 'tables' },
        h(
          'div',
          { className: 'ca-stats-section-head' },
          h('h3', null, t('section.tableRows', null, 'Table rows')),
          h('span', { className: 'ca-stats-section-note' }, t('section.tableRowsNote', null, 'all CA tables')),
        ),
        h(
          'div',
          { className: 'ca-stats-section-body' },
          h(Table, {
            columns: [
              { key: 'name', label: t('table.table', null, 'Table') },
              {
                key: 'count',
                label: t('table.rows', null, 'Rows'),
                align: 'right',
                render: (row) => h('span', { className: 'ca-stats-mono' }, statsFormatCount(row.count)),
              },
            ],
            rows: tableRows,
            rowKey: (row) => row.name,
            empty: t('common.loading', null, 'loading…'),
          }),
        ),
      ),
    )
    if (shardRows.length) {
      sections.push(
        h(
          'section',
          { className: 'ca-stats-section', key: 'shards' },
          h(
            'div',
            { className: 'ca-stats-section-head' },
            h('h3', null, t('section.shards', null, 'Shards')),
            h('span', { className: 'ca-stats-section-note' }, t('section.shardsNote', null, 'per-database provenance')),
          ),
          h(
            'div',
            { className: 'ca-stats-section-body' },
            h(Table, {
              columns: [
                { key: 'shard', label: t('table.shard', null, 'Shard') },
                { key: 'bytes', label: t('table.size', null, 'Size'), align: 'right', render: (row) => statsFormatBytes(row.bytes) },
                { key: 'schema', label: t('table.schema', null, 'Schema'), align: 'right', render: (row) => String(row.schema === undefined || row.schema === null ? '—' : row.schema) },
                { key: 'functions', label: t('table.functions', null, 'Functions'), align: 'right', render: (row) => statsFormatCount(row.functions) },
                { key: 'calls', label: t('table.calls', null, 'Calls'), align: 'right', render: (row) => statsFormatCount(row.calls) },
                { key: 'callbacks', label: t('table.callbacks', null, 'Callbacks'), align: 'right', render: (row) => statsFormatCount(row.callbacks) },
              ],
              rows: shardRows,
              rowKey: (row) => row.shard,
            }),
          ),
        ),
      )
    }
    if (callbacks) {
      sections.push(
        h(
          'section',
          { className: 'ca-stats-section', key: 'callback-kinds' },
          h(
            'div',
            { className: 'ca-stats-section-head' },
            h('h3', null, t('section.callbacks', null, 'Callbacks')),
            h('span', { className: 'ca-stats-section-note' }, t('section.callbacksNote', null, 'cross-TU registration facts')),
          ),
          h(
            'div',
            { className: 'ca-stats-section-body' },
            h(Table, {
              columns: [
                { key: 'kind', label: t('table.registrationKind', null, 'Registration kind') },
                { key: 'count', label: t('table.count', null, 'Count'), align: 'right', render: (row) => statsFormatCount(row.count) },
              ],
              rows: callbackKindRows,
              rowKey: (row) => row.kind,
              empty: t('empty.callbackKinds', null, 'No callback registrations in this database.'),
            }),
          ),
        ),
      )
    }

    return h(
      'div',
      { className: 'ca-stats-page' },
      header,
      h('div', { className: 'ca-stats-scroll' }, sections),
    )
  }

  return StatsPage
}

export default createStatsPage
