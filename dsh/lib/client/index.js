/**
 * Client half entry of the CA DSH plugin (bundle `@local/dsh-ca`).
 *
 * This file is Lead-owned composition: it builds the client services from the
 * ui/shell/api/i18n modules, registers the view factories, and mounts the CA
 * main panel plus its sidebar entry. The browser artifact is produced by
 * `scripts/build-client.mjs` (do not edit `client.js` by hand).
 *
 * Transport: same-origin HTTP routes under `/dsh-ca/*` served by the Host half
 * (see ../CONTRACT.md); the api module wraps them.
 */

import React from 'react'

import { createUI } from './ui/index.js'
import { createApi } from './api.js'
import { createI18n } from './i18n/index.js'
import {
  createShell,
  createPanelIcon,
  PANEL_ID,
  NS,
} from './shell/index.js'

import { createSearchPage } from './pages/search.js'
import { createBackendPage } from './pages/backend.js'
import { createProjectsPage } from './pages/projects.js'
import { createTasksPage } from './pages/tasks.js'
import { createGraphPage } from './pages/graph.js'
import { createStatsPage } from './pages/stats.js'
import { createCallbacksPage } from './pages/callbacks.js'
import { createFunctionPage } from './views/function/index.js'

import { maybeRunSelfTest } from './selftest.js'

/** Client services this module needs from the harness. */
export const inject = ['slots', 'locale']

/**
 * Install the CA client half on the harness Web page.
 * @param {object} ctx - client Cordis context.
 */
export function apply(ctx) {
  const ui = createUI(React, ctx)
  const api = createApi(ctx)
  const i18n = createI18n(ctx)
  const bag = { ui, api, i18n }
  const shell = createShell(React, bag)

  shell.registerPage('search', createSearchPage(React, bag))
  shell.registerPage('backend', createBackendPage(React, bag))
  shell.registerPage('projects', createProjectsPage(React, bag))
  shell.registerPage('tasks', createTasksPage(React, bag))
  shell.registerPage('graph', createGraphPage(React, bag))
  shell.registerPage('stats', createStatsPage(React, bag))
  shell.registerPage('callbacks', createCallbacksPage(React, bag))
  shell.registerPage('function', createFunctionPage(React, bag))

  const PanelIcon = createPanelIcon(React)

  // Use the slot injection contract: registrations install when the owning slot
  // declaration exists and are disposed with it (the host shell declares `main`
  // and `sidebar.panellist`; registering before that would throw "unknown slot").
  ctx.slots.inject('main', () =>
    ctx.slots.register(
      {
        name: 'main',
        key: PANEL_ID,
        locale: NS,
      },
      shell.App,
    ),
  )

  ctx.slots.inject('sidebar.panellist', () =>
    ctx.slots.register(
      {
        name: 'sidebar.panellist',
        id: PANEL_ID,
        order: 20,
        label: () => i18n.t('panel', {}, 'CA'),
        locale: NS,
      },
      PanelIcon,
    ),
  )

  // Optional in-page DOM self test: runs only when the Host created the
  // `selftest-pending.json` marker and this tab has not run it yet. It is
  // non-blocking; results are reported back through RPC and never thrown.
  void maybeRunSelfTest(React, ctx, shell, bag).catch((error) => {
    console.error('[ca] selftest crashed:', error)
  })
}
