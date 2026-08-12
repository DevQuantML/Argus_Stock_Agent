'use strict';

/* api.js — every network call the app makes.
   Callers never see a raw Response; they get data or an ApiError.

   The agent key is NOT stored here. It used to sit in localStorage, readable
   by any script that ever ran on the page. It is now exchanged once for an
   HttpOnly session cookie the browser attaches automatically and JavaScript
   cannot read — see POST /api/session. `auth.authenticated` is a cached
   boolean, never a credential. */

export const auth = {
  authenticated: false,
  has() { return auth.authenticated; },

  async refresh() {
    try {
      const d = await request('/api/session');
      auth.authenticated = Boolean(d.authenticated);
    } catch { auth.authenticated = false; }
    return auth.authenticated;
  },

  /** Exchange AGENT_SECRET for a session cookie. Returns true on success. */
  async unlock(key) {
    try {
      await request('/api/session', { method: 'POST', body: { key: String(key || '') } });
      auth.authenticated = true;
    } catch { auth.authenticated = false; }
    return auth.authenticated;
  },

  async signOut() {
    try { await request('/api/session', { method: 'DELETE' }); } catch { /* already gone */ }
    auth.authenticated = false;
  },
};

export class ApiError extends Error {
  constructor(message, { status = 0, kind = 'generic' } = {}) {
    super(message);
    this.name   = 'ApiError';
    this.status = status;
    this.kind   = kind;   // network | unauthorized | ratelimit | notfound | server | config
  }
}

/* Maps a failed request to a message a person can act on. The server's own
   error strings are preferred when present — they name the actual problem
   (bad ticker suffix, missing key) better than a generic status message. */
function describe(status, body) {
  const detail = body && typeof body === 'object' ? body.detail : null;
  const server = (body && body.error) || (detail && detail.error) || null;

  if (status === 401) return new ApiError('Not authorised — unlock with your agent key.', { status, kind: 'unauthorized' });
  if (status === 429) return new ApiError('Rate limit reached — 20 requests per minute. Wait a moment.', { status, kind: 'ratelimit' });
  if (status === 400) return new ApiError(server || 'Ticker not found. Try adding .NS for NSE or .BO for BSE.', { status, kind: 'notfound' });
  if (status === 503) return new ApiError(server || 'Upstream data feed unavailable.', { status, kind: 'server' });
  return new ApiError(server || `Request failed (${status}).`, { status, kind: 'server' });
}

async function request(path, { method = 'GET', body = null, signal = null } = {}) {
  const headers = {};

  // credentials:'same-origin' sends the HttpOnly session cookie. There is no
  // key to attach by hand any more — that is the point of the change.
  const opts = { method, headers, signal, credentials: 'same-origin' };
  if (body !== null) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    // An aborted fetch rejects here too — without this it would surface as
    // "cannot reach the server", which is a lie when the user hit Stop.
    if (err && err.name === 'AbortError') {
      throw new ApiError('Cancelled.', { kind: 'aborted' });
    }
    throw new ApiError('Cannot reach the server. Is uvicorn still running?', { kind: 'network' });
  }

  let data = null;
  try { data = await res.json(); } catch { /* non-JSON or empty */ }

  if (!res.ok) throw describe(res.status, data);
  if (!data)   throw new ApiError('Server returned an empty response.', { status: res.status });

  return data;
}

export const getInfo      = ()                 => request('/api/info');
export const getBrent     = ()                 => request('/api/brent');
export const getHealth    = ()                 => request('/health');
export const getPortfolio = ()                 => request('/api/portfolio');
export const getQuant     = (ticker)           => request(`/api/demo/${encodeURIComponent(ticker)}`);
export const getHistory   = (ticker, period)   =>
  request(`/api/history/${encodeURIComponent(ticker)}?period=${encodeURIComponent(period)}`);

/* Every research route answers 200 with an {error} field when the server has
   no provider key configured, so a 200 is not proof of success. */
function throwIfErrorField(data) {
  if (!data.error) return data;
  const missingKey = /API_KEY|not configured|No AI provider/i.test(data.error);
  throw new ApiError(
    missingKey
      ? 'AI research unavailable — add PERPLEXITY_API_KEY or GROQ_API_KEY to your server .env file.'
      : data.error,
    { status: 200, kind: missingKey ? 'config' : 'server' },
  );
}

/* Legacy single-shot research — kept for curl and backward compatibility. */
export async function getResearch(ticker, question) {
  const q = question ? `?question=${encodeURIComponent(question)}` : '';
  return throwIfErrorField(
    await request(`/api/research/${encodeURIComponent(ticker)}${q}`, {}),
  );
}

/* Staged pipeline: one module per call so each renders as it lands and the
   run can be stopped part-way. */
export async function getResearchModule(ticker, module, { question, signal } = {}) {
  const q = module === 'report' && question ? `?question=${encodeURIComponent(question)}` : '';
  return throwIfErrorField(
    await request(`/api/research/${encodeURIComponent(ticker)}/${module}${q}`, { signal }),
  );
}

export async function postSynthesis(ticker, payload, { signal } = {}) {
  return throwIfErrorField(
    await request(`/api/research/${encodeURIComponent(ticker)}/synthesis`, {
      method: 'POST', body: payload, signal,
    }),
  );
}

/* ── Store-backed resources ──────────────────────────────────────────────
   All same-origin, all cookie-authenticated where the server requires it. */

export const getProfile   = ()      => request('/api/profile');
export const saveProfile  = (p)     => request('/api/profile', { method: 'POST', body: p });

export const getPositions = ()      => request('/api/positions');
export const savePosition = (p)     => request('/api/positions', { method: 'POST', body: p });
export const delPosition  = (t)     => request(`/api/positions/${encodeURIComponent(t)}`, { method: 'DELETE' });

export const getWatchlist = ()      => request('/api/watchlist');
export const addWatch     = (t, n)  => request('/api/watchlist', { method: 'POST', body: { ticker: t, note: n || '' } });
export const delWatch     = (t)     => request(`/api/watchlist/${encodeURIComponent(t)}`, { method: 'DELETE' });

export const getAnalytics = ()      => request('/api/portfolio/analytics');
export const getOutlook   = ()      => request('/api/portfolio/outlook');
export const runOutlook   = ({ signal } = {}) =>
  request('/api/portfolio/outlook', { method: 'POST', signal }).then(d => {
    if (d && d.error) throwIfErrorField(d);
    return d;
  });
