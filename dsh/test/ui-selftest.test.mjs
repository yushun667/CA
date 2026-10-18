/**
 * UI self-test orchestration tests (fake DOM, no browser).
 *
 * `lib/client/selftest.js` runs a real DOM walk inside the Harness page, but the
 * orchestration is written against an adapter object. These tests drive it with
 * a fake adapter and a fake `api.rpc`, covering:
 *   - the `ui.selftest.pending` gate (false / true / unavailable);
 *   - the sessionStorage duplicate-run guard;
 *   - report submission shape (`ok`, `steps`, `routeChecks`, `errors`, `ua`,
 *     `at`) plus the Host-friendly aliases (`checks`, `summary`);
 *   - window error capture turning the report into a failure;
 *   - a route that fails to render and a project-form validation that never
 *     appears;
 *   - report submission failure, and the hash restore after a failed run.
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  PANEL_SELECTOR,
  ROUTE_CHECKS,
  SELFTEST_DONE_KEY,
  SELFTEST_PENDING_ENDPOINT,
  SELFTEST_REPORT_ENDPOINT,
  createBrowserAdapters,
  maybeRunSelfTest,
} from '../lib/client/selftest.js'

const PROJECT_NAME = 'xv6'

function fakeShell() {
  return {
    getSnapshot() {
      return {
        status: { project: { id: 'p1', name: PROJECT_NAME } },
        projects: {
          activeProjectId: 'p1',
          projects: [{ id: 'p1', name: PROJECT_NAME, active: true }],
        },
      }
    },
  }
}

function fakeStorage(initial) {
  const data = Object.assign({}, initial)
  return {
    data,
    getItem(key) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null
    },
    setItem(key, value) {
      data[key] = String(value)
    },
    removeItem(key) {
      delete data[key]
    },
  }
}

function fakeApi(options) {
  const settings = options && typeof options === 'object' ? options : {}
  const calls = []
  return {
    calls,
    async rpc(endpoint, payload) {
      calls.push({ endpoint, payload })
      if (endpoint === SELFTEST_PENDING_ENDPOINT) {
        if (settings.pendingError) throw settings.pendingError
        return { pending: settings.pending === true }
      }
      if (endpoint === SELFTEST_REPORT_ENDPOINT) {
        if (settings.reportError) throw settings.reportError
        return { saved: true, path: '/tmp/selftest-report.json', pendingCleared: true, bytes: 1024 }
      }
      throw new Error(`unexpected endpoint ${endpoint}`)
    },
  }
}

function element(tagName, textContent) {
  return { tagName, textContent: textContent || '' }
}

/**
 * Fake DOM adapter. `options` scripts the page:
 *   panelVisible:false, failRoute:'#/stats', noProjects:true,
 *   validationMissing:true, capturedErrors:[...]
 */
function fakeAdapters(options) {
  const settings = options && typeof options === 'object' ? options : {}
  const routeSelectors = new Map(ROUTE_CHECKS.map((route) => [route.hash, route.selector]))
  const state = {
    hash: '#/search',
    hashHistory: [],
    panelSelected: false,
    panelAttempts: 0,
    formOpen: false,
    validationShown: false,
    dropdownOpen: false,
    clicks: [],
    clock: 0,
  }
  const buttons = [
    { kind: 'new-project', textContent: 'New project' },
    { kind: 'save', textContent: 'Save project' },
    { kind: 'cancel', textContent: 'Cancel' },
  ]
  const adapters = {
    state,
    isBrowser: true,
    selectPanel() {
      state.panelAttempts += 1
      state.panelSelected = true
    },
    query(selector) {
      if (selector === PANEL_SELECTOR) return state.panelSelected && settings.panelVisible !== false ? element('div') : null
      if (selector === '.ca-projects-page') {
        return state.hash === '#/projects' && settings.failRoute !== '#/projects' ? element('div') : null
      }
      if (selector === '.ca-projects-page .ca-empty') {
        return settings.noProjects === true && state.hash === '#/projects' ? element('div') : null
      }
      if (selector === '.ca-projects-page .ca-projects-grid') return state.formOpen ? element('div') : null
      if (selector === '.ca-projects-page .ca-field-error') return state.validationShown ? element('span') : null
      if (selector === '.ca-project-trigger') return state.hash !== '#/projects' || !settings.failRoute ? element('button') : null
      if (selector === '.ca-project-dropdown') return state.dropdownOpen ? element('div') : null
      const expected = routeSelectors.get(state.hash)
      if (selector === expected) return settings.failRoute === state.hash ? null : element('div')
      return null
    },
    queryAll(selector) {
      if (selector === 'button') return buttons
      if (selector === '.ca-projects-page .ca-table tbody tr') {
        if (settings.noProjects === true) return []
        return state.hash === '#/projects' ? [element('tr')] : []
      }
      if (selector === '.ca-projects-page .ca-projects-grid input') {
        return state.formOpen ? Array.from({ length: 6 }, () => element('input')) : []
      }
      if (selector === '.ca-projects-page .ca-field-error') {
        return state.validationShown ? [element('span')] : []
      }
      return []
    },
    text() {
      const parts = ['CA source explorer']
      if (state.hash === '#/projects') parts.push('Saved projects')
      if (settings.noProjects !== true) parts.push(PROJECT_NAME)
      if (state.dropdownOpen) parts.push(PROJECT_NAME, 'Manage projects')
      return parts.join(' ')
    },
    setHash(hash) {
      state.hash = hash
      state.hashHistory.push(hash)
      state.formOpen = false
      state.validationShown = false
      state.dropdownOpen = false
    },
    getHash() {
      return state.hash
    },
    click(target) {
      const node = typeof target === 'string' ? adapters.query(target) : target
      if (!node) return false
      state.clicks.push(node.kind || node.tagName || 'element')
      if (node.kind === 'new-project') state.formOpen = true
      else if (node.kind === 'save') {
        if (settings.validationMissing !== true) state.validationShown = true
      } else if (node.kind === 'cancel') state.formOpen = false
      else if (node.tagName === 'button') state.dropdownOpen = true
      return true
    },
    dispatchEscape() {
      state.dropdownOpen = false
    },
    now() {
      state.clock += 5
      return state.clock
    },
    ua() {
      return 'fake-ua/1.0'
    },
    viewport() {
      return { width: 1280, height: 800 }
    },
    delay() {
      return Promise.resolve()
    },
    async waitFor(selector) {
      if (selector === PANEL_SELECTOR && settings.panelVisible === false) return false
      if (settings.failRoute && selector === routeSelectors.get(settings.failRoute) && state.hash === settings.failRoute) {
        return false
      }
      return Boolean(adapters.query(selector))
    },
    beginErrorCapture() {
      return { stop: () => (Array.isArray(settings.capturedErrors) ? settings.capturedErrors : []) }
    },
  }
  return adapters
}

function reportCalls(api) {
  return api.calls.filter((call) => call.endpoint === SELFTEST_REPORT_ENDPOINT)
}

test('pending=false: the self test does not run', async () => {
  const api = fakeApi({ pending: false })
  const storage = fakeStorage()
  const adapters = fakeAdapters()
  const result = await maybeRunSelfTest(null, { layout: { selectPanel() {} } }, fakeShell(), { api }, {
    adapters,
    storage,
  })
  assert.equal(result.ran, false)
  assert.equal(result.reason, 'not-pending')
  assert.equal(adapters.state.panelSelected, false)
  assert.equal(reportCalls(api).length, 0)
  assert.equal(storage.getItem(SELFTEST_DONE_KEY), null)
})

test('pending=true: runs the DOM walk and submits the report', async () => {
  const api = fakeApi({ pending: true })
  const storage = fakeStorage()
  const adapters = fakeAdapters()
  const result = await maybeRunSelfTest(null, { layout: { selectPanel() {} } }, fakeShell(), { api }, {
    adapters,
    storage,
  })
  assert.equal(result.ran, true)
  assert.equal(result.ok, true, `expected ok report, steps: ${JSON.stringify(result.report && result.report.steps)}`)
  assert.equal(result.submitted, true)
  assert.equal(adapters.state.panelSelected, true)
  assert.ok(adapters.state.panelAttempts >= 1)
  assert.equal(adapters.state.hashHistory[adapters.state.hashHistory.length - 1], '#/search')

  const calls = reportCalls(api)
  assert.equal(calls.length, 1)
  const report = calls[0].payload.report
  assert.equal(report.ok, true)
  assert.equal(report.page, 'ca')
  assert.equal(report.ua, 'fake-ua/1.0')
  assert.ok(Number.isFinite(report.durationMs))
  assert.ok(!Number.isNaN(Date.parse(report.at)))
  assert.ok(!Number.isNaN(Date.parse(report.startedAt)))
  assert.deepEqual(report.viewport, { width: 1280, height: 800 })
  assert.equal(report.routeChecks.length, ROUTE_CHECKS.length)
  assert.deepEqual(
    report.routeChecks.map((entry) => entry.route),
    ROUTE_CHECKS.map((entry) => entry.hash),
  )
  assert.ok(report.routeChecks.every((entry) => entry.ok))

  const names = report.steps.map((entry) => entry.name)
  for (const expected of [
    'panel.open',
    'route #/search',
    'route #/projects',
    'projects.list',
    'projects.form.open',
    'projects.form.validation',
    'projects.form.close',
    'project.selector',
    'hash.restore',
  ]) {
    assert.ok(names.includes(expected), `missing step ${expected}`)
  }
  assert.ok(report.steps.every((entry) => typeof entry.ms === 'number' && entry.ok === true))
  assert.deepEqual(report.errors, [])
  assert.equal(report.checks.length, report.steps.length)
  assert.equal(report.summary.fail, 0)
  assert.equal(report.summary.pass, report.steps.length)

  // duplicate-run guard
  const marker = storage.getItem(SELFTEST_DONE_KEY)
  assert.equal(typeof marker, 'string')
  assert.ok(marker.startsWith('done:'), `marker=${marker}`)
})

test('a new pending request re-runs despite a stale done marker', async () => {
  // The Host deletes selftest-pending.json after a report, so a pending marker
  // is authoritative; a done marker left in sessionStorage by an earlier page
  // load must not suppress a newly requested run.
  const api = fakeApi({ pending: true })
  const storage = fakeStorage({ [SELFTEST_DONE_KEY]: 'done:1234' })
  const adapters = fakeAdapters()
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, true)
  assert.equal(result.reason, 'pending')
  assert.equal(api.calls.length > 0, true)
  assert.match(String(storage.getItem(SELFTEST_DONE_KEY)), /^done:/)
})

test('pending endpoint failure is non-fatal and does not mark the run', async () => {
  const api = fakeApi({ pendingError: new Error('TRANSPORT: HTTP 404') })
  const storage = fakeStorage()
  const adapters = fakeAdapters()
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, false)
  assert.equal(result.reason, 'pending-unavailable')
  assert.equal(adapters.state.panelSelected, false)
  assert.equal(storage.getItem(SELFTEST_DONE_KEY), null)
})

test('captured window errors fail the report but keep the steps', async () => {
  const api = fakeApi({ pending: true })
  const storage = fakeStorage()
  const adapters = fakeAdapters({
    capturedErrors: [
      { kind: 'error', message: 'Uncaught boom', stack: 'at foo' },
      { kind: 'console.error', message: 'react warning' },
    ],
  })
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, true)
  assert.equal(result.ok, false)
  assert.equal(result.report.errors.length, 2)
  assert.ok(result.report.errors[0].includes('Uncaught boom'))
  assert.equal(result.report.errorDetails.length, 2)
  assert.equal(result.report.summary.fail, 0, 'steps themselves passed')
  assert.equal(result.submitted, true)
})

test('a route that does not render is reported as a failed route check', async () => {
  const api = fakeApi({ pending: true })
  const storage = fakeStorage()
  const adapters = fakeAdapters({ failRoute: '#/stats' })
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, true)
  assert.equal(result.ok, false)
  const stats = result.report.routeChecks.find((entry) => entry.route === '#/stats')
  assert.ok(stats)
  assert.equal(stats.ok, false)
  assert.ok(result.report.summary.fail >= 1)
})

test('missing project-form validation is a failed step', async () => {
  const api = fakeApi({ pending: true })
  const storage = fakeStorage()
  const adapters = fakeAdapters({ validationMissing: true })
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, true)
  assert.equal(result.ok, false)
  const validation = result.report.steps.find((entry) => entry.name === 'projects.form.validation')
  assert.ok(validation)
  assert.equal(validation.ok, false)
  assert.ok(validation.detail.includes('validation'))
})

test('empty project list is accepted through the empty state', async () => {
  const api = fakeApi({ pending: true })
  const storage = fakeStorage()
  const adapters = fakeAdapters({ noProjects: true })
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, true)
  assert.equal(result.ok, true)
  const listStep = result.report.steps.find((entry) => entry.name === 'projects.list')
  assert.ok(listStep)
  assert.equal(listStep.ok, true)
  assert.ok(listStep.detail.includes('empty state'))
})

test('report submission failure is returned without throwing', async () => {
  const api = fakeApi({ pending: true, reportError: new Error('SELFTEST_WRITE_FAILED') })
  const storage = fakeStorage()
  const adapters = fakeAdapters()
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, true)
  assert.equal(result.ok, true)
  assert.equal(result.submitted, false)
  assert.ok(result.submitError.includes('SELFTEST_WRITE_FAILED'))
  assert.ok(String(storage.getItem(SELFTEST_DONE_KEY)).startsWith('done:'))
})

test('panel never appears: fails fast, still restores the hash and submits', async () => {
  const api = fakeApi({ pending: true })
  const storage = fakeStorage()
  const adapters = fakeAdapters({ panelVisible: false })
  const result = await maybeRunSelfTest(null, {}, fakeShell(), { api }, { adapters, storage })
  assert.equal(result.ran, true)
  assert.equal(result.ok, false)
  assert.equal(result.report.steps[0].name, 'panel.open')
  assert.equal(result.report.steps[0].ok, false)
  assert.equal(result.report.routeChecks.length, 0)
  assert.ok(result.report.steps.some((entry) => entry.name === 'hash.restore' && entry.ok))
  assert.equal(result.submitted, true)
})

test('createBrowserAdapters returns null outside a browser', () => {
  assert.equal(createBrowserAdapters({}, {}), null)
})
