"""
api.py — FastAPI HTTP wrapper for cloud deployment.

Once deployed, call your agent via HTTP from anywhere:
    curl "https://your-app.railway.app/api/demo/PLTR"
    curl -H "X-Agent-Key: $AGENT_SECRET" https://your-app.railway.app/api/research/PLTR
    curl "https://your-app.railway.app/health"

Run locally:
    uvicorn api:app --reload

Research runs on Perplexity (live web search), falling back to Groq when only
GROQ_API_KEY is set. See tools/perplexity_research.py:_get_provider().

Hardening applied:
  Task 2 — All route handlers wrapped in try/except; return {"error": str(e)} 500.
           Never expose a Python stack trace to the HTTP caller.
  Task 3 — X-Agent-Key header auth against AGENT_SECRET env var (401 if wrong).
           In-memory rate limiter: 20 requests / 60 seconds per IP (429).
           require_auth    — paid routes (/api/research/*), metered.
           require_session — user-data routes (positions, watchlist, profile,
                             portfolio, analytics, /api/info); meters failures
                             only, so a live session is not locked out of its
                             own dashboard.
           Public: /, /health, /api/demo, /api/history, /api/brent — market
           data only, nothing that reads the book.
  Task 4 — GET /health: checks an AI provider is configured + the Brent feed.
           Always returns 200 with {"status": "healthy"|"degraded", "checks": {...}}.
"""

import hmac
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# Load .env before anything else
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — fall back to real env vars

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

import store
from config import BRENT_LEVELS, GEO_TRANSMISSION
from tools.oil_price import get_brent_price, get_brent_signal
from tools.perplexity_research import (
    run_demo,
    run_perplexity_research,
    run_portfolio_outlook,
    run_research_module,
    run_synthesis,
)
from tools.stock_data import get_price_and_fundamentals, get_price_history
from tools.xirr import xirr

store.bootstrap()

_STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Stock Research Agent",
    description="Personal institutional-grade research agent with Brent crude framework",
    version="1.0.0",
)

# ── No CORS, deliberately ─────────────────────────────────────────────────
# There was a CORSMiddleware here with allow_origins=["*"]. That is a data
# leak on this API: /api/portfolio needs no auth and returns shares, avg_cost,
# cost basis, P&L, stop-loss and thesis text. With a wildcard ACAO header, any
# page open in the same browser could fetch the user's entire financial
# position from localhost.
#
# The frontend is served same-origin by GET /, so CORS bought nothing. If
# cross-origin dev is ever needed, add an explicit origin allowlist behind an
# env var — never a wildcard on an API that returns holdings.


# ── Security headers middleware ────────────────────────────────────────────
# Added to every response. Prevents MIME sniffing, clickjacking, and basic
# XSS attacks even though this is a JSON API (defence in depth).
#
# The CSP is path-aware. A blanket "default-src 'none'" also applies to GET /
# and blocks the SPA's own stylesheet, modules, and fetch() calls — the page
# renders as unstyled HTML with no JS. Document + asset routes therefore get a
# policy that permits same-origin resources only; every JSON route keeps the
# original lockdown.
#
# The frontend ships zero external dependencies (no CDNs, no web fonts), so
# 'self' is the entire allowlist. 'unsafe-inline' is needed for style only —
# animated bar widths are set via element.style — never for script.

_PAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)
_API_CSP = "default-src 'none'; frame-ancestors 'none'"


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Meter the request, then decorate whatever response comes back.

    The limiter runs here rather than on individual routes so that a route
    added later cannot silently miss it — see _budget_for(). Both the early 429
    and the normal response leave through the same header block below, because
    a rejected request needs the security headers just as much as a served one.
    """
    path = request.url.path

    budget = _budget_for(path)
    if budget is not None:
        bucket, limit, window = budget
        if not _check_rate_limit(_client_ip(request), bucket, limit, window):
            logger.warning("rate_limit: %s exceeded %d req/%ds on bucket '%s' (%s)",
                           _client_ip(request), limit, window, bucket, path)
            response = JSONResponse(
                status_code=429,
                content={"error": "rate limit exceeded"},
                headers={"Retry-After": str(window)},
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    # Explicitly 0, not "1; mode=block". The legacy auditor is deprecated and
    # in some browsers introduced vulnerabilities of its own; with a real CSP
    # in place the correct value is off.
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "no-referrer"

    is_page = path == "/" or path.startswith("/static/")
    response.headers["Content-Security-Policy"] = _PAGE_CSP if is_page else _API_CSP
    return response


# ── Task 3: In-memory rate limiter ────────────────────────────────────────
# Stores {ip: [timestamp, timestamp, ...]} for a rolling 60-second window.
# No external packages — plain dict + time.time().

_RATE_LIMIT_MAX      = 20    # requests
_RATE_LIMIT_WINDOW   = 60    # seconds
_RATE_LIMIT_IP_CAP   = 500   # max unique IPs tracked — prevents memory exhaustion
                              # from IP-spoofing / scanner floods
_rate_limit_store: dict[str, list[float]] = defaultdict(list)


def _trusted_hops() -> int:
    """How many reverse proxies you control sit in front of this app.

    0 (the default) means none — the forwarded headers are ignored entirely and
    the socket peer is the only thing trusted. TRUST_PROXY=1 means one proxy,
    which is the Railway / Fly / Render case.
    """
    try:
        return max(0, int(os.getenv("TRUST_PROXY", "0")))
    except ValueError:
        # A non-numeric value is an operator mistake. Fail closed: trusting no
        # proxy costs correctness behind one, but trusting a wrong count costs
        # the rate limiter entirely.
        logger.warning("TRUST_PROXY is not an integer; treating as 0 (trust nothing)")
        return 0


def _forwarded_value(request: Request, header: str) -> str | None:
    """Read the entry a trusted proxy wrote into a comma-separated hop header.

    X-Forwarded-* headers are *appended* to, never replaced. A request arriving
    through one proxy carries `<whatever the client sent>, <what the proxy saw>`.
    So the LEFTMOST entry is attacker-controlled and the RIGHTMOST entries are
    the ones your own proxies wrote. Indexing from the left — which this code
    used to do — hands an attacker a free-form field: rotating it yields a fresh
    rate-limit bucket on every request, so the 20/min cap never fires and
    POST /api/session becomes brute-forceable.

    Counting `hops` back from the right lands on the value written by the
    outermost proxy you actually control, which no client can forge.
    """
    hops = _trusted_hops()
    if hops == 0:
        return None
    raw = request.headers.get(header, "")
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    # Fewer entries than trusted hops means the chain is not what the operator
    # described — the request did not traverse the expected proxies. Refuse to
    # guess rather than read an attacker-supplied position.
    if len(parts) < hops:
        return None
    return parts[-hops]


def _client_ip(request: Request) -> str:
    """Identify the caller for rate-limiting purposes.

    request.client.host is the *socket* peer, so behind a reverse proxy every
    caller collapses into one bucket and a single client can exhaust the limit
    for everyone. X-Forwarded-For fixes that, but only the portion your own
    proxies wrote is trustworthy — see _forwarded_value().
    """
    fwd = _forwarded_value(request, "X-Forwarded-For")
    if fwd:
        return fwd[:64]
    return request.client.host if request.client else "unknown"


def _socket_ip(request: Request) -> str:
    """The raw TCP peer, ignoring every forwarded header.

    Cannot be spoofed by a client at any distance, because it is the address
    packets actually came from. Behind a proxy this collapses to the proxy's
    own address, which makes it useless for general rate limiting but exactly
    right as a backstop on failed authentication: even a caller who fabricates
    a perfect X-Forwarded-For chain still shares one bucket here.
    """
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    """True when this request reached the browser over HTTPS.

    request.url.scheme reflects the raw connection uvicorn accepted, which is
    plain HTTP whenever a TLS-terminating reverse proxy sits in front (Railway,
    Fly, Render all do this) — the proxy speaks HTTPS to the browser and HTTP
    to us. Left unchecked, the session cookie's Secure flag silently comes out
    False on a genuinely HTTPS site. Same trust boundary and same right-indexing
    as _client_ip(): a client-supplied "https" must never flip this on.
    """
    fwd = _forwarded_value(request, "X-Forwarded-Proto")
    if fwd:
        return fwd.lower() == "https"
    return request.url.scheme == "https"


def _check_rate_limit(ip: str, bucket: str = "auth",
                      limit: int = _RATE_LIMIT_MAX,
                      window: int = _RATE_LIMIT_WINDOW) -> bool:
    """
    Returns True if the request should be allowed, False if rate-limited.
    Prunes stale timestamps on every call (O(n) but trivial at this scale).

    `bucket` namespaces the key, so different classes of route meter
    independently. That independence is the point: the middleware meters every
    /api/* path, and the auth dependencies meter the same paid requests again.
    Sharing one budget would charge a research call twice and halve the limit
    that was actually sized for it. With separate buckets the effective limit is
    simply the tighter of the two, which is the intended behaviour.

    Memory cap: once the store holds _RATE_LIMIT_IP_CAP unique keys, new unseen
    keys are rejected until old entries are pruned. This prevents an attacker
    from exhausting memory by sending requests from millions of spoofed IPs.
    """
    key = f"{bucket}:{ip}"
    now = time.time()
    cutoff = now - window

    # Prune this key's stale timestamps
    _rate_limit_store[key] = [t for t in _rate_limit_store[key] if t > cutoff]

    # If this is a new key and we're at the cap, do a global prune first
    if key not in _rate_limit_store or not _rate_limit_store[key]:
        if len(_rate_limit_store) >= _RATE_LIMIT_IP_CAP:
            # Remove all keys whose windows have fully expired
            expired = [
                k for k, v in _rate_limit_store.items()
                if not v or all(t <= cutoff for t in v)
            ]
            for k in expired:
                del _rate_limit_store[k]
            # If still at cap after pruning, reject the new key
            if len(_rate_limit_store) >= _RATE_LIMIT_IP_CAP:
                logger.warning(
                    "rate_limit: store at capacity (%d), rejecting new key %s",
                    _RATE_LIMIT_IP_CAP, key,
                )
                return False

    if len(_rate_limit_store[key]) >= limit:
        return False

    _rate_limit_store[key].append(now)
    return True


# ── Per-path-class budgets for the middleware limiter ─────────────────────
# Previously only require_auth and api_session_create metered anything, so
# /api/demo, /api/history and /api/brent were completely unlimited, and
# /api/portfolio fans out several concurrent yfinance calls per request — N
# requests became many times N upstream calls.
#
# Classification lives here rather than on the routes for one specific reason:
# a route added later must not be able to silently miss the limiter. Anything
# under /api/ that matches no prefix below falls through to _API_DEFAULT_BUDGET,
# so a new endpoint is metered from the moment it exists.
#
# (bucket, limit, window_seconds), most specific prefix first.
_PATH_BUDGETS: tuple[tuple[str, str, int, int], ...] = (
    ("/api/portfolio",  "heavy",  10, 60),   # fans out to yfinance
    ("/api/demo",       "market", 30, 60),
    ("/api/history",    "market", 30, 60),
    ("/api/brent",      "market", 30, 60),
)
_API_DEFAULT_BUDGET = ("default", 60, 60)

# Never metered. /health is exempt deliberately: cloud platforms read a non-200
# health check as a dead instance and will recycle the container, so a 429 here
# would take the app down under exactly the load the limiter exists to survive.
# Pages and static assets are exempt because one page load pulls several files
# and would burn a per-minute budget on a single visit.
_UNMETERED_EXACT = frozenset({"/health", "/"})
_UNMETERED_PREFIX = ("/static/",)


def _budget_for(path: str) -> tuple[str, int, int] | None:
    """Return (bucket, limit, window) for a path, or None when unmetered."""
    if path in _UNMETERED_EXACT or path.startswith(_UNMETERED_PREFIX):
        return None
    for prefix, bucket, limit, window in _PATH_BUDGETS:
        if path.startswith(prefix):
            return bucket, limit, window
    if path.startswith("/api/"):
        return _API_DEFAULT_BUDGET
    return None


# ── Failed-auth backstop ──────────────────────────────────────────────────
# Keyed on the socket peer rather than _client_ip(), and deliberately so. This
# is the layer that still holds when the forwarded-header configuration is
# wrong: TRUST_PROXY set too high, a proxy that does not append, a chain that
# changed underneath the operator. Rate limiting wants per-client granularity
# and accepts proxy-derived identity to get it; brute-force defence wants an
# identity nobody can influence, and accepts the coarseness that comes with it.
#
# Only *failures* are counted, so a legitimate operator who types their key
# correctly never touches this path however often they sign in.

_AUTH_FAIL_MAX    = 10    # failed unlocks ...
_AUTH_FAIL_WINDOW = 900   # ... per socket peer per 15 minutes
_auth_fail_store: dict[str, list[float]] = defaultdict(list)


def _auth_failures_exceeded(socket_ip: str) -> bool:
    """True when this socket peer has burned through its failed-unlock budget."""
    now = time.time()
    cutoff = now - _AUTH_FAIL_WINDOW
    recent = [t for t in _auth_fail_store[socket_ip] if t > cutoff]
    _auth_fail_store[socket_ip] = recent
    return len(recent) >= _AUTH_FAIL_MAX


def _record_auth_failure(socket_ip: str) -> None:
    """Count one failed unlock, pruning the store so it cannot grow unbounded."""
    now = time.time()
    cutoff = now - _AUTH_FAIL_WINDOW
    if len(_auth_fail_store) >= _RATE_LIMIT_IP_CAP:
        for k in [k for k, v in _auth_fail_store.items()
                  if not v or all(t <= cutoff for t in v)]:
            del _auth_fail_store[k]
    _auth_fail_store[socket_ip].append(now)


# ── Task 3: API key auth dependency ───────────────────────────────────────

SESSION_COOKIE = "argus_session"


def _agent_key_ok(candidate: str | None) -> bool:
    """Constant-time comparison against AGENT_SECRET.

    hmac.compare_digest, not `!=`: a short-circuiting string compare returns
    faster the earlier it mismatches, which leaks the matching prefix length
    to an attacker who can time requests.
    """
    secret = os.getenv("AGENT_SECRET")
    if not secret or not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), secret.encode("utf-8"))


def _credential_ok(request: Request, x_agent_key: str | None) -> bool:
    """True when the caller presents a valid session cookie or agent key.

    Fail-closed: with AGENT_SECRET unset there is no credential that can be
    valid, so every caller is rejected rather than waved through.
    """
    if not os.getenv("AGENT_SECRET"):
        # Log by name only — never the value.
        logger.warning("auth: AGENT_SECRET is not set — all auth requests rejected")
        return False

    token = request.cookies.get(SESSION_COOKIE)
    if token and store.session_valid(token):
        return True

    return _agent_key_ok(x_agent_key)


async def require_auth(
    request: Request,
    x_agent_key: str = Header(default=None, alias="X-Agent-Key"),
) -> None:
    """
    FastAPI dependency guarding every paid route (/api/research/*).

    Accepts EITHER a valid session cookie (browser, set by POST /api/session)
    OR the X-Agent-Key header (curl and scripts). The cookie exists so the key
    never has to live in localStorage where any script could read it.

    401 if neither credential is valid. 429 if the caller exceeds the limit.
    """
    from fastapi import HTTPException

    # Rate limit runs before auth so a failed key cannot be probed for free.
    client_ip = _client_ip(request)
    if not _check_rate_limit(client_ip):
        logger.warning("rate_limit: IP %s exceeded %d req/%ds",
                       client_ip, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW)
        raise HTTPException(status_code=429, detail={"error": "rate limit exceeded"})

    if _credential_ok(request, x_agent_key):
        return

    logger.warning("require_auth: rejected request from IP %s — no valid session or key", client_ip)
    raise HTTPException(status_code=401, detail={"error": "unauthorized"})


async def require_session(
    request: Request,
    x_agent_key: str = Header(default=None, alias="X-Agent-Key"),
) -> None:
    """
    FastAPI dependency guarding the user-data routes — positions, watchlist,
    profile, portfolio, analytics.

    These are free to serve but they read and write the operator's book:
    share counts, cost basis, stop-loss levels and private thesis text. They
    were previously open to any caller, which made the whole book readable and
    writable by anyone who could reach the origin.

    Same credentials as require_auth. It differs in one way: a *valid*
    credential is not metered. require_auth's limiter is sized for paid
    research — 20/min covers about four 5-call runs — whereas these routes are
    hit several times on every boot and on every watchlist edit, so metering
    them from the same budget would lock a legitimate session out of its own
    dashboard. Failed attempts are still metered, which is what stops the key
    being brute-forced through this door.
    """
    from fastapi import HTTPException

    if _credential_ok(request, x_agent_key):
        return

    client_ip = _client_ip(request)
    if not _check_rate_limit(client_ip):
        logger.warning("rate_limit: IP %s exceeded %d req/%ds on a failed data-route auth",
                       client_ip, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW)
        raise HTTPException(status_code=429, detail={"error": "rate limit exceeded"})

    logger.warning("require_session: rejected request from IP %s — no valid session or key", client_ip)
    raise HTTPException(status_code=401, detail={"error": "unauthorized"})


# ── API info — session required ──────────────────────────────────────────
# Gated, not public: it returns the holdings list and the watchlist thesis
# text, which is the operator's own research and not something to hand to an
# anonymous caller.

@app.get("/api/info", dependencies=[Depends(require_session)])
def api_info():
    """API status, framework config, and available endpoints.

    `geo_transmission` and `brent_levels` are surfaced so the terminal can
    render the framework it is actually gated on, rather than restating it in
    the frontend where the two could drift apart.
    """
    return {
        "status": "online",
        "portfolio": list(store.positions().keys()),
        "watchlist": {
            t: {"thesis": w.get("thesis"), "tranches": w.get("tranches"),
                "brent_gated": w.get("brent_gated"), "exchange": w.get("exchange")}
            for t, w in store.watchlist().items()
        },
        "brent_levels": {
            k: {"range": list(v["range"]), "signal": v["signal"], "action": v["action"]}
            for k, v in BRENT_LEVELS.items()
        },
        "geo_transmission": GEO_TRANSMISSION,
        "endpoints": {
            "GET /api/research/{ticker}":           "Full research brief in one call (auth required)",
            "GET /api/research/{ticker}/report":    "Stage 1 — institutional brief (auth required)",
            "GET /api/research/{ticker}/context":   "Stage 2 — business model + financials (auth required)",
            "GET /api/research/{ticker}/policy":    "Stage 3 — politician trades + overlap (auth required)",
            "GET /api/research/{ticker}/patterns":  "Stage 4 — cross-source hidden patterns (auth required)",
            "POST /api/research/{ticker}/synthesis": "Stage 5 — stitched BUY/SELL/HOLD verdict (auth required)",
            "GET /api/demo/{ticker}":               "Quant metrics only, no LLM (public)",
            "GET /api/history/{ticker}?period=1y":  "Daily close series for charts (public)",
            "GET /api/brent":                       "Current Brent crude signal (public)",
            "GET /health":                          "Service health check (public)",
            "GET /api/portfolio":                   "Live price + P&L for every holding (session required)",
            "GET /api/positions":                   "The book — shares, cost basis, thesis (session required)",
            "GET /api/watchlist":                   "Watchlist entries (session required)",
            "GET /api/portfolio/analytics":         "Invested, P&L, XIRR (session required)",
        }
    }


# ── Task 4: Health endpoint — no auth required ────────────────────────────

@app.get("/health")
def health():
    """
    Health check for cloud platforms (Railway, Fly.io, Render auto-probe this).

    Checks:
      1. An AI provider is configured — PERPLEXITY_API_KEY or GROQ_API_KEY
      2. get_brent_price() returns a float without throwing

    perplexity_key and groq_key are reported individually so the frontend can
    tell which provider will actually serve a run (Perplexity has live web
    search, Groq does not). Only `ai_provider` feeds the overall status —
    having just one of the two is a fully working configuration, not degraded.

    Always returns HTTP 200 — never 500.
    Status field is "healthy" if all checks pass, "degraded" otherwise.
    """
    checks = {}

    # Check 1: AI provider — Perplexity preferred, Groq is the fallback
    perplexity_key = os.getenv("PERPLEXITY_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")
    checks["perplexity_key"] = bool(perplexity_key)
    checks["groq_key"] = bool(groq_key)
    checks["ai_provider"] = bool(perplexity_key or groq_key)
    if not checks["ai_provider"]:
        logger.warning("health: no AI provider — set PERPLEXITY_API_KEY or GROQ_API_KEY")

    # Check 2: Brent price feed
    try:
        price = get_brent_price()
        checks["brent_feed"] = isinstance(price, float)
        if not checks["brent_feed"]:
            logger.warning("health: get_brent_price() returned non-float: %r", price)
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: get_brent_price() raised: %s", exc)
        checks["brent_feed"] = False

    # perplexity_key/groq_key are informational — one provider is enough.
    overall = "healthy" if (checks["ai_provider"] and checks["brent_feed"]) else "degraded"

    # Always 200 — cloud platforms must not see 500 from health endpoints
    return JSONResponse(
        status_code=200,
        content={"status": overall, "checks": checks},
    )


# ── Frontend serving ──────────────────────────────────────────────────────
# Serves the static SPA from the /static directory.
# HTMLResponse used directly (no aiofiles needed — synchronous reads are fine
# for small assets at this traffic level).

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_frontend():
    """Serve the main SPA."""
    html_file = _STATIC_DIR / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>Frontend not found — run build first.</h1>", status_code=404)
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


# The v2 landing-page frontend is NOT served. static/v2/ stays in the tree as
# unwired source, but the route is gone until it is rewritten, because that
# code contradicts three decisions this app made deliberately:
#   * it keeps AGENT_SECRET in localStorage (static/v2/app.js), which is
#     exactly what the HttpOnly session cookie above exists to prevent;
#   * it loads marked.js and Google Fonts from CDNs, breaking the
#     zero-external-request rule the CSP enforces;
#   * it pipes model output through marked into innerHTML, and marked does not
#     escape HTML — that is an XSS sink. The main frontend routes model output
#     through md.js, which escapes before parsing.
# None of that was reachable, because /v2 matched neither arm of the is_page
# test below and so received the API's "default-src 'none'" policy, which
# stopped the page working at all. Do NOT "fix" that by adding /v2 to is_page:
# it would switch all three problems on at once.


@app.get("/static/{filename:path}", include_in_schema=False)
def serve_static(filename: str):
    """
    Serve static assets (CSS, JS, images) with path-traversal protection.
    Security: rejects any path containing '..' or absolute segments.
    """
    # Reject path-traversal attempts
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        return Response(status_code=404)

    file_path = _STATIC_DIR / filename

    # Double-check resolved path stays inside static dir
    try:
        file_path.resolve().relative_to(_STATIC_DIR.resolve())
    except ValueError:
        return Response(status_code=404)

    if not file_path.exists() or not file_path.is_file():
        return Response(status_code=404)

    MIME = {
        ".css":   "text/css",
        ".js":    "application/javascript",
        ".png":   "image/png",
        ".jpg":   "image/jpeg",
        ".svg":   "image/svg+xml",
        ".ico":   "image/x-icon",
        ".woff2": "font/woff2",
    }
    media_type = MIME.get(file_path.suffix, "text/plain")
    try:
        return Response(file_path.read_bytes(), media_type=media_type)
    except Exception:
        return Response(status_code=500)


# ── Public JSON API (no auth) ──────────────────────────────────────────────
# Genuinely public: market data only. Nothing here reads the operator's book.
# Anything that touches store.py belongs below, behind require_session.

@app.get("/api/brent")
def api_brent():
    """Live Brent crude signal. No auth required."""
    try:
        signal = get_brent_signal()
        if "error" in signal:
            return JSONResponse(status_code=503, content=signal)
        return signal
    except Exception as exc:
        logger.debug("api_brent: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/api/demo/{ticker}")
def api_demo(ticker: str):
    """
    Demo mode — returns quant metrics + fundamentals without LLM.
    No auth required so GitHub visitors can try the quant engine.
    """
    ticker = ticker.upper()
    try:
        result = run_demo(ticker)
        if "error" in result:
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        logger.debug("api_demo: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/api/portfolio", dependencies=[Depends(require_session)])
def api_portfolio():
    """
    Live snapshot of every holding in the store, in one call.

    The dashboard needs all six positions at once; six serial yfinance
    round-trips would take 10-20s, so they run in a thread pool. Each ticker
    is isolated — one dead feed degrades that row, not the whole response.

    Session required — this returns the operator's own holdings and P&L.
    """
    try:
        book = store.positions()
        tickers = list(book.keys())

        def _fetch(t: str) -> dict:
            try:
                return get_price_and_fundamentals(t)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "api_portfolio: fetch failed for '%s' (type=%s): %s",
                    t, type(exc).__name__, exc,
                )
                return {"ticker": t, "error": "fetch failed"}

        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = dict(zip(tickers, pool.map(_fetch, tickers)))

        positions = []
        cost_basis = 0.0
        market_value = 0.0

        for ticker in tickers:
            data = fetched.get(ticker) or {"ticker": ticker, "error": "fetch failed"}
            cfg = book[ticker]

            # Always expose the static config even when the live feed failed,
            # so the row still renders with thesis/sector/tranches.
            position = {
                "ticker":   ticker,
                "name":     data.get("name", ticker),
                "sector":   cfg.get("sector"),
                "price":    data.get("price"),
                "currency": data.get("currency", "USD"),
                "pct_from_52w_high": data.get("pct_from_52w_high"),
                "analyst_target":    data.get("analyst_target"),
                "my_position":       data.get("my_position"),
            }
            if "error" in data:
                position["error"] = data["error"]
            positions.append(position)

            price  = data.get("price")
            shares = cfg.get("shares")
            avg    = cfg.get("avg_cost")
            if price is not None and shares is not None and avg is not None:
                cost_basis   += shares * avg
                market_value += shares * price

        pnl = market_value - cost_basis
        totals = {
            "cost_basis":         round(cost_basis, 2),
            "market_value":       round(market_value, 2),
            "unrealized_pnl":     round(pnl, 2),
            "unrealized_pnl_pct": round((pnl / cost_basis) * 100, 2) if cost_basis else None,
        }

        try:
            brent = get_brent_signal()
        except Exception:  # noqa: BLE001
            brent = {"error": "brent feed unavailable", "gate_open": False}

        return {
            "positions": positions,
            "totals":    totals,
            "brent":     brent,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as exc:  # noqa: BLE001
        logger.debug("api_portfolio: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/api/history/{ticker}")
def api_history(ticker: str, period: str = "1y"):
    """Daily close series for the price chart. No auth required."""
    ticker = ticker.upper()
    try:
        result = get_price_history(ticker, period)
        if "error" in result:
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.debug("api_history: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Auth-gated JSON API ────────────────────────────────────────────────────

@app.get("/api/research/{ticker}", dependencies=[Depends(require_auth)])
def api_research(ticker: str, question: str = None):
    """
    Full Perplexity sonar-pro research. Requires X-Agent-Key header.
    Returns structured JSON with report, bull case, bear case, and all metrics.
    """
    ticker = ticker.upper()
    try:
        result = run_perplexity_research(ticker, question)
        if "error" in result and "ticker" not in result:
            return JSONResponse(status_code=500, content={"error": "internal server error"})
        return result
    except Exception as exc:
        logger.debug("api_research: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Staged research pipeline ───────────────────────────────────────────────
# The frontend runs these one at a time so each section renders as it lands and
# the user can stop between stages. A full run is 5 authed calls against the
# 20 req/min/IP limiter in require_auth — roughly 4 complete runs per minute.

class SynthesisBody(BaseModel):
    """Module outputs posted back for the final stitched verdict.

    Capped here as a request-size guard; run_synthesis truncates again per
    module before building the prompt.
    """
    report:   str = Field(default="", max_length=24000)
    context:  str = Field(default="", max_length=24000)
    policy:   str = Field(default="", max_length=24000)
    patterns: str = Field(default="", max_length=24000)


def _module_response(ticker: str, module: str, question: str = None):
    """Shared wrapper — mirrors api_research's error handling."""
    try:
        result = run_research_module(ticker.upper(), module, question)
        if "error" in result and "ticker" not in result:
            return JSONResponse(status_code=500, content={"error": "internal server error"})
        return result
    except Exception as exc:
        logger.debug("api_research_%s: error (type=%s): %s", module, type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/api/research/{ticker}/report", dependencies=[Depends(require_auth)])
def api_research_report(ticker: str, question: str = None):
    """Main institutional research brief — situation, quant read, catalysts, verdict."""
    return _module_response(ticker, "report", question)


@app.get("/api/research/{ticker}/context", dependencies=[Depends(require_auth)])
def api_research_context(ticker: str):
    """Company deep context — business model, financials, customers, competitors, future fit."""
    return _module_response(ticker, "context")


@app.get("/api/research/{ticker}/policy", dependencies=[Depends(require_auth)])
def api_research_policy(ticker: str):
    """U.S. politician and government-official trading activity, and portfolio overlap."""
    return _module_response(ticker, "policy")


@app.get("/api/research/{ticker}/patterns", dependencies=[Depends(require_auth)])
def api_research_patterns(ticker: str):
    """Cross-source hidden patterns — insider, 13F, options, supply chain, hiring."""
    return _module_response(ticker, "patterns")


@app.post("/api/research/{ticker}/synthesis", dependencies=[Depends(require_auth)])
def api_research_synthesis(ticker: str, body: SynthesisBody):
    """Stitch the module outputs into one BUY/SELL/HOLD verdict plus bull/bear debate."""
    try:
        result = run_synthesis(ticker.upper(), body.model_dump())
        if "error" in result and "ticker" not in result:
            return JSONResponse(status_code=500, content={"error": "internal server error"})
        return result
    except Exception as exc:
        logger.debug("api_research_synthesis: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Session auth ───────────────────────────────────────────────────────────
# Replaces keeping AGENT_SECRET in localStorage, where any script on the page
# could read it. The browser gets an HttpOnly cookie it cannot read; curl keeps
# using the X-Agent-Key header. Both are accepted by require_auth.

class SessionBody(BaseModel):
    key: str = Field(default="", max_length=512)


@app.post("/api/session")
def api_session_create(request: Request, body: SessionBody, response: Response):
    """Exchange AGENT_SECRET for an HttpOnly session cookie."""
    client_ip = _client_ip(request)
    socket_ip = _socket_ip(request)

    # Checked before the per-client limiter on purpose: this is the bound that
    # survives a misconfigured proxy chain, so nothing spoofable gates it.
    if _auth_failures_exceeded(socket_ip):
        logger.warning("api_session_create: failed-unlock budget exhausted for %s", socket_ip)
        return JSONResponse(
            status_code=429,
            content={"error": "too many failed attempts"},
            # Same as the middleware's 429. Without it the caller cannot tell a
            # lockout from a wrong key, retries, and extends its own lockout.
            headers={"Retry-After": str(_AUTH_FAIL_WINDOW)},
        )

    if not _check_rate_limit(client_ip):
        return JSONResponse(status_code=429, content={"error": "rate limit exceeded"})

    if not _agent_key_ok(body.key):
        _record_auth_failure(socket_ip)
        logger.warning("api_session_create: rejected unlock from IP %s", client_ip)
        # Deliberately identical for "wrong key" and "no key configured" —
        # never confirm to a caller which of the two it hit.
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    store.purge_expired_sessions()
    token, expires = store.create_session()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=store.SESSION_DAYS * 86400,
        httponly=True,                       # JS cannot read it — that is the point
        samesite="strict",                   # not sent on cross-site requests
        secure=_is_https(request),
        path="/",
    )
    return {"ok": True, "expires_at": expires.isoformat()}


@app.delete("/api/session")
def api_session_destroy(request: Request, response: Response):
    """Sign out — clears the server row and the cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        store.destroy_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/session")
def api_session_status(request: Request):
    """Whether this browser currently holds a valid session. Never echoes the token."""
    token = request.cookies.get(SESSION_COOKIE)
    return {"authenticated": bool(token and store.session_valid(token))}


# ── Profile ────────────────────────────────────────────────────────────────

class ProfileBody(BaseModel):
    name:       str = Field(default="", max_length=60)
    experience: str = Field(default="", max_length=20)
    risk:       str = Field(default="", max_length=20)
    horizon:    str = Field(default="", max_length=20)
    sectors:    list[str] = Field(default_factory=list, max_length=12)


@app.get("/api/profile", dependencies=[Depends(require_session)])
def api_profile_get():
    """Profile, or {profile: null} when onboarding has not run."""
    return {"profile": store.profile()}


@app.post("/api/profile", dependencies=[Depends(require_session)])
def api_profile_save(body: ProfileBody):
    try:
        return {"profile": store.save_profile(body.model_dump())}
    except Exception as exc:  # noqa: BLE001
        logger.debug("api_profile_save: (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Positions & watchlist CRUD ─────────────────────────────────────────────

class PositionBody(BaseModel):
    ticker:    str = Field(max_length=16)
    shares:    float | None = None
    avg_cost:  float | None = None
    buy_date:  str | None = Field(default=None, max_length=10)
    stop_loss: float | None = None
    trim_at:   float | None = None
    thesis:    str = Field(default="", max_length=2000)
    sector:    str = Field(default="", max_length=80)


class WatchBody(BaseModel):
    ticker: str = Field(max_length=16)
    note:   str = Field(default="", max_length=500)


@app.get("/api/positions", dependencies=[Depends(require_session)])
def api_positions_list():
    return {"positions": store.positions()}


@app.post("/api/positions", dependencies=[Depends(require_session)])
def api_positions_upsert(body: PositionBody):
    try:
        # exclude_unset so a partial update touches only the fields the caller
        # actually sent. Without it, every omitted field arrives as its default
        # (None / "") and wipes the stored value — dating one holding would
        # blank its share count.
        data = body.model_dump(exclude_unset=True)
        if not data.get("ticker"):
            return JSONResponse(status_code=400, content={"error": "ticker is required"})
        # Reject a malformed date rather than storing a string XIRR cannot parse.
        if data.get("buy_date") and store.parse_buy_date(data["buy_date"]) is None:
            return JSONResponse(status_code=400, content={"error": "buy_date must be YYYY-MM-DD"})
        ticker = store.upsert_position(data.pop("ticker"), data)
        return {"ok": True, "ticker": ticker, "positions": store.positions()}
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@app.delete("/api/positions/{ticker}", dependencies=[Depends(require_session)])
def api_positions_delete(ticker: str):
    try:
        removed = store.delete_position(ticker)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": removed, "positions": store.positions()}


@app.get("/api/watchlist", dependencies=[Depends(require_session)])
def api_watchlist_list():
    return {"watchlist": store.watchlist()}


@app.post("/api/watchlist", dependencies=[Depends(require_session)])
def api_watchlist_add(body: WatchBody):
    try:
        t = store.add_watch(body.ticker, body.note)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "ticker": t, "watchlist": store.watchlist()}


@app.delete("/api/watchlist/{ticker}", dependencies=[Depends(require_session)])
def api_watchlist_delete(ticker: str):
    try:
        removed = store.delete_watch(ticker)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": removed, "watchlist": store.watchlist()}


# ── Portfolio analytics (free) ─────────────────────────────────────────────

@app.get("/api/portfolio/analytics", dependencies=[Depends(require_session)])
def api_portfolio_analytics():
    """
    Invested, market value, P&L and XIRR.

    XIRR needs dated cash flows. Positions without a buy_date are excluded from
    the flow list and reported in `xirr_note` — a money-weighted return computed
    from undated buys would be fiction, so it returns null instead.
    """
    try:
        book = store.positions()
        tickers = list(book.keys())
        if not tickers:
            return {"positions": [], "totals": None, "xirr": None,
                    "xirr_note": "No positions configured."}

        def _fetch(t: str) -> dict:
            try:
                return get_price_and_fundamentals(t)
            except Exception as exc:  # noqa: BLE001
                logger.debug("analytics: fetch failed for %s (%s)", t, type(exc).__name__)
                return {"ticker": t, "error": "fetch failed"}

        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = dict(zip(tickers, pool.map(_fetch, tickers)))

        rows, invested, value, flows, undated = [], 0.0, 0.0, [], []
        # Tracked separately: XIRR's closing flow must cover exactly the
        # positions whose buys are in the flow list. Pairing one position's
        # cost with the whole book's value produces a wildly inflated rate.
        dated_value = 0.0

        for t in tickers:
            cfg   = book[t]
            data  = fetched.get(t) or {}
            price = data.get("price")
            sh    = cfg.get("shares")
            avg   = cfg.get("avg_cost")
            if price is None or sh is None or avg is None:
                rows.append({"ticker": t, "error": "no price"})
                continue

            cost, mkt = sh * avg, sh * price
            invested += cost
            value    += mkt

            bd = store.parse_buy_date(cfg.get("buy_date"))
            if bd:
                flows.append((bd, -cost))
                dated_value += mkt
            else:
                undated.append(t)

            rows.append({
                "ticker": t, "name": data.get("name", t), "sector": cfg.get("sector"),
                "shares": sh, "avg_cost": avg, "price": price,
                "currency": data.get("currency", "USD"), "buy_date": cfg.get("buy_date"),
                "cost_basis": round(cost, 2), "market_value": round(mkt, 2),
                "pnl": round(mkt - cost, 2),
                "pnl_pct": round(((mkt - cost) / cost) * 100, 2) if cost else None,
            })

        for r in rows:
            if "market_value" in r:
                r["weight_pct"] = round((r["market_value"] / value) * 100, 2) if value else None
                r["contribution_pct"] = round((r["pnl"] / invested) * 100, 2) if invested else None

        pnl = value - invested
        totals = {
            "invested":     round(invested, 2),
            "market_value": round(value, 2),
            "unrealized_pnl": round(pnl, 2),
            "unrealized_pnl_pct": round((pnl / invested) * 100, 2) if invested else None,
        }

        rate, note = None, None
        if flows:
            flows.append((datetime.now().date(), dated_value))
            rate = xirr(flows)
            if rate is None:
                note = "Not computable — holding period too short or flows degenerate."
            elif undated:
                # Say what the number actually covers. A rate presented as
                # portfolio-wide while measuring one position is misleading.
                covered = len(flows) - 1
                note = (f"Covers {covered} of {covered + len(undated)} holdings. "
                        f"No buy date for {', '.join(undated)} — add dates in Profile "
                        f"for a whole-portfolio figure.")
        else:
            note = "Not computable — no buy dates recorded. Add them in Profile."

        return {"positions": rows, "totals": totals, "xirr": rate, "xirr_note": note,
                "undated": undated, "timestamp": datetime.now().isoformat()}

    except Exception as exc:  # noqa: BLE001
        logger.debug("api_portfolio_analytics: (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Analyst outlook (paid + cached) ────────────────────────────────────────

@app.get("/api/portfolio/outlook", dependencies=[Depends(require_session)])
def api_outlook_get():
    """Cached outlook with its age, or null. Free — never triggers a call."""
    return {"outlook": store.get_outlook()}


@app.post("/api/portfolio/outlook", dependencies=[Depends(require_auth)])
def api_outlook_run():
    """PAID — one provider call covering every holding. Result is cached."""
    try:
        book = store.positions()
        if not book:
            return JSONResponse(status_code=400, content={"error": "no positions configured"})
        try:
            brent = get_brent_signal()
        except Exception:  # noqa: BLE001
            brent = {"error": "brent feed unavailable"}

        result = run_portfolio_outlook(book, brent)
        if "error" in result and "outlook" not in result:
            return result
        store.save_outlook(result)
        return {"outlook": store.get_outlook()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("api_outlook_run: (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Local runner ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
