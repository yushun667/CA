/**
 * CA backend settings page (`#/backend`).
 *
 * Shows the RPC `status`/`backend.get` snapshot for the CA backend managed by
 * the Host half, exposes start/stop/restart, local or remote probe, remote
 * installation (a long-running Host action polled through
 * `backend.installStatus`), a CA `/api/health` check, and the editable backend
 * configuration (local database or remote SSH target).
 *
 * The config shape mirrors dsh/lib/host/config.js:
 *   { backend: 'auto'|'local'|'remote', autoStart,
 *     local: { repoRoot, python, db, dbDir, host, port, ... },
 *     remote: { ssh: { host, user, port, keyPath }, repoPath, installDir,
 *               db, dbDir, serveRemotePort, localPort, ... } }
 */

import {
  formatBytes,
  formatDuration,
  formatTimestamp,
  toApiError,
} from '../api.js'

const BACKEND_CSS = `
.ca-backend-page { gap: 14px; overflow: auto; padding: 18px; }
.ca-backend-inner { width: 100%; max-width: 1080px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
.ca-backend-head { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.ca-backend-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
.ca-backend-sub { margin: 2px 0 0; font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-backend-status-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
.ca-backend-stat { display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; border: 1px solid var(--dsw-alias-border-l1, #eaeef2); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-bg-layer-2, rgba(127, 127, 127, 0.04)); min-width: 0; }
.ca-backend-stat-label { font-size: 11px; color: var(--dsw-alias-label-tertiary, #8b949e); text-transform: uppercase; letter-spacing: 0.03em; }
.ca-backend-stat-value { font-size: 12.5px; overflow-wrap: anywhere; }
.ca-backend-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ca-backend-form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px 14px; }
.ca-backend-group { display: flex; flex-direction: column; gap: 10px; }
.ca-backend-group-title { display: flex; align-items: center; gap: 8px; margin: 4px 0 0; padding-bottom: 4px; border-bottom: 1px solid var(--dsw-alias-border-l1, #eaeef2); font-size: 12.5px; font-weight: 600; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-backend-check { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--dsw-alias-label-primary, #1f2328); cursor: pointer; user-select: none; }
.ca-backend-check input { width: 14px; height: 14px; accent-color: var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-backend-notice { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-bg-layer-2, rgba(127, 127, 127, 0.05)); font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-backend-report { max-height: 320px; }
`

const MODE_OPTIONS = [
  { value: 'auto', label: 'auto' },
  { value: 'local', label: 'local' },
  { value: 'remote', label: 'remote (SSH)' },
]

function asText(value) {
  return value === undefined || value === null ? '' : String(value)
}

function nullIfEmpty(value) {
  const trimmed = String(value === undefined || value === null ? '' : value).trim()
  return trimmed.length ? trimmed : null
}

function intOrNull(value, field, { min = 0, max = 65535 } = {}) {
  const text = String(value === undefined || value === null ? '' : value).trim()
  if (!text) return { ok: true, value: null }
  const number = Number(text)
  if (!Number.isInteger(number) || number < min || number > max) {
    return { ok: false, message: `${field} must be an integer between ${min} and ${max}` }
  }
  return { ok: true, value: number }
}

function configToForm(config) {
  const source = config && typeof config === 'object' ? config : {}
  const local = source.local && typeof source.local === 'object' ? source.local : {}
  const remote = source.remote && typeof source.remote === 'object' ? source.remote : {}
  const ssh = remote.ssh && typeof remote.ssh === 'object' ? remote.ssh : {}
  return {
    backend: ['auto', 'local', 'remote'].includes(source.backend) ? source.backend : 'auto',
    autoStart: source.autoStart !== false,
    local_repoRoot: asText(local.repoRoot),
    local_python: asText(local.python),
    local_db: asText(local.db),
    local_dbDir: asText(local.dbDir),
    local_host: asText(local.host) || '127.0.0.1',
    local_port: asText(local.port === undefined || local.port === null ? '' : local.port),
    remote_sshHost: asText(ssh.host),
    remote_sshUser: asText(ssh.user),
    remote_sshPort: asText(ssh.port === undefined || ssh.port === null ? '' : ssh.port),
    remote_sshKeyPath: asText(ssh.keyPath),
    remote_repoPath: asText(remote.repoPath),
    remote_installDir: asText(remote.installDir),
    remote_db: asText(remote.db),
    remote_dbDir: asText(remote.dbDir),
    remote_serveRemotePort: asText(
      remote.serveRemotePort === undefined || remote.serveRemotePort === null ? '' : remote.serveRemotePort,
    ),
    remote_localPort: asText(
      remote.localPort === undefined || remote.localPort === null ? '' : remote.localPort,
    ),
  }
}

function formToPatch(form) {
  const errors = []
  const localPort = intOrNull(form.local_port, 'local.port')
  if (!localPort.ok) errors.push(localPort.message)
  const sshPort = intOrNull(form.remote_sshPort, 'remote.ssh.port', { min: 1, max: 65535 })
  if (!sshPort.ok) errors.push(sshPort.message)
  const remoteServePort = intOrNull(form.remote_serveRemotePort, 'remote.serveRemotePort')
  if (!remoteServePort.ok) errors.push(remoteServePort.message)
  const remoteLocalPort = intOrNull(form.remote_localPort, 'remote.localPort')
  if (!remoteLocalPort.ok) errors.push(remoteLocalPort.message)
  if (errors.length > 0) return { ok: false, errors }
  const patch = {
    backend: form.backend,
    autoStart: Boolean(form.autoStart),
    local: {
      repoRoot: nullIfEmpty(form.local_repoRoot),
      python: nullIfEmpty(form.local_python),
      db: nullIfEmpty(form.local_db),
      dbDir: nullIfEmpty(form.local_dbDir),
      host: nullIfEmpty(form.local_host) || '127.0.0.1',
      port: localPort.value === null ? 0 : localPort.value,
    },
    remote: {
      ssh: {
        host: nullIfEmpty(form.remote_sshHost),
        user: nullIfEmpty(form.remote_sshUser),
        port: sshPort.value,
        keyPath: nullIfEmpty(form.remote_sshKeyPath),
      },
      repoPath: nullIfEmpty(form.remote_repoPath),
      installDir: nullIfEmpty(form.remote_installDir),
      db: nullIfEmpty(form.remote_db),
      dbDir: nullIfEmpty(form.remote_dbDir),
      serveRemotePort: remoteServePort.value === null ? 0 : remoteServePort.value,
      localPort: remoteLocalPort.value === null ? 0 : remoteLocalPort.value,
    },
  }
  return { ok: true, patch }
}

function backendStateTone(backend) {
  if (!backend || typeof backend !== 'object') return 'neutral'
  if (backend.state === 'running') return 'success'
  if (backend.state === 'starting') return 'warn'
  if (backend.state === 'error') return 'error'
  return 'muted'
}

/**
 * Create the backend settings page component.
 *
 * @param {object} React React namespace.
 * @param {{ ui: object, api: object, i18n: object }} bag
 */
export function createBackendPage(React, bag) {
  const { ui, api, i18n } = bag || {}
  if (!ui || !api || !i18n) throw new TypeError('createBackendPage(React, { ui, api, i18n })')
  const h = React.createElement
  const t = (key, vars, fallback) => i18n.t(key, vars, fallback)

  function formatActionError(error) {
    const apiError = toApiError(error)
    return apiError
  }

  function BackendPage(props) {
    ui.useStyles(BACKEND_CSS)

    const navigate = props && typeof props.navigate === 'function' ? props.navigate : null

    const [phase, setPhase] = React.useState('loading')
    const [loadError, setLoadError] = React.useState(null)
    const [status, setStatus] = React.useState(null)
    const [config, setConfig] = React.useState(null)
    const [form, setForm] = React.useState(() => configToForm(null))
    const [busy, setBusy] = React.useState('')
    const [actionError, setActionError] = React.useState(null)
    const [notice, setNotice] = React.useState('')
    const [probe, setProbe] = React.useState(null)
    const [probeError, setProbeError] = React.useState(null)
    const [health, setHealth] = React.useState(null)
    const [healthError, setHealthError] = React.useState(null)
    const [install, setInstall] = React.useState(null)

    const mountedRef = React.useRef(true)
    const noticeTimer = React.useRef(null)

    React.useEffect(
      () => () => {
        mountedRef.current = false
        if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current)
      },
      [],
    )

    function showNotice(message) {
      setNotice(message)
      if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current)
      noticeTimer.current = window.setTimeout(() => {
        noticeTimer.current = null
        if (mountedRef.current) setNotice('')
      }, 4500)
    }

    function applySnapshot(snapshot) {
      if (!snapshot || typeof snapshot !== 'object') return
      if (snapshot.status) setStatus(snapshot.status)
      if (snapshot.config) {
        setConfig(snapshot.config)
        setForm(configToForm(snapshot.config))
      }
    }

    const load = React.useCallback(async () => {
      setPhase('loading')
      setLoadError(null)
      try {
        const snapshot = await api.backend.get()
        if (!mountedRef.current) return
        applySnapshot(snapshot)
        setPhase('ready')
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setLoadError(apiError)
        setPhase('error')
      }
    }, [])

    async function refreshStatus() {
      try {
        const snapshot = await api.backend.get()
        if (!mountedRef.current) return
        applySnapshot(snapshot)
        if (phase === 'error') setPhase('ready')
      } catch (error) {
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      }
    }

    async function checkHealth() {
      setBusy('health')
      setHealthError(null)
      try {
        const data = await api.caGet('/api/health')
        if (!mountedRef.current) return
        setHealth(data || null)
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = formatActionError(error)
        if (apiError.code === 'ABORTED') return
        setHealth(null)
        setHealthError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    React.useEffect(() => {
      void load()
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // Refresh the status snapshot while this page is visible.
    React.useEffect(() => {
      const timer = window.setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        void refreshStatus()
      }, 4000)
      return () => window.clearInterval(timer)
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // Poll the remote installation state while one is running.
    const installRunning = Boolean(install && install.status === 'running')
    React.useEffect(() => {
      if (!installRunning) return undefined
      let cancelled = false
      const tick = async () => {
        try {
          const state = await api.backend.installStatus()
          if (cancelled || !mountedRef.current) return
          setInstall(state || null)
          if (state && state.status !== 'running') {
            showNotice(
              state.status === 'succeeded'
                ? t('backend.installSucceeded', null, 'Installation finished.')
                : t('backend.installFailed', null, 'Installation failed.'),
            )
            void refreshStatus()
          }
        } catch (error) {
          if (cancelled || !mountedRef.current) return
          const apiError = toApiError(error)
          if (apiError.code === 'ABORTED') return
          setActionError(apiError)
          setInstall((current) => (current ? Object.assign({}, current, { status: 'failed' }) : current))
        }
      }
      void tick()
      const timer = window.setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        void tick()
      }, 1500)
      return () => {
        cancelled = true
        window.clearInterval(timer)
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [installRunning])

    // Health probe follows a running backend.
    React.useEffect(() => {
      if (status && status.state === 'running' && !health && !busy) {
        void checkHealth()
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [status && status.state])

    async function runAction(kind, action) {
      setBusy(kind)
      setActionError(null)
      try {
        const result = await action()
        if (!mountedRef.current) return
        if (result && typeof result === 'object') {
          if (result.status) setStatus(result.status)
          if (result.config) {
            setConfig(result.config)
            setForm(configToForm(result.config))
          }
        }
        if (kind === 'start') showNotice(t('backend.started', null, 'Backend started'))
        else if (kind === 'stop') showNotice(t('backend.stopped', null, 'Backend stopped'))
        else if (kind === 'restart') showNotice(t('backend.restarted', null, 'Backend restarted'))
        else if (kind === 'save') showNotice(t('backend.config.saved', null, 'Configuration saved.'))
        await refreshStatus()
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = formatActionError(error)
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    function saveConfig() {
      const built = formToPatch(form)
      if (!built.ok) {
        setActionError(toApiError({ code: 'INVALID_ARGUMENT', message: built.errors.join('; ') }))
        return
      }
      void runAction('save', () => api.backend.configure(built.patch))
    }

    function startBackend() {
      const mode = form.backend === 'auto' ? '' : form.backend
      if (mode === 'local' || (form.backend === 'auto' && (form.local_db || form.local_dbDir))) {
        if (!form.local_db && !form.local_dbDir) {
          setActionError(
            toApiError({ code: 'CONFIG_INVALID', message: t('backend.config.missingDb', null, 'Set a local database (or shard directory) before starting the backend.') }),
          )
          return
        }
      }
      if (mode === 'remote' && !form.remote_sshHost) {
        setActionError(
          toApiError({ code: 'CONFIG_INVALID', message: t('backend.config.missingSsh', null, 'Set the remote SSH host and database paths before installing or starting.') }),
        )
        return
      }
      void runAction('start', () => api.backend.start())
    }

    function probeBackend(target) {
      const payload = { target }
      if (target === 'remote') {
        payload.ssh = {
          host: nullIfEmpty(form.remote_sshHost),
          user: nullIfEmpty(form.remote_sshUser),
          port: intOrNull(form.remote_sshPort, 'remote.ssh.port', { min: 1, max: 65535 }).value,
          keyPath: nullIfEmpty(form.remote_sshKeyPath),
        }
      }
      setBusy('probe')
      setProbeError(null)
      api.backend
        .probe(payload)
        .then((report) => {
          if (!mountedRef.current) return
          setProbe(report || null)
        })
        .catch((error) => {
          if (!mountedRef.current) return
          const apiError = toApiError(error)
          if (apiError.code === 'ABORTED') return
          setProbeError(apiError)
        })
        .finally(() => {
          if (mountedRef.current) setBusy('')
        })
    }

    function installRemote() {
      if (!form.remote_sshHost) {
        setActionError(
          toApiError({ code: 'CONFIG_INVALID', message: t('backend.config.missingSsh', null, 'Set the remote SSH host and database paths before installing or starting.') }),
        )
        return
      }
      setBusy('install')
      setActionError(null)
      api.backend
        .install({
          ssh: {
            host: nullIfEmpty(form.remote_sshHost),
            user: nullIfEmpty(form.remote_sshUser),
            port: intOrNull(form.remote_sshPort, 'remote.ssh.port', { min: 1, max: 65535 }).value,
            keyPath: nullIfEmpty(form.remote_sshKeyPath),
          },
          opts: {
            repoPath: nullIfEmpty(form.remote_repoPath),
            installDir: nullIfEmpty(form.remote_installDir),
          },
        })
        .then((result) => {
          if (!mountedRef.current) return
          setInstall({
            started: true,
            status: 'running',
            startedAt: Date.now(),
            ...(result && typeof result === 'object' ? result : {}),
          })
          showNotice(t('backend.installRunning', null, 'Installing CA on the remote host. This can take several minutes.'))
        })
        .catch((error) => {
          if (!mountedRef.current) return
          const apiError = toApiError(error)
          if (apiError.code === 'ABORTED') return
          setActionError(apiError)
        })
        .finally(() => {
          if (mountedRef.current) setBusy('')
        })
    }

    // -- derived ----------------------------------------------------------- //
    const backend = status || {}
    const effectiveMode = backend.mode || 'unconfigured'
    const running = backend.state === 'running'
    const starting = backend.state === 'starting'
    const anyBusy = busy !== ''
    const isRemoteForm = form.backend === 'remote'

    function setField(name, value) {
      setForm((current) => Object.assign({}, current, { [name]: value }))
    }

    if (phase === 'loading' && !status) {
      return h(
        'div',
        { className: 'ca-page ca-backend-page' },
        h(
          'div',
          { className: 'ca-backend-inner' },
          h(ui.Spinner, { size: 18, label: t('common.loading', null, 'loading…') }),
        ),
      )
    }

    if (phase === 'error') {
      return h(
        'div',
        { className: 'ca-page ca-backend-page' },
        h(
          'div',
          { className: 'ca-backend-inner' },
          h(ui.ErrorBox, {
            title: t('backend.title', null, 'CA backend'),
            error: loadError,
            hint: t('error.rpc', null, 'The CA plugin host is not reachable.'),
            retryLabel: t('common.retry', null, 'Retry'),
            onRetry: () => {
              void load()
            },
          }),
        ),
      )
    }

    const statusCard = h(
      ui.Card,
      {
        title: t('backend.title', null, 'CA backend'),
        subtitle: t('backend.subtitle', null, 'Manage the local or remote CA analysis backend used by this plugin.'),
        actions: [
          h(
            ui.Button,
            {
              key: 'refresh',
              variant: 'ghost',
              size: 'sm',
              disabled: anyBusy,
              onClick: () => {
                void refreshStatus()
              },
            },
            h(ui.Icon, { name: 'refresh', size: 13 }),
            t('backend.refresh', null, 'Refresh status'),
          ),
        ].map((node) => node),
      },
      h(
        'div',
        { className: 'ca-col' },
        h(
          'div',
          { className: 'ca-backend-status-grid' },
          h(
            'div',
            { className: 'ca-backend-stat' },
            h('span', { className: 'ca-backend-stat-label' }, t('backend.state', null, 'State')),
            h(
              'span',
              { className: 'ca-row' },
              h(ui.Badge, { tone: backendStateTone(backend) }, String(backend.state || 'unknown')),
              running && backend.version ? h(ui.Badge, { tone: 'neutral' }, `v${backend.version}`) : null,
            ),
          ),
          h(
            'div',
            { className: 'ca-backend-stat' },
            h('span', { className: 'ca-backend-stat-label' }, t('backend.mode', null, 'Mode')),
            h('span', { className: 'ca-backend-stat-value' }, String(effectiveMode)),
          ),
          h(
            'div',
            { className: 'ca-backend-stat' },
            h('span', { className: 'ca-backend-stat-label' }, t('backend.baseUrl', null, 'Base URL')),
            h('span', { className: 'ca-backend-stat-value ca-mono' }, backend.baseUrl || '—'),
          ),
          h(
            'div',
            { className: 'ca-backend-stat' },
            h('span', { className: 'ca-backend-stat-label' }, t('backend.db', null, 'Database')),
            h('span', { className: 'ca-backend-stat-value ca-mono' }, backend.db || backend.dbDir || '—'),
          ),
          h(
            'div',
            { className: 'ca-backend-stat' },
            h('span', { className: 'ca-backend-stat-label' }, t('backend.since', null, 'Since')),
            h('span', { className: 'ca-backend-stat-value' }, formatTimestamp(backend.since)),
          ),
          h(
            'div',
            { className: 'ca-backend-stat' },
            h('span', { className: 'ca-backend-stat-label' }, t('backend.health', null, 'Health')),
            h(
              'span',
              { className: 'ca-backend-stat-value' },
              health ? t('backend.health.ok', null, 'healthy') : healthError ? t('backend.health.failed', null, 'unhealthy') : '—',
            ),
          ),
        ),
        backend.error
          ? h(ui.ErrorBox, {
              title: t('backend.lastError', null, 'Last error'),
              error: backend.error,
            })
          : null,
        h(
          'div',
          { className: 'ca-backend-actions' },
          h(
            ui.Button,
            {
              variant: 'primary',
              disabled: anyBusy || running || starting,
              onClick: startBackend,
            },
            busy === 'start' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'play', size: 13 }),
            busy === 'start' ? t('backend.starting', null, 'Starting…') : t('backend.start', null, 'Start'),
          ),
          h(
            ui.Button,
            {
              variant: 'secondary',
              disabled: anyBusy || (!running && !starting),
              onClick: () => {
                void runAction('stop', () => api.backend.stop())
              },
            },
            busy === 'stop' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'stop', size: 13 }),
            busy === 'stop' ? t('backend.stopping', null, 'Stopping…') : t('backend.stop', null, 'Stop'),
          ),
          h(
            ui.Button,
            {
              variant: 'secondary',
              disabled: anyBusy,
              onClick: () => {
                void runAction('restart', () => api.backend.restart())
              },
            },
            busy === 'restart' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'refresh', size: 13 }),
            busy === 'restart' ? t('backend.restarting', null, 'Restarting…') : t('backend.restart', null, 'Restart'),
          ),
          h(
            ui.Button,
            {
              variant: 'ghost',
              disabled: anyBusy,
              onClick: () => probeBackend(isRemoteForm ? 'remote' : 'local'),
            },
            busy === 'probe' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'check', size: 13 }),
            isRemoteForm
              ? t('backend.probeRemote', null, 'Probe remote host')
              : t('backend.probe', null, 'Probe local environment'),
          ),
          isRemoteForm
            ? h(
                ui.Button,
                {
                  variant: 'secondary',
                  disabled: anyBusy || installRunning,
                  onClick: installRemote,
                },
                busy === 'install' || installRunning
                  ? h(ui.Spinner, { size: 12 })
                  : h(ui.Icon, { name: 'external', size: 13 }),
                installRunning ? t('backend.installing', null, 'Installing…') : t('backend.install', null, 'Install on remote host'),
              )
            : null,
          h(
            ui.Button,
            { variant: 'ghost', disabled: anyBusy || !running, onClick: () => void checkHealth() },
            busy === 'health' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'info', size: 13 }),
            t('backend.health.check', null, 'Check health'),
          ),
        ),
        notice ? h('div', { className: 'ca-backend-notice' }, h(ui.Icon, { name: 'check', size: 13 }), notice) : null,
        actionError
          ? h(ui.ErrorBox, {
              title: t('common.error', null, 'Error'),
              error: actionError,
              retryLabel: t('common.close', null, 'Close'),
              onRetry: () => setActionError(null),
            })
          : null,
      ),
    )

    const projectHintCard = h(
      ui.Card,
      {
        title: t('backend.projectHintTitle', null, 'Manage indexes as projects'),
        subtitle: t(
          'backend.projectHint',
          null,
          'Recommended: create and switch projects on the Projects page — a project binds a source tree, a database and a backend target. This page keeps the low-level backend status and configuration.',
        ),
        actions: navigate
          ? h(
              ui.Button,
              { variant: 'primary', size: 'sm', onClick: () => navigate({ page: 'projects' }) },
              h(ui.Icon, { name: 'folder', size: 13 }),
              t('backend.openProjects', null, 'Open projects'),
            )
          : null,
      },
      h(
        'div',
        { className: 'ca-backend-notice' },
        h(ui.Icon, { name: 'info', size: 13 }),
        h(
          'span',
          null,
          t(
            'backend.projectHintLowLevel',
            null,
            'The controls below are low-level: they edit the backend configuration directly and are not tied to a saved project.',
          ),
        ),
      ),
    )

    const healthCard = health || healthError
      ? h(
          ui.Card,
          {
            title: t('backend.health', null, 'Health'),
            actions: health ? h(ui.Badge, { tone: 'success' }, t('backend.health.ok', null, 'healthy')) : h(ui.Badge, { tone: 'error' }, t('backend.health.failed', null, 'unhealthy')),
          },
          healthError
            ? h(ui.ErrorBox, { error: healthError })
            : h(ui.KV, {
                rows: [
                  { key: 'db_ok', label: t('backend.health.dbOk', null, 'database ok'), value: String(Boolean(health && health.db_ok)) },
                  { key: 'version', label: t('backend.version', null, 'Version'), value: asText(health && health.version) || '—' },
                  {
                    key: 'databases',
                    label: t('backend.db', null, 'Database'),
                    value: Array.isArray(health && health.databases) ? health.databases.join(', ') : asText(health && (health.database || health.db)) || '—',
                  },
                  {
                    key: 'db_bytes',
                    label: t('table.size', null, 'Size'),
                    value: formatBytes(health && health.db_bytes),
                  },
                  {
                    key: 'counts',
                    label: t('stats.cardFunctions', null, 'Functions'),
                    value: asText(health && health.tables && health.tables.functions) || '—',
                  },
                ],
              }),
        )
      : null

    const probeCard = probe || probeError
      ? h(
          ui.Card,
          {
            title: t('backend.probeTitle', null, 'Probe report'),
            actions: probeError
              ? h(ui.Badge, { tone: 'error' }, t('backend.probeFailed', null, 'Probe failed'))
              : h(ui.Badge, { tone: 'success' }, t('common.details', null, 'Details')),
          },
          probeError
            ? h(ui.ErrorBox, { error: probeError })
            : h(ui.Pre, { className: 'ca-backend-report' }, JSON.stringify(probe, null, 2)),
        )
      : null

    const installCard = install
      ? h(
          ui.Card,
          {
            title: t('backend.installTitle', null, 'Remote installation'),
            actions: h(
              ui.Badge,
              {
                tone:
                  install.status === 'succeeded'
                    ? 'success'
                    : install.status === 'failed'
                      ? 'error'
                      : 'warn',
              },
              String(install.status || 'idle'),
            ),
          },
          h(ui.KV, {
            rows: [
              { key: 'ssh', label: 'SSH', value: asText((install.ssh && install.ssh.host) || form.remote_sshHost) || '—' },
              { key: 'started', label: t('backend.installStartedAt', null, 'Started'), value: formatTimestamp(install.startedAt) },
              {
                key: 'elapsed',
                label: t('backend.installFinishedAt', null, 'Finished'),
                value: install.finishedAt
                  ? `${formatTimestamp(install.finishedAt)} (${formatDuration(install.finishedAt - (install.startedAt || install.finishedAt))})`
                  : '—',
              },
            ],
          }),
          install.status === 'running'
            ? h(
                'p',
                { className: 'ca-muted', style: { margin: '10px 0 0', fontSize: 12.5 } },
                t('backend.installRunning', null, 'Installing CA on the remote host. This can take several minutes.'),
              )
            : null,
          install.error
            ? h(ui.ErrorBox, { title: t('backend.installFailed', null, 'Installation failed.'), error: install.error })
            : null,
          install.result
            ? h(ui.Pre, { className: 'ca-backend-report' }, JSON.stringify(install.result, null, 2))
            : null,
        )
      : null

    const showLocal = form.backend !== 'remote'
    const showRemote = form.backend !== 'local'

    function field(name, label, options) {
      const settings = options && typeof options === 'object' ? options : {}
      return h(
        ui.Field,
        {
          key: name,
          label,
          hint: settings.hint,
        },
        h(ui.Input, {
          value: form[name],
          placeholder: settings.placeholder,
          ariaLabel: label,
          onChange: (event) => setField(name, event.target.value),
        }),
      )
    }

    const configCard = h(
      ui.Card,
      {
        title: t('backend.config', null, 'Configuration'),
        subtitle: t('backend.configHint', null, 'Saved on the DSH host; SSH passwords are never stored — use keys or ssh-agent.'),
      },
      h(
        'div',
        { className: 'ca-backend-group' },
        h(
          'div',
          { className: 'ca-backend-form-grid' },
          h(
            ui.Field,
            { key: 'backend-mode', label: t('backend.config.mode', null, 'Backend mode') },
            h(ui.Select, {
              value: form.backend,
              options: MODE_OPTIONS.map((option) => ({
                value: option.value,
                label:
                  option.value === 'auto'
                    ? t('backend.mode.auto', null, 'auto')
                    : option.value === 'local'
                      ? t('backend.mode.local', null, 'local')
                      : t('backend.mode.remote', null, 'remote (SSH)'),
              })),
              ariaLabel: t('backend.config.mode', null, 'Backend mode'),
              onChange: (value) => setField('backend', value),
            }),
          ),
          h(
            'label',
            { className: 'ca-backend-check' },
            h('input', {
              type: 'checkbox',
              checked: Boolean(form.autoStart),
              onChange: (event) => setField('autoStart', event.target.checked),
            }),
            t('backend.config.autoStart', null, 'Start backend automatically'),
          ),
        ),
        showLocal
          ? h(
              'div',
              { className: 'ca-backend-group' },
              h('div', { className: 'ca-backend-group-title' }, h(ui.Icon, { name: 'backend', size: 13 }), t('backend.config.local', null, 'Local backend')),
              h(
                'div',
                { className: 'ca-backend-form-grid' },
                field('local_db', t('backend.field.db', null, 'Index database (single DB)'), {
                  placeholder: t('backend.field.placeholderPath', null, '/absolute/path or ~/path'),
                }),
                field('local_dbDir', t('backend.field.dbDir', null, 'Shard directory (federated)'), {
                  placeholder: t('backend.field.placeholderPath', null, '/absolute/path or ~/path'),
                }),
                field('local_repoRoot', t('backend.field.repoRoot', null, 'CA repository root'), {
                  placeholder: t('backend.field.placeholderRepo', null, 'repository root on the DSH host'),
                }),
                field('local_python', t('backend.field.python', null, 'Python interpreter'), {
                  placeholder: t('backend.field.placeholderPath', null, '/absolute/path or ~/path'),
                }),
                field('local_host', t('backend.field.host', null, 'Bind host')),
                field('local_port', t('backend.field.port', null, 'Port (0 = auto)')),
              ),
            )
          : null,
        showRemote
          ? h(
              'div',
              { className: 'ca-backend-group' },
              h('div', { className: 'ca-backend-group-title' }, h(ui.Icon, { name: 'external', size: 13 }), t('backend.config.remote', null, 'Remote backend (SSH)')),
              h(
                'div',
                { className: 'ca-backend-form-grid' },
                field('remote_sshHost', t('backend.field.sshHost', null, 'SSH host / alias'), {
                  placeholder: t('backend.field.placeholderSsh', null, 'user@host or ssh config alias'),
                }),
                field('remote_sshUser', t('backend.field.sshUser', null, 'SSH user')),
                field('remote_sshPort', t('backend.field.sshPort', null, 'SSH port')),
                field('remote_sshKeyPath', t('backend.field.sshKeyPath', null, 'SSH private key path'), {
                  placeholder: t('backend.field.placeholderPath', null, '/absolute/path or ~/path'),
                }),
                field('remote_repoPath', t('backend.field.repoPath', null, 'Existing CA checkout path (remote)'), {
                  placeholder: '~/CA',
                }),
                field('remote_installDir', t('backend.field.installDir', null, 'Install directory (remote)'), {
                  placeholder: '~/dsh-ca',
                }),
                field('remote_db', t('backend.field.remoteDb', null, 'Remote index database'), {
                  placeholder: '~/CA/index/ca.db',
                }),
                field('remote_dbDir', t('backend.field.remoteDbDir', null, 'Remote shard directory'), {
                  placeholder: '~/CA/index',
                }),
                field('remote_serveRemotePort', t('backend.field.serveRemotePort', null, 'Remote serve port (0 = auto)')),
                field('remote_localPort', t('backend.field.localPort', null, 'Local tunnel port (0 = auto)')),
              ),
            )
          : null,
        h(
          'div',
          { className: 'ca-row' },
          h(
            ui.Button,
            { variant: 'primary', disabled: anyBusy, onClick: saveConfig },
            busy === 'save' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'check', size: 13 }),
            busy === 'save'
              ? t('backend.config.saving', null, 'Saving…')
              : t('backend.config.save', null, 'Save configuration'),
          ),
          h('span', { className: 'ca-faint', style: { fontSize: 11.5 } }, t('backend.installHint', null, '')),
        ),
      ),
    )

    const unconfiguredHint =
      effectiveMode === 'unconfigured'
        ? h(
            'div',
            { className: 'ca-backend-notice' },
            h(ui.Icon, { name: 'info', size: 13 }),
            h('span', null, t('backend.unconfiguredHint', null, 'Fill in the local database path or the remote SSH host, save, and start the backend.')),
          )
        : null

    return h(
      'div',
      { className: 'ca-page ca-backend-page' },
      h(
        'div',
        { className: 'ca-backend-inner' },
        h(
          'div',
          { className: 'ca-backend-head' },
          h('h1', { className: 'ca-backend-title' }, t('backend.title', null, 'CA backend')),
          h('span', { className: 'ca-spacer' }),
          h('span', { className: 'ca-faint', style: { fontSize: 11.5 } }, t('shell.readonly', null, 'Read-only')),
        ),
        unconfiguredHint,
        statusCard,
        projectHintCard,
        healthCard,
        probeCard,
        installCard,
        configCard,
        h(
          'div',
          { className: 'ca-search-hint' },
          h('span', { className: 'ca-mono' }, config && config.paths && config.paths.settingsPath ? config.paths.settingsPath : ''),
        ),
      ),
    )
  }

  BackendPage.displayName = 'CaBackendPage'
  return BackendPage
}

export default createBackendPage
