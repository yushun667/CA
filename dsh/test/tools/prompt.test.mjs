/**
 * System-prompt section registration tests: placement next to TOOL_READ, the
 * fallback order, optional-service tolerance and concise guidance content.
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  PROMPT_SECTION_NAME,
  PROMPT_SECTION_TEXT,
  registerPrompt,
} from '../../lib/host/prompt.js'

function fakeCtx({ systemPrompt, failOrder = false } = {}) {
  const sections = []
  const effects = []
  const service = systemPrompt ?? {
    section(section) {
      sections.push(section)
      return () => {
        const index = sections.indexOf(section)
        if (index >= 0) sections.splice(index, 1)
      }
    },
    getSectionOrder(name) {
      if (failOrder) throw new Error(`unknown order ${name}`)
      return name === 'TOOL_READ' ? 1200 : 9999
    },
  }
  const ctx = {
    sections,
    effects,
    get(name) {
      return name === 'systemPrompt' ? service : undefined
    },
    effect(callback, label) {
      const dispose = callback()
      effects.push({ label, dispose })
      return () => {
        try {
          dispose?.()
        } catch {
          // ignore
        }
      }
    },
  }
  return ctx
}

test('registers the CA section next to TOOL_READ (order + 5)', () => {
  const ctx = fakeCtx()
  const dispose = registerPrompt(ctx, {})
  assert.equal(ctx.sections.length, 1)
  const section = ctx.sections[0]
  assert.equal(section.name, PROMPT_SECTION_NAME)
  assert.equal(section.order, 1205)
  assert.equal(section.text, PROMPT_SECTION_TEXT)
  dispose()
  assert.equal(ctx.sections.length, 0)
})

test('falls back to order 1205 when getSectionOrder is missing or throws', () => {
  const missing = fakeCtx()
  missing.get('systemPrompt').getSectionOrder = undefined
  registerPrompt(missing, {})
  assert.equal(missing.sections[0].order, 1205)

  const throwing = fakeCtx({ failOrder: true })
  registerPrompt(throwing, {})
  assert.equal(throwing.sections[0].order, 1205)
})

test('resolves the service through ctx.get() when it is an optional dependency', () => {
  const ctx = fakeCtx()
  registerPrompt(ctx, {})
  assert.equal(ctx.sections.length, 1)
  assert.equal(ctx.sections[0].name, PROMPT_SECTION_NAME)
})

test('never reads ctx.systemPrompt directly (Cordis inject guard regression)', () => {
  const ctx = fakeCtx()
  Object.defineProperty(ctx, 'systemPrompt', {
    configurable: true,
    get() {
      throw new Error('cannot get property "systemPrompt" without inject')
    },
  })
  const dispose = registerPrompt(ctx, {})
  assert.equal(ctx.sections.length, 1, 'the section is still registered via ctx.get()')
  assert.doesNotThrow(() => dispose())
  assert.equal(ctx.sections.length, 0)
})

test('a throwing ctx.get() degrades to a no-op instead of failing activation', () => {
  const bare = {
    get() {
      throw new Error('service registry unavailable')
    },
  }
  const dispose = registerPrompt(bare, {})
  assert.equal(typeof dispose, 'function')
  assert.doesNotThrow(() => dispose())
})

test('is a no-op without the systemPrompt service or when disabled', () => {
  const bare = { get: () => undefined }
  const dispose = registerPrompt(bare, {})
  assert.equal(typeof dispose, 'function')
  assert.doesNotThrow(() => dispose())

  const ctx = fakeCtx()
  registerPrompt(ctx, { promptSection: false })
  assert.equal(ctx.sections.length, 0)
})

test('the section gives short, non-duplicated CA guidance', () => {
  assert.ok(PROMPT_SECTION_TEXT.length < 1600, 'section stays concise')
  assert.match(PROMPT_SECTION_TEXT, /ca_status/)
  assert.match(PROMPT_SECTION_TEXT, /ca_explain/)
  assert.match(PROMPT_SECTION_TEXT, /ca_task/)
  assert.match(PROMPT_SECTION_TEXT, /nextOffset/)
  assert.match(PROMPT_SECTION_TEXT, /never compiles or edits sources/)
})
