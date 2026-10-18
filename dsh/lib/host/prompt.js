/**
 * System-prompt guidance for the native CA tools.
 *
 * The section is intentionally short: it tells the model *when* the ca_*
 * tools apply and the one ordering rule that matters (check status when unsure,
 * poll long tasks instead of blocking). Tool/parameter details live in the tool
 * schemas, so they are not repeated here.
 *
 * `systemPrompt` is an optional service: when it is missing (headless host or
 * an older profile) registration is a no-op instead of a plugin failure.
 */

const SECTION_NAME = 'dsh-ca'
const FALLBACK_ORDER = 1205

const SECTION_TEXT = `CA is a source analyzer backed by a real build index (functions, calls, CFG/data-flow facts) exposed through ca_* tools.
- Call ca_status when backend availability, the indexed database, or index coverage is unknown; other ca_* tools start a configured backend on demand when possible.
- Resolve symbols with ca_search first, then use ca_symbol for facts, ca_source for code, ca_graph for callers/callees, ca_facts for one fact kind, and ca_stats/ca_callbacks/ca_registry for index metadata.
- For "why/how does this function work" questions use ca_explain instead of speculating; it gathers indexed evidence for one symbol.
- Long jobs (index, update, ensure_deep, prewarm, resolve_callbacks, watch) run through ca_task: start the task, then poll action=logs/status (pass nextOffset back as offset). Never block waiting for a long CA task.
- CA describes an existing index; it never compiles or edits sources. Empty results usually mean a symbol spelling mismatch or a stale/unindexed database — re-check ca_status before retrying.`

/**
 * Read the optional systemPrompt service without tripping the Cordis inject
 * guard (which `ctx.systemPrompt` raises when the service was not injected).
 * @param {any} ctx
 */
function resolveSystemPrompt(ctx) {
  try {
    if (ctx && typeof ctx.get === 'function') {
      return ctx.get('systemPrompt')
    }
  } catch {
    // Optional service unavailable in this profile: the section is skipped.
  }
  return undefined
}

/**
 * Resolve the section order next to the read-tool prompt block.
 * @param {any} systemPrompt
 */
function resolveOrder(systemPrompt) {
  try {
    if (typeof systemPrompt?.getSectionOrder === 'function') {
      const order = systemPrompt.getSectionOrder('TOOL_READ')
      if (Number.isFinite(order)) return order + 5
    }
  } catch {
    // Unknown placement name: fall back to a stable late-prompt slot.
  }
  return FALLBACK_ORDER
}

/**
 * Register the CA guidance system-prompt section.
 *
 * @param {any} ctx Cordis plugin context (systemPrompt is optional)
 * @param {Record<string, any>} [config]
 * @returns {() => void} disposer (no-op when the service is unavailable)
 */
export function registerPrompt(ctx, config = {}) {
  if (config?.promptSection === false) return () => {}
  // `systemPrompt` is an optional service: reading `ctx.systemPrompt` directly
  // throws a Cordis inject guard when the plugin did not declare it. `ctx.get`
  // is the only safe lookup.
  const systemPrompt = resolveSystemPrompt(ctx)
  if (!systemPrompt || typeof systemPrompt.section !== 'function') {
    return () => {}
  }
  const install = () =>
    systemPrompt.section({
      name: SECTION_NAME,
      order: resolveOrder(systemPrompt),
      text: SECTION_TEXT,
    })
  if (ctx && typeof ctx.effect === 'function') {
    return ctx.effect(() => {
      const dispose = install()
      return typeof dispose === 'function' ? dispose : () => {}
    }, 'dsh-ca: system prompt section')
  }
  const dispose = install()
  return typeof dispose === 'function' ? dispose : () => {}
}

export { SECTION_NAME as PROMPT_SECTION_NAME, SECTION_TEXT as PROMPT_SECTION_TEXT }
