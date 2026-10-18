/**
 * In-page DOM self test for the CA panel.
 *
 * The deployment has no browser automation, so the plugin verifies its own
 * rendered UI: when the Host reports `ui.selftest.pending` (a marker file the
 * Lead creates), the Client opens the CA panel, walks every hash route, checks
 * the projects page/form validation, opens the top-bar project selector,
 * collects window errors, and submits a report through `ui.selftest.report`
 * (the Host writes `selftest-report.json` next to the pending marker).
 *
 * Design notes:
 *  - all DOM/browser access goes through an adapter object (`adapters`), so the
 *    step orchestration is testable with a fake DOM in node:test;
 *  - `sessionStorage['ca.selftest.done']` prevents a second run in the same
 *    tab; the marker is written before the run (running) and after (done);
 *  - the test never creates, edits or deletes real data: the projects form is
 *    opened and submitted with an empty name, which the Client-side validation
 *    rejects before any RPC;
 *  - the hash is restored to `#/search` and the CA panel stays open.
 */

/** Host RPC endpoint names. */
export const SELFTEST_PENDING_ENDPOINT = 'ui.selftest.pending'
export const SELFTEST_REPORT_ENDPOINT = 'ui.selftest.report'

/** sessionStorage key that prevents duplicate runs in one browser tab. */
export const SELFTEST_DONE_KEY = 'ca.selftest.done'

/** Root selector of the mounted CA application. */
export const PANEL_SELECTOR = '.ca-app'

/** Every route is switched through `location.hash` and asserted by root class. */
export const ROUTE_CHECKS = Object.freeze([
  { hash: '#/search', selector: '.ca-search-page' },
  { hash: '#/stats', selector: '.ca-stats-page' },
  { hash: '#/callbacks', selector: '.ca-callbacks-page' },
  { hash: '#/graph', selector: '.ca-graph-page' },
  { hash: '#/tasks', selector: '.ca-tasks-page' },
  { hash: '#/backend', selector: '.ca-backend-page' },
  { hash: '#/projects', selector: '.ca-projects-page' },
])

/** Visible button texts by language (the active locale decides one of them). */
const BUTTON_TEXT = Object.freeze({
  newProject: ['New project', '新建项目'],
  save: ['Save project', '保存项目'],
  cancel: ['Cancel', '取消'],
})

const WAIT_PANEL_MS = 5000
const WAIT_ROUTE_MS = 5000
const WAIT_DETAIL_MS = 3000
const POLL_MS = 50

function errorText(error) {
  if (error === undefined || error === null) return 'unknown error'
  if (typeof error === 'string') return error
  if (typeof error.message === 'string' && error.message) return error.message
  try {
    return JSON.stringify(error)
  } catch (_error) {
    return String(error)
  }
}

function stringifyArg(value) {
  if (value === undefined) return 'undefined'
  if (value === null) return 'null'
  if (typeof value === 'string') return value
  if (value instanceof Error) return `${value.name}: ${value.message}`
  try {
    return JSON.stringify(value)
  } catch (_error) {
    return String(value)
  }
}

/** Active project display name from the shell snapshot ('' when unknown). */
function activeProjectName(shell) {
  try {
    const snapshot = shell && typeof shell.getSnapshot === 'function' ? shell.getSnapshot() : null
    if (!snapshot || typeof snapshot !== 'object') return ''
    const status = snapshot.status
    if (status && status.project && typeof status.project.name === 'string') return status.project.name
    const projects = snapshot.projects
    if (projects && Array.isArray(projects.projects)) {
      const activeId = projects.activeProjectId
      const active =
        (activeId && projects.projects.find((project) => project && project.id === activeId)) ||
        projects.projects.find((project) => project && project.active === true)
      if (active && typeof active.name === 'string') return active.name
    }
  } catch (_error) {
    /* the snapshot is best effort */
  }
  return ''
}

/** Find the first button whose visible text matches one of the labels. */
function findButton(adapters, labels) {
  let buttons = []
  try {
    buttons = adapters.queryAll('button') || []
  } catch (_error) {
    return null
  }
  for (const button of buttons) {
    if (!button) continue
    const text = typeof button.textContent === 'string' ? button.textContent : ''
    if (labels.some((label) => text.includes(label))) return button
  }
  return null
}

function viewportOf(adapters) {
  try {
    if (adapters && typeof adapters.viewport === 'function') return adapters.viewport()
  } catch (_error) {
    /* ignore */
  }
  return { width: 0, height: 0 }
}

/**
 * Build the real browser adapter object.
 *
 * @param {object} ctx Client plugin context (`ctx.layout.selectPanel`).
 * @param {object} [overrides] test seams: { window, document, navigator }.
 * @returns {object|null} null outside a browser.
 */
export function createBrowserAdapters(ctx, overrides) {
  if (typeof document === 'undefined' || typeof window === 'undefined') return null
  const settings = overrides && typeof overrides === 'object' ? overrides : {}
  const doc = settings.document || document
  const win = settings.window || window
  const nav =
    settings.navigator || (typeof navigator !== 'undefined' && navigator ? navigator : null)

  function query(selector) {
    try {
      return doc.querySelector(selector)
    } catch (_error) {
      return null
    }
  }

  function queryAll(selector) {
    try {
      return Array.from(doc.querySelectorAll(selector))
    } catch (_error) {
      return []
    }
  }

  function delay(ms) {
    return new Promise((resolve) => {
      try {
        win.setTimeout(resolve, ms)
      } catch (_error) {
        setTimeout(resolve, ms)
      }
    })
  }

  return {
    isBrowser: true,
    el: doc,
    selectPanel() {
      const layout = ctx && ctx.layout
      if (layout && typeof layout.selectPanel === 'function') layout.selectPanel('ca')
    },
    query,
    queryAll,
    text() {
      try {
        return doc.body && typeof doc.body.textContent === 'string' ? doc.body.textContent : ''
      } catch (_error) {
        return ''
      }
    },
    setHash(hash) {
      try {
        win.location.hash = hash
      } catch (_error) {
        /* location may be locked down */
      }
    },
    getHash() {
      try {
        return win.location.hash || ''
      } catch (_error) {
        return ''
      }
    },
    click(target) {
      const element = typeof target === 'string' ? query(target) : target
      if (!element) return false
      try {
        if (typeof element.click === 'function') {
          element.click()
          return true
        }
      } catch (_error) {
        /* fall through to a dispatched MouseEvent */
      }
      try {
        const MouseEventCtor = win.MouseEvent || (typeof MouseEvent !== 'undefined' ? MouseEvent : null)
        if (MouseEventCtor) {
          element.dispatchEvent(new MouseEventCtor('click', { bubbles: true, cancelable: true }))
          return true
        }
      } catch (_error) {
        /* not clickable */
      }
      return false
    },
    dispatchEscape() {
      try {
        const KeyboardEventCtor =
          win.KeyboardEvent || (typeof KeyboardEvent !== 'undefined' ? KeyboardEvent : null)
        if (KeyboardEventCtor) {
          doc.dispatchEvent(new KeyboardEventCtor('keydown', { key: 'Escape', bubbles: true }))
          return true
        }
      } catch (_error) {
        /* ignore */
      }
      return false
    },
    now() {
      return Date.now()
    },
    ua() {
      try {
        return nav && typeof nav.userAgent === 'string' ? nav.userAgent : ''
      } catch (_error) {
        return ''
      }
    },
    viewport() {
      try {
        return { width: Number(win.innerWidth) || 0, height: Number(win.innerHeight) || 0 }
      } catch (_error) {
        return { width: 0, height: 0 }
      }
    },
    delay,
    async waitFor(selector, timeoutMs) {
      const deadline = Date.now() + (Number(timeoutMs) > 0 ? Number(timeoutMs) : WAIT_ROUTE_MS)
      for (;;) {
        if (query(selector)) return true
        if (Date.now() > deadline) return Boolean(query(selector))
        await delay(POLL_MS)
      }
    },
    beginErrorCapture() {
      const entries = []
      const onError = (event) => {
        entries.push({
          kind: 'error',
          message: event && event.message ? String(event.message) : 'window error',
          stack:
            event && event.error && typeof event.error.stack === 'string'
              ? String(event.error.stack).slice(0, 2000)
              : '',
        })
      }
      const onRejection = (event) => {
        const reason = event && event.reason
        entries.push({
          kind: 'unhandledrejection',
          message: reason instanceof Error ? `${reason.name}: ${reason.message}` : stringifyArg(reason),
          stack: reason instanceof Error && typeof reason.stack === 'string' ? reason.stack.slice(0, 2000) : '',
        })
      }
      const originalConsoleError = console.error
      const patchedConsoleError = (...args) => {
        entries.push({ kind: 'console.error', message: args.map(stringifyArg).join(' ').slice(0, 2000) })
        try {
          originalConsoleError.apply(console, args)
        } catch (_error) {
          /* console may be unavailable */
        }
      }
      try {
        win.addEventListener('error', onError)
        win.addEventListener('unhandledrejection', onRejection)
      } catch (_error) {
        /* listeners are best effort */
      }
      try {
        console.error = patchedConsoleError
      } catch (_error) {
        /* ignore */
      }
      return {
        stop() {
          try {
            win.removeEventListener('error', onError)
            win.removeEventListener('unhandledrejection', onRejection)
          } catch (_error) {
            /* ignore */
          }
          try {
            console.error = originalConsoleError
          } catch (_error) {
            /* ignore */
          }
          return entries
        },
      }
    },
  }
}

/** Safe sessionStorage accessor (private mode may throw). */
function safeSessionStorage(overrides) {
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, 'storage')) {
    return overrides.storage
  }
  try {
    if (typeof window !== 'undefined' && window.sessionStorage) return window.sessionStorage
  } catch (_error) {
    /* storage can be blocked */
  }
  return null
}

function readMarker(storage) {
  if (!storage || typeof storage.getItem !== 'function') return ''
  try {
    return String(storage.getItem(SELFTEST_DONE_KEY) || '')
  } catch (_error) {
    return ''
  }
}

function writeMarker(storage, value) {
  if (!storage || typeof storage.setItem !== 'function') return
  try {
    storage.setItem(SELFTEST_DONE_KEY, value)
  } catch (_error) {
    /* best effort */
  }
}

/**
 * Run the DOM self test through an adapter and submit the report.
 *
 * @param {{ api: object, shell?: object, adapters: object }} options
 * @returns {Promise<{ran: boolean, ok: boolean, report: object, submitted: boolean, submitError: string}>}
 */
export async function runSelfTest(options) {
  const settings = options && typeof options === 'object' ? options : {}
  const api = settings.api
  const adapters = settings.adapters
  const shell = settings.shell || null
  if (!api || typeof api.rpc !== 'function') {
    throw new TypeError('runSelfTest({ api }) requires api.rpc')
  }
  if (!adapters || typeof adapters !== 'object') {
    throw new TypeError('runSelfTest({ adapters }) requires DOM adapters')
  }

  const steps = []
  const routeChecks = []
  const startedMs = adapters.now()
  const startedAt = new Date().toISOString()
  const capture =
    typeof adapters.beginErrorCapture === 'function'
      ? adapters.beginErrorCapture()
      : { stop: () => [] }

  async function step(name, run) {
    const stepStarted = adapters.now()
    try {
      const detail = await run()
      steps.push({
        name,
        ok: true,
        ms: Math.max(0, adapters.now() - stepStarted),
        detail: detail === undefined || detail === null ? '' : String(detail),
      })
      return true
    } catch (error) {
      steps.push({
        name,
        ok: false,
        ms: Math.max(0, adapters.now() - stepStarted),
        detail: errorText(error),
      })
      return false
    }
  }

  let panelReady = false
  let fatal = ''
  try {
    panelReady = await step('panel.open', async () => {
      // The main slot registration lands slightly after apply(); retry the panel
      // selection while waiting for the application root to mount.
      const deadline = adapters.now() + WAIT_PANEL_MS
      let lastError = ''
      for (;;) {
        try {
          adapters.selectPanel()
        } catch (error) {
          lastError = errorText(error)
        }
        if (await adapters.waitFor(PANEL_SELECTOR, 250)) return 'panel visible'
        if (adapters.now() >= deadline) {
          throw new Error(
            `panel selector ${PANEL_SELECTOR} not visible within ${WAIT_PANEL_MS}ms${
              lastError ? `: ${lastError}` : ''
            }`,
          )
        }
      }
    })

    if (panelReady) {
      for (const route of ROUTE_CHECKS) {
        const routeStarted = adapters.now()
        let ok = false
        let detail = ''
        try {
          adapters.setHash(route.hash)
          ok = await adapters.waitFor(route.selector, WAIT_ROUTE_MS)
          if (!ok) detail = `selector ${route.selector} not visible`
        } catch (error) {
          detail = errorText(error)
        }
        const ms = Math.max(0, adapters.now() - routeStarted)
        routeChecks.push({ route: route.hash, selector: route.selector, ok, ms, detail })
        steps.push({ name: `route ${route.hash}`, ok, ms, detail })
      }

      // The project route is where the main data model is exercised.
      if (adapters.query('.ca-projects-page')) {
        const projectName = activeProjectName(shell)
        await step('projects.list', async () => {
          const rows = adapters.queryAll('.ca-projects-page .ca-table tbody tr')
          const emptyState = adapters.query('.ca-projects-page .ca-empty')
          if (rows.length === 0 && emptyState) return 'no projects yet (empty state)'
          if (rows.length === 0) throw new Error('no project rows rendered and no empty state')
          if (projectName && !adapters.text().includes(projectName)) {
            throw new Error(`active project "${projectName}" is not visible in the list`)
          }
          return `rows=${rows.length}${projectName ? ` active=${projectName}` : ''}`
        })

        const newProjectButton = findButton(adapters, BUTTON_TEXT.newProject)
        let formOpen = false
        formOpen = await step('projects.form.open', async () => {
          if (!newProjectButton) throw new Error('New project button not found')
          if (!adapters.click(newProjectButton)) throw new Error('could not click New project')
          const visible = await adapters.waitFor('.ca-projects-page .ca-projects-grid', WAIT_DETAIL_MS)
          if (!visible) throw new Error('project form did not open')
          const inputs = adapters.queryAll('.ca-projects-page .ca-projects-grid input')
          if (inputs.length < 4) throw new Error(`expected at least 4 form inputs, found ${inputs.length}`)
          return `inputs=${inputs.length}`
        })

        if (formOpen) {
          await step('projects.form.validation', async () => {
            const saveButton = findButton(adapters, BUTTON_TEXT.save)
            if (!saveButton) throw new Error('Save project button not found')
            if (!adapters.click(saveButton)) throw new Error('could not click Save project')
            const visible = await adapters.waitFor('.ca-projects-page .ca-field-error', WAIT_DETAIL_MS)
            if (!visible) throw new Error('empty name did not show a field validation error')
            const errors = adapters.queryAll('.ca-projects-page .ca-field-error')
            return `validationErrors=${errors.length}`
          })
          await step('projects.form.close', async () => {
            const cancelButton = findButton(adapters, BUTTON_TEXT.cancel)
            if (cancelButton) adapters.click(cancelButton)
            return 'form closed'
          })
        }
      } else {
        steps.push({ name: 'projects.list', ok: false, ms: 0, detail: '.ca-projects-page missing' })
      }

      await step('project.selector', async () => {
        const trigger = adapters.query('.ca-project-trigger')
        if (!trigger) throw new Error('project selector trigger not found')
        if (!adapters.click(trigger)) throw new Error('could not open the project selector')
        const opened = await adapters.waitFor('.ca-project-dropdown', WAIT_DETAIL_MS)
        if (!opened) throw new Error('project selector dropdown did not open')
        const name = activeProjectName(shell)
        const selectorText = adapters.text()
        if (typeof adapters.dispatchEscape === 'function') adapters.dispatchEscape()
        if (name && !selectorText.includes(name)) {
          throw new Error(`active project "${name}" is not listed in the selector`)
        }
        return name ? `project=${name}` : 'dropdown opened'
      })
    }
  } catch (error) {
    fatal = errorText(error)
    steps.push({ name: 'selftest', ok: false, ms: 0, detail: fatal })
  } finally {
    // Always leave the app on the search route and the panel open.
    await step('hash.restore', async () => {
      adapters.setHash('#/search')
      const visible = await adapters.waitFor('.ca-search-page', WAIT_DETAIL_MS)
      if (!visible) throw new Error('search page did not render after restoring the hash')
      return '#/search'
    })
  }

  const captured = capture && typeof capture.stop === 'function' ? capture.stop() : []
  const fatalErrors = captured.filter((entry) => entry && entry.kind !== 'console.error')
  const ok =
    !fatal &&
    panelReady &&
    steps.every((entry) => entry.ok) &&
    routeChecks.every((entry) => entry.ok) &&
    fatalErrors.length === 0
  const finishedMs = adapters.now()
  const finishedAt = new Date().toISOString()
  const checks = steps.map((entry) => ({
    id: entry.name,
    status: entry.ok ? 'pass' : 'fail',
    detail: entry.detail,
  }))
  const summary = {
    pass: checks.filter((entry) => entry.status === 'pass').length,
    fail: checks.filter((entry) => entry.status === 'fail').length,
    skip: 0,
  }
  const report = {
    page: 'ca',
    ok,
    steps,
    routeChecks,
    errors: captured.map((entry) => `${entry.kind}: ${entry.message}`),
    errorDetails: captured,
    ua: typeof adapters.ua === 'function' ? adapters.ua() : '',
    at: finishedAt,
    startedAt,
    finishedAt,
    durationMs: Math.max(0, finishedMs - startedMs),
    viewport: viewportOf(adapters),
    checks,
    summary,
  }

  let submitted = false
  let submitError = ''
  try {
    await api.rpc(SELFTEST_REPORT_ENDPOINT, { report })
    submitted = true
  } catch (error) {
    submitError = errorText(error)
    try {
      console.error('[ca] selftest report failed:', submitError)
    } catch (_error) {
      /* console may be unavailable */
    }
  }

  return { ran: true, ok, report, submitted, submitError }
}

/**
 * Entry point called from the Client `apply`: run the DOM self test when the
 * Host asks for it (`ui.selftest.pending`) and this tab has not run it yet.
 *
 * @param {object} React React namespace (unused; part of the Client contract).
 * @param {object} ctx Client plugin context.
 * @param {object} shell Result of `createShell()` (for the active project name).
 * @param {{ api: object }} bag Client services.
 * @param {object} [overrides] Test seams: { adapters, storage, layout }.
 * @returns {Promise<{ran: boolean, reason?: string}>}
 */
export async function maybeRunSelfTest(React, ctx, shell, bag, overrides) {
  void React
  const settings = overrides && typeof overrides === 'object' ? overrides : {}
  const api = bag && bag.api
  if (!api || typeof api.rpc !== 'function') return { ran: false, reason: 'no-api' }

  const storage = safeSessionStorage(settings)
  if (readMarker(storage)) return { ran: false, reason: 'already-ran' }

  let adapters = settings.adapters || null
  if (!adapters) {
    adapters = createBrowserAdapters(ctx, settings)
    if (!adapters) return { ran: false, reason: 'no-browser' }
  }

  let pendingValue = null
  try {
    pendingValue = await api.rpc(SELFTEST_PENDING_ENDPOINT, {})
  } catch (error) {
    return { ran: false, reason: 'pending-unavailable', error: errorText(error) }
  }
  if (!pendingValue || pendingValue.pending !== true) return { ran: false, reason: 'not-pending' }

  writeMarker(storage, `running:${Date.now()}`)
  let result
  try {
    result = await runSelfTest({ api, shell, adapters })
  } catch (error) {
    result = { ran: true, ok: false, report: null, submitted: false, submitError: errorText(error) }
  }
  writeMarker(storage, `done:${Date.now()}`)
  return Object.assign(
    { ran: true, reason: 'pending' },
    result,
  )
}

export default maybeRunSelfTest
