import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildQueryString,
  buildUrl,
  createHttpClient,
  isEnvelope,
} from '../../lib/host/backend/http.js';
import { BackendError } from '../../lib/host/backend/errors.js';
import {
  createFakeFetch,
  createSseResponse,
  hangingFetch,
  healthResponse,
  jsonResponse,
  nextTick,
  unreachableFetch,
} from './helpers/fakes.mjs';

const BASE = 'http://127.0.0.1:8765';

test('buildUrl/buildQueryString encode paths and skip empty values', () => {
  assert.equal(
    buildUrl(BASE, '/api/search', { q: 'sys_open', limit: 5, offset: undefined, empty: null }),
    `${BASE}/api/search?q=sys_open&limit=5`,
  );
  assert.equal(buildUrl(BASE, 'api/health'), `${BASE}/api/health`);
  assert.equal(buildQueryString({ tags: ['a', 'b'] }), 'tags=a&tags=b');
  assert.equal(buildQueryString(undefined), '');
  assert.throws(
    () => buildUrl('not-a-url', '/api/health'),
    (error) => error instanceof BackendError && error.code === 'INVALID_ARGUMENT',
  );
  assert.throws(() => buildUrl(BASE, ''), /不能为空/);
  assert.throws(() => buildUrl(BASE, 'http://evil.example/x'), /相对路径/);
});

test('request returns the CA envelope unchanged and sends JSON bodies', async () => {
  const envelope = { ok: true, data: { items: [1, 2], count: 2 } };
  const fetchImpl = createFakeFetch(() => jsonResponse(envelope));
  const client = createHttpClient({ fetchImpl });

  const result = await client.request(BASE, '/api/jobs', {
    method: 'POST',
    body: { kind: 'update', payload: { note: 'x' } },
    headers: { 'x-test': 'yes' },
  });
  assert.deepEqual(result, envelope);
  assert.equal(result.httpStatus, 200, 'transport metadata is non-enumerable');
  assert.deepEqual(Object.keys(result).sort(), ['data', 'ok']);

  const call = fetchImpl.calls[0];
  assert.equal(call.url, `${BASE}/api/jobs`);
  assert.equal(call.init.method, 'POST');
  assert.equal(call.init.body, JSON.stringify({ kind: 'update', payload: { note: 'x' } }));
  assert.equal(call.init.headers['content-type'], 'application/json');
  assert.equal(call.init.headers['x-test'], 'yes');
  assert.equal(call.init.redirect, 'error');
});

test('request passes through non-2xx CA envelopes (application errors)', async () => {
  const envelope = { ok: false, error: { code: 'NOT_FOUND', message: 'job not found' } };
  const fetchImpl = createFakeFetch(() => jsonResponse(envelope, 404));
  const client = createHttpClient({ fetchImpl });
  const result = await client.request(BASE, '/api/jobs/42');
  assert.deepEqual(result, envelope);
  assert.equal(result.httpStatus, 404);
});

test('request maps non-envelope HTTP errors to BackendError codes', async () => {
  const cases = [
    [403, 'HTTP_UNAUTHORIZED'],
    [404, 'HTTP_NOT_FOUND'],
    [409, 'HTTP_CONFLICT'],
    [500, 'HTTP_SERVER_ERROR'],
    [418, 'HTTP_STATUS'],
  ];
  for (const [status, code] of cases) {
    const fetchImpl = createFakeFetch(() => jsonResponse('<html>nope</html>', status));
    const client = createHttpClient({ fetchImpl });
    await assert.rejects(
      client.request(BASE, '/api/x'),
      (error) => {
        assert.ok(error instanceof BackendError);
        assert.equal(error.code, code);
        assert.equal(error.details.status, status);
        return true;
      },
    );
  }
});

test('request wraps bare 2xx JSON and empty bodies', async () => {
  const client = createHttpClient({
    fetchImpl: createFakeFetch((call) =>
      call.url.endsWith('/empty') ? jsonResponse('', 204) : jsonResponse({ hello: 'world' }),
    ),
  });
  assert.deepEqual(await client.request(BASE, '/api/bare'), { ok: true, data: { hello: 'world' } });
  assert.deepEqual(await client.request(BASE, '/api/empty'), { ok: true, data: null });

  const invalid = createHttpClient({ fetchImpl: createFakeFetch(() => jsonResponse('not json')) });
  await assert.rejects(
    invalid.request(BASE, '/api/bad'),
    (error) => error instanceof BackendError && error.code === 'HTTP_INVALID_JSON',
  );
});

test('request maps network failures and timeouts', async () => {
  const unreachable = createHttpClient({ fetchImpl: unreachableFetch() });
  await assert.rejects(
    unreachable.request(BASE, '/api/health', { timeoutMs: 500 }),
    (error) => {
      assert.equal(error.code, 'HTTP_NETWORK');
      assert.match(error.message, /无法连接 CA 后端/);
      return true;
    },
  );

  const hanging = createHttpClient({ fetchImpl: hangingFetch() });
  await assert.rejects(
    hanging.request(BASE, '/api/health', { timeoutMs: 20 }),
    (error) => {
      assert.equal(error.code, 'HTTP_TIMEOUT');
      assert.match(error.message, /超时/);
      return true;
    },
  );
});

test('request reports caller cancellation as CANCELLED', async () => {
  const client = createHttpClient({ fetchImpl: hangingFetch() });
  const controller = new AbortController();
  const promise = client.request(BASE, '/api/health', { timeoutMs: 5000, signal: controller.signal });
  setTimeout(() => controller.abort(), 10);
  await assert.rejects(promise, (error) => error.code === 'CANCELLED');
});

test('isEnvelope recognises CA envelopes only', () => {
  assert.equal(isEnvelope({ ok: true, data: null }), true);
  assert.equal(isEnvelope({ ok: false, error: {} }), true);
  assert.equal(isEnvelope({ data: {} }), false);
  assert.equal(isEnvelope([]), false);
  assert.equal(isEnvelope(null), false);
});

test('requestStream parses SSE events and resolves on close', async () => {
  const sse = createSseResponse();
  const fetchImpl = createFakeFetch(() => sse.response);
  const client = createHttpClient({ fetchImpl });
  const events = [];
  const handle = await client.requestStream(BASE, '/events', {
    onEvent: (event) => events.push(event),
  });
  sse.push(': ping\n\n');
  sse.push('id: 7\nevent: job.updated\ndata: {"a":1}\n\n');
  sse.push('id: 8\ndata: {"b":2}\n');
  sse.push('\n');
  await nextTick();
  handle.close();
  sse.close();
  const done = await handle.done;
  assert.equal(done.aborted, true);
  assert.equal(events.length, 2);
  assert.equal(events[0].event, 'job.updated');
  assert.equal(events[0].id, '7');
  assert.deepEqual(events[0].data, { a: 1 });
  assert.equal(events[1].event, 'message');
  assert.deepEqual(events[1].data, { b: 2 });
  assert.match(fetchImpl.calls[0].init.headers.accept, /text\/event-stream/);
});

test('requestStream surfaces HTTP status errors', async () => {
  const fetchImpl = createFakeFetch(() => jsonResponse('nope', 404));
  const client = createHttpClient({ fetchImpl });
  await assert.rejects(
    client.requestStream(BASE, '/events'),
    (error) => error instanceof BackendError && error.code === 'HTTP_NOT_FOUND',
  );
});

test('requestStream rejects when the consumer callback throws', async () => {
  const sse = createSseResponse();
  const client = createHttpClient({ fetchImpl: createFakeFetch(() => sse.response) });
  const handle = await client.requestStream(BASE, '/events', {
    onEvent: () => {
      throw new Error('consumer boom');
    },
  });
  sse.push('data: {}\n\n');
  sse.close();
  await assert.rejects(handle.done, (error) => error.code === 'HTTP_NETWORK');
});

test('requestStream resolves aborted when the caller signal fires', async () => {
  const fetchImpl = createFakeFetch((call) => {
    const sse = createSseResponse();
    call.init.signal?.addEventListener('abort', () => sse.abort(), { once: true });
    return sse.response;
  });
  const client = createHttpClient({ fetchImpl });
  const controller = new AbortController();
  const handle = await client.requestStream(BASE, '/events', { signal: controller.signal });
  controller.abort();
  const done = await handle.done;
  assert.equal(done.aborted, true);
});
