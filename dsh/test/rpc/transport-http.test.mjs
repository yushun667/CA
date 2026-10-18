/**
 * End-to-end transport test over a real `node:http` server.
 *
 * The unit tests use request/response doubles; this one proves the same route
 * handler works with genuine IncomingMessage/ServerResponse objects (real
 * `req.url` retains `/dsh-ca/<endpoint>`, real streaming bodies, real status
 * lines). Fake services are still used, so no CA backend is started.
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'

import { registerRpc } from '../../lib/host/rpc/index.js'
import {
  createFakeBackend,
  createFakeCtx,
  createFakeExplain,
  createFakeTasks,
} from './helpers.mjs'

async function startServer(t, options = {}) {
  const ctx = createFakeCtx({ rejection: options.rejection })
  const backend = createFakeBackend()
  const tasks = createFakeTasks()
  const explain = createFakeExplain()
  registerRpc(ctx, { backend, tasks, explain, config: {} })
  const server = http.createServer((req, res) => ctx.route.handler(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  t.after(() => new Promise((resolve) => server.close(resolve)))
  const base = `http://127.0.0.1:${port}`
  const post = (endpoint, payload, init = {}) =>
    fetch(`${base}/dsh-ca/${endpoint}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: payload === undefined ? '{}' : JSON.stringify(payload),
      ...init,
    })
  return { server, base, post, ctx, backend, tasks }
}

test('real HTTP round trip: POST envelope, security headers and auth rejection', async (t) => {
  const { post } = await startServer(t)
  const response = await post('ping')
  assert.equal(response.status, 200)
  assert.match(String(response.headers.get('content-type')), /application\/json/)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff')
  const envelope = await response.json()
  assert.equal(envelope.ok, true)
  assert.equal(envelope.value.pong, true)

  const rejected = await startServer(t, { rejection: 401 })
  const unauthorized = await rejected.post('ping')
  assert.equal(unauthorized.status, 401)
  assert.equal((await unauthorized.json()).error.code, 'UNAUTHORIZED')
})

test('real HTTP round trip: status, body limits and method/path errors', async (t) => {
  const { base, post } = await startServer(t)

  const get = await fetch(`${base}/dsh-ca/ping`, { method: 'GET' })
  assert.equal(get.status, 405)
  assert.equal(get.headers.get('allow'), 'POST')

  const unknown = await post('nope')
  assert.equal(unknown.status, 404)
  assert.equal((await unknown.json()).error.code, 'UNKNOWN_ENDPOINT')

  const outside = await fetch(`${base}/elsewhere`, { method: 'POST', body: '{}' })
  assert.equal(outside.status, 404)

  const invalidJson = await fetch(`${base}/dsh-ca/ping`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{oops',
  })
  assert.equal(invalidJson.status, 400)
  assert.equal((await invalidJson.json()).error.code, 'INVALID_JSON')

  // A >1 MiB body must be rejected before payload validation.
  const huge = await fetch(`${base}/dsh-ca/ping`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: 'x'.repeat(1024 * 1024 + 10),
  })
  assert.equal(huge.status, 413)
  assert.equal((await huge.json()).error.code, 'BODY_TOO_LARGE')
})

test('real HTTP round trip: business error stays 200 and validation stays 400', async (t) => {
  const { post } = await startServer(t)
  const missingTask = await post('tasks.get', { id: 'task-404' })
  assert.equal(missingTask.status, 200)
  assert.equal((await missingTask.json()).error.code, 'TASK_NOT_FOUND')

  const invalid = await post('tasks.get', {})
  assert.equal(invalid.status, 400)
  assert.equal((await invalid.json()).error.code, 'INVALID_ARGUMENT')

  const forbidden = await post('ca.request', { path: '/etc/passwd' })
  assert.equal(forbidden.status, 400)
  assert.equal((await forbidden.json()).error.code, 'FORBIDDEN_PATH')
})

test('real HTTP round trip: JSON body forwarding and CA envelope passthrough', async (t) => {
  const { post, backend } = await startServer(t)
  const response = await post('ca.request', {
    method: 'POST',
    path: '/api/registry/active',
    body: { variant_id: 'v2' },
  })
  assert.equal(response.status, 200)
  const envelope = await response.json()
  assert.equal(envelope.ok, true)
  assert.equal(envelope.value.ok, true)
  assert.deepEqual(backend.calls.request[0].options.body, { variant_id: 'v2' })
})
