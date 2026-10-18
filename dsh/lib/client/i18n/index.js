/**
 * CA DSH client i18n: en/zh dictionary plus a small facade over the Harness
 * locale service.
 *
 * The DSH Client locale service owns the active language and persists the
 * user's choice (Settings → General → Language). This module registers the
 * `ca` namespace exactly once, binds a translator for it, and falls back to a
 * local dictionary before the service is available (or when the plugin runs in
 * a composition without the locale service).
 *
 * `t(key, vars?, fallback?)`:
 *   - looks the key up in the active language, then English, then `fallback`,
 *     then returns the key itself;
 *   - interpolates `{name}` placeholders from `vars`;
 *   - `fallback` is the second argument when it is a string, so
 *     `t('fn.source', 'Source')` works; otherwise pass `null` for `vars`.
 *
 * The dictionary files (`en.js`, `zh.js`) keep the same flat key style as
 * src/ca/ui/static/i18n.js so keys ported from the existing CA web UI resolve
 * unchanged.
 */

import { en } from './en.js'
import { zh } from './zh.js'

/** Locale namespace registered by this plugin. */
export const NS = 'ca'

/** Languages this plugin ships dictionaries for. */
export const LOCALES = Object.freeze(['en', 'zh'])

const DICTS = { en, zh }

/** Normalize a language tag to one of the shipped locales ('' when unknown). */
function normalizeLang(value) {
  if (typeof value !== 'string') return ''
  const tag = value.trim().toLowerCase()
  if (tag === 'en' || tag.startsWith('en-') || tag.startsWith('en_')) return 'en'
  if (tag === 'zh' || tag.startsWith('zh-') || tag.startsWith('zh_')) return 'zh'
  return ''
}

/** Best-effort browser language for local-only use (no locale service). */
function detectBrowserLang() {
  try {
    const candidates =
      typeof navigator !== 'undefined' && Array.isArray(navigator.languages)
        ? navigator.languages
        : typeof navigator !== 'undefined'
          ? [navigator.language]
          : []
    for (const candidate of candidates) {
      const lang = normalizeLang(candidate)
      if (lang) return lang
    }
  } catch (_error) {
    /* navigator access is best effort */
  }
  return 'en'
}

/** Fill `{name}` placeholders; unknown names stay verbatim. */
export function interpolate(template, vars) {
  if (typeof template !== 'string') return template
  if (!vars) return template
  return template.replace(/\{(\w+)\}/g, (match, name) => {
    if (!Object.prototype.hasOwnProperty.call(vars, name)) return match
    const value = vars[name]
    return value === undefined || value === null ? match : String(value)
  })
}

/** Look a key up in one local dictionary; returns undefined when absent. */
function localLookup(lang, key) {
  const table = DICTS[lang]
  if (!table) return undefined
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined
}

/**
 * Create the translator facade.
 *
 * @param {object} [ctx] DSH Client plugin context (may be absent in tests).
 * @returns {{
 *   t: (key: string, vars?: object|string|null, fallback?: string) => string,
 *   lang: string,
 *   locales: readonly string[],
 *   ns: string,
 *   subscribe: (fn: () => void) => () => void,
 *   setLang: (lang: string) => boolean,
 *   isAvailable: () => boolean,
 * }}
 */
export function createI18n(ctx) {
  const listeners = new Set()
  const service =
    ctx && ctx.locale && typeof ctx.locale === 'object' ? ctx.locale : null

  let active = service
    ? normalizeLang(callSafe(service, 'getSnapshot')) ||
      normalizeLang(service.active) ||
      detectBrowserLang()
    : detectBrowserLang()
  if (!DICTS[active]) active = 'en'

  let bound = null
  let registered = false
  let disposed = false

  function callSafe(target, method) {
    try {
      if (!target || typeof target[method] !== 'function') return ''
      const value = target[method]()
      return value && typeof value === 'object' ? value.active : ''
    } catch (_error) {
      return ''
    }
  }

  function refreshActive() {
    if (!service) return
    const snapshot = typeof service.getSnapshot === 'function' ? service.getSnapshot() : null
    const next = normalizeLang(snapshot && snapshot.active)
    if (next && next !== active) active = next
  }

  function notify() {
    refreshActive()
    for (const fn of [...listeners]) {
      try {
        fn()
      } catch (error) {
        console.error('[ca] i18n subscriber crashed:', error)
      }
    }
  }

  // -- register the namespace as an owned effect (removed on plugin unload) -- //
  if (service && typeof service.register === 'function') {
    const register = () => service.register(NS, { en, zh })
    try {
      if (ctx && typeof ctx.effect === 'function') {
        ctx.effect(() => {
          const dispose = register()
          registered = true
          return () => {
            registered = false
            if (typeof dispose === 'function') dispose()
          }
        }, 'ca: i18n dictionaries')
      } else {
        const dispose = register()
        registered = true
        void dispose
      }
    } catch (error) {
      console.warn('[ca] locale namespace registration failed:', error)
    }
    if (typeof service.bind === 'function') {
      try {
        bound = service.bind(NS)
      } catch (error) {
        console.warn('[ca] locale bind failed:', error)
      }
    }
    if (typeof service.subscribe === 'function') {
      const subscribe = () => service.subscribe(notify)
      try {
        if (ctx && typeof ctx.effect === 'function') {
          ctx.effect(() => {
            const dispose = subscribe()
            return () => {
              if (typeof dispose === 'function') dispose()
            }
          }, 'ca: i18n language changes')
        } else {
          const dispose = subscribe()
          void dispose
        }
      } catch (error) {
        console.warn('[ca] locale subscription failed:', error)
      }
    }
  }

  /** Local-only lookup chain: active → en → fallback → key. */
  function localTranslate(key, vars, fallback) {
    let template = localLookup(active, key)
    if (template === undefined) template = localLookup('en', key)
    if (template === undefined) template = fallback
    if (template === undefined) return key
    return interpolate(template, vars)
  }

  function t(key, vars, fallback) {
    if (typeof key !== 'string' || key.length === 0) return fallback || ''
    const first = typeof vars === 'string' ? vars : null
    const params = typeof vars === 'object' && vars !== null ? vars : null
    const fallbackText = typeof fallback === 'string' ? fallback : first
    if (bound) {
      try {
        const text = bound(key, params || undefined)
        if (typeof text === 'string' && text !== key) return text
      } catch (error) {
        console.warn('[ca] locale lookup failed:', error)
      }
    }
    return localTranslate(key, params, fallbackText)
  }

  return {
    t,
    get lang() {
      return active
    },
    locales: LOCALES,
    ns: NS,
    isAvailable: () => registered || bound !== null,
    subscribe(fn) {
      if (typeof fn !== 'function') return () => {}
      listeners.add(fn)
      return () => listeners.delete(fn)
    },
    /** Switch language through the locale service; returns false when it refused. */
    setLang(lang) {
      const next = normalizeLang(lang)
      if (!next) return false
      if (service && typeof service.setLocale === 'function') {
        try {
          service.setLocale(next)
          active = next
          notify()
          return true
        } catch (error) {
          console.warn('[ca] locale switch failed:', error)
          return false
        }
      }
      if (next === active) return true
      active = next
      notify()
      return true
    },
    dispose() {
      if (disposed) return
      disposed = true
      listeners.clear()
    },
  }
}

export default createI18n
