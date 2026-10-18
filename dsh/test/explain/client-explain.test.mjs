/**
 * Tests for the Client AI explanation panel (`dsh/lib/client/views/explain/`).
 *
 * `react` is not installed in this repository (the Client half is bundled by the
 * Host), so the component is exercised with a tiny hook-aware React stand-in.
 * That keeps the important guarantees under test without adding dependencies:
 * the RPC payload/abort signal, loading/error/retry states, Markdown rendering
 * through React elements (never raw HTML), evidence/model footer and copy
 * feedback.
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  createExplainPanel,
  buildExplainRequest,
  normalizeExplainError,
  normalizeExplainResult,
  normalizeLanguage,
  unwrapExplainRpc,
} from '../../lib/client/views/explain/index.js'
import {
  MAX_CODE_BLOCK_CHARS,
  MAX_MARKDOWN_BLOCKS,
  MAX_MARKDOWN_CHARS,
  parseInline,
  parseMarkdown,
} from '../../lib/client/views/explain/markdown.js'

// --------------------------------------------------------------------------- //
// Minimal hook-aware React stand-in
// --------------------------------------------------------------------------- //

function createFakeReact() {
  const state = []
  const refs = []
  const effects = []
  let cursor = 0
  let dirty = false

  function createElement(type, props, ...children) {
    const normalized = children.length <= 1 ? children[0] : children
    if (typeof type === 'function') {
      return type({ ...(props || {}), children: normalized })
    }
    return { type, props: { ...(props || {}), children: normalized } }
  }

  const React = {
    createElement,
    useState(initial) {
      const index = cursor
      cursor += 1
      if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial
      const set = (next) => {
        state[index] = typeof next === 'function' ? next(state[index]) : next
        dirty = true
      }
      return [state[index], set]
    },
    useRef(initial) {
      const index = cursor
      cursor += 1
      if (!(index in refs)) refs[index] = { current: initial }
      return refs[index]
    },
    useEffect(fn, deps) {
      const index = cursor
      cursor += 1
      const previous = effects[index]
      const changed =
        !previous ||
        !deps ||
        !previous.deps ||
        deps.length !== previous.deps.length ||
        deps.some((value, offset) => value !== previous.deps[offset])
      if (changed) {
        if (previous && typeof previous.cleanup === 'function') {
          try {
            previous.cleanup()
          } catch {
            // ignore cleanup failures in the stand-in
          }
        }
        effects[index] = { fn, deps: deps ? deps.slice() : null, cleanup: undefined, pending: true }
      }
    },
    useMemo(fn) {
      return fn()
    },
  }

  function runEffects() {
    for (const effect of effects) {
      if (effect && effect.pending) {
        effect.pending = false
        const cleanup = effect.fn()
        effect.cleanup = typeof cleanup === 'function' ? cleanup : undefined
      }
    }
  }

  function renderInto(Component, props) {
    let tree = null
    for (let guard = 0; guard < 100; guard += 1) {
      dirty = false
      cursor = 0
      tree = Component({ ...props })
      runEffects()
      if (!dirty) return tree
    }
    throw new Error('fake React render loop did not settle')
  }

  async function settleInto(Component, props, tree) {
    let current = tree
    let idle = 0
    for (let index = 0; index < 40 && idle < 2; index += 1) {
      await new Promise((resolve) => setImmediate(resolve))
      if (dirty) {
        idle = 0
        current = renderInto(Component, props)
      } else {
        idle += 1
      }
    }
    return current
  }

  function destroy() {
    for (let index = effects.length - 1; index >= 0; index -= 1) {
      const effect = effects[index]
      if (effect && typeof effect.cleanup === 'function') {
        try {
          effect.cleanup()
        } catch {
          // ignore
        }
      }
    }
  }

  return { React, renderInto, settleInto, destroy }
}

function collectText(node, output = []) {
  if (node === null || node === undefined || node === true || node === false) return output
  if (typeof node === 'string' || typeof node === 'number') {
    output.push(String(node))
    return output
  }
  if (Array.isArray(node)) {
    for (const child of node) collectText(child, output)
    return output
  }
  if (typeof node === 'object') collectText(node.props ? node.props.children : undefined, output)
  return output
}

function findNodes(node, predicate, output = []) {
  if (node === null || node === undefined) return output
  if (Array.isArray(node)) {
    for (const child of node) findNodes(child, predicate, output)
    return output
  }
  if (typeof node !== 'object') return output
  if (predicate(node)) output.push(node)
  findNodes(node.props ? node.props.children : undefined, predicate, output)
  return output
}

function createHarness(bag, props) {
  const fake = createFakeReact()
  const Component = createExplainPanel(fake.React, bag)
  let tree = fake.renderInto(Component, props)
  const harness = {
    fake,
    get tree() {
      return tree
    },
    text() {
      return collectText(tree).join('')
    },
    buttons() {
      return findNodes(tree, (node) => node.type === 'button' && typeof node.props.onClick === 'function')
    },
    async click(label) {
      const button = harness
        .buttons()
        .find((node) => collectText(node).join('').includes(label))
      assert.ok(button, `button "${label}" not found; have: ${harness.buttons().map((node) => collectText(node).join('')).join(' | ')}`)
      button.props.onClick()
      tree = await fake.settleInto(Component, props, tree)
      return tree
    },
    async type(value) {
      const input = findNodes(tree, (node) => node.type === 'input')[0]
      assert.ok(input, 'question input not found')
      input.props.onChange({ target: { value } })
      tree = await fake.settleInto(Component, props, tree)
      return tree
    },
    async refresh() {
      tree = await fake.settleInto(Component, props, tree)
      return tree
    },
    destroy() {
      fake.destroy()
    },
  }
  return harness
}

// --------------------------------------------------------------------------- //
// Markdown parser
// --------------------------------------------------------------------------- //

test('parseMarkdown parses headings, lists, quotes, rules and fenced code', () => {
  const { blocks, truncated } = parseMarkdown(
    [
      '## Purpose',
      '',
      'Opens a **device** and validates `flags` [E1].',
      '',
      '- first item',
      '- second item',
      '',
      '1. ordered one',
      '2. ordered two',
      '',
      '> quoted line',
      '',
      '---',
      '',
      '```c',
      'int main(void) {',
      '    return 0;',
      '}',
      '```',
      '',
      'Unclosed fence starts here:',
      '```',
      'raw <b>text</b>',
    ].join('\n'),
  )

  assert.equal(truncated, false)
  assert.deepEqual(
    blocks.map((block) => block.type),
    ['heading', 'paragraph', 'list', 'list', 'quote', 'hr', 'code', 'paragraph', 'code'],
  )
  assert.equal(blocks[0].level, 2)
  assert.equal(blocks[0].text, 'Purpose')
  assert.equal(blocks[2].ordered, false)
  assert.deepEqual(blocks[2].items, ['first item', 'second item'])
  assert.equal(blocks[3].ordered, true)
  assert.deepEqual(blocks[3].items, ['ordered one', 'ordered two'])
  assert.equal(blocks[4].text, 'quoted line')
  assert.equal(blocks[6].lang, 'c')
  assert.match(blocks[6].text, /int main\(void\)/)
  assert.equal(blocks[8].type, 'code')
  assert.equal(blocks[8].text, 'raw <b>text</b>')
})

test('parseInline extracts code, bold, evidence references and source locations', () => {
  const tokens = parseInline('Use **bold** and `x = 1` [E3] see drivers/core.c:42 and kernel/../a.c:7')
  const types = tokens.map((token) => token.type)
  assert.deepEqual(types, ['text', 'strong', 'text', 'code', 'text', 'evidence', 'text', 'location', 'text', 'location'])
  assert.deepEqual(tokens[1].tokens, [{ type: 'text', text: 'bold' }])
  assert.equal(tokens[3].text, 'x = 1')
  assert.equal(tokens[5].ref, 'E3')
  assert.equal(tokens[7].file, 'drivers/core.c')
  assert.equal(tokens[7].line, 42)
  assert.equal(tokens[9].file, 'kernel/../a.c')
  assert.equal(tokens[9].line, 7)
})

test('parseMarkdown treats model output as data (no HTML interpretation)', () => {
  const { blocks } = parseMarkdown('<img src=x onerror="alert(1)"> **bold** <script>alert(2)</script>')
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].type, 'paragraph')
  assert.ok(blocks[0].text.includes('<img src=x onerror="alert(1)">'))
  assert.ok(blocks[0].text.includes('<script>alert(2)</script>'))
  // Inline tokens are plain data; the renderer creates React elements from them.
  const tokens = parseInline(blocks[0].text)
  for (const token of tokens) {
    assert.ok(['text', 'strong', 'code', 'evidence', 'location'].includes(token.type))
    assert.equal('dangerouslySetInnerHTML' in token, false)
  }
})

test('parseMarkdown bounds input size, block count and fenced-code content', () => {
  const huge = `${'x'.repeat(MAX_MARKDOWN_CHARS + 5000)}`
  const parsed = parseMarkdown(huge)
  assert.equal(parsed.truncated, true)
  assert.ok(parsed.blocks.length >= 1)

  const manyHeadings = Array.from({ length: MAX_MARKDOWN_BLOCKS + 50 }, (_, index) => `## heading ${index}`).join('\n\n')
  const bounded = parseMarkdown(manyHeadings)
  assert.ok(bounded.blocks.length <= MAX_MARKDOWN_BLOCKS)
  assert.equal(bounded.truncated, true)

  const bigCode = `\`\`\`c\n${'a'.repeat(MAX_CODE_BLOCK_CHARS + 1000)}\n\`\`\``
  const code = parseMarkdown(bigCode)
  assert.equal(code.blocks[0].type, 'code')
  assert.ok(code.blocks[0].text.length <= MAX_CODE_BLOCK_CHARS)
  assert.equal(code.truncated, true)
})

// --------------------------------------------------------------------------- //
// Pure request/response helpers
// --------------------------------------------------------------------------- //

test('normalizeLanguage only allows en and zh', () => {
  assert.equal(normalizeLanguage('zh-CN'), 'zh')
  assert.equal(normalizeLanguage(' ZH '), 'zh')
  assert.equal(normalizeLanguage('en-US'), 'en')
  assert.equal(normalizeLanguage(undefined), 'en')
  assert.equal(normalizeLanguage('fr'), 'en')
})

test('buildExplainRequest trims and validates the RPC payload', () => {
  assert.deepEqual(buildExplainRequest({ symbol: ' do_open ', language: 'zh', question: '  why?  ' }), {
    symbol: 'do_open',
    language: 'zh',
    question: 'why?',
  })
  assert.deepEqual(buildExplainRequest({ symbol: 'do_open' }), { symbol: 'do_open', language: 'en' })
  assert.throws(() => buildExplainRequest({}), (error) => error.code === 'EXPLAIN_INVALID_ARGUMENT')
  assert.throws(() => buildExplainRequest({ symbol: 'x'.repeat(600) }), (error) => error.code === 'EXPLAIN_INVALID_ARGUMENT')
})

test('normalizeExplainResult validates and bounds the Host result', () => {
  const result = normalizeExplainResult(
    { symbol: 'do_open', text: '  ## Purpose  ', model: { provider: 'p', model: 'm' }, evidenceCount: '4', usage: { outputTokens: 3 } },
    'fallback',
  )
  assert.deepEqual(result, {
    symbol: 'do_open',
    text: '## Purpose',
    model: { provider: 'p', model: 'm' },
    evidenceCount: 4,
    usage: { outputTokens: 3 },
  })
  assert.throws(() => normalizeExplainResult({ text: '   ' }), (error) => error.code === 'EXPLAIN_EMPTY')
  assert.throws(() => normalizeExplainResult(null), (error) => error.code === 'EXPLAIN_PROTOCOL_ERROR')
})

test('normalizeExplainError maps abort and nested error envelopes to stable codes', () => {
  const abort = new Error('aborted')
  abort.name = 'AbortError'
  assert.equal(normalizeExplainError(abort).code, 'EXPLAIN_ABORTED')
  assert.equal(normalizeExplainError({ error: { code: 'BACKEND_UNCONFIGURED', message: 'no backend' } }).code, 'BACKEND_UNCONFIGURED')
  const long = normalizeExplainError({ code: 'X', message: 'y'.repeat(5000) })
  assert.equal(long.code, 'X')
  assert.ok(long.message.length <= 601)
})

test('unwrapExplainRpc supports envelopes and already-unwrapped values', async () => {
  const calls = []
  const envelopeApi = {
    async rpc(endpoint, payload, signal) {
      calls.push({ endpoint, payload, signal })
      return { ok: true, value: { symbol: payload.symbol, text: 'answer', model: { provider: 'p', model: 'm' }, evidenceCount: 2 } }
    },
  }
  const unwrapped = await unwrapExplainRpc(envelopeApi, { symbol: 'do_open', language: 'en' })
  assert.equal(unwrapped.text, 'answer')
  assert.equal(calls[0].endpoint, 'explain')

  const directApi = {
    async rpc() {
      return { symbol: 'do_open', text: 'direct' }
    },
  }
  assert.equal((await unwrapExplainRpc(directApi, { symbol: 'do_open', language: 'en' })).text, 'direct')

  await assert.rejects(unwrapExplainRpc(null, { symbol: 'do_open', language: 'en' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_UNAVAILABLE')
    return true
  })

  const failingApi = {
    async rpc() {
      throw { code: 'EXPLAIN_UNAVAILABLE', message: 'No default DSH model' }
    },
  }
  await assert.rejects(unwrapExplainRpc(failingApi, { symbol: 'do_open', language: 'en' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_UNAVAILABLE')
    assert.match(error.message, /No default DSH model/)
    return true
  })

  const errorEnvelopeApi = {
    async rpc() {
      return { ok: false, error: { code: 'EXPLAIN_NOT_FOUND', message: 'missing symbol' } }
    },
  }
  await assert.rejects(unwrapExplainRpc(errorEnvelopeApi, { symbol: 'x', language: 'en' }), (error) => {
    assert.equal(error.code, 'EXPLAIN_NOT_FOUND')
    return true
  })
})

// --------------------------------------------------------------------------- //
// Panel component (rendered through the React stand-in)
// --------------------------------------------------------------------------- //

const SUCCESS_TEXT = [
  '## Purpose',
  '',
  'Opens a **device** and validates `flags` [E1].',
  '',
  '```c',
  'mutex_lock(&drv->lock);',
  '```',
].join('\n')

function successfulApi(calls) {
  return {
    async rpc(endpoint, payload, signal) {
      calls.push({ endpoint, payload, signal })
      return {
        ok: true,
        value: {
          symbol: payload.symbol,
          text: SUCCESS_TEXT,
          model: { provider: 'deepseek', model: 'deepseek-chat' },
          evidenceCount: 7,
          usage: { outputTokens: 20 },
        },
      }
    },
  }
}

test('createExplainPanel requires React and exposes a component', () => {
  assert.throws(() => createExplainPanel(null, {}), TypeError)
  assert.throws(() => createExplainPanel({}, {}), TypeError)
  const fake = createFakeReact()
  const Component = createExplainPanel(fake.React, { ui: {}, api: null, i18n: { lang: 'en', t: () => undefined } })
  assert.equal(typeof Component, 'function')
  assert.equal(Component.displayName, 'CaExplainPanel')
})

test('panel calls the explain RPC and renders a safe Markdown result', async () => {
  const calls = []
  const harness = createHarness(
    {
      ui: {},
      api: successfulApi(calls),
      i18n: { lang: 'en', t: () => undefined, subscribe: () => () => {} },
    },
    { symbol: 'do_open' },
  )

  assert.ok(harness.text().includes('Explain with AI'))
  await harness.click('Explain with AI')

  assert.equal(calls.length, 1)
  assert.equal(calls[0].endpoint, 'explain')
  assert.deepEqual(calls[0].payload, { symbol: 'do_open', language: 'en' })
  assert.ok(calls[0].signal instanceof AbortSignal)

  const text = harness.text()
  assert.match(text, /Purpose/)
  assert.match(text, /device/)
  assert.match(text, /mutex_lock/)
  assert.match(text, /deepseek\/deepseek-chat/)
  assert.match(text, /Based on 7 evidence items/)
  assert.match(text, /verify against the source/)
  // Rendered through React elements: the code block content is a plain child.
  const strongNodes = findNodes(harness.tree, (node) => node.type === 'strong')
  assert.equal(strongNodes.length, 1)
  assert.equal(collectText(strongNodes[0]).join(''), 'device')
  harness.destroy()
})

test('panel forwards the optional question and handles errors with retry', async () => {
  const calls = []
  let failNext = true
  const api = {
    async rpc(endpoint, payload, signal) {
      calls.push({ endpoint, payload, signal })
      if (failNext) throw { code: 'EXPLAIN_UNAVAILABLE', message: 'No default DSH model is selected' }
      return {
        ok: true,
        value: { symbol: payload.symbol, text: 'Recovered explanation [E1].', model: { provider: 'p', model: 'm' }, evidenceCount: 1 },
      }
    },
  }
  const harness = createHarness(
    {
      ui: {},
      api,
      i18n: { lang: 'en', t: () => undefined, subscribe: () => () => {} },
    },
    { symbol: 'do_open' },
  )

  await harness.type('what happens on error?')
  await harness.click('Explain with AI')

  const errorText = harness.text()
  assert.match(errorText, /Explanation failed/)
  assert.match(errorText, /EXPLAIN_UNAVAILABLE/)
  assert.match(errorText, /No default DSH model is selected/)
  assert.match(errorText, /Select a default model/)
  assert.deepEqual(calls[0].payload, { symbol: 'do_open', language: 'en', question: 'what happens on error?' })

  failNext = false
  await harness.click('Retry')
  assert.match(harness.text(), /Recovered explanation/)
  harness.destroy()
})

test('panel renders model-authored HTML as text and offers copy feedback', async () => {
  const calls = []
  const api = {
    async rpc() {
      calls.push(1)
      return {
        ok: true,
        value: {
          symbol: 'do_open',
          text: '<img src=x onerror="alert(1)"> **safe**',
          model: { provider: 'p', model: 'm' },
          evidenceCount: 0,
        },
      }
    },
  }
  const harness = createHarness(
    {
      ui: {},
      api,
      i18n: { lang: 'en', t: () => undefined, subscribe: () => () => {} },
    },
    { symbol: 'do_open' },
  )
  await harness.click('Explain with AI')

  // The raw tag is a text child, never an element type.
  const htmlNodes = findNodes(harness.tree, (node) => typeof node.type === 'string' && node.type.startsWith('<'))
  assert.equal(htmlNodes.length, 0)
  assert.ok(harness.text().includes('<img src=x onerror="alert(1)">'))

  // Copy in Node has no navigator.clipboard/document, so the failure feedback shows.
  await harness.click('Copy')
  assert.match(harness.text(), /Copy failed/)
  harness.destroy()
})

test('panel uses the shared explain.* i18n dictionary and follows language changes', async () => {
  const listeners = new Set()
  let lang = 'en'
  const dict = {
    en: {
      'explain.title': 'EN title',
      'explain.explain': 'EN explain',
      'explain.regenerate': 'EN regenerate',
      'explain.evidence': 'EN evidence {count}',
      'explain.copy': 'EN copy',
      'explain.disclaimer': 'EN disclaimer {model}',
    },
    zh: {
      'explain.title': 'ZH 标题',
      'explain.explain': 'ZH 解释',
      'explain.regenerate': 'ZH 重新生成',
      'explain.evidence': 'ZH 证据 {count}',
      'explain.copy': 'ZH 复制',
      'explain.disclaimer': 'ZH 声明 {model}',
    },
  }
  const i18n = {
    get lang() {
      return lang
    },
    t(key, vars) {
      const template = dict[lang][key]
      if (typeof template !== 'string') return key
      if (!vars) return template
      return template.replace(/\{(\w+)\}/g, (match, name) => (vars[name] === undefined ? match : String(vars[name])))
    },
    subscribe(fn) {
      listeners.add(fn)
      return () => listeners.delete(fn)
    },
  }
  const calls = []
  const harness = createHarness({ ui: {}, api: successfulApi(calls), i18n }, { symbol: 'do_open' })

  assert.match(harness.text(), /EN title/)
  await harness.click('EN explain')
  assert.match(harness.text(), /EN evidence 7/)
  assert.match(harness.text(), /EN disclaimer deepseek\/deepseek-chat/)
  assert.match(harness.text(), /EN copy/)

  lang = 'zh'
  for (const notify of [...listeners]) notify()
  await harness.refresh()
  assert.match(harness.text(), /ZH 标题/)
  assert.match(harness.text(), /ZH 重新生成/)
  assert.match(harness.text(), /ZH 证据 7/)
  assert.match(harness.text(), /ZH 复制/)
  harness.destroy()
})

test('panel shows the unavailable hint and disables explaining without a symbol', () => {
  const harness = createHarness(
    {
      ui: {},
      api: { async rpc() { throw new Error('should not be called') } },
      i18n: { lang: 'zh', t: () => undefined, subscribe: () => () => {} },
    },
    { symbol: '' },
  )
  assert.match(harness.text(), /请先打开一个函数/)
  const explainButton = harness.buttons().find((node) => collectText(node).join('').includes('AI 解释'))
  assert.ok(explainButton)
  assert.equal(explainButton.props.disabled, true)
  harness.destroy()
})

test('the Client explain module never uses dangerouslySetInnerHTML', async () => {
  const files = [
    new URL('../../lib/client/views/explain/index.js', import.meta.url),
    new URL('../../lib/client/views/explain/markdown.js', import.meta.url),
  ]
  for (const file of files) {
    const source = await readFile(file, 'utf8')
    assert.equal(source.includes('dangerouslySetInnerHTML'), false, `${file.pathname} must not inject raw HTML`)
  }
})
