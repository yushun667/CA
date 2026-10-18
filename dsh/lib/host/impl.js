/**
 * Host implementation of the CA DSH plugin (bundle `@local/dsh-ca`).
 *
 * Lead-owned wiring: build the backend manager, task manager and explain
 * service, then hand them to the RPC route, the native tools and the system
 * prompt. Module interfaces are documented in ../CONTRACT.md; feature modules
 * are owned by their respective teammates.
 *
 * Loaded through a cache-busted dynamic import from `bootstrap.js`, so edits
 * here are picked up by re-activating the plugin.
 */

import { createBackendManager } from './backend/manager.js'
import { createTaskManager } from './tasks/index.js'
import { createExplainService } from './explain/index.js'
import { registerRpc } from './rpc/index.js'
import { registerTools } from './tools/index.js'
import { registerPrompt } from './prompt.js'

export function apply(ctx, config = {}) {
  const backend = createBackendManager(ctx, config)
  const tasks = createTaskManager(ctx, { backend, config })
  const explain = createExplainService(ctx, { backend, config })

  ctx.effect(
    () => () => {
      try {
        tasks.dispose()
      } catch (error) {
        ctx.logger?.warn?.('dsh-ca: task manager dispose failed', error)
      }
      try {
        backend.dispose()
      } catch (error) {
        ctx.logger?.warn?.('dsh-ca: backend manager dispose failed', error)
      }
    },
    'dsh-ca: backend/task lifetime',
  )

  registerRpc(ctx, { backend, tasks, explain, config })
  registerTools(ctx, { backend, tasks, explain, config })
  registerPrompt(ctx, config)
}
