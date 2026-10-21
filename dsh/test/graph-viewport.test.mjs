import assert from 'node:assert/strict'
import test from 'node:test'

import { createCallGraph } from '../lib/client/views/graph/index.js'

/**
 * The call-graph viewport is the part of the graph that regressed: the shell
 * re-renders or remounts the panel on every status poll, and the old
 * implementation kept zoom/pan in component state only, so auto-fit snapped
 * the graph back to its fitted size. These tests mount the real component
 * against a minimal React runtime (hooks + effects, no renderer) and assert
 * the viewport survives interactions, re-renders and remounts.
 */

const GRAPH = {
  root: 'main',
  nodes: [
    { id: 'a', name: 'main', depth: 0, direction: 'root', file: 'main.c', line: 1 },
    { id: 'b', name: 'boot', depth: 1, direction: 'forward', file: 'boot.c', line: 10 },
    { id: 'c', name: 'init', depth: 2, direction: 'forward', file: 'init.c', line: 20 },
  ],
  edges: [
    { from: 'a', to: 'b', kind: 'direct' },
    { from: 'b', to: 'c', kind: 'direct' },
  ],
  stats: { nodes: 3, edges: 2 },
}

const CANVAS_WIDTH = 900
const CANVAS_HEIGHT = 600

const sameDeps = (left, right) =>
  Array.isArray(left) &&
  Array.isArray(right) &&
  left.length === right.length &&
  left.every((value, index) => Object.is(value, right[index]))

let activeRuntime = null

/** Minimal React: element creation plus the five hooks the component uses. */
const React = {
  createElement(type, props, ...children) {
    const next = Object.assign({}, props)
    if (children.length) next.children = children.length === 1 ? children[0] : children
    return { type, props: next }
  },
  useState: (initial) => activeRuntime.useState(initial),
  useRef: (initial) => activeRuntime.useRef(initial),
  useMemo: (factory, deps) => activeRuntime.useMemo(factory, deps),
  useEffect: (effect, deps) => activeRuntime.useEffect(effect, deps),
  useId: () => activeRuntime.useId(),
}

/** Render hook-free function components (the `ui` fallbacks) to host elements. */
function expand(element) {
  if (!element || typeof element !== 'object') return element
  if (Array.isArray(element)) return element.map(expand)
  if (!element.props) return element
  if (typeof element.type === 'function') return expand(element.type(element.props))
  return { type: element.type, props: Object.assign({}, element.props, { children: expand(element.props.children) }) }
}

function walk(element, visit) {
  if (!element || typeof element !== 'object') return
  if (Array.isArray(element)) {
    element.forEach((child) => walk(child, visit))
    return
  }
  if (!element.props) return
  visit(element)
  walk(element.props.children, visit)
}

/** A detached stand-in for the scroll canvas: fixed box, no real events. */
function canvasNode(className) {
  return {
    className,
    clientWidth: CANVAS_WIDTH,
    clientHeight: CANVAS_HEIGHT,
    addEventListener() {},
    removeEventListener() {},
    setPointerCapture() {},
    releasePointerCapture() {},
    getBoundingClientRect() {
      return { left: 0, top: 0, width: CANVAS_WIDTH, height: CANVAS_HEIGHT }
    },
    closest() {
      return null
    },
  }
}

function createRuntime(symbol) {
  const CallGraph = createCallGraph(React, {
    ui: {},
    api: { caGet: () => Promise.resolve(GRAPH) },
    i18n: {},
  })
  const props = { symbol }
  const hooks = []
  const queued = []
  const nodes = new Map()
  let hookIndex = 0
  let dirty = false
  let tree = null

  const runtime = {
    get tree() {
      return tree
    },
    useState(initial) {
      const index = hookIndex++
      if (!hooks[index]) hooks[index] = { value: typeof initial === 'function' ? initial() : initial }
      const slot = hooks[index]
      return [
        slot.value,
        (next) => {
          const value = typeof next === 'function' ? next(slot.value) : next
          if (Object.is(value, slot.value)) return
          slot.value = value
          dirty = true
        },
      ]
    },
    useRef(initial) {
      const index = hookIndex++
      if (!hooks[index]) hooks[index] = { current: initial }
      return hooks[index]
    },
    useMemo(factory, deps) {
      const index = hookIndex++
      const slot = hooks[index]
      if (slot && sameDeps(slot.deps, deps)) return slot.value
      const value = factory()
      hooks[index] = { value, deps }
      return value
    },
    useEffect(effect, deps) {
      const index = hookIndex++
      const slot = hooks[index]
      if (slot && sameDeps(slot.deps, deps)) return
      const previousCleanup = slot && slot.cleanup
      if (typeof previousCleanup === 'function') queued.push(previousCleanup)
      hooks[index] = { deps, cleanup: null }
      queued.push(() => {
        const cleanup = effect()
        hooks[index].cleanup = typeof cleanup === 'function' ? cleanup : null
      })
    },
    useId() {
      const index = hookIndex++
      if (!hooks[index]) hooks[index] = { value: 'graph-under-test' }
      return hooks[index].value
    },
    pass() {
      activeRuntime = runtime
      dirty = false
      hookIndex = 0
      tree = expand(CallGraph(props))
      bindRefs(tree)
      while (queued.length) queued.shift()()
      return dirty
    },
  }

  function bindRefs(element) {
    if (!element || typeof element !== 'object') return
    if (Array.isArray(element)) {
      element.forEach(bindRefs)
      return
    }
    if (!element.props) return
    if (element.props.ref && typeof element.props.ref === 'object') {
      const className = typeof element.props.className === 'string' ? element.props.className : ''
      if (!nodes.has(className)) nodes.set(className, canvasNode(className))
      element.props.ref.current = nodes.get(className)
    }
    bindRefs(element.props.children)
  }

  return runtime
}

/** Run render passes until the data promise and every effect have settled. */
async function settle(runtime) {
  for (let pass = 0; pass < 10; pass += 1) {
    runtime.pass()
    await new Promise((resolve) => setTimeout(resolve, 0))
  }
}

function viewportTransform(runtime) {
  let transform = null
  walk(runtime.tree, (element) => {
    if (element.props.className === 'ca-graph-viewport') transform = element.props.transform
  })
  return transform
}

function viewport(runtime) {
  const match = /translate\(([-\d.]+) ([-\d.]+)\) scale\(([\d.]+)\)/.exec(String(viewportTransform(runtime) || ''))
  assert.ok(match, `expected a viewport transform, got ${viewportTransform(runtime)}`)
  return { tx: Number(match[1]), ty: Number(match[2]), zoom: Number(match[3]) }
}

function button(runtime, label) {
  let found = null
  walk(runtime.tree, (element) => {
    if (element.type === 'button' && element.props.children === label) found = element
  })
  assert.ok(found, `expected a "${label}" button`)
  return found
}

function canvas(runtime) {
  let found = null
  walk(runtime.tree, (element) => {
    const className = element.props.className
    if (typeof className === 'string' && className.indexOf('ca-graph-canvas') === 0) found = element
  })
  assert.ok(found, 'expected the canvas element')
  return found
}

test('the first render fits the graph and centers it in the canvas', async () => {
  const runtime = createRuntime('fit-on-mount')
  await settle(runtime)
  const fitted = viewport(runtime)
  assert.ok(fitted.zoom > 0 && fitted.zoom <= 1.4, `fitted zoom ${fitted.zoom}`)
  assert.ok(fitted.tx >= 0 && fitted.tx < CANVAS_WIDTH, `centered tx ${fitted.tx}`)
  assert.ok(fitted.ty >= 0 && fitted.ty < CANVAS_HEIGHT, `centered ty ${fitted.ty}`)
})

test('zoom-in clicks keep increasing the viewport instead of snapping back', async () => {
  const runtime = createRuntime('zoom-clicks')
  await settle(runtime)
  const zooms = [viewport(runtime).zoom]
  for (let click = 0; click < 3; click += 1) {
    button(runtime, '+').props.onClick()
    await settle(runtime)
    zooms.push(viewport(runtime).zoom)
  }
  for (let index = 1; index < zooms.length; index += 1) {
    assert.ok(zooms[index] > zooms[index - 1], `zoom ${zooms[index - 1]} → ${zooms[index]}`)
  }
})

test('dragging the canvas pans by exactly the pointer delta', async () => {
  const runtime = createRuntime('drag-pan')
  await settle(runtime)
  button(runtime, '+').props.onClick()
  await settle(runtime)
  const before = viewport(runtime)
  const target = canvas(runtime)
  target.props.onPointerDown({ button: 0, pointerId: 7, clientX: 100, clientY: 100 })
  target.props.onPointerMove({ pointerId: 7, clientX: 160, clientY: 130 })
  target.props.onPointerUp({ pointerId: 7 })
  await settle(runtime)
  const after = viewport(runtime)
  assert.ok(Math.abs(after.tx - (before.tx + 60)) < 0.01, `tx ${before.tx} → ${after.tx}`)
  assert.ok(Math.abs(after.ty - (before.ty + 30)) < 0.01, `ty ${before.ty} → ${after.ty}`)
  assert.equal(after.zoom, before.zoom)
})

test('a shell re-render keeps the user viewport', async () => {
  const runtime = createRuntime('rerender')
  await settle(runtime)
  button(runtime, '+').props.onClick()
  await settle(runtime)
  const adjusted = viewportTransform(runtime)
  await settle(runtime)
  assert.equal(viewportTransform(runtime), adjusted)
})

test('a remount restores the user viewport instead of auto-fitting it away', async () => {
  const symbol = 'remount-restore'
  const first = createRuntime(symbol)
  await settle(first)
  button(first, '+').props.onClick()
  await settle(first)
  const target = canvas(first)
  target.props.onPointerDown({ button: 0, pointerId: 3, clientX: 0, clientY: 0 })
  target.props.onPointerMove({ pointerId: 3, clientX: -40, clientY: 25 })
  target.props.onPointerUp({ pointerId: 3 })
  await settle(first)
  const expected = viewport(first)

  const second = createRuntime(symbol)
  await settle(second)
  assert.deepEqual(viewport(second), expected)
})

test('Fit forgets the adjusted viewport and a later remount fits again', async () => {
  const symbol = 'fit-reset'
  const runtime = createRuntime(symbol)
  await settle(runtime)
  const fitted = viewport(runtime)
  button(runtime, '+').props.onClick()
  button(runtime, '+').props.onClick()
  await settle(runtime)
  assert.ok(viewport(runtime).zoom > fitted.zoom)

  button(runtime, 'Fit').props.onClick()
  await settle(runtime)
  assert.deepEqual(viewport(runtime), fitted)

  const remounted = createRuntime(symbol)
  await settle(remounted)
  assert.deepEqual(viewport(remounted), fitted)
})
