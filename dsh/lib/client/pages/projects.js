/**
 * CA projects page (`#/projects`) — minimal project setup.
 *
 * One job: add a project by pointing at a directory.
 *   - This computer: pick a directory (Host workspace picker when available,
 *     otherwise type a path).
 *   - Remote server: username + host/IP + port + password or key, then connect,
 *     browse the server's directories and pick the project directory.
 *
 * Creating a project runs `projects.createSimple` followed by
 * `projects.prepare` (install backend when missing, create the index directory,
 * start the backend); progress comes from `backend.installStatus` and `status`.
 * Projects are listed with directory/type/status and a switch action; editing
 * shows name + directory + authentication with legacy fields under “Advanced”.
 */

import { toApiError } from '../api.js'

const PROJECTS_CSS = `
.ca-projects-page { gap: 12px; overflow: auto; padding: 18px; }
.ca-projects-inner { width: 100%; max-width: 900px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }
.ca-projects-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ca-projects-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
.ca-projects-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-wrap: wrap; }
.ca-projects-add { display: flex; flex-direction: column; gap: 10px; }
.ca-projects-switch { display: inline-flex; align-items: center; gap: 2px; padding: 2px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-sm, 6px); width: fit-content; }
.ca-projects-switch .ca-btn { min-height: 24px; padding: 0 10px; font-size: 12px; }
.ca-projects-switch .ca-btn-active { background: var(--dsw-alias-button-ghost-active-fill, rgba(127, 127, 127, 0.14)); }
.ca-projects-row { display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; }
.ca-projects-row > .ca-field { flex: 1 1 220px; min-width: 160px; }
.ca-projects-dir { display: flex; align-items: flex-end; gap: 8px; flex-wrap: wrap; }
.ca-projects-dir > .ca-field { flex: 1 1 320px; min-width: 220px; }
.ca-projects-browse { border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); overflow: hidden; }
.ca-projects-browse-head { display: flex; align-items: center; gap: 6px; padding: 6px 8px; border-bottom: 1px solid var(--dsw-alias-border-l1, #eaeef2); background: var(--dsw-alias-bg-layer-2, rgba(127, 127, 127, 0.05)); }
.ca-projects-browse-path { flex: 1 1 auto; min-width: 0; }
.ca-projects-browse-list { max-height: 280px; overflow: auto; padding: 4px; }
.ca-projects-browse-row { display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 8px; border: 0; border-radius: var(--dsw-radius-sm, 6px); background: transparent; color: inherit; font: inherit; font-size: 12.5px; text-align: left; cursor: pointer; }
.ca-projects-browse-row:hover { background: var(--dsw-alias-interactive-bg-hover, rgba(127, 127, 127, 0.09)); }
.ca-projects-browse-empty { padding: 14px; text-align: center; color: var(--dsw-alias-label-tertiary, #8b949e); font-size: 12px; }
.ca-projects-progress { display: flex; flex-direction: column; gap: 6px; }
.ca-projects-steps { display: flex; flex-direction: column; gap: 2px; }
.ca-projects-step { display: flex; gap: 7px; font-size: 12px; align-items: baseline; }
.ca-projects-step-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--dsw-alias-state-idle-primary, #8b949e); flex: 0 0 auto; }
.ca-projects-step-dot-info { background: var(--dsw-alias-state-business-primary, #2f6feb); }
.ca-projects-step-dot-success { background: var(--dsw-alias-state-success-primary, #1a7f37); }
.ca-projects-step-dot-error { background: var(--dsw-alias-state-error-primary, #d1242f); }
.ca-projects-step-status { color: var(--dsw-alias-label-tertiary, #8b949e); font-size: 11px; }
.ca-projects-step-detail { color: var(--dsw-alias-label-tertiary, #8b949e); font-size: 11px; overflow-wrap: anywhere; }
.ca-projects-log { max-height: 180px; }
.ca-projects-list-row { display: flex; align-items: center; gap: 10px; padding: 7px 2px; border-bottom: 1px solid var(--dsw-alias-border-l1, #eaeef2); min-width: 0; }
.ca-projects-list-row:last-child { border-bottom: 0; }
.ca-projects-list-main { display: flex; flex-direction: column; gap: 0; flex: 1 1 auto; min-width: 0; }
.ca-projects-list-name { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 550; min-width: 0; flex-wrap: wrap; }
.ca-projects-list-path { font-family: var(--ds-font-family-code, ui-monospace, monospace); font-size: 10.5px; color: var(--dsw-alias-label-tertiary, #8b949e); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ca-projects-list-actions { display: flex; align-items: center; gap: 4px; }
.ca-projects-short { font-size: 11.5px; color: var(--dsw-alias-label-tertiary, #8b949e); }
.ca-projects-advanced { border-top: 1px solid var(--dsw-alias-border-l1, #eaeef2); padding-top: 8px; }
.ca-projects-advanced-head { display: flex; align-items: center; gap: 6px; border: 0; background: transparent; color: var(--dsw-alias-label-secondary, #57606a); font: inherit; font-size: 12px; cursor: pointer; padding: 2px 0; }
.ca-projects-error { font-size: 12px; color: var(--dsw-alias-state-error-primary, #d1242f); }
`

const AUTH_MODES = [
  { value: 'password', labelKey: 'projects.auth.password', fallback: 'Password' },
  { value: 'key', labelKey: 'projects.auth.key', fallback: 'Key / agent' },
]

function asText(value) {
  return value === undefined || value === null ? '' : String(value)
}

function basename(path) {
  if (typeof path !== 'string') return ''
  const cleaned = path.replace(/[\\/]+$/, '')
  const parts = cleaned.split(/[\\/]/)
  return parts[parts.length - 1] || cleaned
}

function isAbsolute(path) {
  return typeof path === 'string' && path.startsWith('/')
}

function projectDirectory(project) {
  if (!project || typeof project !== 'object') return ''
  return asText(project.sourceRoot || project.directory || '')
}

function projectTypeKey(project) {
  return project && project.backend === 'remote' ? 'projects.type.remote' : 'projects.type.local'
}

function projectTypeFallback(project) {
  return project && project.backend === 'remote' ? 'remote' : 'local'
}

function projectStatusLabel(project, t) {
  const status = project && project.status && typeof project.status === 'object' ? project.status : null
  const state = status && typeof status.state === 'string' ? status.state : ''
  if (state) return t(`shell.backendState.${state}`, null, state)
  if (project && project.active === true) return t('projects.active', null, 'active')
  return ''
}

function normalizeSteps(raw) {
  const steps = raw && typeof raw === 'object' ? raw.steps : null
  const list = Array.isArray(steps) ? steps : []
  return list.map((entry, index) => {
    if (typeof entry === 'string') return { id: '', title: entry, status: 'info', detail: '' }
    const value = entry && typeof entry === 'object' ? entry : {}
    return {
      id: asText(value.id),
      title: asText(value.title || value.name || value.step),
      status: asText(value.status || value.state || 'info'),
      detail: asText(value.detail || value.message || ''),
      index,
    }
  })
}

function normalizeLogs(raw) {
  const logs = raw && typeof raw === 'object' ? raw.logs : null
  const list = Array.isArray(logs) ? logs : []
  const lines = []
  for (const entry of list) {
    if (typeof entry === 'string') lines.push(entry)
    else if (entry && typeof entry === 'object') lines.push(asText(entry.line || entry.text || entry.message))
  }
  const joined = lines.filter(Boolean).join('\n')
  return joined.length > 20000 ? joined.slice(-20000) : joined
}

function stepTone(stateName) {
  if (stateName === 'succeeded') return 'success'
  if (stateName === 'failed' || stateName === 'error') return 'error'
  if (stateName === 'running') return 'info'
  return 'neutral'
}

/**
 * Create the projects page component.
 *
 * @param {object} React React namespace.
 * @param {{ ui: object, api: object, i18n: object }} bag
 */
export function createProjectsPage(React, bag) {
  const { ui, api, i18n } = bag || {}
  if (!ui || !api || !i18n) throw new TypeError('createProjectsPage(React, { ui, api, i18n })')
  const h = React.createElement
  const t = (key, vars, fallback) => i18n.t(key, vars, fallback)

  function notifyProjectsChanged() {
    try {
      if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        window.dispatchEvent(new CustomEvent('ca:projectschange'))
      }
    } catch (_error) {
      /* shell refresh is a convenience */
    }
  }

  function ProjectsPage(props) {
    ui.useStyles(PROJECTS_CSS)
    const navigate = props && typeof props.navigate === 'function' ? props.navigate : null

    const [list, setList] = React.useState(null)
    const [loadError, setLoadError] = React.useState(null)
    const [mode, setMode] = React.useState('local')
    const [localPath, setLocalPath] = React.useState('')
    const [remoteForm, setRemoteForm] = React.useState({
      user: '',
      host: '',
      port: '22',
      auth: 'password',
      password: '',
      keyPath: '',
    })
    const [connect, setConnect] = React.useState(null) // {busy, info, error}
    const [browse, setBrowse] = React.useState(null) // {busy, path, parent, home, entries, error}
    const [creating, setCreating] = React.useState(null) // {phase, project, steps, logs, error, startedAt}
    const [edit, setEdit] = React.useState(null)
    const [busy, setBusy] = React.useState('')
    const [notice, setNotice] = React.useState('')
    const [actionError, setActionError] = React.useState(null)
    const [pickerUnavailable, setPickerUnavailable] = React.useState(false)

    const mountedRef = React.useRef(true)
    const noticeTimer = React.useRef(null)

    React.useEffect(
      () => () => {
        mountedRef.current = false
        if (noticeTimer.current !== null && typeof window !== 'undefined') window.clearTimeout(noticeTimer.current)
      },
      [],
    )

    function showNotice(message) {
      setNotice(message)
      if (noticeTimer.current !== null && typeof window !== 'undefined') window.clearTimeout(noticeTimer.current)
      if (typeof window !== 'undefined') {
        noticeTimer.current = window.setTimeout(() => {
          noticeTimer.current = null
          if (mountedRef.current) setNotice('')
        }, 5000)
      }
    }

    function applyList(value) {
      if (value && typeof value === 'object' && Array.isArray(value.projects)) {
        setList({ projects: value.projects, activeProjectId: value.activeProjectId || null })
      }
    }

    async function loadProjects() {
      try {
        const value = await api.projects.list()
        if (!mountedRef.current) return
        if (value && value.ok === false && value.error) {
          setLoadError(toApiError(value.error))
          return
        }
        applyList(value)
        setLoadError(null)
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setLoadError(apiError)
      }
    }

    React.useEffect(() => {
      void loadProjects()
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    const projects = list && Array.isArray(list.projects) ? list.projects : []
    const activeId = list ? list.activeProjectId : null

    // -- local flow -------------------------------------------------------- //

    async function chooseLocalDirectory() {
      setActionError(null)
      try {
        const picked = await api.pickDirectory()
        if (picked && typeof picked === 'string') {
          setLocalPath(picked)
          setPickerUnavailable(false)
          return
        }
        setPickerUnavailable(true)
      } catch (error) {
        setPickerUnavailable(true)
        const apiError = toApiError(error)
        if (apiError.code !== 'ABORTED') setActionError(apiError)
      }
    }

    function createLocalProject() {
      const directory = localPath.trim()
      if (!isAbsolute(directory)) {
        setActionError(toApiError({ code: 'INVALID_ARGUMENT', message: t('projects.pickDirectoryFirst', null, 'Choose a directory first.') }))
        return
      }
      void createAndPrepare({ kind: 'local', directory })
    }

    // -- remote flow ------------------------------------------------------- //

    function remoteSsh() {
      const keyPath = remoteForm.auth === 'key' ? remoteForm.keyPath.trim() : ''
      const ssh = {
        host: remoteForm.host.trim(),
        user: remoteForm.user.trim() || undefined,
        port: remoteForm.port.trim() ? Number(remoteForm.port.trim()) : undefined,
        keyPath: keyPath || undefined,
      }
      if (remoteForm.auth === 'password' && remoteForm.password) ssh.password = remoteForm.password
      return ssh
    }

    function remoteSshForProject() {
      const ssh = remoteSsh()
      delete ssh.password
      return ssh
    }

    async function connectRemote() {
      if (!remoteForm.host.trim()) {
        setActionError(toApiError({ code: 'INVALID_ARGUMENT', message: t('projects.hostRequired', null, 'Host / IP is required.') }))
        return
      }
      setConnect({ busy: true, info: null, error: null })
      setBrowse(null)
      setActionError(null)
      try {
        const info = await api.remote.connect(remoteSsh())
        if (!mountedRef.current) return
        if (info && typeof info === 'object' && info.ok === false && info.error) {
          setConnect({ busy: false, info: null, error: toApiError(info.error) })
          return
        }
        setConnect({ busy: false, info: info || {}, error: null })
        await browseDirectory(info && typeof info.home === 'string' && info.home ? info.home : undefined)
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setConnect({ busy: false, info: null, error: apiError })
      }
    }

    async function browseDirectory(path) {
      if (!mountedRef.current) return
      setBrowse((current) => Object.assign({ busy: true, entries: [], path: current && current.path }, current || {}))
      try {
        const result = await api.remote.browse(remoteSsh(), path)
        if (!mountedRef.current) return
        if (result && typeof result === 'object' && result.ok === false && result.error) {
          setBrowse((current) =>
            Object.assign({}, current || {}, { busy: false, error: toApiError(result.error), entries: [] }),
          )
          return
        }
        setBrowse({
          busy: false,
          path: asText(result && result.path),
          parent: asText(result && result.parent),
          home: asText(result && result.home),
          entries: result && Array.isArray(result.entries) ? result.entries : [],
          error: null,
        })
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setBrowse((current) =>
          Object.assign({}, current || {}, { busy: false, error: apiError, entries: current ? current.entries : [] }),
        )
      }
    }

    function selectRemoteDirectory() {
      const directory = browse && browse.path ? browse.path : ''
      if (!isAbsolute(directory)) {
        setActionError(toApiError({ code: 'INVALID_ARGUMENT', message: t('projects.pickDirectoryFirst', null, 'Choose a directory first.') }))
        return
      }
      const home = connect && connect.info && typeof connect.info.home === 'string' ? connect.info.home : ''
      void createAndPrepare({
        kind: 'remote',
        directory,
        ssh: remoteSshForProject(),
        ...(home ? { home } : {}),
      })
    }

    // -- create + prepare -------------------------------------------------- //

    async function createAndPrepare(input) {
      setBusy('create')
      setActionError(null)
      setCreating({ phase: 'creating', project: null, steps: [], logs: [], error: null, startedAt: Date.now() })
      try {
        const created = await api.projects.createSimple(input)
        if (!mountedRef.current) return
        const project = created && created.project ? created.project : created
        if (!project || !project.id) {
          throw toApiError({ code: 'BACKEND_ERROR', message: t('projects.createFailed', null, 'Could not create the project.') })
        }
        applyList(created)
        notifyProjectsChanged()
        setCreating({ phase: 'preparing', project, steps: [], logs: [], error: null, startedAt: Date.now() })
        await api.projects.prepare(project.id)
        await loadProjects()
        if (!mountedRef.current) return
        showNotice(t('projects.preparing', null, 'Preparing backend and index…'))
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setCreating((current) => Object.assign({}, current || {}, { phase: 'failed', error: apiError }))
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    // Poll prepare progress (install steps + backend state) until ready.
    const preparing = Boolean(creating && creating.phase === 'preparing')
    const preparingProjectId = creating && creating.project ? creating.project.id : ''
    React.useEffect(() => {
      if (!preparing) return undefined
      let cancelled = false
      let ticks = 0
      const tick = async () => {
        ticks += 1
        const project = creating && creating.project ? creating.project : null
        const scope = project && project.backend === 'remote' ? 'remote' : 'local'
        try {
          const raw = await api.backend.installStatus({ scope })
          const status = raw && typeof raw === 'object' ? raw[scope] || raw : null
          const prepare =
            raw && typeof raw === 'object' && raw.prepare && typeof raw.prepare === 'object'
              ? raw.prepare
              : null
          const backendStatus = await api.getStatus()
          if (cancelled || !mountedRef.current) return
          const backend = backendStatus && backendStatus.backend ? backendStatus.backend : null
          const installState = status ? asText(status.status || status.state) : ''
          const prepareState = prepare ? asText(prepare.state || prepare.status) : ''
          setCreating((current) =>
            current
              ? Object.assign({}, current, {
                  steps: normalizeSteps({ steps: (status && status.steps) || [] }),
                  logs: normalizeLogs({ logs: (status && status.logs) || [] }),
                  installState,
                  prepareState,
                  backendState: backend ? asText(backend.state) : '',
                })
              : current,
          )
          if (ticks % 2 === 0) void loadProjects()
          const failed =
            prepareState === 'failed' ||
            installState === 'failed' ||
            Boolean(status && status.error) ||
            Boolean(backend && backend.state === 'error' && installState !== 'running')
          if (failed) {
            const failure = prepare && prepare.error
              ? toApiError(prepare.error)
              : status && status.error
                ? toApiError(status.error)
                : toApiError({
                    code: 'BACKEND_ERROR',
                    message:
                      (backend && backend.error) ||
                      t('projects.prepareFailed', null, 'Could not prepare the project.'),
                  })
            setCreating((current) =>
              current ? Object.assign({}, current, { phase: 'failed', error: failure }) : current,
            )
            setActionError(failure)
            return
          }
          const ready = Boolean(
            backend &&
              backend.state === 'running' &&
              (prepareState === 'succeeded' || installState === 'succeeded' || (!prepareState && installState !== 'running')),
          )
          if (ready) {
            setCreating((current) => (current ? Object.assign({}, current, { phase: 'done' }) : current))
            showNotice(t('projects.prepareDone', null, 'Project ready.'))
            notifyProjectsChanged()
            void loadProjects()
            // The prepare started the backend for this project; activate it so
            // the project list (and next page load) uses it as the active one.
            try {
              const current = await api.projects.list()
              const alreadyActive =
                current && typeof current === 'object' && current.activeProjectId === project.id
              if (!alreadyActive) {
                await api.projects.activate(project.id)
                await waitForRunning(30000)
                if (mountedRef.current && typeof window !== 'undefined' && window.location && typeof window.location.reload === 'function') {
                  window.location.reload()
                }
              }
            } catch (_error) {
              /* leave the manual "Switch" action as the fallback */
            }
          }
        } catch (error) {
          if (cancelled || !mountedRef.current) return
          const apiError = toApiError(error)
          if (apiError.code === 'ABORTED') return
          /* transient: keep polling */
        }
      }
      void tick()
      const timer = window.setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        const startedAt = creating && creating.startedAt ? creating.startedAt : Date.now()
        if (Date.now() - startedAt > 15 * 60 * 1000) {
          setCreating((current) =>
            current
              ? Object.assign({}, current, {
                  phase: 'failed',
                  error: toApiError({ code: 'TIMEOUT', message: t('projects.prepareTimeout', null, 'Preparing the project timed out.') }),
                })
              : current,
          )
          return
        }
        void tick()
      }, 1000)
      return () => {
        cancelled = true
        window.clearInterval(timer)
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [preparing, preparingProjectId])

    // -- list actions ------------------------------------------------------ //

    async function waitForRunning(timeoutMs) {
      const startedAt = Date.now()
      while (Date.now() - startedAt < (timeoutMs || 30000)) {
        try {
          const status = await api.getStatus()
          const backend = status && status.backend ? status.backend : null
          if (backend && (backend.state === 'running' || backend.state === 'error')) return backend
        } catch (_error) {
          /* keep polling */
        }
        await new Promise((resolve) => setTimeout(resolve, 600))
      }
      return null
    }

    async function activateProject(project) {
      if (!project || !project.id || busy) return
      setBusy(`activate:${project.id}`)
      setActionError(null)
      try {
        await api.projects.activate(project.id)
        const backend = await waitForRunning(30000)
        if (!mountedRef.current) return
        if (backend && backend.state === 'running') {
          showNotice(t('projects.activated', null, 'Project activated; reloading…'))
          notifyProjectsChanged()
          if (typeof window !== 'undefined' && window.location && typeof window.location.reload === 'function') {
            window.location.reload()
          }
          return
        }
        setActionError(toApiError({ code: 'BACKEND_ERROR', message: t('shell.projectSwitchTimeout', null, 'The backend did not become ready in time.') }))
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    function startEdit(project) {
      if (!project) return
      const ssh = project.ssh && typeof project.ssh === 'object' ? project.ssh : {}
      setEdit({
        project,
        name: asText(project.name),
        directory: projectDirectory(project),
        user: asText(ssh.user),
        host: asText(ssh.host),
        port: asText(ssh.port || '22'),
        keyPath: asText(ssh.keyPath),
        backend: asText(project.backend || 'local'),
        db: asText(project.db),
        dbDir: asText(project.dbDir),
        compdb: asText(project.compdb),
        python: asText(project.python),
        caRepoPath: asText(project.caRepoPath),
        advanced: false,
        error: '',
        busy: false,
      })
    }

    function setEditField(name, value) {
      setEdit((current) => (current ? Object.assign({}, current, { [name]: value }) : current))
    }

    async function saveEdit() {
      if (!edit) return
      const project = edit.project
      const payload = {
        id: project.id,
        name: edit.name.trim(),
        backend: edit.backend === 'remote' ? 'remote' : 'local',
        sourceRoot: edit.directory.trim(),
        db: edit.db.trim() || null,
        dbDir: edit.dbDir.trim() || null,
        compdb: edit.compdb.trim() || null,
        python: edit.python.trim() || null,
        caRepoPath: edit.caRepoPath.trim() || null,
        ssh:
          edit.backend === 'remote'
            ? {
                host: edit.host.trim(),
                user: edit.user.trim() || null,
                port: edit.port.trim() ? Number(edit.port.trim()) : null,
                keyPath: edit.keyPath.trim() || null,
              }
            : null,
      }
      if (!payload.name) {
        setEdit((current) => (current ? Object.assign({}, current, { error: t('projects.nameRequired', null, 'Name is required.') }) : current))
        return
      }
      if (!isAbsolute(payload.sourceRoot)) {
        setEdit((current) => (current ? Object.assign({}, current, { error: t('projects.pickDirectoryFirst', null, 'Choose a directory first.') }) : current))
        return
      }
      setEdit((current) => (current ? Object.assign({}, current, { busy: true, error: '' }) : current))
      try {
        const value = await api.projects.save(payload)
        if (!mountedRef.current) return
        applyList(value)
        setEdit(null)
        showNotice(t('projects.created', null, 'Project saved.'))
        notifyProjectsChanged()
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setEdit((current) => (current ? Object.assign({}, current, { busy: false, error: apiError.message }) : current))
      }
    }

    async function removeProject(project) {
      if (!project) return
      setBusy(`remove:${project.id}`)
      setActionError(null)
      try {
        const value = await api.projects.remove(project.id)
        if (!mountedRef.current) return
        applyList(value)
        setEdit(null)
        showNotice(t('projects.removed', null, 'Project deleted.'))
        notifyProjectsChanged()
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    // -- render helpers ---------------------------------------------------- //

    function fieldRow(label, value, placeholder, onChange) {
      return h(
        ui.Field,
        { key: label, label },
        h(ui.Input, {
          value,
          placeholder,
          ariaLabel: label,
          onChange: (event) => onChange(event.target.value),
        }),
      )
    }

    function stepLabelOf(step) {
      if (step.id) {
        const key = `install.steps.${step.id}`
        const mapped = t(key, null, '')
        if (mapped && mapped !== key) return mapped
      }
      return step.title || step.id || ''
    }

    function progressView() {
      if (!creating) return null
      const phaseLabel =
        creating.phase === 'creating'
          ? t('projects.creating', null, 'Creating project…')
          : creating.phase === 'preparing'
            ? t('projects.preparing', null, 'Preparing backend and index…')
            : creating.phase === 'done'
              ? t('projects.prepareDone', null, 'Project ready.')
              : t('projects.prepareFailed', null, 'Could not prepare the project.')
      const steps = Array.isArray(creating.steps) ? creating.steps : []
      const logs = creating.logs || ''
      const project = creating.project
      return h(
        ui.Card,
        {
          title: phaseLabel,
          actions:
            creating.phase === 'creating' || creating.phase === 'preparing'
              ? h(ui.Spinner, { size: 13 })
              : h(
                  ui.Badge,
                  { tone: creating.phase === 'failed' ? 'error' : 'success' },
                  creating.phase === 'failed' ? t('install.status.failed', null, 'failed') : t('install.status.succeeded', null, 'succeeded'),
                ),
        },
        h(
          'div',
          { className: 'ca-projects-progress' },
          project ? h('div', { className: 'ca-projects-short' }, projectDirectory(project)) : null,
          steps.length > 0
            ? h(
                'div',
                { className: 'ca-projects-steps' },
                steps.map((step, index) =>
                  h(
                    'div',
                    { key: `${index}-${step.id || step.title}`, className: 'ca-projects-step' },
                    h('span', { className: `ca-projects-step-dot ca-projects-step-dot-${stepTone(step.status)}`, 'aria-hidden': 'true' }),
                    h('span', null, stepLabelOf(step)),
                    h('span', { className: 'ca-projects-step-status' }, t(`install.status.${step.status}`, null, step.status)),
                    step.detail ? h('span', { className: 'ca-projects-step-detail' }, step.detail) : null,
                  ),
                ),
              )
            : null,
          creating.phase === 'done'
            ? h(
                'div',
                { className: 'ca-projects-short' },
                t(
                  'projects.emptyIndexHint',
                  null,
                  'Backend ready. The index is empty until an index task runs.',
                ),
              )
            : null,
          creating.error
            ? h(ui.ErrorBox, {
                error: creating.error,
                hint:
                  creating.error.details && Array.isArray(creating.error.details.hints)
                    ? `${t('install.error.hints', null, 'Suggested fixes')}: ${creating.error.details.hints.join(' · ')}`
                    : '',
              })
            : null,
          logs ? h(ui.Pre, { className: 'ca-projects-log' }, logs) : null,
          creating.phase === 'failed'
            ? h(
                ui.Button,
                {
                  variant: 'secondary',
                  size: 'sm',
                  onClick: () => {
                    setCreating(null)
                    setActionError(null)
                  },
                },
                t('common.close', null, 'Close'),
              )
            : null,
        ),
      )
    }

    function localPanel() {
      return h(
        'div',
        { className: 'ca-projects-add' },
        h(
          'div',
          { className: 'ca-projects-dir' },
          h(
            ui.Field,
            { key: 'local-dir', label: t('projects.directory', null, 'Directory') },
            h(ui.Input, {
              value: localPath,
              placeholder: '/home/user/src/project',
              ariaLabel: t('projects.directory', null, 'Directory'),
              onChange: (event) => {
                setLocalPath(event.target.value)
                setPickerUnavailable(false)
              },
              onKeyDown: (event) => {
                if (event.key === 'Enter') createLocalProject()
              },
            }),
          ),
          h(
            ui.Button,
            { variant: 'secondary', onClick: () => void chooseLocalDirectory() },
            h(ui.Icon, { name: 'folder', size: 13 }),
            t('projects.pickDirectory', null, 'Choose directory…'),
          ),
          h(
            ui.Button,
            { variant: 'primary', disabled: busy === 'create' || preparing, onClick: createLocalProject },
            busy === 'create' || preparing ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'play', size: 13 }),
            t('projects.create', null, 'Add project'),
          ),
        ),
        pickerUnavailable
          ? h('div', { className: 'ca-projects-error' }, t('projects.pickerUnavailable', null, 'Directory picker unavailable; type the path above.'))
          : null,
      )
    }

    function remotePanel() {
      const connected = Boolean(connect && connect.info)
      return h(
        'div',
        { className: 'ca-projects-add' },
        h(
          'div',
          { className: 'ca-projects-row' },
          fieldRow(t('projects.user', null, 'Username'), remoteForm.user, 'root', (value) => setRemoteForm((c) => Object.assign({}, c, { user: value }))),
          fieldRow(t('projects.host', null, 'Host / IP'), remoteForm.host, '192.168.1.10', (value) => setRemoteForm((c) => Object.assign({}, c, { host: value }))),
          fieldRow(t('projects.port', null, 'Port'), remoteForm.port, '22', (value) => setRemoteForm((c) => Object.assign({}, c, { port: value }))),
          h(
            ui.Field,
            { key: 'auth', label: t('projects.auth', null, 'Authentication') },
            h(ui.Select, {
              value: remoteForm.auth,
              options: AUTH_MODES.map((entry) => ({ value: entry.value, label: t(entry.labelKey, null, entry.fallback) })),
              ariaLabel: t('projects.auth', null, 'Authentication'),
              onChange: (value) => setRemoteForm((c) => Object.assign({}, c, { auth: value })),
            }),
          ),
          remoteForm.auth === 'password'
            ? h(
                ui.Field,
                { key: 'password', label: t('projects.password', null, 'Password') },
                h(ui.Input, {
                  type: 'password',
                  value: remoteForm.password,
                  placeholder: '••••••',
                  ariaLabel: t('projects.password', null, 'Password'),
                  onChange: (event) => setRemoteForm((c) => Object.assign({}, c, { password: event.target.value })),
                }),
              )
            : fieldRow(
                t('projects.keyPath', null, 'Private key path'),
                remoteForm.keyPath,
                '~/.ssh/id_ed25519',
                (value) => setRemoteForm((c) => Object.assign({}, c, { keyPath: value })),
              ),
          h(
            ui.Button,
            {
              variant: 'primary',
              disabled: Boolean(connect && connect.busy),
              onClick: () => void connectRemote(),
            },
            connect && connect.busy ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'external', size: 13 }),
            t('projects.connect', null, 'Connect'),
          ),
        ),
        connect && connect.error
          ? h(ui.ErrorBox, { title: t('projects.connectFailed', null, 'Connection failed'), error: connect.error })
          : null,
        connected && connect.info && connect.info.python && connect.info.python.supported === false
          ? h(
              'div',
              { className: 'ca-projects-error' },
              t(
                'projects.pythonUnsupported',
                null,
                'The server Python is older than 3.11; install Python 3.11+ or provide an existing CA venv.',
              ),
            )
          : null,
        connected && connect.info
          ? h(
              'div',
              { className: 'ca-projects-short' },
              (() => {
                const platform = connect.info.platform
                const platformLabel =
                  platform && typeof platform === 'object'
                    ? asText(platform.label || platform.platformId || platform.os)
                    : asText(platform)
                return [
                  asText(connect.info.target || ''),
                  platformLabel,
                  connect.info.home ? `home: ${asText(connect.info.home)}` : '',
                  connect.info.python && connect.info.python.path
                    ? `python ${asText(connect.info.python.versionText || connect.info.python.version || '')}: ${asText(connect.info.python.path)}`
                    : '',
                ]
                  .filter(Boolean)
                  .join(' · ')
              })(),
            )
          : null,
        connected ? browseView() : null,
      )
    }

    function browseView() {
      const state = browse
      const currentPath = state && state.path ? state.path : ''
      return h(
        'div',
        { className: 'ca-projects-browse' },
        h(
          'div',
          { className: 'ca-projects-browse-head' },
          h(
            ui.IconButton,
            {
              label: t('projects.up', null, 'Up'),
              title: t('projects.up', null, 'Up'),
              size: 24,
              disabled: !state || !state.parent,
              onClick: () => void browseDirectory(state && state.parent ? state.parent : undefined),
            },
            h(ui.Icon, { name: 'chevron-right', size: 12 }),
          ),
          h(
            ui.IconButton,
            {
              label: t('projects.home', null, 'Home'),
              title: t('projects.home', null, 'Home'),
              size: 24,
              onClick: () => void browseDirectory(state && state.home ? state.home : undefined),
            },
            h(ui.Icon, { name: 'folder', size: 12 }),
          ),
          h(
            'span',
            { className: 'ca-projects-browse-path' },
            h(ui.Input, {
              value: currentPath,
              ariaLabel: t('projects.directory', null, 'Directory'),
              onChange: (event) =>
                setBrowse((current) => Object.assign({}, current || {}, { path: event.target.value })),
              onKeyDown: (event) => {
                if (event.key === 'Enter') void browseDirectory(event.target.value)
              },
            }),
          ),
          h(
            ui.Button,
            { variant: 'primary', size: 'sm', disabled: Boolean(state && state.busy), onClick: selectRemoteDirectory },
            t('projects.selectDirectory', null, 'Select this directory'),
          ),
        ),
        state && state.error
          ? h(ui.ErrorBox, { title: t('projects.browseFailed', null, 'Cannot open directory'), error: state.error })
          : null,
        state && state.busy
          ? h('div', { className: 'ca-projects-browse-empty' }, h(ui.Spinner, { size: 14 }))
          : h(
              'div',
              { className: 'ca-projects-browse-list' },
              state && state.entries && state.entries.length > 0
                ? state.entries.map((entry) =>
                    h(
                      'button',
                      {
                        key: asText(entry && entry.path),
                        type: 'button',
                        className: 'ca-projects-browse-row',
                        onClick: () => void browseDirectory(asText(entry && entry.path)),
                      },
                      h(ui.Icon, { name: 'folder', size: 13 }),
                      h('span', null, asText(entry && entry.name)),
                    ),
                  )
                : h('div', { className: 'ca-projects-browse-empty' }, t('projects.noSubdirs', null, 'No subdirectories.')),
            ),
      )
    }

    function editView() {
      if (!edit) return null
      const remote = edit.backend === 'remote'
      return h(
        ui.Card,
        {
          title: t('projects.editProject', null, 'Edit project'),
          actions: h(
            ui.Button,
            { variant: 'ghost', size: 'sm', onClick: () => setEdit(null) },
            t('common.cancel', null, 'Cancel'),
          ),
        },
        h(
          'div',
          { className: 'ca-col' },
          h(
            'div',
            { className: 'ca-projects-row' },
            fieldRow(t('projects.field.name', null, 'Name'), edit.name, t('projects.field.nameHint', null, 'Short name'), (value) => setEditField('name', value)),
            fieldRow(t('projects.directory', null, 'Directory'), edit.directory, '/home/user/src/project', (value) => setEditField('directory', value)),
          ),
          remote
            ? h(
                'div',
                { className: 'ca-projects-row' },
                fieldRow(t('projects.user', null, 'Username'), edit.user, 'root', (value) => setEditField('user', value)),
                fieldRow(t('projects.host', null, 'Host / IP'), edit.host, '192.168.1.10', (value) => setEditField('host', value)),
                fieldRow(t('projects.port', null, 'Port'), edit.port, '22', (value) => setEditField('port', value)),
                fieldRow(t('projects.keyPath', null, 'Private key path'), edit.keyPath, '~/.ssh/id_ed25519', (value) => setEditField('keyPath', value)),
              )
            : null,
          h(
            'div',
            { className: 'ca-projects-advanced' },
            h(
              'button',
              {
                type: 'button',
                className: 'ca-projects-advanced-head',
                'aria-expanded': edit.advanced ? 'true' : 'false',
                onClick: () => setEditField('advanced', !edit.advanced),
              },
              h(ui.Icon, { name: edit.advanced ? 'chevron-down' : 'chevron-right', size: 13 }),
              t('projects.advanced', null, 'Advanced'),
            ),
            edit.advanced
              ? h(
                  'div',
                  { className: 'ca-projects-row', style: { marginTop: 8 } },
                  fieldRow(t('backend.field.db', null, 'Database'), edit.db, '/home/user/.dsh-ca/data/x/index.db', (value) => setEditField('db', value)),
                  fieldRow(t('backend.field.dbDir', null, 'Shard directory'), edit.dbDir, '/home/user/CA/index', (value) => setEditField('dbDir', value)),
                  fieldRow(t('backend.field.compdb', null, 'compile_commands.json'), edit.compdb, '/home/user/src/compile_commands.json', (value) => setEditField('compdb', value)),
                  fieldRow(t('backend.field.python', null, 'Python interpreter'), edit.python, '/home/user/venv/bin/python', (value) => setEditField('python', value)),
                  fieldRow(t('backend.field.caRepoPath', null, 'CA repository path'), edit.caRepoPath, '/home/user/CA', (value) => setEditField('caRepoPath', value)),
                  h(
                    ui.Field,
                    { key: 'backend', label: t('projects.field.backend', null, 'Backend') },
                    h(ui.Select, {
                      value: edit.backend,
                      options: [
                        { value: 'local', label: t('projects.type.local', null, 'local') },
                        { value: 'remote', label: t('projects.type.remote', null, 'remote') },
                      ],
                      ariaLabel: t('projects.field.backend', null, 'Backend'),
                      onChange: (value) => setEditField('backend', value),
                    }),
                  ),
                )
              : null,
          ),
          edit.error ? h('div', { className: 'ca-projects-error' }, edit.error) : null,
          h(
            'div',
            { className: 'ca-row' },
            h(
              ui.Button,
              { variant: 'primary', disabled: edit.busy, onClick: () => void saveEdit() },
              edit.busy ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'check', size: 13 }),
              t('projects.save', null, 'Save project'),
            ),
            h(
              ui.Button,
              { variant: 'danger', size: 'sm', disabled: edit.busy, onClick: () => void removeProject(edit.project) },
              t('projects.delete', null, 'Delete'),
            ),
          ),
        ),
      )
    }

    function listView() {
      return h(
        ui.Card,
        { title: t('projects.listTitle', null, 'Projects'), actions: h(ui.Badge, { tone: 'neutral' }, String(projects.length)) },
        projects.length === 0
          ? h(ui.EmptyState, {
              icon: h(ui.Icon, { name: 'folder', size: 24 }),
              title: t('projects.empty', null, 'No projects yet'),
              body: t('projects.emptyShort', null, 'Add a directory to get started.'),
            })
          : h(
              'div',
              null,
              projects.map((project) => {
                const isActive = Boolean(activeId && project.id === activeId)
                const directory = projectDirectory(project)
                const state = projectStatusLabel(project, t)
                return h(
                  'div',
                  { key: String(project.id), className: 'ca-projects-list-row' },
                  h(ui.Icon, { name: project.backend === 'remote' ? 'external' : 'folder', size: 15 }),
                  h(
                    'span',
                    { className: 'ca-projects-list-main' },
                    h(
                      'span',
                      { className: 'ca-projects-list-name' },
                      basename(directory) || asText(project.name) || String(project.id),
                      h(ui.Badge, { tone: 'muted' }, t(projectTypeKey(project), null, projectTypeFallback(project))),
                      isActive ? h(ui.Badge, { tone: 'success' }, t('projects.active', null, 'active')) : null,
                      state ? h(ui.Badge, { tone: 'neutral' }, state) : null,
                    ),
                    h('span', { className: 'ca-projects-list-path' }, directory || '—'),
                  ),
                  h(
                    'span',
                    { className: 'ca-projects-list-actions' },
                    h(
                      ui.Button,
                      {
                        variant: isActive ? 'secondary' : 'primary',
                        size: 'sm',
                        disabled: isActive || busy !== '',
                        onClick: () => void activateProject(project),
                      },
                      isActive ? t('projects.active', null, 'active') : t('projects.switch', null, 'Switch'),
                    ),
                    h(
                      ui.IconButton,
                      {
                        label: t('projects.edit', null, 'Edit'),
                        title: t('projects.edit', null, 'Edit'),
                        size: 24,
                        onClick: () => startEdit(project),
                      },
                      h(ui.Icon, { name: 'settings', size: 13 }),
                    ),
                  ),
                )
              }),
            ),
      )
    }

    return h(
      'div',
      { className: 'ca-page ca-projects-page' },
      h(
        'div',
        { className: 'ca-projects-inner' },
        h(
          'div',
          { className: 'ca-projects-head' },
          h('h1', { className: 'ca-projects-title' }, t('projects.title', null, 'Projects')),
          h(
            'div',
            { className: 'ca-projects-actions' },
            navigate
              ? h(
                  ui.Button,
                  { variant: 'ghost', size: 'sm', onClick: () => navigate({ page: 'search' }) },
                  h(ui.Icon, { name: 'search', size: 13 }),
                  t('shell.nav.search', null, 'Search'),
                )
              : null,
            h(
              ui.Button,
              { variant: 'ghost', size: 'sm', disabled: busy !== '', onClick: () => void loadProjects() },
              h(ui.Icon, { name: 'refresh', size: 13 }),
              t('projects.refresh', null, 'Refresh'),
            ),
          ),
        ),
        notice ? h('div', { className: 'ca-projects-short' }, notice) : null,
        loadError ? h(ui.ErrorBox, { error: loadError }) : null,
        actionError
          ? h(ui.ErrorBox, {
              error: actionError,
              retryLabel: t('common.close', null, 'Close'),
              onRetry: () => setActionError(null),
            })
          : null,
        creating ? progressView() : null,
        edit ? editView() : null,
        !edit
          ? h(
              ui.Card,
              { title: t('projects.add', null, 'Add project') },
              h(
                'div',
                { className: 'ca-projects-add' },
                h(
                  'div',
                  { className: 'ca-projects-switch', role: 'radiogroup', 'aria-label': t('projects.add', null, 'Add project') },
                  h(
                    ui.Button,
                    {
                      variant: 'ghost',
                      size: 'sm',
                      active: mode === 'local',
                      'aria-pressed': mode === 'local' ? 'true' : 'false',
                      onClick: () => setMode('local'),
                    },
                    h(ui.Icon, { name: 'folder', size: 13 }),
                    t('projects.thisComputer', null, 'This computer'),
                  ),
                  h(
                    ui.Button,
                    {
                      variant: 'ghost',
                      size: 'sm',
                      active: mode === 'remote',
                      'aria-pressed': mode === 'remote' ? 'true' : 'false',
                      onClick: () => setMode('remote'),
                    },
                    h(ui.Icon, { name: 'external', size: 13 }),
                    t('projects.remoteServer', null, 'Remote server'),
                  ),
                ),
                mode === 'local' ? localPanel() : remotePanel(),
              ),
            )
          : null,
        listView(),
      ),
    )
  }

  ProjectsPage.displayName = 'CaProjectsPage'
  return ProjectsPage
}

export default createProjectsPage
