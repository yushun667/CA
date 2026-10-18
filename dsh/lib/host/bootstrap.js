/**
 * Host entry of the CA DSH plugin (bundle `@local/dsh-ca`).
 *
 * The implementation lives in `impl.js` and is loaded through a cache-busted
 * dynamic import so that edits to the host half are picked up when the loader
 * re-activates this plugin. Keep this file small and stable: changing it (or
 * `inject`) requires a DSH restart because the loader caches the entry module.
 */

/** Cordis plugin name used by loader diagnostics. */
export const name = 'dsh-ca'

/**
 * Hard service dependencies. `webServer` is required because
 * `ctx.connection.rpc.handle(...)` registers the `/dsh-ca` route through the
 * calling context's `webServer` service.
 */
export const inject = ['tools', 'connection', 'webServer']

/**
 * Load the current implementation generation and run it on this context.
 *
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {object} config - loader row configuration.
 * @returns {Promise<unknown>} the implementation's apply result.
 */
export async function apply(ctx, config = {}) {
  const generation = Date.now()
  const impl = await import(`./impl.js?v=${generation}`)
  return impl.apply(ctx, config)
}
