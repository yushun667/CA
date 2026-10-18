/**
 * CA projects page (`#/projects`).
 *
 * A project is the user-managed unit the plugin serves: one source tree, one
 * database (a single SQLite file or a federated shard directory) and one
 * backend target (local machine or remote Linux host over SSH). Projects are
 * created, named and edited **manually** — there is no directory scanning
 * here. Activating a project maps it onto the backend configuration, restarts
 * the backend and reloads the page so every view reads the new database.
 *
 * RPC: `projects.list` / `projects.save` / `projects.remove` /
 * `projects.activate` through `bag.api.projects`; responses are the Host
 * envelopes already unwrapped by the api layer.
 */

import { toApiError } from '../api.js'

const PROJECTS_CSS = `
.ca-projects-page { gap: 14px; overflow: auto; padding: 18px; }
.ca-projects-inner { width: 100%; max-width: 1080px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }
.ca-projects-head { display: flex; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.ca-projects-title { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -0.01em; }
.ca-projects-sub { margin: 2px 0 0; font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-projects-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ca-projects-hint { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-bg-layer-2, rgba(127, 127, 127, 0.05)); font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-projects-notice { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border: 1px solid var(--dsw-alias-border-l2, #e5e7eb); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-bg-layer-2, rgba(127, 127, 127, 0.05)); font-size: 12.5px; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-projects-row-active td { background: var(--dsw-alias-button-ghost-active-fill, rgba(127, 127, 127, 0.13)); }
.ca-projects-cell-main { display: flex; flex-direction: column; gap: 0; min-width: 0; }
.ca-projects-name { display: flex; align-items: center; gap: 6px; font-weight: 550; overflow-wrap: anywhere; }
.ca-projects-path { font-family: var(--ds-font-family-code, ui-monospace, monospace); font-size: 10.5px; color: var(--dsw-alias-label-tertiary, #8b949e); overflow-wrap: anywhere; }
.ca-projects-form-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ca-projects-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px 14px; }
.ca-projects-section { display: flex; flex-direction: column; gap: 10px; }
.ca-projects-section-title { display: flex; align-items: center; gap: 8px; margin: 4px 0 0; padding-bottom: 4px; border-bottom: 1px solid var(--dsw-alias-border-l1, #eaeef2); font-size: 12.5px; font-weight: 600; color: var(--dsw-alias-label-secondary, #57606a); }
.ca-projects-form-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ca-projects-danger { color: var(--dsw-alias-state-error-primary, #d1242f); }
`

const BACKEND_OPTIONS = [
  { value: 'local', labelKey: 'projects.backend.local', fallback: 'local' },
  { value: 'remote', labelKey: 'projects.backend.remote', fallback: 'remote (SSH)' },
]

const DB_KIND_OPTIONS = [
  { value: 'db', labelKey: 'projects.field.dbSingle', fallback: 'Single DB file' },
  { value: 'dbDir', labelKey: 'projects.field.dbDir', fallback: 'Shard directory' },
]

const EMPTY_FORM = Object.freeze({
  name: '',
  backend: 'local',
  sourceRoot: '',
  dbKind: 'db',
  dbPath: '',
  compdb: '',
  python: '',
  caRepoPath: '',
  sshHost: '',
  sshUser: '',
  sshPort: '',
  sshKeyPath: '',
})

function asText(value) {
  return value === undefined || value === null ? '' : String(value)
}

function projectToForm(project) {
  const source = project && typeof project === 'object' ? project : {}
  const ssh = source.ssh && typeof source.ssh === 'object' ? source.ssh : {}
  const hasDb = typeof source.db === 'string' && source.db.length > 0
  return {
    name: asText(source.name),
    backend: source.backend === 'remote' ? 'remote' : 'local',
    sourceRoot: asText(source.sourceRoot),
    dbKind: hasDb ? 'db' : 'dbDir',
    dbPath: hasDb ? asText(source.db) : asText(source.dbDir),
    compdb: asText(source.compdb),
    python: asText(source.python),
    caRepoPath: asText(source.caRepoPath),
    sshHost: asText(ssh.host),
    sshUser: asText(ssh.user),
    sshPort: asText(ssh.port),
    sshKeyPath: asText(ssh.keyPath),
  }
}

function isAbsolutePath(path) {
  return typeof path === 'string' && path.startsWith('/')
}

/**
 * Validate the form and build the project payload sent to `projects.save`.
 * Local path existence is validated by the Host manager; this only covers
 * shape and the fields a user can fix in place.
 */
function formToProject(form) {
  const errors = []
  const name = asText(form.name).trim()
  if (!name) errors.push({ field: 'name', messageKey: 'projects.error.nameRequired', fallback: 'Name is required.' })
  const backend = form.backend === 'remote' ? 'remote' : 'local'
  const sourceRoot = asText(form.sourceRoot).trim()
  if (!isAbsolutePath(sourceRoot)) {
    errors.push({
      field: 'sourceRoot',
      messageKey: 'projects.error.sourceRootRequired',
      fallback: 'Source root must be an absolute path.',
    })
  }
  const dbPath = asText(form.dbPath).trim()
  if (!dbPath) {
    errors.push({
      field: 'dbPath',
      messageKey: 'projects.error.databaseRequired',
      fallback: 'Choose a database path (single DB or shard directory).',
    })
  }
  const ssh = { host: asText(form.sshHost).trim(), user: null, port: null, keyPath: null }
  if (backend === 'remote') {
    if (!ssh.host) {
      errors.push({
        field: 'sshHost',
        messageKey: 'projects.error.sshHostRequired',
        fallback: 'SSH host is required for a remote project.',
      })
    }
    const portText = asText(form.sshPort).trim()
    if (portText) {
      const port = Number(portText)
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        errors.push({
          field: 'sshPort',
          messageKey: 'projects.error.portInvalid',
          fallback: 'SSH port must be an integer between 1 and 65535.',
        })
      } else {
        ssh.port = port
      }
    }
    if (asText(form.sshUser).trim()) ssh.user = asText(form.sshUser).trim()
    if (asText(form.sshKeyPath).trim()) ssh.keyPath = asText(form.sshKeyPath).trim()
  }
  if (errors.length > 0) return { ok: false, errors }
  const project = {
    name,
    backend,
    sourceRoot,
    db: form.dbKind === 'db' ? dbPath : null,
    dbDir: form.dbKind === 'dbDir' ? dbPath : null,
    compdb: asText(form.compdb).trim() || null,
    python: asText(form.python).trim() || null,
    caRepoPath: asText(form.caRepoPath).trim() || null,
    ssh: backend === 'remote' ? ssh : null,
  }
  return { ok: true, project }
}

function projectDbLabel(project) {
  if (!project || typeof project !== 'object') return ''
  const db = typeof project.db === 'string' ? project.db : ''
  const dbDir = typeof project.dbDir === 'string' ? project.dbDir : ''
  if (db) return db
  if (dbDir) return dbDir
  return ''
}

function projectDbBasename(project) {
  const path = projectDbLabel(project)
  if (!path) return ''
  const cleaned = path.replace(/[\\/]+$/, '')
  const parts = cleaned.split(/[\\/]/)
  return parts[parts.length - 1] || cleaned
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
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
      try {
        window.dispatchEvent(new CustomEvent('ca:projectschange'))
      } catch (_error) {
        /* the shell refresh is a convenience */
      }
    }
  }

  function ProjectsPage(props) {
    ui.useStyles(PROJECTS_CSS)

    const navigate = props && typeof props.navigate === 'function' ? props.navigate : null

    const [phase, setPhase] = React.useState('loading')
    const [loadError, setLoadError] = React.useState(null)
    const [list, setList] = React.useState(null) // { projects, activeProjectId }
    const [formOpen, setFormOpen] = React.useState(false)
    const [editingId, setEditingId] = React.useState('')
    const [form, setForm] = React.useState(() => Object.assign({}, EMPTY_FORM))
    const [formErrors, setFormErrors] = React.useState([])
    const [busy, setBusy] = React.useState('')
    const [actionError, setActionError] = React.useState(null)
    const [notice, setNotice] = React.useState('')
    const [confirmDeleteId, setConfirmDeleteId] = React.useState('')

    const mountedRef = React.useRef(true)
    const noticeTimer = React.useRef(null)

    React.useEffect(
      () => () => {
        mountedRef.current = false
        if (noticeTimer.current !== null && typeof window !== 'undefined') {
          window.clearTimeout(noticeTimer.current)
        }
      },
      [],
    )

    function showNotice(message) {
      setNotice(message)
      if (noticeTimer.current !== null && typeof window !== 'undefined') {
        window.clearTimeout(noticeTimer.current)
      }
      if (typeof window !== 'undefined') {
        noticeTimer.current = window.setTimeout(() => {
          noticeTimer.current = null
          if (mountedRef.current) setNotice('')
        }, 4500)
      }
    }

    function applyListResult(value) {
      if (value && typeof value === 'object') {
        if (Array.isArray(value.projects)) {
          setList({
            projects: value.projects,
            activeProjectId:
              value.activeProjectId !== undefined ? value.activeProjectId : list ? list.activeProjectId : null,
          })
        }
      }
    }

    async function loadProjects() {
      setPhase((current) => (current === 'ready' ? current : 'loading'))
      setLoadError(null)
      try {
        const value = await api.projects.list()
        if (!mountedRef.current) return
        if (value && typeof value === 'object' && value.ok === false && value.error) {
          setLoadError(toApiError(value.error))
          setPhase('error')
          return
        }
        applyListResult(value)
        setPhase('ready')
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = toApiError(error)
        if (apiError.code === 'ABORTED') return
        setLoadError(apiError)
        setPhase('error')
      }
    }

    React.useEffect(() => {
      void loadProjects()
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    const projects = list && Array.isArray(list.projects) ? list.projects : []
    const activeId = list ? list.activeProjectId : null

    function startCreate() {
      setEditingId('')
      setForm(Object.assign({}, EMPTY_FORM))
      setFormErrors([])
      setActionError(null)
      setFormOpen(true)
    }

    function startEdit(project) {
      setEditingId(String(project.id))
      setForm(projectToForm(project))
      setFormErrors([])
      setActionError(null)
      setFormOpen(true)
    }

    function cancelForm() {
      setFormOpen(false)
      setEditingId('')
      setFormErrors([])
    }

    function setField(field, value) {
      setForm((current) => Object.assign({}, current, { [field]: value }))
      setFormErrors((current) => current.filter((entry) => entry.field !== field))
    }

    function errorFor(field) {
      const entry = formErrors.find((item) => item.field === field)
      if (!entry) return ''
      if (typeof entry.messageKey === 'string' && entry.messageKey) {
        return t(entry.messageKey, null, entry.fallback)
      }
      return entry.fallback || ''
    }

    function describeActionError(apiError) {
      if (apiError.code === 'PROJECT_ACTIVATION_FAILED') {
        apiError.message = `${apiError.message} ${t(
          'projects.activateFailedHint',
          null,
          'The previous configuration was restored; fix the project fields and retry.',
        )}`.trim()
      }
      if (apiError.code === 'BACKEND_UNSUPPORTED') {
        apiError.message = t(
          'projects.unsupported',
          null,
          'This backend build does not support the project model yet; restart or upgrade the CA plugin.',
        )
      }
      return apiError
    }

    async function saveProject() {
      const built = formToProject(form)
      if (!built.ok) {
        setFormErrors(built.errors)
        return
      }
      setBusy('save')
      setActionError(null)
      try {
        const payload = built.project
        if (editingId) payload.id = editingId
        const value = await api.projects.save(payload)
        if (!mountedRef.current) return
        const wasCreating = !editingId
        const savedProject = value && value.project ? value.project : null
        applyListResult(value)
        setFormOpen(false)
        setEditingId('')
        notifyProjectsChanged()
        const hasActive = Boolean(value && value.activeProjectId)
        if (wasCreating && !hasActive && savedProject) {
          // First project: save then activate it right away, like a project picker.
          showNotice(t('projects.activatingFirst', null, 'Project created; activating…'))
          await activateProject(savedProject)
        } else if (savedProject && hasActive && savedProject.id === (value.activeProjectId || '')) {
          showNotice(
            t(
              'projects.savedNeedsActivate',
              null,
              'Project saved. Activate it to apply the new configuration to the backend.',
            ),
          )
        } else {
          showNotice(t('projects.created', null, 'Project saved.'))
        }
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = describeActionError(toApiError(error))
        if (apiError.code === 'ABORTED') return
        const field = apiError.details && typeof apiError.details.field === 'string' ? apiError.details.field : ''
        if (field) {
          const known = ['name', 'backend', 'sourceRoot', 'db', 'dbDir', 'compdb', 'python', 'caRepoPath', 'ssh', 'sshHost', 'id']
          const formField =
            field === 'db' || field === 'dbDir'
              ? 'dbPath'
              : field === 'ssh'
                ? 'sshHost'
                : known.includes(field)
                  ? field
                  : ''
          if (formField) {
            setFormErrors([{ field: formField, messageKey: '', fallback: apiError.message }])
            return
          }
        }
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    async function waitForBackendSettled(timeoutMs) {
      const startedAt = Date.now()
      while (Date.now() - startedAt < (timeoutMs || 30000)) {
        try {
          const status = await api.getStatus()
          if (!mountedRef.current) return null
          const backend = status && status.backend ? status.backend : null
          if (backend && (backend.state === 'running' || backend.state === 'error')) return backend
        } catch (_error) {
          /* keep polling while the backend restarts */
        }
        await new Promise((resolve) => {
          if (typeof window !== 'undefined' && typeof window.setTimeout === 'function') {
            window.setTimeout(resolve, 600)
          } else {
            setTimeout(resolve, 600)
          }
        })
      }
      return null
    }

    async function activateProject(project) {
      if (!project || !project.id) return
      setBusy(`activate:${project.id}`)
      setActionError(null)
      try {
        const value = await api.projects.activate(project.id)
        if (!mountedRef.current) return
        applyListResult(value)
        const backend = await waitForBackendSettled(30000)
        if (!mountedRef.current) return
        if (backend && backend.state === 'running') {
          showNotice(t('projects.activated', null, 'Project activated; reloading…'))
          notifyProjectsChanged()
          if (
            typeof window !== 'undefined' &&
            window.location &&
            typeof window.location.reload === 'function'
          ) {
            window.location.reload()
          }
          return
        }
        setActionError(
          toApiError({
            code: 'BACKEND_ERROR',
            message: t('shell.projectSwitchTimeout', null, 'The backend did not become ready in time.'),
          }),
        )
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = describeActionError(toApiError(error))
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    async function removeProject(project) {
      if (!project || !project.id) return
      if (confirmDeleteId !== String(project.id)) {
        setConfirmDeleteId(String(project.id))
        return
      }
      setBusy(`remove:${project.id}`)
      setActionError(null)
      setConfirmDeleteId('')
      try {
        const value = await api.projects.remove(project.id)
        if (!mountedRef.current) return
        applyListResult(value)
        if (editingId === String(project.id)) {
          setFormOpen(false)
          setEditingId('')
        }
        if (value && value.activeCleared === true) {
          showNotice(
            t(
              'projects.activeCleared',
              null,
              'The active project was deleted; the backend keeps running on the old configuration. Create or activate a project to choose a database.',
            ),
          )
        } else {
          showNotice(t('projects.removed', null, 'Project deleted.'))
        }
        notifyProjectsChanged()
      } catch (error) {
        if (!mountedRef.current) return
        const apiError = describeActionError(toApiError(error))
        if (apiError.code === 'ABORTED') return
        setActionError(apiError)
      } finally {
        if (mountedRef.current) setBusy('')
      }
    }

    function rowActive(project) {
      return Boolean(
        (activeId && project.id === activeId) || project.active === true,
      )
    }

    function renderFormField(field, label, options) {
      const settings = options && typeof options === 'object' ? options : {}
      return h(
        ui.Field,
        {
          key: field,
          label,
          hint: settings.hint,
          error: settings.errorField ? errorFor(settings.errorField) : '',
          optional: settings.optional === true,
        },
        h(ui.Input, {
          value: form[field],
          placeholder: settings.placeholder,
          ariaLabel: label,
          onChange: (event) => setField(field, event.target.value),
        }),
      )
    }

    const columns = [
      {
        key: 'name',
        label: t('projects.field.name', null, 'Name'),
        render: (project) =>
          h(
            'span',
            { className: 'ca-projects-cell-main' },
            h(
              'span',
              { className: 'ca-projects-name' },
              h(ui.Icon, { name: 'folder', size: 13 }),
              projectDisplayName(project),
              rowActive(project)
                ? h(ui.Badge, { tone: 'success' }, t('projects.active', null, 'active'))
                : null,
              project.status && project.status.state
                ? h(
                    ui.Badge,
                    { tone: project.status.state === 'running' ? 'info' : project.status.state === 'error' ? 'error' : 'warn' },
                    t(`shell.backendState.${project.status.state}`, null, String(project.status.state)),
                  )
                : null,
            ),
            project.sourceRoot
              ? h('span', { className: 'ca-projects-path' }, project.sourceRoot)
              : null,
          ),
      },
      {
        key: 'backend',
        label: t('projects.field.backend', null, 'Backend'),
        width: '110px',
        render: (project) =>
          h(
            ui.Badge,
            { tone: project.backend === 'remote' ? 'info' : 'neutral' },
            project.backend === 'remote'
              ? t('projects.backend.remote', null, 'remote (SSH)')
              : t('projects.backend.local', null, 'local'),
          ),
      },
      {
        key: 'database',
        label: t('projects.field.database', null, 'Database'),
        render: (project) =>
          h(
            'span',
            { className: 'ca-projects-cell-main' },
            h(
              'span',
              null,
              projectDbBasename(project) || '—',
              project.dbDir
                ? h(
                    ui.Badge,
                    { tone: 'muted' },
                    t('projects.field.dbDir', null, 'Shard directory'),
                  )
                : null,
            ),
            projectDbLabel(project)
              ? h('span', { className: 'ca-projects-path' }, projectDbLabel(project))
              : null,
          ),
      },
      {
        key: 'actions',
        label: '',
        width: '250px',
        render: (project) => {
          const projectActive = rowActive(project)
          const activating = busy === `activate:${project.id}`
          const removing = busy === `remove:${project.id}`
          const confirming = confirmDeleteId === String(project.id)
          return h(
            'span',
            { className: 'ca-row', style: { gap: 6, flexWrap: 'wrap' } },
            h(
              ui.Button,
              {
                variant: projectActive ? 'secondary' : 'primary',
                size: 'sm',
                disabled: projectActive || busy !== '',
                onClick: () => {
                  void activateProject(project)
                },
              },
              activating ? h(ui.Spinner, { size: 11 }) : null,
              projectActive
                ? t('projects.active', null, 'active')
                : activating
                  ? t('projects.activating', null, 'Activating…')
                  : t('projects.activate', null, 'Activate'),
            ),
            h(
              ui.Button,
              {
                variant: 'ghost',
                size: 'sm',
                disabled: busy !== '',
                onClick: () => startEdit(project),
              },
              t('projects.edit', null, 'Edit'),
            ),
            h(
              ui.Button,
              {
                variant: 'danger',
                size: 'sm',
                disabled: busy !== '',
                title: confirming
                  ? t('projects.deleteConfirm', { name: project.name }, `Delete project “${project.name}”?`)
                  : undefined,
                onClick: () => {
                  void removeProject(project)
                },
              },
              removing
                ? h(ui.Spinner, { size: 11 })
                : confirming
                  ? t('projects.confirmDelete', null, 'Confirm delete')
                  : t('projects.delete', null, 'Delete'),
            ),
          )
        },
      },
    ]

    function projectDisplayName(project) {
      return project && typeof project.name === 'string' && project.name ? project.name : '—'
    }

    const showRemote = form.backend === 'remote'

    const formCard = formOpen
      ? h(
          ui.Card,
          {
            title: editingId
              ? t('projects.formTitleEdit', null, 'Edit project')
              : t('projects.formTitleNew', null, 'New project'),
            subtitle: t(
              'projects.hint',
              null,
              "The generic CA tools read the active project's database; switching projects restarts the backend.",
            ),
            actions: h(
              ui.Button,
              {
                variant: 'ghost',
                size: 'sm',
                disabled: busy !== '',
                onClick: cancelForm,
              },
              h(ui.Icon, { name: 'close', size: 12 }),
              t('projects.cancel', null, 'Cancel'),
            ),
          },
          h(
            'div',
            { className: 'ca-projects-section' },
            h(
              'div',
              { className: 'ca-projects-grid' },
              renderFormField('name', t('projects.field.name', null, 'Name'), {
                hint: t('projects.field.nameHint', null, 'Unique display name, e.g. oh-xv6'),
                errorField: 'name',
              }),
              h(
                ui.Field,
                { key: 'backend', label: t('projects.field.backend', null, 'Backend') },
                h(ui.Select, {
                  value: form.backend,
                  options: BACKEND_OPTIONS.map((option) => ({
                    value: option.value,
                    label: t(option.labelKey, null, option.fallback),
                  })),
                  ariaLabel: t('projects.field.backend', null, 'Backend'),
                  onChange: (value) => setField('backend', value),
                }),
              ),
              renderFormField('sourceRoot', t('projects.field.sourceRoot', null, 'Source root'), {
                hint: t(
                  'projects.field.sourceRootHint',
                  null,
                  'Absolute path of the source tree on the backend host.',
                ),
                placeholder: '/home/user/src/linux',
                errorField: 'sourceRoot',
              }),
              h(
                ui.Field,
                { key: 'dbKind', label: t('projects.field.databaseKind', null, 'Kind') },
                h(ui.Select, {
                  value: form.dbKind,
                  options: DB_KIND_OPTIONS.map((option) => ({
                    value: option.value,
                    label: t(option.labelKey, null, option.fallback),
                  })),
                  ariaLabel: t('projects.field.databaseKind', null, 'Kind'),
                  onChange: (value) => setField('dbKind', value),
                }),
              ),
              renderFormField('dbPath', t('projects.field.dbPath', null, 'Database path'), {
                hint: t(
                  'projects.field.dbHint',
                  null,
                  'Set exactly one of a single DB file or a shard directory.',
                ),
                placeholder: form.dbKind === 'dbDir' ? '/home/user/CA/index' : '/home/user/CA/index/ca.db',
                errorField: 'dbPath',
              }),
              renderFormField('compdb', t('projects.field.compdb', null, 'compile_commands.json'), {
                optional: true,
                placeholder: '/home/user/src/linux/compile_commands.json',
              }),
              renderFormField('python', t('projects.field.python', null, 'Python interpreter'), {
                optional: true,
                placeholder: '/home/user/.venv/bin/python',
              }),
              renderFormField('caRepoPath', t('projects.field.caRepoPath', null, 'CA repository path'), {
                optional: true,
                placeholder: '/home/user/CA',
              }),
            ),
            showRemote
              ? h(
                  'div',
                  { className: 'ca-projects-section' },
                  h(
                    'div',
                    { className: 'ca-projects-section-title' },
                    h(ui.Icon, { name: 'external', size: 13 }),
                    t('projects.section.remote', null, 'Remote SSH'),
                  ),
                  h(
                    'div',
                    { className: 'ca-projects-grid' },
                    renderFormField('sshHost', t('projects.field.sshHost', null, 'SSH host'), {
                      placeholder: 'user@host or ssh alias',
                      errorField: 'sshHost',
                    }),
                    renderFormField('sshUser', t('projects.field.sshUser', null, 'SSH user'), {
                      optional: true,
                    }),
                    renderFormField('sshPort', t('projects.field.sshPort', null, 'SSH port'), {
                      optional: true,
                      placeholder: '22',
                      errorField: 'sshPort',
                    }),
                    renderFormField('sshKeyPath', t('projects.field.sshKeyPath', null, 'SSH private key path'), {
                      optional: true,
                      placeholder: '~/.ssh/id_ed25519',
                    }),
                  ),
                )
              : null,
            h(
              'div',
              { className: 'ca-projects-form-actions' },
              h(
                ui.Button,
                { variant: 'primary', disabled: busy === 'save', onClick: () => void saveProject() },
                busy === 'save' ? h(ui.Spinner, { size: 12 }) : h(ui.Icon, { name: 'check', size: 13 }),
                busy === 'save'
                  ? t('projects.saving', null, 'Saving…')
                  : t('projects.save', null, 'Save project'),
              ),
              h(
                ui.Button,
                { variant: 'ghost', disabled: busy !== '', onClick: cancelForm },
                t('projects.cancel', null, 'Cancel'),
              ),
              errorFor('name') || errorFor('sourceRoot') || errorFor('dbPath') || errorFor('sshHost') || errorFor('sshPort')
                ? h(
                    'span',
                    { className: 'ca-projects-danger', style: { fontSize: 12 } },
                    t('projects.formErrorHint', null, 'Fix the highlighted fields before saving.'),
                  )
                : null,
            ),
          ),
        )
      : null

    if (phase === 'loading') {
      return h(
        'div',
        { className: 'ca-page ca-projects-page' },
        h(
          'div',
          { className: 'ca-projects-inner' },
          h(ui.Spinner, { size: 18, label: t('common.loading', null, 'loading…') }),
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
            'p',
            { className: 'ca-projects-sub' },
            t(
              'projects.subtitle',
              null,
              'A project binds a source tree, a database (single DB or shard directory) and a backend target.',
            ),
          ),
          h('span', { className: 'ca-spacer' }),
          h(
            'div',
            { className: 'ca-projects-actions' },
            h(
              ui.Button,
              {
                variant: 'ghost',
                size: 'sm',
                disabled: busy !== '',
                onClick: () => void loadProjects(),
              },
              h(ui.Icon, { name: 'refresh', size: 13 }),
              t('projects.refresh', null, 'Refresh'),
            ),
            h(
              ui.Button,
              {
                variant: 'primary',
                size: 'sm',
                disabled: busy !== '',
                onClick: startCreate,
              },
              h(ui.Icon, { name: 'folder', size: 13 }),
              t('projects.new', null, 'New project'),
            ),
            navigate
              ? h(
                  ui.Button,
                  {
                    variant: 'ghost',
                    size: 'sm',
                    onClick: () => navigate({ page: 'backend' }),
                  },
                  h(ui.Icon, { name: 'backend', size: 13 }),
                  t('shell.nav.backend', null, 'Backend'),
                )
              : null,
          ),
        ),
        h(
          'div',
          { className: 'ca-projects-hint' },
          h(ui.Icon, { name: 'info', size: 13 }),
          h(
            'span',
            null,
            t(
              'projects.hint',
              null,
              "The generic CA tools read the active project's database; switching projects restarts the backend.",
            ),
          ),
        ),
        phase === 'error' && loadError
          ? h(ui.ErrorBox, {
              title: t('projects.loadFailed', null, 'Could not load projects'),
              error: loadError,
              retryLabel: t('common.retry', null, 'Retry'),
              onRetry: () => void loadProjects(),
            })
          : null,
        actionError
          ? h(ui.ErrorBox, {
              title: t('projects.actionFailed', null, 'Project action failed'),
              error: actionError,
              retryLabel: t('common.close', null, 'Close'),
              onRetry: () => setActionError(null),
            })
          : null,
        notice ? h('div', { className: 'ca-projects-notice' }, h(ui.Icon, { name: 'check', size: 13 }), notice) : null,
        formCard,
        h(
          ui.Card,
          {
            title: t('projects.listTitle', null, 'Saved projects'),
            subtitle: activeId
              ? t('projects.activeHint', { name: (projects.find((project) => project.id === activeId) || {}).name || activeId }, `Active: ${activeId}`)
              : t('projects.noActive', null, 'No active project'),
            actions: h(ui.Badge, { tone: 'neutral' }, t('projects.count', { count: projects.length }, `${projects.length} projects`)),
          },
          projects.length === 0
            ? h(ui.EmptyState, {
                icon: h(ui.Icon, { name: 'folder', size: 26 }),
                title: t('projects.empty', null, 'No projects yet'),
                body: t(
                  'projects.emptyBody',
                  null,
                  'Create a project to choose the source tree and database this plugin should serve.',
                ),
                action: h(
                  ui.Button,
                  { variant: 'primary', onClick: startCreate },
                  h(ui.Icon, { name: 'folder', size: 13 }),
                  t('projects.new', null, 'New project'),
                ),
              })
            : h(ui.Table, {
                columns,
                rows: projects,
                rowKey: (project) => String(project.id),
                rowClassName: (project) => (rowActive(project) ? 'ca-projects-row-active' : undefined),
                selectedKey: activeId ? String(activeId) : undefined,
                empty: t('projects.empty', null, 'No projects yet'),
              }),
        ),
      ),
    )
  }

  ProjectsPage.displayName = 'CaProjectsPage'
  return ProjectsPage
}

export default createProjectsPage
