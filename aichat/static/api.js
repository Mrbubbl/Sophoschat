import { emit } from './store.js';

export class ApiError extends Error {
  constructor(status, code, message, extra) {
    super(message || code);
    this.status = status;
    this.code = code;
    this.extra = extra || {};
  }
}

export async function api(method, path, body, opts = {}) {
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData;
  let res;
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: body && !isForm ? { 'Content-Type': 'application/json' } : {},
      body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, 'network_error', '网络不可用，请检查连接');
  }
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('json') ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    const err = (data && data.error) || {};
    if (res.status === 401 && !opts.quiet401) emit('unauthenticated');
    const msg = err.message || (data && data.detail ? '请求参数不正确' : res.statusText);
    throw new ApiError(res.status, err.code || 'http_error', msg, err);
  }
  return data;
}

export const get = (p, o) => api('GET', p, null, o);
export const post = (p, b, o) => api('POST', p, b || {}, o);
export const patch = (p, b) => api('PATCH', p, b || {});
export const del = (p) => api('DELETE', p);

export function qs(params) {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== '') u.set(k, v);
  const s = u.toString();
  return s ? '?' + s : '';
}
