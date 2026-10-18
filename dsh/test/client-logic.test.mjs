import assert from 'node:assert/strict'
import test from 'node:test'

import {
  AUTO_START_THROTTLE_MS,
  shouldAutoStartBackend,
  shouldLandOnProjects,
} from '../lib/client/shell/logic.js'
import { KNOWN_PAGES, parseHash } from '../lib/client/shell/index.js'

test('the removed backend page has no route and old links land on projects', () => {
  assert.equal(KNOWN_PAGES.includes('backend'), false)
  assert.equal(parseHash('#/backend').page, 'projects')
  assert.equal(parseHash('#/backend?x=1').page, 'projects')
  assert.equal(parseHash('#/projects').page, 'projects')
})

test('routes that exist still parse as before', () => {
  assert.equal(parseHash('').page, 'search')
  assert.equal(parseHash('#/fn/kalloc/branches').page, 'function')
  assert.equal(parseHash('#/fn/kalloc/branches').symbol, 'kalloc')
  assert.equal(parseHash('#/tasks').page, 'tasks')
  assert.equal(parseHash('#/nope').page, 'notfound')
})

test('the backend auto-starts only for a configured stopped backend with a project', () => {
  const project = { id: 'p1' }
  const stopped = { mode: 'local', state: 'stopped' }
  assert.equal(shouldAutoStartBackend(stopped, project, 0, 1000), true)
  assert.equal(shouldAutoStartBackend(stopped, null, 0, 1000), false)
  assert.equal(shouldAutoStartBackend(null, project, 0, 1000), false)
  assert.equal(shouldAutoStartBackend({ mode: 'unconfigured', state: 'stopped' }, project, 0, 1000), false)
  assert.equal(shouldAutoStartBackend({ mode: 'local', state: 'running' }, project, 0, 1000), false)
  assert.equal(
    shouldAutoStartBackend(stopped, project, 1000, 1000 + AUTO_START_THROTTLE_MS - 1),
    false,
  )
  assert.equal(
    shouldAutoStartBackend(stopped, project, 1000, 1000 + AUTO_START_THROTTLE_MS),
    true,
  )
})

test('the panel lands on projects only after status is known and no project exists', () => {
  const base = { statusLoading: false, connected: true, project: null, routePage: 'search' }
  assert.equal(shouldLandOnProjects(base), true)
  assert.equal(shouldLandOnProjects({ ...base, statusLoading: true }), false)
  assert.equal(shouldLandOnProjects({ ...base, connected: null }), false)
  assert.equal(shouldLandOnProjects({ ...base, project: { id: 'p1' } }), false)
  assert.equal(shouldLandOnProjects({ ...base, routePage: 'projects' }), false)
})
