/**
 * CA long-running tasks page (`#/tasks`).
 *
 * Lists the Host TaskManager tasks (`tasks.list`), starts new CA CLI jobs
 * (`tasks.start`), shows task metadata plus a streaming log, and cancels
 * cancellable tasks (`tasks.cancel`).
 *
 * The log is polled incrementally: the first request uses offset 0 and later
 * requests continue from `nextOffset`; `nextOffset === null` means "no new
 * lines, keep the same offset". When the Host reports `clipped` (ring buffer
 * dropped early lines) the visible buffer is replaced with the returned batch.
 */

import {
  elapsedBetween,
  formatBytes,
  formatDuration,
  formatTimestamp,
  toApiError,
} from '../api.js'

const TASKS_CSS = `
.ca-tasks-page { gap: 14px; overflow: auto; padding: 18px; }
.ca-tasks-inner { width: 100%; max-width: 1220px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
.ca-tasks-head { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.ca-tasks-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
.ca-tasks-sub { margin: 2px 0 0; font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-tasks-counts { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 11.5px; color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-tasks-start { display: flex; flex-direction: column; gap: 10px; }
.ca-tasks-start-grid { display: grid; grid-template-columns: minmax(180px, 0.45fr) minmax(280px, 1fr); gap: 10px 14px; align-items: start; }
@media (max-width: 820px) { .ca-tasks-start-grid { grid-template-columns: 1fr; } }
.ca-tasks-kind-hint { margin: 0; font-size: 11.5px; color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-tasks-columns { display: grid; grid-template-columns: minmax(340px, 0.9fr) minmax(380px, 1.1fr); gap: 14px; align-items: start; }
@media (max-width: 980px) { .ca-tasks-columns { grid-template-columns: 1fr; } }
.ca-tasks-list .ca-table-wrap { max-height: 420px; }
.ca-tasks-id { font-family: var(--ds-font-family-code, ui-monospace, monospace); font-size: 11px; color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-tasks-detail-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ca-tasks-log-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 4px 0; }
.ca-tasks-log { height: 340px; overflow: auto; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-markdown-code-block, #f6f8fa); font-family: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 11.5px; line-height: 1.5; }
.ca-tasks-log-empty { display: flex; align-items: center; justify-content: center; height: 100%; min-height: 120px; color: var(--dsw-alias-label-tertiary, #8b949e); font-family: var(--dsw-font-family, system-ui, sans-serif); font-size: 12.5px; }
.ca-tasks-log-line { display: table-row; }
.ca-tasks-log-num { display: table-cell; padding: 0 8px 0 10px; text-align: right; color: var(--dsw-alias-label-tertiary, #8b949e); white-space: nowrap; user-select: none; }
.ca-tasks-log-text { display: table-cell; padding: 0 12px 0 2px; white-space: pre-wrap; overflow-wrap: anywhere; }
.ca-tasks-log-stderr .ca-tasks-log-text { color: var(--dsw-alias-state-error-primary, #d1242f); }
.ca-tasks-log-body { display: table; min-width: 100%; }
.ca-tasks-check { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--dsw-alias-label-secondary, #57606a); cursor: pointer; user-select: none; }
.ca-tasks-check input { width: 13px; height: 13px; accent-color: var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-tasks-notice { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-bg-layer-2, rgba(127, 127, 127, 0.05)); font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-tasks-params { max-height: 180px; }
`

const TASK_KINDS = [
  { value: 'index', hintKey: 'tasks.kind.indexHint', hintFallback: 'Build a fresh index from a repository and compile database.' },
  { value: 'update', hintKey: 'tasks.kind.updateHint', hintFallback: 'Incrementally update an existing index.' },
  { value: 'ensure_deep', hintKey: 'tasks.kind.ensure_deepHint', hintFallback: 'Compute CFG and data-flow facts for deep analysis.' },
  { value: 'prewarm', hintKey: 'tasks.kind.prewarmHint', hintFallback: 'Warm the query caches used by the UI.' },
  { value: 'resolve_callbacks', hintKey: 'tasks.kind.resolve_callbacksHint', hintFallback: 'Resolve cross-TU callback candidates.' },
]

function statusTone(status) {
  switch (status) {
    case 'running':
      return 'info'
    case 'queued':
      return 'warn'
    case 'succeeded':
      return 'success'
    case 'failed':
      return 'error'
    case 'cancelled':
      return 'muted'
    default:
      return 'neutral'
  }
}

function taskElapsed(task) {
  if (!task || typeof task !== 'object') return '—'
  if (task.startedAt) {
    const end = task.finishedAt || Date.now()
    return elapsedBetween(task.startedAt, end)
  }
  if (task.createdAt) return formatDuration(Math.max(0, Date.now() - Number(task.createdAt)))
  return '—'
}

/**
 * Create the tasks page component.
 *
 * @param {object} React React namespace.
 * @param {{ ui: object, api: object, i18n: object }} bag
 */
export function createTasksPage(React, bag) {
  const { ui, api, i18n } = bag || {}
  if (!ui || !api || !i18n) throw new TypeError('createTasksPage(React, { ui, api, i18n })')
  const h = React.createElement
  const t = (key, vars, fallback) => i18n.t(key, vars, fallback)

  function TasksPage() {
    ui.useStyles(TASKS_CSS)

    const [phase, setPhase] = React.useState('loading')
    const [loadError, setLoadError] = React.useState(null)
    const [tasks, setTasks] = React.useState([])
    const [counts, setCounts] = React.useState(null)
    const [selectedId, setSelectedId] = React.useState('')
    const [busy, setBusy] = React.useState('')
    const [actionError, setActionError] = React.useState(null)
    const [notice, setNotice] = React.useState('')

    const [kind, setKind] = React.useState('index')
    const [paramsText, setParamsText] = React.useState('')
    const [paramsError, setParamsError] = React.useState('')

    const [logState, setLogState] = React.useState({ lines: [], nextOffset: 0, total: 0, droppedLines: 0, clipped: false })
    const [logPhase, setLogPhase] = React.useState('idle')
    const [logError, setLogError] = React.useState(null)
    const [autoScroll, setAutoScroll] = React.useState(true)
    const [confirmCancel, setConfirmCancel] = React.useState('')
    const [logReload, setLogReload] = React.useState(0)
    const [backendState, setBackendState] = React.useState(null)

    const mountedRef = React.useRef(true)
    const logBoxRef = React.useRef(null)
    const noticeTimer = React.useRef(null)
    const abortRef = React.useRef(null)
    const selectedIdRef = React.useRef('')
    const logOffsetRef = React.useRef(0)

    React.useEffect(
      () => () => {
        mountedRef.current = false
        if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current)
        if (abortRef.current) abortRef.current.abort()
      },
      [],
    )

    selectedIdRef.current = selectedId

    function showNotice(message) {
      setNotice(message)
      if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current)
      noticeTimer.current = window.setTimeout(() => {
        noticeTimer.current = null
        if (mountedRef.current) setNotice('')
      }, 4000)
    }

    const selectedTask = tasks.find((task) => String(task.id) === String(selectedId)) || null

    const refreshList = React.useCallback(async () => {
      try {
        const data = await api.tasks.list()
        if (!mountedRef.current) return
        const rows = data && Array.isArray(data.tasks) ? data.tasks : []
        setTasks(rows)
        setCounts(data && data.counts ? data.counts : null)
        setPhase('ready')
        setLoadError(null)
        setSelectedId((current) => {
          if (current && rows.some((task) => String(task.id) === String(current))) return current
          const running = rows.find((task) => task.status === 'running' || task.status === 'queued')
          if (running) return String(running.id)
          return rows.length > 0 ? String(rows[0].id) : ''
        })
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setLoadError(apiError)
        setPhase((current) => (current === 'loading' ? 'error' : current))
      }
    }, [])

    React.useEffect(() => {
      void refreshList()
      api
        .getStatus()
        .then((status) => {
          if (!mountedRef.current) return
          setBackendState(status && status.backend ? status.backend : null)
        })
        .catch(() => {})
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // Poll the task list.
    React.useEffect(() => {
      const timer = window.setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        void refreshList()
      }, 2000)
      return () => window.clearInterval(timer)
    }, [refreshList])

    // Reset + load the selected task's log.
    React.useEffect(() => {
      setLogState({ lines: [], nextOffset: 0, total: 0, droppedLines: 0, clipped: false })
      setLogError(null)
      setConfirmCancel('')
      logOffsetRef.current = 0
      if (!selectedId) {
        setLogPhase('idle')
        return undefined
      }
      setLogPhase('loading')
      let cancelled = false
      const load = async () => {
        try {
          const data = await api.tasks.logs(selectedId, { offset: 0, limit: 200 })
          if (cancelled || !mountedRef.current) return
          const lines = data && Array.isArray(data.lines) ? data.lines : []
          const nextOffset = data && Number.isInteger(data.nextOffset) ? data.nextOffset : null
          logOffsetRef.current = nextOffset === null ? Number(data && data.offset) || 0 : nextOffset
          setLogState({
            lines,
            nextOffset: logOffsetRef.current,
            total: data && Number.isFinite(Number(data.total)) ? Number(data.total) : lines.length,
            droppedLines: data && Number.isFinite(Number(data.droppedLines)) ? Number(data.droppedLines) : 0,
            clipped: Boolean(data && data.clipped),
          })
          setLogPhase('streaming')
        } catch (error) {
          if (cancelled || !mountedRef.current) return
          const apiError = toApiError(error)
          if (apiError.code === 'ABORTED') return
          setLogError(apiError)
          setLogPhase('error')
        }
      }
      void load()
      return () => {
        cancelled = true
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [selectedId, logReload])

    // Incrementally poll the log while the selected task is active.
    const selectedRunning = Boolean(
      selectedTask && (selectedTask.status === 'running' || selectedTask.status === 'queued'),
    )
    React.useEffect(() => {
      if (!selectedId || !selectedRunning) return undefined
      let cancelled = false
      let inFlight = false
      const tick = async () => {
        if (inFlight) return
        inFlight = true
        try {
          const offset = logOffsetRef.current
          const data = await api.tasks.logs(selectedId, { offset, limit: 200 })
          if (cancelled || !mountedRef.current) return
          const lines = data && Array.isArray(data.lines) ? data.lines : []
          const nextOffset = data && Number.isInteger(data.nextOffset) ? data.nextOffset : null
          const clipped = Boolean(data && data.clipped)
          if (nextOffset !== null) logOffsetRef.current = nextOffset
          setLogError(null)
          setLogPhase('streaming')
          setLogState((current) => {
            const nextLines = clipped ? lines : current.lines.concat(lines)
            return {
              lines: nextLines,
              nextOffset: logOffsetRef.current,
              total: data && Number.isFinite(Number(data.total)) ? Number(data.total) : nextLines.length,
              droppedLines: data && Number.isFinite(Number(data.droppedLines)) ? Number(data.droppedLines) : current.droppedLines,
              clipped: clipped || current.clipped,
            }
          })
        } catch (error) {
          if (cancelled || !mountedRef.current) return
          const apiError = toApiError(error)
          if (apiError.code === 'ABORTED') return
          if (apiError.code !== 'TASK_NOT_FOUND') setLogError(apiError)
        } finally {
          inFlight = false
        }
      }
      const timer = window.setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        void tick()
      }, 1200)
      return () => {
        cancelled = true
        window.clearInterval(timer)
      }
    }, [selectedId, selectedRunning])

    // Auto-scroll the log viewer.
    React.useEffect(() => {
      if (!autoScroll) return
      const box = logBoxRef.current
      if (box) box.scrollTop = box.scrollHeight
    }, [logState.lines, autoScroll])

    // Re-render once a second so elapsed times advance.
    const [, setClockTick] = React.useState(0)
    React.useEffect(() => {
      const timer = window.setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        setClockTick((value) => value + 1)
      }, 1000)
      return () => window.clearInterval(timer)
    }, [])

    async function startTask() {
      let params = {}
      const text = paramsText.trim()
      if (text) {
        try {
          const parsed = JSON.parse(text)
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            setParamsError(t('tasks.paramsInvalid', null, 'Parameters must be a JSON object.'))
            return
          }
          params = parsed
        } catch (_error) {
          setParamsError(t('tasks.paramsInvalid', null, 'Parameters must be a JSON object.'))
          return
        }
      }
      setParamsError('')
      setBusy('start')
      setActionError(null)
      try {
        const result = await api.tasks.start(kind, params)
        if (!mountedRef.current) return
        const task = result && result.task ? result.task : result
        if (task && task.id !== undefined) setSelectedId(String(task.id))
        showNotice(t('tasks.started', null, 'Task started.'))
        await refreshList()
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    async function cancelTask(taskId) {
      setBusy(`cancel:${taskId}`)
      setActionError(null)
      try {
        await api.tasks.cancel(taskId)
        if (!mountedRef.current) return
        setConfirmCancel('')
        showNotice(t('tasks.cancelled', null, 'Task cancelled.'))
        await refreshList()
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    const cancelledCount = counts && counts.cancelled ? Number(counts.cancelled) : 0
    const countsText = counts
      ? t(
          'tasks.counts',
          {
            queued: counts.queued || 0,
            running: counts.running || 0,
            succeeded: counts.succeeded || 0,
            failed: counts.failed || 0,
            cancelled: cancelledCount,
          },
          `${counts.queued || 0} queued · ${counts.running || 0} running · ${counts.succeeded || 0} succeeded · ${counts.failed || 0} failed · ${cancelledCount} cancelled`,
        )
      : ''

    const backendNotRunning = backendState && backendState.mode !== 'unconfigured' && backendState.state !== 'running'
    const backendUnconfigured = backendState && backendState.mode === 'unconfigured'

    const startCard = h(
      ui.Card,
      { title: t('tasks.startTitle', null, 'Start a new task') },
      h(
        'div',
        { className: 'ca-tasks-start' },
        backendNotRunning || backendUnconfigured
          ? h(
              'div',
              { className: 'ca-tasks-notice' },
              h(ui.Icon, { name: 'warning', size: 13 }),
              t('tasks.backendNotRunning', null, 'The CA backend is not running; start it on the Backend page first.'),
            )
          : null,
        h(
          'div',
          { className: 'ca-tasks-start-grid' },
          h(
            ui.Field,
            {
              key: 'kind',
              label: t('tasks.kind', null, 'Kind'),
              hint: t(
                TASK_KINDS.find((entry) => entry.value === kind)?.hintKey || 'tasks.kind.indexHint',
                null,
                TASK_KINDS.find((entry) => entry.value === kind)?.hintFallback || '',
              ),
            },
            h(ui.Select, {
              value: kind,
              options: TASK_KINDS.map((entry) => ({
                value: entry.value,
                label: t(`tasks.kind.${entry.value}`, null, entry.value),
              })),
              ariaLabel: t('tasks.kind', null, 'Kind'),
              onChange: (value) => setKind(value),
            }),
          ),
          h(
            ui.Field,
            {
              key: 'params',
              label: t('tasks.params', null, 'Parameters (JSON object)'),
              hint: t('tasks.paramsHint', null, 'Optional arguments passed to the CA CLI command.'),
              error: paramsError,
            },
            h(ui.TextArea, {
              value: paramsText,
              rows: 3,
              placeholder: t(
                'tasks.paramsPlaceholder',
                null,
                '{"repo": "/src/linux", "compdb": "/src/linux/compile_commands.json"}',
              ),
              ariaLabel: t('tasks.params', null, 'Parameters (JSON object)'),
              onChange: (event) => {
                setParamsText(event.target.value)
                if (paramsError) setParamsError('')
              },
            }),
          ),
        ),
        h(
          'div',
          { className: 'ca-row' },
          h(
            ui.Button,
            { variant: 'primary', disabled: busy === 'start', onClick: startTask },
            busy === 'start' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'play', size: 13 }),
            busy === 'start' ? t('tasks.starting', null, 'Starting…') : t('tasks.start', null, 'Start task'),
          ),
          countsText ? h('span', { className: 'ca-tasks-counts' }, countsText) : null,
          h('span', { className: 'ca-spacer' }),
          h(
            ui.Button,
            {
              variant: 'ghost',
              size: 'sm',
              disabled: busy === 'refresh',
              onClick: () => {
                setBusy('refresh')
                void refreshList().finally(() => {
                  if (mountedRef.current) setBusy('')
                })
              },
            },
            h(ui.Icon, { name: 'refresh', size: 13 }),
            t('tasks.refresh', null, 'Refresh'),
          ),
        ),
      ),
    )

    const columns = [
      {
        key: 'status',
        label: t('tasks.status', null, 'Status'),
        width: '96px',
        render: (task) => h(ui.Badge, { tone: statusTone(task.status) }, t(`tasks.status.${task.status}`, null, String(task.status || ''))),
      },
      {
        key: 'kind',
        label: t('tasks.kind', null, 'Kind'),
        render: (task) => t(`tasks.kind.${task.kind}`, null, task.kind || ''),
      },
      {
        key: 'createdAt',
        label: t('tasks.created', null, 'Created'),
        render: (task) => h('span', { className: 'ca-mono' }, formatTimestamp(task.createdAt)),
      },
      {
        key: 'elapsed',
        label: t('tasks.duration', null, 'Duration'),
        align: 'right',
        width: '90px',
        render: (task) => h('span', { className: 'ca-mono' }, taskElapsed(task)),
      },
      {
        key: 'id',
        label: 'ID',
        width: '92px',
        render: (task) => h('span', { className: 'ca-tasks-id', title: String(task.id) }, String(task.id).slice(0, 10)),
      },
    ]

    const listCard = h(
      ui.Card,
      {
        title: t('tasks.list', null, 'Task list'),
        className: 'ca-tasks-list',
        actions: h(ui.Badge, { tone: 'neutral' }, String(tasks.length)),
      },
      tasks.length === 0
        ? h(ui.EmptyState, {
            icon: h(ui.Icon, { name: 'tasks', size: 24 }),
            title: t('tasks.empty', null, 'No tasks in this session.'),
            body: t('tasks.emptyHint', null, 'Start an indexing or update job above; it runs through the CA backend.'),
          })
        : h(ui.Table, {
            columns,
            rows: tasks,
            rowKey: (task) => String(task.id),
            selectedKey: selectedId,
            onRowClick: (task) => setSelectedId(String(task.id)),
            empty: t('tasks.empty', null, 'No tasks in this session.'),
          }),
    )

    const logLines = logState.lines
    const logBody =
      logPhase === 'error'
        ? h(ui.ErrorBox, {
            error: logError,
            retryLabel: t('common.retry', null, 'Retry'),
            onRetry: () => {
              setLogError(null)
              setLogPhase('idle')
              setLogState({ lines: [], nextOffset: 0, total: 0, droppedLines: 0, clipped: false })
              setLogReload((value) => value + 1)
            },
          })
        : h(
            'div',
            { className: 'ca-tasks-log', ref: logBoxRef },
            logLines.length === 0
              ? h(
                  'div',
                  { className: 'ca-tasks-log-empty' },
                  logPhase === 'loading'
                    ? t('common.loading', null, 'loading…')
                    : t('tasks.noLogs', null, 'No log output yet.'),
                )
              : h(
                  'div',
                  { className: 'ca-tasks-log-body' },
                  logLines.map((line, index) => {
                    const stream = line && line.stream === 'stderr' ? 'stderr' : 'stdout'
                    return h(
                      'div',
                      {
                        key: `${line && line.n !== undefined ? line.n : index}-${index}`,
                        className: `ca-tasks-log-line${stream === 'stderr' ? ' ca-tasks-log-stderr' : ''}`,
                      },
                      h('div', { className: 'ca-tasks-log-num' }, String(line && line.n !== undefined ? line.n : index + 1)),
                      h(
                        'div',
                        { className: 'ca-tasks-log-text' },
                        line && line.text !== undefined && line.text !== null ? String(line.text) : '',
                      ),
                    )
                  }),
                ),
          )

    const detailCard = h(
      ui.Card,
      {
        title: t('tasks.detail', null, 'Task detail'),
        actions: selectedTask
          ? [
              selectedTask.status === 'running' || selectedTask.status === 'queued'
                ? h(
                    ui.Button,
                    {
                      key: 'cancel',
                      variant: 'danger',
                      size: 'sm',
                      disabled: busy === `cancel:${selectedTask.id}`,
                      onClick: () => {
                        if (confirmCancel === String(selectedTask.id)) void cancelTask(selectedTask.id)
                        else setConfirmCancel(String(selectedTask.id))
                      },
                    },
                    busy === `cancel:${selectedTask.id}` ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'stop', size: 12 }),
                    confirmCancel === String(selectedTask.id)
                      ? t('tasks.cancel', null, 'Cancel task')
                      : busy === `cancel:${selectedTask.id}`
                        ? t('tasks.cancelling', null, 'Cancelling…')
                        : t('tasks.cancel', null, 'Cancel task'),
                  )
                : null,
            ].map((node) => node)
          : null,
      },
      selectedTask
        ? h(
            'div',
            { className: 'ca-col' },
            h(
              'div',
              { className: 'ca-tasks-detail-head' },
              h(ui.Badge, { tone: statusTone(selectedTask.status) }, t(`tasks.status.${selectedTask.status}`, null, String(selectedTask.status || ''))),
              h('strong', null, t(`tasks.kind.${selectedTask.kind}`, null, selectedTask.kind || '')),
              h('span', { className: 'ca-tasks-id' }, String(selectedTask.id)),
              h('span', { className: 'ca-spacer' }),
              h('span', { className: 'ca-tasks-id' }, taskElapsed(selectedTask)),
            ),
            h(ui.KV, {
              rows: [
                { key: 'created', label: t('tasks.created', null, 'Created'), value: formatTimestamp(selectedTask.createdAt) },
                { key: 'started', label: t('tasks.startedAt', null, 'Started'), value: formatTimestamp(selectedTask.startedAt) },
                { key: 'finished', label: t('tasks.finishedAt', null, 'Finished'), value: formatTimestamp(selectedTask.finishedAt) },
                { key: 'exit', label: t('tasks.exitCode', null, 'Exit code'), value: selectedTask.exitCode === undefined || selectedTask.exitCode === null ? '—' : String(selectedTask.exitCode) },
                { key: 'logBytes', label: t('tasks.logBytes', null, 'Log size'), value: formatBytes(selectedTask.logBytes) },
              ],
            }),
            selectedTask.error
              ? h(ui.ErrorBox, {
                  title: t('tasks.status.failed', null, 'failed'),
                  error: selectedTask.error,
                })
              : null,
            Object.keys(selectedTask.params || {}).length > 0
              ? h(ui.Pre, { className: 'ca-tasks-params' }, JSON.stringify(selectedTask.params, null, 2))
              : null,
            h('div', { className: 'ca-hr' }),
            h(
              'div',
              { className: 'ca-tasks-log-toolbar' },
              h('strong', { style: { fontSize: 12.5 } }, t('tasks.logs', null, 'Log')),
              h(ui.Badge, { tone: 'neutral' }, String(logState.total)),
              logState.droppedLines > 0
                ? h(ui.Badge, { tone: 'warn' }, t('tasks.droppedLines', { count: logState.droppedLines }, `${logState.droppedLines} earlier lines dropped.`))
                : null,
              h('span', { className: 'ca-spacer' }),
              h(
                'label',
                { className: 'ca-tasks-check' },
                h('input', {
                  type: 'checkbox',
                  checked: autoScroll,
                  onChange: (event) => setAutoScroll(event.target.checked),
                }),
                t('tasks.autoScroll', null, 'Auto-scroll'),
              ),
            ),
            logBody,
          )
        : h(ui.EmptyState, {
            icon: h(ui.Icon, { name: 'tasks', size: 24 }),
            title: t('tasks.selectHint', null, 'Select a task to inspect its log and parameters.'),
          }),
    )

    if (phase === 'loading') {
      return h(
        'div',
        { className: 'ca-page ca-tasks-page' },
        h('div', { className: 'ca-tasks-inner' }, h(ui.Spinner, { size: 18, label: t('common.loading', null, 'loading…') })),
      )
    }

    return h(
      'div',
      { className: 'ca-page ca-tasks-page' },
      h(
        'div',
        { className: 'ca-tasks-inner' },
        h(
          'div',
          { className: 'ca-tasks-head' },
          h('h1', { className: 'ca-tasks-title' }, t('tasks.title', null, 'Tasks')),
          h(
            'p',
            { className: 'ca-tasks-sub' },
            t('tasks.subtitle', null, 'Long-running CA jobs: indexing, updates, deep facts and callback resolution.'),
          ),
          h('span', { className: 'ca-spacer' }),
          h('span', { className: 'ca-tasks-counts' }, t('tasks.concurrency', null, 'At most 2 tasks run at the same time.')),
        ),
        phase === 'error' && loadError
          ? h(ui.ErrorBox, {
              title: t('common.error', null, 'Error'),
              error: loadError,
              retryLabel: t('common.retry', null, 'Retry'),
              onRetry: () => {
                setPhase('loading')
                void refreshList()
              },
            })
          : null,
        notice ? h('div', { className: 'ca-tasks-notice' }, h(ui.Icon, { name: 'check', size: 13 }), notice) : null,
        actionError
          ? h(ui.ErrorBox, {
              title: t('common.error', null, 'Error'),
              error: actionError,
              retryLabel: t('common.close', null, 'Close'),
              onRetry: () => setActionError(null),
            })
          : null,
        startCard,
        h('div', { className: 'ca-tasks-columns' }, listCard, detailCard),
      ),
    )
  }

  TasksPage.displayName = 'CaTasksPage'
  return TasksPage
}

export default createTasksPage
