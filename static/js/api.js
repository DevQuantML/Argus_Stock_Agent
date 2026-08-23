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
  stale: false,        // true when the last refresh could not reach the server
  tier: null,          // 'owner' | 'guest' | null — null means no session at all
  guest: null,         // {expires_at, dive_ticker, modules_total, modules_used, exhausted}

  has()     { return auth.authenticated; },
  isOwner() { return auth.tier === 'owner'; },
  isGuest() { return auth.tier === 'guest'; },

  /* 'visitor' is a frontend-only tier: someone browsing with no session at all.
     The server never issues it, so it is derived here rather than read. */
  effectiveTier() { return auth.tier || 'visitor'; },

  /* Stages left in a guest's single included dive. Deliberately a count: the
     allowance is five separately consumable stages, and a guest one stage in
     still has four. Reporting that as a spent/unspent boolean tells them their
     dive is gone while most of it remains. */
  divesLeft() {
    const g = auth.guest;
    if (!g) return 0;
    return Math.max(0, (g.modules_total || 0) - (g.modules_used || 0));
  },

  /* Hours until a guest key dies, from the SERVER's expires_at. Never assume
     the two clocks agree — this is display only, and the server re-checks. */
  hoursLeft() {
    if (!auth.guest?.expires_at) return null;
    const ms = Date.parse(auth.guest.expires_at) - Date.now();
    return Number.isFinite(ms) ? Math.max(0, ms / 3.6e6) : null;
  },

  _apply(d) {
    auth.authenticated = Boolean(d && d.authenticated);
    auth.tier  = (d && d.tier) || null;
    auth.guest = (d && d.guest) || null;
  },

  async refresh() {
    /* Only an ANSWER clears the cached identity.

       GET /api/session is ungated and returns 200 {authenticated:false} when
       there genuinely is no session — so a throw here means something else:
       a dropped network, a 5xx, or a 429. Treating those as "no session"
       downgraded a live keyholder to visitor: at boot the owner was shown the
       public landing asking them to request access to their own terminal, and
       mid-run a guest's chip flipped to FREE MODE with four stages still
       unspent. The cookie is HttpOnly and still there; only our cache was
       wrong. */
    try {
      auth._apply(await request('/api/session'));
      auth.stale = false;
    } catch (err) {
      if (err && err.status === 401) {
        auth._apply(null);        // authoritative: the session is gone
        auth.stale = false;
      } else {
        auth.stale = true;        // keep what we had; the server never answered
      }
    }
    return auth.authenticated;
  },

  /** Exchange AGENT_SECRET for a session cookie. Returns true on success. */
  // Returns { ok } on success, or { ok:false, status, message } on failure.
  // It used to return a bare boolean, which threw away the status — so the
  // unlock screen showed "Rejected." for a wrong key, a lockout, and a dead
  // server alike. Being locked out looks exactly like guessing wrong, so the
  // natural response is to try again, which extends the lockout. The caller
  // needs the status to say something true.
  async unlock(key) {
    try {
      const d = await request('/api/session', { method: 'POST', body: { key: String(key || '') } });
      // POST returns the same shape as GET, so the tier and allowance are known
      // immediately — no second round trip before painting the key chip.
      auth._apply({ authenticated: true, tier: d.tier, guest: d.guest });
      return { ok: true, tier: d.tier, guest: d.guest };
    } catch (err) {
      auth._apply(null);
      return {
        ok:      false,
        status:  (err && err.status) || 0,
        code:    (err && err.code) || '',
        message: (err && err.message) || '',
      };
    }
  },

  async signOut() {
    /* Returns whether the server actually confirmed it. The cookie is cleared
       by the DELETE's response, so if that never arrived the session is still
       live and a reload restores full access — which is the wrong thing to
       report as "signed out" on a machine someone else can reach, and a guest
       key exists precisely to be used on someone else's machine. */
    let confirmed = false;
    try {
      await request('/api/session', { method: 'DELETE' });
      confirmed = true;
    } catch (err) {
      confirmed = Boolean(err && err.status === 401);   // already gone counts
    }
    auth._apply(null);
    return confirmed;
  },
};

export class ApiError extends Error {
  constructor(message, { status = 0, kind = 'generic', code = '', detail = null } = {}) {
    super(message);
    this.name   = 'ApiError';
    this.status = status;
    // network | unauthorized | forbidden | budget | bound | stage | expired
    // | provider | ratelimit | notfound | server | config | aborted
    this.kind   = kind;
    this.code   = code;     // the server's machine-readable reason, when it sent one
    this.detail = detail;   // structured extras, e.g. {bound_ticker}
  }
}

/* Maps a failed request to a message a person can act on. The server's own
   error strings are preferred when present — they name the actual problem
   (bad ticker suffix, missing key) better than a generic status message. */
function describe(status, body) {
  const d = body && typeof body === 'object' ? body.detail : null;
  const server = (body && body.error) || (d && d.error) || null;

  // FastAPI nests HTTPException payloads under `detail`, while our own
  // JSONResponse bodies are flat. Look in both, or half the codes go missing.
  const code   = (body && body.code) || (d && d.code) || '';
  const extra  = (body && body.detail && !d?.error ? body.detail : null) || null;

  // Status alone is not enough to act on. The staged research loop treats a
  // generic failure as a per-module warning and CARRIES ON to the next stage —
  // correct for a flaky upstream, badly wrong for "your allowance is spent",
  // which would fire four more doomed paid requests and print four red toasts.
  // These kinds exist so the caller can stop.
  if (status === 402) {
    // 'stage' is recoverable and 'budget' is terminal. Collapsing them halted
    // the whole run over a single already-claimed stage, stranding the rest of
    // the guest's allowance.
    return new ApiError(server || 'Your included deep dive is already used.',
                        { status, kind: code === 'guest_stage_used' ? 'stage' : 'budget',
                          code, detail: extra });
  }
  if (status === 409 && code === 'guest_ticker_bound') {
    const t = extra && extra.bound_ticker;
    return new ApiError(
      t ? `Your guest key is committed to ${t} and cannot be moved to another ticker.`
        : (server || 'Your guest key is committed to a different ticker.'),
      { status, kind: 'bound', code, detail: extra });
  }
  if (status === 403) {
    return new ApiError(server || 'That action belongs to the terminal\'s owner.',
                        { status, kind: 'forbidden', code });
  }
  if (status === 401) {
    // A guest whose key died must NOT be sent to the unlock screen to enter an
    // operator secret they were never given.
    if (code === 'guest_expired' || code === 'session_expired') {
      return new ApiError(server || 'Your session has expired.',
                          { status, kind: 'expired', code });
    }
    return new ApiError('Not authorised — unlock with your key.', { status, kind: 'unauthorized', code });
  }
  if (status === 502 && code === 'provider_unavailable') {
    // Terminal for the run. Every remaining stage calls the same provider, so
    // carrying on means four more failures and four more toasts. Nothing was
    // charged for the ones that were refused before billing.
    return new ApiError(server || 'The research provider is unavailable right now.',
                        { status, kind: 'provider', code });
  }
  if (status === 429) return new ApiError(server || 'Rate limit reached. Wait a moment.', { status, kind: 'ratelimit', code });
  if (status === 400) return new ApiError(server || 'Ticker not found. Try adding .NS for NSE or .BO for BSE.', { status, kind: 'notfound', code });
  if (status === 503) return new ApiError(server || 'Upstream data feed unavailable.', { status, kind: 'server', code });
  return new ApiError(server || `Request failed (${status}).`, { status, kind: 'server', code });
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

/* ── Public: no session needed ───────────────────────────────────────────── */

/* Framework + disclosures for a keyless visitor. Deliberately NOT /api/info,
   which also carries the book and stays gated. */
export const getMeta     = ()   => request('/api/meta');
export const getNews     = (t)  => request(`/api/news/${encodeURIComponent(t)}`);
export const getEarnings = (t)  => request(`/api/earnings/${encodeURIComponent(t)}`);

/* Ask the operator for a key. The reply is deliberately identical whether the
   address is new, pending, approved or previously denied — do not try to
   distinguish them here, there is nothing to distinguish. */
export const requestAccess = (email, note) =>
  request('/api/access-request', { method: 'POST', body: { email, note: note || '' } });

/* ── Owner-only administration ───────────────────────────────────────────── */

export const adminRequests = ()    => request('/api/admin/requests');
export const adminApprove  = (id)  => request(`/api/admin/requests/${id}/approve`, { method: 'POST' });
export const adminDeny     = (id)  => request(`/api/admin/requests/${id}/deny`,    { method: 'POST' });
export const adminReopen   = (id)  => request(`/api/admin/requests/${id}/reopen`,  { method: 'POST' });
export const adminGuestKeys= ()    => request('/api/admin/guest-keys');
export const adminRevoke   = (id)  => request(`/api/admin/guest-keys/${id}/revoke`, { method: 'POST' });
export const adminRevokeAll= ()    => request('/api/admin/sessions/revoke-all', { method: 'POST' });
export const runOutlook   = ({ signal } = {}) =>
  request('/api/portfolio/outlook', { method: 'POST', signal }).then(d => {
    if (d && d.error) throwIfErrorField(d);
    return d;
  });
