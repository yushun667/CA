import test from 'node:test'
import assert from 'node:assert/strict'

/**
 * Regression test for the client style bridge.
 *
 * `injectCss` keeps a refcount per CSS string in a Map keyed by the string
 * itself. `renderStyles` must write the map *keys*; an earlier version pushed
 * the map *values* and therefore put the literal reference count ("1") into
 * the <style> element, silently dropping every plugin stylesheet.
 */

function installDocumentStub() {
  const created = []
  const element = () => ({
    tagName: 'STYLE',
    attrs: {},
    textContent: '',
    isConnected: false,
    setAttribute(name, value) {
      this.attrs[name] = String(value)
    },
    getAttribute(name) {
      return this.attrs[name]
    },
    remove() {
      this.isConnected = false
    },
  })
  const head = {
    querySelector() {
      return null
    },
    appendChild(node) {
      node.isConnected = true
      created.push(node)
      return node
    },
  }
  const previous = globalThis.document
  globalThis.document = {
    head,
    createElement(tag) {
      return { ...element(), tagName: String(tag).toUpperCase() }
    },
  }
  return {
    created,
    restore() {
      if (previous === undefined) delete globalThis.document
      else globalThis.document = previous
    },
  }
}

test('renderStyles writes CSS text, not the reference count', async (t) => {
  const stub = installDocumentStub()
  t.after(() => stub.restore())
  const ui = await import('../lib/client/ui/index.js')
  const disposeA = ui.injectCss('.ca-regression-a { color: red; }')
  const disposeB = ui.injectCss('.ca-regression-b { color: blue; }')

  const style = stub.created.at(-1)
  assert.ok(style, 'one <style data-ca-ui> element is created')
  assert.equal(style.attrs['data-ca-ui'], 'ca')

  const css = () => stub.created.at(-1).textContent
  assert.match(css(), /\.ca-regression-a/)
  assert.match(css(), /\.ca-regression-b/)
  assert.doesNotMatch(css(), /^1$/)

  disposeA()
  assert.doesNotMatch(css(), /\.ca-regression-a/)
  assert.match(css(), /\.ca-regression-b/)

  disposeB()
  assert.equal(css().trim(), '')
})
