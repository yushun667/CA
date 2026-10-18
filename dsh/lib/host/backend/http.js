/**
 * Minimal HTTP client for the CA backend (`ca serve`).
 *
 * Uses the platform `fetch` (Node >= 18) and returns the CA JSON envelope
 * unchanged: `{ok:true,data}` / `{ok:false,error}`. Only transport problems
 * (timeout, connection refused, non-JSON garbage, non-envelope error pages)
 * throw BackendError; application-level errors stay in the envelope so the RPC
 * and tool layers can relay CA's own error codes.
 *
 * The `fetch` implementation is injectable for tests; all request state
 * (timeout timer, abort listeners) is cleaned up in `finally`, and streams are
 * fully cancellable so `dispose()` cannot leak sockets.
 */

import { BackendError, describeCause, isAbortError } from './errors.js';

/** HTTP status -> stable BackendError code. */
function statusCodeForStatus(status) {
  if (status === 401 || status === 403) return 'HTTP_UNAUTHORIZED';
  if (status === 404) return 'HTTP_NOT_FOUND';
  if (status === 409) return 'HTTP_CONFLICT';
  if (status >= 500) return 'HTTP_SERVER_ERROR';
  return 'HTTP_STATUS';
}

function truncate(text, max = 400) {
  const value = String(text ?? '');
  return value.length <= max ? value : `${value.slice(0, max)}…`;
}

/**
 * Serialize a query object into a URLSearchParams string. Arrays become
 * repeated keys; null/undefined values are omitted (CA treats a missing key
 * differently from an empty string).
 * @param {Record<string, unknown>|undefined} query
 */
export function buildQueryString(query) {
  if (!query || typeof query !== 'object') return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item === undefined || item === null) continue;
        params.append(key, String(item));
      }
    } else {
      params.append(key, String(value));
    }
  }
  return params.toString();
}

/**
 * Join a base URL like `http://127.0.0.1:8765` with a path like `/api/health`.
 * @param {string} baseUrl
 * @param {string} pathname
 * @param {Record<string, unknown>} [query]
 * @returns {string}
 */
export function buildUrl(baseUrl, pathname, query) {
  const base = String(baseUrl ?? '').trim();
  if (!/^https?:\/\//i.test(base)) {
    throw new BackendError('INVALID_ARGUMENT', `CA 后端地址无效: ${base || '(空)'}`, { baseUrl: base });
  }
  const rawPath = String(pathname ?? '').trim();
  if (rawPath.length === 0) {
    throw new BackendError('INVALID_ARGUMENT', 'CA 请求路径不能为空');
  }
  if (/^https?:\/\//i.test(rawPath)) {
    throw new BackendError('INVALID_ARGUMENT', `CA 请求路径必须是相对路径: ${rawPath}`);
  }
  const normalizedPath = rawPath.startsWith('/') ? rawPath : `/${rawPath}`;
  const url = new URL(normalizedPath, base.endsWith('/') ? base : `${base}/`);
  const queryString = buildQueryString(query);
  if (queryString) {
    const existing = url.search ? url.search.slice(1) : '';
    url.search = existing ? `${existing}&${queryString}` : queryString;
  }
  return url.toString();
}

/** True when `value` looks like a CA envelope. */
export function isEnvelope(value) {
  return (
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    typeof /** @type {Record<string, unknown>} */ (value).ok === 'boolean'
  );
}

/**
 * Attach non-enumerable transport metadata (`httpStatus`/`url`) to an envelope
 * without changing its JSON shape or deep-equality behavior.
 */
function annotateEnvelope(envelope, response, url) {
  try {
    Object.defineProperty(envelope, 'httpStatus', {
      value: response?.status,
      enumerable: false,
      configurable: true,
    });
    Object.defineProperty(envelope, 'url', { value: url, enumerable: false, configurable: true });
  } catch {
    // frozen envelope: metadata is optional
  }
  return envelope;
}

/**
 * @param {object} [options]
 * @param {typeof fetch|null} [options.fetchImpl]
 * @param {() => number} [options.now]
 * @param {number} [options.defaultTimeoutMs]
 * @param {number} [options.connectTimeoutMs]
 */
export function createHttpClient(options = {}) {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const defaultTimeoutMs = Math.max(0, Number(options.defaultTimeoutMs) || 30000);
  const connectTimeoutMs = Math.max(0, Number(options.connectTimeoutMs) || 30000);

  function requireFetch() {
    if (typeof fetchImpl !== 'function') {
      throw new BackendError('FETCH_UNAVAILABLE', '当前 Node 环境缺少 fetch（需要 Node >= 18）');
    }
    return fetchImpl;
  }

  /**
   * Perform a JSON request and return the CA envelope as-is.
   *
   * @param {string} baseUrl
   * @param {string} pathname
   * @param {object} [requestOptions]
   * @param {'GET'|'POST'|'PUT'|'DELETE'|'PATCH'} [requestOptions.method]
   * @param {Record<string, unknown>} [requestOptions.query]
   * @param {unknown} [requestOptions.body]
   * @param {number} [requestOptions.timeoutMs]
   * @param {AbortSignal} [requestOptions.signal]
   * @param {Record<string,string>} [requestOptions.headers]
   */
  async function request(baseUrl, pathname, requestOptions = {}) {
    const method = String(requestOptions.method || 'GET').toUpperCase();
    const timeoutMs =
      requestOptions.timeoutMs === undefined ? defaultTimeoutMs : Number(requestOptions.timeoutMs) || 0;
    const url = buildUrl(baseUrl, pathname, requestOptions.query);
    const fetchFn = requireFetch();

    let body;
    let contentType = null;
    if (requestOptions.body !== undefined && requestOptions.body !== null) {
      if (typeof requestOptions.body === 'string' || requestOptions.body instanceof Uint8Array) {
        body = requestOptions.body;
      } else {
        body = JSON.stringify(requestOptions.body);
        contentType = 'application/json';
      }
    }
    const headers = { accept: 'application/json', ...(requestOptions.headers || {}) };
    if (contentType) headers['content-type'] = contentType;

    const controller = new AbortController();
    let timedOut = false;
    let timer = null;
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
    }
    const callerSignal = requestOptions.signal;
    const relayAbort = () => controller.abort();
    if (callerSignal) {
      if (callerSignal.aborted) {
        if (timer) clearTimeout(timer);
        throw new BackendError('CANCELLED', `CA 请求已取消: ${method} ${url}`, { url, method });
      }
      if (typeof callerSignal.addEventListener === 'function') {
        callerSignal.addEventListener('abort', relayAbort, { once: true });
      }
    }

    let response;
    try {
      response = await fetchFn(url, {
        method,
        headers,
        body,
        signal: controller.signal,
        redirect: 'error',
      });
    } catch (error) {
      if (callerSignal?.aborted) {
        throw new BackendError('CANCELLED', `CA 请求已取消: ${method} ${url}`, { url, method });
      }
      if (timedOut) {
        throw new BackendError(
          'HTTP_TIMEOUT',
          `CA 请求超时（${timeoutMs}ms）：${method} ${url}；请检查 ca serve 是否运行或增大 timeoutMs`,
          { url, method, timeoutMs },
        );
      }
      throw new BackendError(
        'HTTP_NETWORK',
        `无法连接 CA 后端（${method} ${url}）：${describeCause(error)}；请确认后端已启动（backend.start）且端口可达`,
        { url, method, cause: describeCause(error) },
      );
    } finally {
      if (timer) clearTimeout(timer);
      if (callerSignal && typeof callerSignal.removeEventListener === 'function') {
        callerSignal.removeEventListener('abort', relayAbort);
      }
    }

    let text;
    try {
      text = await response.text();
    } catch (error) {
      if (callerSignal?.aborted) {
        throw new BackendError('CANCELLED', `CA 请求已取消: ${method} ${url}`, { url, method });
      }
      throw new BackendError(
        'HTTP_NETWORK',
        `读取 CA 响应失败（${method} ${url}）：${describeCause(error)}`,
        { url, method },
      );
    }

    let parsed;
    let parseError = null;
    if (text.trim().length > 0) {
      try {
        parsed = JSON.parse(text);
      } catch (error) {
        parseError = error;
      }
    }

    if (isEnvelope(parsed)) {
      return annotateEnvelope(parsed, response, url);
    }

    if (!response.ok) {
      const code = statusCodeForStatus(response.status);
      throw new BackendError(code, `CA 返回 HTTP ${response.status}（${method} ${url}）：${truncate(text)}`, {
        url,
        method,
        status: response.status,
        body: truncate(text),
      });
    }

    if (parseError) {
      throw new BackendError(
        'HTTP_INVALID_JSON',
        `CA 返回了非 JSON 响应（${method} ${url}）：${truncate(text)}`,
        { url, method, status: response.status },
      );
    }
    // 2xx with a bare JSON value or empty body: wrap it in the envelope shape
    // so callers always see `{ok,data}`.
    return annotateEnvelope({ ok: true, data: parsed === undefined ? null : parsed }, response, url);
  }

  /**
   * Open a Server-Sent-Events stream (used for `/events`).
   *
   * Resolves once response headers arrive; returns `{close, done, response}`.
   * `onEvent` is awaited per event so a slow consumer applies backpressure.
   * `done` resolves `{aborted:boolean}` on graceful end and rejects with a
   * BackendError on network/parse errors.
   *
   * @param {string} baseUrl
   * @param {string} pathname
   * @param {object} [streamOptions]
   * @param {Record<string, unknown>} [streamOptions.query]
   * @param {AbortSignal} [streamOptions.signal]
   * @param {(event: {event: string, id: string|null, data: unknown, raw: string}) => void|Promise<void>} [streamOptions.onEvent]
   * @param {number} [streamOptions.timeoutMs] connect timeout only
   * @param {Record<string,string>} [streamOptions.headers]
   * @param {string|null} [streamOptions.lastEventId]
   */
  async function requestStream(baseUrl, pathname, streamOptions = {}) {
    const url = buildUrl(baseUrl, pathname, streamOptions.query);
    const fetchFn = requireFetch();
    const timeoutMs =
      streamOptions.timeoutMs === undefined ? connectTimeoutMs : Number(streamOptions.timeoutMs) || 0;

    const controller = new AbortController();
    let timedOut = false;
    let timer = null;
    if (timeoutMs > 0) {
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
    }
    const callerSignal = streamOptions.signal;
    const relayAbort = () => controller.abort();
    if (callerSignal) {
      if (callerSignal.aborted) {
        if (timer) clearTimeout(timer);
        throw new BackendError('CANCELLED', `CA 事件流已取消: ${url}`, { url });
      }
      if (typeof callerSignal.addEventListener === 'function') {
        callerSignal.addEventListener('abort', relayAbort, { once: true });
      }
    }

    const headers = { accept: 'text/event-stream', ...(streamOptions.headers || {}) };
    if (streamOptions.lastEventId) headers['last-event-id'] = String(streamOptions.lastEventId);
    let response;
    try {
      response = await fetchFn(url, {
        method: 'GET',
        headers,
        signal: controller.signal,
        redirect: 'error',
      });
    } catch (error) {
      if (timer) clearTimeout(timer);
      if (callerSignal && typeof callerSignal.removeEventListener === 'function') {
        callerSignal.removeEventListener('abort', relayAbort);
      }
      if (callerSignal?.aborted) {
        throw new BackendError('CANCELLED', `CA 事件流已取消: ${url}`, { url });
      }
      if (timedOut) {
        throw new BackendError('HTTP_TIMEOUT', `CA 事件流连接超时（${timeoutMs}ms）：${url}`, { url, timeoutMs });
      }
      throw new BackendError(
        'HTTP_NETWORK',
        `无法连接 CA 事件流（${url}）：${describeCause(error)}`,
        { url, cause: describeCause(error) },
      );
    }
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }

    if (!response.ok) {
      let body = '';
      try {
        body = await response.text();
      } catch {
        // ignore: status is the primary signal
      }
      throw new BackendError(
        statusCodeForStatus(response.status),
        `CA 事件流返回 HTTP ${response.status}：${truncate(body)}`,
        { url, status: response.status },
      );
    }

    let closedByCaller = false;
    const onEvent = streamOptions.onEvent;

    const done = (async () => {
      let eventName = 'message';
      let dataLines = [];
      let lastId = streamOptions.lastEventId ? String(streamOptions.lastEventId) : null;
      const decoder = new TextDecoder();
      let buffer = '';

      const dispatch = async () => {
        const raw = dataLines.join('\n');
        const event = eventName;
        dataLines = [];
        eventName = 'message';
        if (raw.length === 0) return;
        let data = raw;
        try {
          data = JSON.parse(raw);
        } catch {
          // CA always sends JSON; keep raw text when it does not.
        }
        if (typeof onEvent === 'function') {
          await onEvent({ event, id: lastId, data, raw });
        }
      };

      try {
        if (response.body && typeof response.body.getReader === 'function') {
          const reader = response.body.getReader();
          try {
            for (;;) {
              const { done: streamDone, value } = await reader.read();
              if (streamDone) break;
              buffer += decoder.decode(value, { stream: true });
              let index = buffer.indexOf('\n');
              while (index >= 0) {
                let line = buffer.slice(0, index);
                buffer = buffer.slice(index + 1);
                if (line.endsWith('\r')) line = line.slice(0, -1);
                if (line.length === 0) {
                  await dispatch();
                } else if (line.startsWith(':')) {
                  // heartbeat/comment
                } else {
                  const colon = line.indexOf(':');
                  const field = colon === -1 ? line : line.slice(0, colon);
                  let fieldValue = colon === -1 ? '' : line.slice(colon + 1);
                  if (fieldValue.startsWith(' ')) fieldValue = fieldValue.slice(1);
                  if (field === 'event') eventName = fieldValue;
                  else if (field === 'id') lastId = fieldValue;
                  else if (field === 'data') dataLines.push(fieldValue);
                }
                index = buffer.indexOf('\n');
              }
            }
          } finally {
            try {
              reader.releaseLock();
            } catch {
              // already released
            }
          }
        }
        buffer += decoder.decode();
        if (buffer.trim().length > 0) {
          const line = buffer.endsWith('\r') ? buffer.slice(0, -1) : buffer;
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
          buffer = '';
        }
        await dispatch();
        return { aborted: closedByCaller };
      } catch (error) {
        if (closedByCaller || callerSignal?.aborted) return { aborted: true };
        if (isAbortError(error)) return { aborted: true };
        throw new BackendError('HTTP_NETWORK', `CA 事件流中断（${url}）：${describeCause(error)}`, {
          url,
          cause: describeCause(error),
        });
      } finally {
        if (callerSignal && typeof callerSignal.removeEventListener === 'function') {
          callerSignal.removeEventListener('abort', relayAbort);
        }
      }
    })();

    return {
      response,
      url,
      /** Stop the stream; `done` resolves `{aborted:true}`. */
      close() {
        closedByCaller = true;
        try {
          controller.abort();
        } catch {
          // already aborted
        }
      },
      done,
    };
  }

  return { request, requestStream, buildUrl, baseUrlOf: (baseUrl) => buildUrl(baseUrl, '/') };
}
