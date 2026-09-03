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
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

# Load .env before anything else
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — fall back to real env vars

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

import store
from config import BRENT_LEVELS, GEO_EVENT_LIBRARY, GEO_TRANSMISSION
from tools.notify import notify_telegram, start_telegram_reply_listener
from tools.oil_price import get_brent_price, get_brent_signal
from tools.perplexity_research import (
    _PROVIDER_REASON_TEXT,
    run_demo,
    run_model_court,
    run_perplexity_research,
    run_portfolio_outlook,
    run_research_module,
    run_synthesis,
)
from tools.setup_wizard import (
    PERSISTENT_PROVIDERS,
    PROVIDER_KEY_NAMES,
    ensure_agent_secret,
    soft_validate,
    write_env_key,
)
from tools.stock_data import (
    get_news,
    get_next_earnings,
    get_price_and_fundamentals,
    get_price_history,
)
from tools.validator import _MAX_QUESTION_LEN, sanitize_prompt_text, validate_ticker
from tools.xirr import xirr

_STATIC_DIR = Path(__file__).parent / "static"
_ENV_FILE_PATH = Path(__file__).parent / ".env"
_ENV_EXAMPLE_PATH = Path(__file__).parent / ".env.example"

logger = logging.getLogger(__name__)

# First-run bootstrap: AGENT_SECRET must exist before anyone can unlock as
# owner, and nothing else — not the browser, not the CLI wizard — can supply
# the FIRST one, since proving ownership requires already knowing it. This is
# what makes starting the server the only step that ever needs a terminal;
# every AI-provider key after that is added from the browser's CONFIG modal
# (POST /api/settings/provider-key, further down). Never touches
# PERPLEXITY_API_KEY/GROQ_API_KEY — those stay genuinely absent until the
# operator adds one, on purpose. Skipped whenever AGENT_SECRET is already
# set — including by every verification harness, which all set a real value
# in os.environ before importing this module, so this branch is only ever
# live on a genuinely fresh clone with no .env at all.
if not os.getenv("AGENT_SECRET"):
    try:
        _generated_secret = ensure_agent_secret(_ENV_FILE_PATH, _ENV_EXAMPLE_PATH)
        if _generated_secret:
            # Set explicitly, never via load_dotenv(override=True) — that
            # would reload every OTHER key in the file over whatever this
            # process already has, which is wrong the moment anything
            # (a test harness, a container's own env) intentionally set a
            # different value for something else.
            os.environ["AGENT_SECRET"] = _generated_secret
            logger.critical("=" * 64)
            logger.critical("First run — generated your AGENT_SECRET (saved to .env):")
            logger.critical("    %s", _generated_secret)
            logger.critical('Paste it into "I HAVE A KEY" in the browser to unlock.')
            logger.critical("This will not be shown again — find it in .env if you lose it.")
            logger.critical("=" * 64)
    except Exception as _bootstrap_secret_exc:  # noqa: BLE001 — must never block startup
        logger.warning(
            "could not auto-generate AGENT_SECRET (%s) — set it manually in .env",
            _bootstrap_secret_exc,
        )

try:
    store.bootstrap()
except Exception as _bootstrap_exc:
    # A failure here means the DB cannot be created or opened. uvicorn would
    # otherwise surface a raw SQLite traceback buried in its import output.
    # Exit loudly with a single actionable line instead.
    logger.critical(
        "Fatal: store.bootstrap() failed — server cannot start.\n"
        "  Check the path set by ARGUS_DB (default: argus.db next to store.py),\n"
        "  ensure the directory exists and is writable.\n"
        "  Underlying error: %s",
        _bootstrap_exc,
    )
    raise SystemExit(1) from _bootstrap_exc

# The Telegram reply listener (approve-by-replying-with-the-email) starts
# here rather than at import time. FastAPI's lifespan only runs when the ASGI
# app is actually served — a bare `import api`, and TestClient(api.app) used
# without a `with` block (this project's whole verify_access.py harness),
# never trigger it. That is what keeps a "spends nothing, touches nothing
# external" test run from spawning a background thread that long-polls a real
# Telegram API. _handle_telegram_reply is defined further down, near the rest
# of the access-request code; referencing it here by name is fine because
# this coroutine only runs (and only looks the name up) at real startup, long
# after the whole module has finished importing.
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    stop = start_telegram_reply_listener(_handle_telegram_reply)
    yield
    if stop is not None:
        stop.set()


app = FastAPI(
    title="Stock Research Agent",
    description="Personal institutional-grade research agent with Brent crude framework",
    version="1.0.0",
    lifespan=_lifespan,
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
    # Cloud platforms (Railway/Fly/Render) typically inject HSTS at the edge,
    # but the app sets it too so a direct HTTPS hit is covered either way.
    # Never set over plain HTTP — that would pin a host that cannot yet serve
    # HTTPS at all, e.g. local dev.
    if _is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

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
    ("/api/meta",       "market", 30, 60),
    ("/api/news",       "market", 30, 60),
    ("/api/earnings",   "market", 30, 60),
    # A public form that writes to disk needs a far tighter bound than the
    # default 60/min. The hour-long window means these bucket keys live an hour
    # in _rate_limit_store, so a wide flood can fill the 500-key cap and start
    # fail-closing new IPs — acceptable here, and the persistent caps in
    # store.create_access_request() are the defence that survives a restart.
    ("/api/access-request", "signup", 5, 3600),
    # Anonymous and opt-in (ALLOW_BYOK_VISITORS) — no session, no owner
    # entitlement standing behind it. The AI spend is the visitor's own, but
    # the yfinance calls and CPU inside run_perplexity_research() /
    # run_research_module() / run_synthesis() are the operator's. Sized for
    # the STAGED pipeline's usage pattern (5 requests per full run — report,
    # context, policy, patterns, synthesis), not the one-shot route alone:
    # 20/300s is ~4 full runs per 5 minutes, still well under "market"'s
    # effective 150/5min despite also being anonymous.
    ("/api/byok",       "byok",   20, 300),
    # Session-gated (owner or guest — never anonymous), but a compound,
    # expensive operation: two provider calls plus a third comparison call
    # per request, on the caller's own keys. Reuses the SAME bucket
    # /api/portfolio already uses for "fans out to multiple upstream calls,
    # requires a session" rather than inventing a new one — this is exactly
    # that shape again.
    ("/api/model-court", "heavy",  10, 60),
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


class AuthCtx:
    """Who the caller is, not merely whether they are someone.

    This replaced a bool. The boolean was the entire vulnerability: every
    session row looked alike to require_auth, so the moment a second class of
    credential exists (a guest key), its holder is indistinguishable from the
    operator and inherits routes that spend real money with no budget attached.
    Carrying the tier means each route can decide for itself.
    """

    __slots__ = ("tier", "guest_key_id")

    def __init__(self, tier: str, guest_key_id: int | None = None):
        self.tier = tier                      # "owner" | "guest"
        self.guest_key_id = guest_key_id

    @property
    def is_owner(self) -> bool:
        return self.tier == "owner"

    @property
    def is_guest(self) -> bool:
        return self.tier == "guest"


def _auth_ctx(request: Request, x_agent_key: str | None) -> AuthCtx | None:
    """Resolve the caller to an AuthCtx, or None when unauthenticated.

    Fail-closed: with AGENT_SECRET unset there is no credential that can be
    valid, so every caller is rejected rather than waved through.

    X-Agent-Key is always the operator — it *is* AGENT_SECRET, and a guest is
    never given it. Guest keys are redeemed for a cookie at POST /api/session
    and travel no other way.
    """
    if not os.getenv("AGENT_SECRET"):
        # Log by name only — never the value.
        logger.warning("auth: AGENT_SECRET is not set — all auth requests rejected")
        return None

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        info = store.session_info(token)
        if info:
            return AuthCtx(info["tier"], info["guest_key_id"])

    if _agent_key_ok(x_agent_key):
        return AuthCtx("owner")

    return None


def _credential_ok(request: Request, x_agent_key: str | None) -> bool:
    """Back-compat wrapper: is the caller anyone at all?"""
    return _auth_ctx(request, x_agent_key) is not None


def _unauthorized_detail(request: Request) -> dict:
    """Distinguish "your session died" from "you never had one".

    A revoked or expired guest key makes session_info() destroy the row and
    return None, which is indistinguishable at this layer from an anonymous
    caller — so both produced a bare 401. The frontend answers a code-less 401
    by opening the unlock dialog, which asks a guest for an operator secret
    they were never given.

    The cookie's presence is the tell. It is not proof the session was a
    guest's, but it is proof there WAS one, which is enough for the UI to send
    them somewhere useful instead of somewhere impossible.
    """
    if request.cookies.get(SESSION_COOKIE):
        return {"error": "your session has expired or been revoked",
                "code": "session_expired"}
    return {"error": "unauthorized"}


async def require_auth(
    request: Request,
    x_agent_key: str = Header(default=None, alias="X-Agent-Key"),
) -> AuthCtx:
    """
    FastAPI dependency guarding the five BUDGETED paid stages. The unbudgeted
    paid routes — the legacy one-shot GET /api/research/{t} and
    POST /api/portfolio/outlook — use require_owner instead, because a guest
    allowance cannot bound them.

    Accepts EITHER a valid session cookie (browser, set by POST /api/session)
    OR the X-Agent-Key header (curl and scripts). The cookie exists so the key
    never has to live in localStorage where any script could read it.

    Returns the AuthCtx rather than None so a paid handler can ask *which*
    tier is calling. Passing this as a dependency still gates the route; taking
    it as a parameter additionally hands the handler the caller's identity.

    401 if neither credential is valid. 429 if the caller exceeds the limit.
    """
    from fastapi import HTTPException

    # Rate limit runs before auth so a failed key cannot be probed for free.
    client_ip = _client_ip(request)
    if not _check_rate_limit(client_ip):
        logger.warning("rate_limit: IP %s exceeded %d req/%ds",
                       client_ip, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW)
        raise HTTPException(status_code=429, detail={"error": "rate limit exceeded"})

    ctx = _auth_ctx(request, x_agent_key)
    if ctx is not None:
        return ctx

    logger.warning("require_auth: rejected request from IP %s — no valid session or key", client_ip)
    raise HTTPException(status_code=401, detail=_unauthorized_detail(request))


async def require_owner(
    request: Request,
    x_agent_key: str = Header(default=None, alias="X-Agent-Key"),
) -> AuthCtx:
    """
    FastAPI dependency for routes only the operator may reach.

    Two kinds of route need this. Writes to the book — positions, watchlist,
    profile — because a guest is a reader in someone else's terminal and must
    not be able to edit or delete holdings. And paid actions with no per-caller
    budget, which a guest allowance cannot bound.

    A guest gets 403, not 401: they are authenticated, just not entitled. The
    distinction matters to the frontend, which sends a 401 back to the unlock
    screen — exactly the wrong move for a guest, who would be asked for an
    operator secret they were never given.
    """
    from fastapi import HTTPException

    ctx = _auth_ctx(request, x_agent_key)
    if ctx is not None and ctx.is_owner:
        return ctx

    client_ip = _client_ip(request)
    if ctx is not None:
        logger.warning("require_owner: guest session refused a owner-only route (IP %s)", client_ip)
        raise HTTPException(
            status_code=403,
            detail={"error": "this action belongs to the terminal's owner",
                    "code": "guest_read_only"},
        )

    if not _check_rate_limit(client_ip):
        logger.warning("rate_limit: IP %s exceeded %d req/%ds on a failed owner-route auth",
                       client_ip, _RATE_LIMIT_MAX, _RATE_LIMIT_WINDOW)
        raise HTTPException(status_code=429, detail={"error": "rate limit exceeded"})

    logger.warning("require_owner: rejected request from IP %s — no valid session or key", client_ip)
    raise HTTPException(status_code=401, detail=_unauthorized_detail(request))


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


_SECTOR_WORD_RE = re.compile(r"[a-z0-9]+")


def _sector_words(sector: str) -> set:
    """Whole words in a sector string, lowercased.

    Tokenised, not raw text: "Fintech" must not match keyword "tech" — a raw
    `"tech" in sector.lower()` check would wrongly flag a LatAm fintech name
    as having India/China tech exposure just because "fintech" contains the
    letters "tech". Matching against these tokens happens in
    _keyword_matches(), which uses prefix matching (not equality), so a
    plural/suffixed sector like "Technology" or "Semiconductors" still
    matches the singular keyword "tech"/"semiconductor" — see that
    function's docstring for why "Fintech" still correctly stays excluded
    under prefix matching too.
    """
    return set(_SECTOR_WORD_RE.findall(sector.lower()))


def _keyword_matches(words: set, keywords: set) -> bool:
    """
    True if any keyword matches the tokenized sector words.

    Keywords longer than 2 characters match by prefix, so a plural or
    suffixed form ("Technology", "Semiconductors", "Industrials") matches a
    singular/stem keyword ("tech", "semiconductor", "industrial") without
    every inflection being spelled out in GEO_EVENT_LIBRARY. Crucially,
    prefix still correctly excludes "Fintech" from keyword "tech": prefix
    matching requires the WORD to start with the keyword, and "fintech"
    does not start with "tech" — it merely contains it, which is exactly
    the substring-match bug _sector_words() exists to avoid.

    Keywords of 2 characters or fewer ("ai") require an exact whole-word
    match instead — as a prefix, "ai" would match "airlines", "aircraft"
    and "aid", which is the same false-positive shape in miniature.
    """
    for kw in keywords:
        if len(kw) <= 2:
            if kw in words:
                return True
        elif any(word.startswith(kw) for word in words):
            return True
    return False


def _compute_geo_transmission() -> dict:
    """
    Merge the operator's hand-written GEO_TRANSMISSION with auto-generated
    entries from GEO_EVENT_LIBRARY, matched against the actual book.

    An event key already present in GEO_TRANSMISSION is left exactly as
    written — the operator wrote it on purpose and it always wins. Every
    other library event is scored against every held/watched ticker's stored
    sector string; an event with zero matches is omitted entirely, so the
    panel stays scoped to positions actually held or watched rather than a
    global macro feed. Pure in-memory matching — no network call, no cost.

    A held position's sector wins over a watchlist entry's on ticker
    collision (positions merged in last) — a position is the richer,
    operator-maintained record; a watchlist row's sector is usually blank
    unless separately set. Each malformed GEO_EVENT_LIBRARY entry is caught
    and skipped individually so one bad entry can't 500 the whole /api/info
    response, which also carries portfolio/watchlist/brent_levels that have
    nothing to do with the geo panel.
    """
    book = {**store.watchlist(), **store.positions()}
    has_book = bool(book)
    out = dict(GEO_TRANSMISSION)

    for event, spec in GEO_EVENT_LIBRARY.items():
        if event in out:
            continue

        try:
            if spec.get("affects_all"):
                if has_book:
                    out[event] = [f"all — {spec.get('note', '')}"]
                continue

            keywords = set(spec.get("keywords", []))
            matched = [
                ticker for ticker, cfg in book.items()
                if _keyword_matches(_sector_words(cfg.get("sector") or ""), keywords)
            ]
            if matched:
                # The note explains the EVENT, not the ticker — put it on the
                # first match only and leave the rest bare, matching how
                # hand-written multi-ticker entries already render (e.g.
                # GEO_TRANSMISSION's ai_export_controls: ["MSFT", "GOOGL"]).
                # Repeating an identical note once per matching ticker would
                # stack duplicate lines in the panel.
                note = spec.get("note", "")
                out[event] = [
                    f"{ticker} — {note}" if i == 0 else ticker
                    for i, ticker in enumerate(matched)
                ]
        except Exception:
            logger.exception("geo event %r malformed in GEO_EVENT_LIBRARY — skipped", event)
            continue

    return out


# ── Public metadata ───────────────────────────────────────────────────────
# What a keyless visitor is allowed to know. This exists so /api/info can stay
# gated exactly as it is: that route also carries the book, and loosening it
# would be a much larger blast radius than adding a small deliberate one.

DISCLAIMER = (
    "ARGUS is an educational research tool, not investment advice. Its output is "
    "generated by AI from public data — it can be wrong, incomplete, or out of date, "
    "and it is never a personalised recommendation to buy or sell anything. Make your "
    "own decisions and consult a licensed financial professional before acting."
)


@app.get("/api/meta")
def api_meta():
    """Framework config and disclosures for callers with no session.

    Carries BRENT_LEVELS but never GEO_TRANSMISSION. The distinction is not
    cosmetic: config.py's placeholder BRENT_LEVELS is never overridden by
    config_local.py — the local override imports only MY_PORTFOLIO, WATCHLIST
    and GEO_TRANSMISSION — so the Brent ladder is always the published
    framework and is safe to serve. GEO_TRANSMISSION *is* overridden, and its
    values name real holdings by construction ("AAPL (supply chain exposure)"),
    which makes the operator's geo map a description of their book.
    """
    try:
        return {
            "status": "online",
            "brent_levels": BRENT_LEVELS,
            "data_sources": {
                "quotes": "Yahoo Finance via yfinance — delayed, not real-time",
                "research": ("Perplexity sonar-pro with live web search"
                             if os.getenv("PERPLEXITY_API_KEY")
                             else "Groq fallback — model knowledge only, no live web search"
                             if os.getenv("GROQ_API_KEY")
                             else "no AI provider configured"),
            },
            "disclaimer": DISCLAIMER,
        }
    except Exception as exc:
        logger.debug("api_meta: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


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
        "geo_transmission": _compute_geo_transmission(),
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


# /v2 — the landing-page frontend was removed (see git history for static/v2/).
# It stored AGENT_SECRET in localStorage, loaded external CDN scripts (violating
# font-src/script-src 'self'), and piped model output into innerHTML via marked
# without HTML-escaping. Any request to /v2 returns 404 with default-src 'none'.


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

@app.get("/api/research/{ticker}", dependencies=[Depends(require_owner)])
def api_research(ticker: str, question: str = Query(default=None, max_length=_MAX_QUESTION_LEN)):
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


def _validated_ticker(ticker: str):
    """Return (symbol, None) or (None, 400-response).

    Hoisted ahead of the guest gate deliberately. consume_guest_unit() calls
    validate_ticker() as its first statement, and the gate runs before
    _module_response's try — so a malformed symbol raised straight out of the
    handler for a guest while the owner, who skips the gate, got a clean
    response. Same request, two tiers, two behaviours, one of them a 500.

    400 matches what /api/demo and /api/history already return for a bad
    symbol, so the frontend's existing `notfound` mapping applies unchanged.
    """
    try:
        return validate_ticker(ticker), None
    except ValueError as exc:
        return None, JSONResponse(status_code=400, content={"error": str(exc)})


def _guest_gate(ctx: AuthCtx, ticker: str, module: str):
    """Claim one stage of a guest's dive, or explain why not. None means proceed.

    Runs BEFORE the provider is touched, so a refused guest never costs money.

    Each refusal carries a machine-readable `code` beside the human string. The
    frontend needs it: a spent allowance and a wrong-ticker claim are different
    conversations, and neither should be reported as a generic failure that the
    staged loop then retries four more times.
    """
    # Tested positively, so an unrecognised tier DENIES rather than proceeds.
    # `if not ctx.is_guest: return None` reads the same for owner and guest but
    # differs for anything else: a tier that is neither would have sailed
    # straight past the budget onto a paid route, unmetered. require_owner
    # already tests positively (`ctx.is_owner`), so the two dependencies
    # disagreed — and the permissive one guarded the money. Not reachable today
    # (create_session validates the tier and both SQL defaults are 'owner'), but
    # the whole point of the AuthCtx rewrite was that a credential check must
    # not fail open.
    if ctx.is_owner:
        return None
    if not ctx.is_guest:
        logger.warning("_guest_gate: refusing unrecognised session tier %r", ctx.tier)
        return JSONResponse(
            status_code=403,
            content={"error": "this action belongs to the terminal's owner",
                     "code": "guest_read_only"},
        )

    outcome = store.consume_guest_unit(ctx.guest_key_id, ticker, module)
    if outcome == "ok":
        return None

    usage = store.guest_usage(ctx.guest_key_id)

    if outcome == "other_ticker":
        return JSONResponse(
            status_code=409,
            content={"error": f"your guest key is committed to {usage['dive_ticker']}",
                     "code": "guest_ticker_bound",
                     "detail": {"bound_ticker": usage["dive_ticker"]}},
        )
    if outcome == "used":
        # Two different situations wear the same store-level outcome, and
        # collapsing them strands a guest's allowance. "used" means THIS stage
        # was already claimed — which, unless all five are gone, leaves the rest
        # perfectly claimable. Reporting both as "budget exhausted" made the
        # frontend halt the whole run, so a guest whose first run was cancelled
        # or hit a transient failure could never claim the remaining stages.
        if usage["exhausted"]:
            return JSONResponse(
                status_code=402,
                content={"error": "your included deep dive is fully used",
                         "code": "guest_budget_exhausted", "detail": usage},
            )
        return JSONResponse(
            status_code=402,
            content={"error": f"the {module} stage of your dive already ran",
                     "code": "guest_stage_used", "detail": usage},
        )
    # missing / revoked / expired
    return JSONResponse(
        status_code=401,
        content={"error": "your guest key is no longer valid", "code": "guest_expired"},
    )


def _module_response(ticker: str, module: str, question: str = None,
                     ctx: AuthCtx = None):
    """Shared wrapper — mirrors api_research's error handling."""
    sym, bad = _validated_ticker(ticker)
    if bad is not None:
        return bad

    gate = _guest_gate(ctx, sym, module) if ctx else None
    if gate is not None:
        return gate

    try:
        result = run_research_module(ticker.upper(), module, question)
        # Refund BEFORE the shape check. The no-provider failure returns its
        # ticker alongside the error, so gating the refund on "ticker" not in
        # result skipped the one case the refund was written for and charged a
        # guest a stage for a call that never left the process.
        if "error" in result:
            _refund_if_free(ctx, module, result)
            # 502, not 200-with-an-error-body. The caller ticked the stage green
            # and stored an apology as the report when this returned success.
            # `code` lets the staged loop stop rather than fire four more
            # requests at a provider that is plainly down.
            return JSONResponse(
                status_code=502,
                content={"error": result["error"], "code": "provider_unavailable"},
            )
        return result
    except Exception as exc:
        logger.debug("api_research_%s: error (type=%s): %s", module, type(exc).__name__, exc)
        _refund_if_free(ctx, module, None)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


def _refund_if_free(ctx: AuthCtx | None, module: str, result: dict | None) -> None:
    """Return a guest's stage only when the failure provably cost nothing.

    _get_provider() raises before any network call when no provider key is
    configured, so that specific failure spent $0 and charging a stage for it
    would be taking an allowance for nothing. Every other failure keeps the
    stage: the call may already have been billed upstream, and refunding on a
    mid-flight timeout would hand out unlimited retries of a paid operation.
    """
    if ctx is None or not ctx.is_guest:
        return

    # Structured flag, not a substring match on English prose. The old test
    # ("api_key" in text or …) inferred "this cost nothing" from wording, which
    # was brittle against a provider changing its message and — once a
    # caller-controlled ticker could appear in an error string — would have let
    # a guest reclaim a stage by requesting /api/research/API_KEY/report.
    # The layer that made (or did not make) the network call now says so.
    if (result or {}).get("cost_incurred") is False:
        logger.info("refunding guest stage %s (reason=%s, nothing was billed)",
                    module, (result or {}).get("reason"))
        store.refund_guest_unit(ctx.guest_key_id, module)


@app.get("/api/research/{ticker}/report")
def api_research_report(ticker: str, question: str = Query(default=None, max_length=_MAX_QUESTION_LEN),
                        ctx: AuthCtx = Depends(require_auth)):
    """Main institutional research brief — situation, quant read, catalysts, verdict."""
    return _module_response(ticker, "report", question, ctx)


@app.get("/api/research/{ticker}/context")
def api_research_context(ticker: str, ctx: AuthCtx = Depends(require_auth)):
    """Company deep context — business model, financials, customers, competitors, future fit."""
    return _module_response(ticker, "context", None, ctx)


@app.get("/api/research/{ticker}/policy")
def api_research_policy(ticker: str, ctx: AuthCtx = Depends(require_auth)):
    """U.S. politician and government-official trading activity, and portfolio overlap."""
    return _module_response(ticker, "policy", None, ctx)


@app.get("/api/research/{ticker}/patterns")
def api_research_patterns(ticker: str, ctx: AuthCtx = Depends(require_auth)):
    """Cross-source hidden patterns — insider, 13F, options, supply chain, hiring."""
    return _module_response(ticker, "patterns", None, ctx)


@app.post("/api/research/{ticker}/synthesis")
def api_research_synthesis(ticker: str, body: SynthesisBody,
                           ctx: AuthCtx = Depends(require_auth)):
    """Stitch the module outputs into one BUY/SELL/HOLD verdict plus bull/bear debate."""
    sym, bad = _validated_ticker(ticker)
    if bad is not None:
        return bad

    gate = _guest_gate(ctx, sym, "synthesis")
    if gate is not None:
        return gate

    # Fence the posted module text before it reaches a prompt.
    #
    # _synthesis_prompt() interpolates these four fields raw, and run_synthesis
    # fires three paid calls off them. While the only caller was the operator's
    # own browser echoing back model output, that was a trusted loop. Guest
    # access makes it an untrusted one: a caller could post "ignore prior
    # instructions..." as `report` and steer all three calls on the operator's
    # provider account. Stored thesis/note/sector have always been fenced this
    # way (perplexity_research.py) — this closes the same hole on the one input
    # that comes straight off the wire.
    #
    # Applied for every tier, not just guests: it costs the operator nothing and
    # a guard that only runs for some callers is a guard waiting to be bypassed.
    fenced = {
        k: sanitize_prompt_text(v, label=f"MODULE_{k.upper()}") if v else v
        for k, v in body.model_dump().items()
    }

    try:
        result = run_synthesis(ticker.upper(), fenced)
        if "error" in result:
            _refund_if_free(ctx, "synthesis", result)
            return JSONResponse(
                status_code=502,
                content={"error": result["error"], "code": "provider_unavailable"},
            )
        return result
    except Exception as exc:
        logger.debug("api_research_synthesis: error (type=%s): %s", type(exc).__name__, exc)
        _refund_if_free(ctx, "synthesis", None)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Anonymous, self-funded BYOK research ───────────────────────────────────
# Placed after SynthesisBody (used by api_byok_synthesis below) rather than
# alongside /api/demo — Python evaluates parameter type annotations at
# def-time, not lazily, so this block must live after that class exists.

def _byok_auth(x_provider: str | None, x_provider_key: str | None):
    """Shared gate for every /api/byok/* route: feature flag, provider name,
    key format. Returns (override_tuple, None) on success, or (None,
    error_response) to return verbatim.

    Off by default: refuses outright unless the operator has set
    ALLOW_BYOK_VISITORS in .env. Every /api/byok/* route is anonymous-
    reachable and spends the OPERATOR's yfinance quota and CPU on every call
    even though the AI spend is the visitor's own — an operator updating an
    existing deployment should not wake up with this newly reachable.

    soft_validate() runs before any provider call on every route that calls
    this — proven by a counting stub in scripts/verify_byok_visitor.py, not
    just asserted here.
    """
    if not os.getenv("ALLOW_BYOK_VISITORS"):
        return None, JSONResponse(status_code=403, content={
            "error": "self-funded research is not enabled on this instance"})

    # Membership in the one canonical map, not a hardcoded tuple re-typed
    # per call site — this is what makes Gemini reachable here specifically
    # (BYOK) while POST /api/settings/provider-key, checking PERSISTENT_
    # PROVIDERS instead, keeps refusing it. Two constants, two scopes, no
    # copy that can silently drift from the other.
    if x_provider not in PROVIDER_KEY_NAMES:
        return None, JSONResponse(status_code=400, content={
            "error": f"X-Provider header must be one of: {', '.join(sorted(PROVIDER_KEY_NAMES))}"})

    ok, reason = soft_validate(x_provider_key or "")
    if not ok:
        return None, JSONResponse(status_code=400, content={"error": reason})

    return (x_provider, x_provider_key), None


def _byok_module_response(ticker: str, module: str, question: str | None,
                           override: tuple[str, str]):
    """Mirrors _module_response()'s shape for the anonymous BYOK path: no
    guest gate (there is no session to gate) and no refund bookkeeping
    (nothing was ever charged to any allowance — this caller has none).

    Unlike _module_response() (the owner/guest staged pipeline), this path
    does NOT reuse run_research_module()'s own generic error text
    ("the research provider is unavailable right now") verbatim. That
    genericism is deliberate over there — a guest must never be told to
    edit the OPERATOR's .env — but it is actively unhelpful here: the key
    that failed is the CALLER's own, so telling them nothing more specific
    than "unavailable" leaves them unable to tell "my key is wrong" from
    "I'm rate-limited" from "this instance has no path to the provider at
    all". _PROVIDER_REASON_TEXT is the same translation run_model_court
    already uses for exactly this reason — one vocabulary, one meaning,
    reused rather than re-invented per caller."""
    sym, bad = _validated_ticker(ticker)
    if bad is not None:
        return bad
    try:
        result = run_research_module(ticker.upper(), module, question, override=override)
        if "error" in result:
            reason_text = _PROVIDER_REASON_TEXT.get(result.get("reason"), "provider call failed")
            return JSONResponse(status_code=502,
                                 content={"error": reason_text, "code": "provider_unavailable"})
        return result
    except Exception as exc:  # noqa: BLE001 — type only, see api_byok_research's docstring
        logger.debug("api_byok_%s: error (type=%s)", module, type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/api/byok/{ticker}")
def api_byok_research(
    ticker: str,
    x_provider: str = Header(default=None, alias="X-Provider"),
    x_provider_key: str = Header(default=None, alias="X-Provider-Key"),
    question: str = Query(default=None, max_length=_MAX_QUESTION_LEN),
):
    """
    Anonymous, self-funded research — a visitor's OWN Groq/Perplexity key,
    never the operator's. No session, no AGENT_SECRET, no guest key. See
    _byok_auth()'s docstring for the shared security model every /api/byok/*
    route holds to.

    One-shot: report + bull/bear in a single call. Kept alongside the staged
    routes below for the same reason /api/research/{ticker} (legacy) is kept
    alongside /api/research/{ticker}/{module} — curl-friendly, one round trip.

    The key from these two headers is used for exactly this one call via
    run_perplexity_research(..., override=(provider, key)) — see that
    function and _get_provider()'s docstring for the full chain. It is
    never written to os.environ, never persisted to .env, never logged, and
    the exception handler below deliberately logs only the exception TYPE,
    not its message, because unlike every other error path in this file
    that message could in rare cases echo request context back from an SDK
    — and this is one of the routes in the whole app answering with a
    different, unrelated person's money on every single call.
    """
    override, err = _byok_auth(x_provider, x_provider_key)
    if err is not None:
        return err

    ticker = ticker.upper()
    try:
        result = run_perplexity_research(ticker, question, override=override)
        if "error" in result and "ticker" not in result:
            return JSONResponse(status_code=500, content={"error": "internal server error"})
        return result
    except Exception as exc:  # noqa: BLE001 — see docstring on why only the type is logged
        logger.debug("api_byok_research: error (type=%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Staged BYOK pipeline ────────────────────────────────────────────────
# Mirrors /api/research/{ticker}/{module} exactly, so the frontend can offer
# the same à-la-carte per-module UX (ui.showCostModal's owner branch) to a
# self-funded visitor — pick which stages to run, see each render as it
# lands, stop between stages. Every route here shares _byok_auth()'s
# security model; none of it is repeated per-route.

@app.get("/api/byok/{ticker}/report")
def api_byok_report(
    ticker: str,
    question: str = Query(default=None, max_length=_MAX_QUESTION_LEN),
    x_provider: str = Header(default=None, alias="X-Provider"),
    x_provider_key: str = Header(default=None, alias="X-Provider-Key"),
):
    """Anonymous, self-funded — stage 1 of 5. See api_byok_research's docstring."""
    override, err = _byok_auth(x_provider, x_provider_key)
    if err is not None:
        return err
    return _byok_module_response(ticker, "report", question, override)


@app.get("/api/byok/{ticker}/context")
def api_byok_context(
    ticker: str,
    x_provider: str = Header(default=None, alias="X-Provider"),
    x_provider_key: str = Header(default=None, alias="X-Provider-Key"),
):
    """Anonymous, self-funded — stage 2 of 5."""
    override, err = _byok_auth(x_provider, x_provider_key)
    if err is not None:
        return err
    return _byok_module_response(ticker, "context", None, override)


@app.get("/api/byok/{ticker}/policy")
def api_byok_policy(
    ticker: str,
    x_provider: str = Header(default=None, alias="X-Provider"),
    x_provider_key: str = Header(default=None, alias="X-Provider-Key"),
):
    """Anonymous, self-funded — stage 3 of 5."""
    override, err = _byok_auth(x_provider, x_provider_key)
    if err is not None:
        return err
    return _byok_module_response(ticker, "policy", None, override)


@app.get("/api/byok/{ticker}/patterns")
def api_byok_patterns(
    ticker: str,
    x_provider: str = Header(default=None, alias="X-Provider"),
    x_provider_key: str = Header(default=None, alias="X-Provider-Key"),
):
    """Anonymous, self-funded — stage 4 of 5."""
    override, err = _byok_auth(x_provider, x_provider_key)
    if err is not None:
        return err
    return _byok_module_response(ticker, "patterns", None, override)


@app.post("/api/byok/{ticker}/synthesis")
def api_byok_synthesis(
    ticker: str,
    body: SynthesisBody,
    x_provider: str = Header(default=None, alias="X-Provider"),
    x_provider_key: str = Header(default=None, alias="X-Provider-Key"),
):
    """
    Anonymous, self-funded — stage 5 of 5: stitched verdict + bull/bear.

    Fences the posted module text exactly like /api/research/{ticker}/synthesis
    does, and for the identical reason — this is untrusted input steering three
    more paid calls, and a guard that runs for only some callers is a guard
    waiting to be bypassed. Applied here even though the "cost" is the
    caller's own: prompt injection could still misdirect the verdict, which is
    a correctness problem independent of who is paying.
    """
    override, err = _byok_auth(x_provider, x_provider_key)
    if err is not None:
        return err

    sym, bad = _validated_ticker(ticker)
    if bad is not None:
        return bad

    fenced = {
        k: sanitize_prompt_text(v, label=f"MODULE_{k.upper()}") if v else v
        for k, v in body.model_dump().items()
    }

    try:
        result = run_synthesis(ticker.upper(), fenced, override=override)
        if "error" in result:
            # Same reasoning as _byok_module_response: this is the caller's
            # own key, so the specific reason (not run_synthesis's own
            # generic, owner/guest-safe text) is what's actually useful here.
            reason_text = _PROVIDER_REASON_TEXT.get(result.get("reason"), "provider call failed")
            return JSONResponse(status_code=502,
                                 content={"error": reason_text, "code": "provider_unavailable"})
        return result
    except Exception as exc:  # noqa: BLE001 — type only, see api_byok_research's docstring
        logger.debug("api_byok_synthesis: error (type=%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Model Court — Perplexity + Gemini in parallel, compared ────────────────
# Different trust model from /api/byok/* above: this requires a SESSION
# (owner or guest via require_session), not anonymity — decided explicitly
# in chat rather than left implicit. A real allowlist/access-code gate
# beyond "any session" was requested for a later phase; this is the honest
# interim boundary, not that boundary's finished version.

@app.post("/api/model-court/{ticker}", dependencies=[Depends(require_session)])
def api_model_court(
    ticker: str,
    question: str = Query(default=None, max_length=_MAX_QUESTION_LEN),
    x_perplexity_key: str = Header(default=None, alias="X-Perplexity-Key"),
    x_gemini_key: str = Header(default=None, alias="X-Gemini-Key"),
):
    """
    Run Perplexity and Gemini on the same ticker in parallel, using the
    caller's own two keys, and return a comparison of what each caught.

    Requires a session (owner or guest) — see the module comment above for
    why that's the gate rather than anonymity. Neither the owner's nor a
    guest's existing entitlements extend here: both keys are the caller's
    own, this never touches the guest dive budget, and neither key is ever
    written to os.environ, persisted to .env, or logged — same guarantee
    every BYOK path in this file already holds, extended to two keys.

    Named headers (X-Perplexity-Key / X-Gemini-Key) rather than the generic
    X-Provider/X-Provider-Key pair /api/byok/* uses — this route always
    needs exactly these two, never an arbitrary provider choice.
    """
    for key_value, key_label in ((x_perplexity_key, "X-Perplexity-Key"),
                                  (x_gemini_key, "X-Gemini-Key")):
        ok, reason = soft_validate(key_value or "")
        if not ok:
            return JSONResponse(status_code=400,
                                 content={"error": f"{key_label}: {reason}"})

    try:
        result = run_model_court(ticker, question, x_perplexity_key, x_gemini_key)
        if "error" in result and "ticker" not in result:
            return JSONResponse(status_code=500, content={"error": "internal server error"})
        return result
    except Exception as exc:  # noqa: BLE001 — type only, matching /api/byok/*'s own reasoning:
        # two different callers' money on this one request, not just one.
        logger.debug("api_model_court: error (type=%s)", type(exc).__name__)
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

    # The operator's secret is tried first and in constant time. A guest key is
    # only consulted once that fails, so the common path is unchanged.
    if _agent_key_ok(body.key):
        store.purge_expired_sessions()
        token, expires = store.create_session("owner")
        max_age = store.SESSION_DAYS * 86400
        payload = {"ok": True, "tier": "owner", "expires_at": expires.isoformat(),
                   "guest": None}
    else:
        gk = store.guest_key_for(body.key)
        if gk is None or gk["dead"]:
            # Every miss feeds the socket backstop, so guest-key guessing gets
            # the same brute-force ceiling as operator-secret guessing for free.
            _record_auth_failure(socket_ip)
            logger.warning("api_session_create: rejected unlock from IP %s", client_ip)
            if gk is not None:
                # Saying "expired" reveals nothing: the caller already holds the
                # key. Saying "unauthorized" would send them to ask for a new
                # key they in fact already had, which helps nobody.
                return JSONResponse(
                    status_code=401,
                    content={"error": f"this guest key has been {gk['dead']}",
                             "code": "guest_expired"},
                )
            # Deliberately identical for "wrong key" and "no key configured" —
            # never confirm to a caller which of the two it hit.
            return JSONResponse(status_code=401, content={"error": "unauthorized"})

        key_expires = datetime.fromisoformat(gk["expires_at"])
        store.purge_expired_sessions()
        token, expires = store.create_session("guest", gk["id"], expires_at=key_expires)
        # Cookie life is the key's remaining life, never a flat 24h. Redeeming
        # in the key's last hour must not hand out a fresh day.
        remaining = (key_expires - datetime.now(key_expires.tzinfo)).total_seconds()
        max_age = max(1, int(remaining))
        payload = {"ok": True, "tier": "guest", "expires_at": expires.isoformat(),
                   "guest": store.guest_usage(gk["id"])}

    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,                       # JS cannot read it — that is the point
        samesite="strict",                   # not sent on cross-site requests
        secure=_is_https(request),
        path="/",
    )
    return payload


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
    """Who this browser is, and what allowance it has left. Never echoes the token.

    This is the single contract the frontend paints every tier-dependent
    element from — the key chip, the cost modal's guest strip, the boot log —
    so GET and POST /api/session deliberately return the same shape:

        {authenticated, tier: "owner"|"guest"|null, guest: {...}|null}

    `guest.modules_used` is a COUNT, not a flag. The allowance is five
    separately consumable stages; collapsing it to a boolean tells a guest one
    stage in that their whole dive is spent.
    """
    token = request.cookies.get(SESSION_COOKIE)
    info = store.session_info(token) if token else None
    if not info:
        return {"authenticated": False, "tier": None, "guest": None}
    return {
        "authenticated": True,
        "tier": info["tier"],
        "guest": store.guest_usage(info["guest_key_id"]) if info["tier"] == "guest" else None,
    }


# ── Free extras — public, yfinance only, no provider call ─────────────────
# Standalone routes rather than extra fields on /api/demo. /api/demo is the hot
# path behind every `quant` and `research` command, and folding two more network
# round-trips into it would add latency and new failure modes to the one call
# that must stay fast. The frontend fetches these lazily instead, so a slow or
# missing headline feed never delays the numbers.

@app.get("/api/earnings/{ticker}")
def api_earnings(ticker: str):
    """Next scheduled earnings date. A null date is normal, not an error."""
    try:
        result = get_next_earnings(ticker)
        if "error" in result:
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        logger.debug("api_earnings: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/api/news/{ticker}")
def api_news(ticker: str):
    """Recent headlines, whitelist-mapped. Links that are not http(s) are dropped
    server-side, so the browser is not the only thing checking them."""
    try:
        result = get_news(ticker)
        if "error" in result:
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as exc:
        logger.debug("api_news: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


# ── Access requests ────────────────────────────────────────────────────────
# Public. Someone who reached the landing page with no key asks the operator
# for one; the operator reviews by hand in the admin view and delivers the key
# personally. There is no outbound MAIL path here by design — the owner is
# pinged on Telegram (optional; see tools/notify.py) that a request landed,
# but the key itself is still always hand-delivered, never emailed by the app.

# Deliberately a stdlib regex, not pydantic's EmailStr — that would pull in the
# email-validator package, and this project ships no new dependencies.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccessRequestBody(BaseModel):
    email: str = Field(default="", max_length=254)
    note:  str = Field(default="", max_length=280)


@app.post("/api/access-request")
def api_access_request(body: AccessRequestBody, background_tasks: BackgroundTasks):
    """Record a request for access.

    The success body is byte-identical whether the address is new, already
    pending, already approved, or previously denied. Anything else turns this
    into an oracle for whether a given person has asked for access, which is
    exactly the kind of question a public endpoint must refuse to answer.
    """
    email = (body.email or "").strip()
    norm = email.lower()
    if not _EMAIL_RE.match(norm):
        return JSONResponse(status_code=400,
                            content={"error": "enter a valid email address"})

    try:
        outcome, is_new = store.create_access_request(email, norm, body.note or "")
    except Exception as exc:
        logger.debug("api_access_request: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})

    if outcome == "closed":
        # Depends on global queue state, not on this address, so it still tells
        # the caller nothing about themselves.
        return JSONResponse(
            status_code=503,
            content={"error": "access requests are closed at the moment — try again later"},
        )

    if is_new:
        # Runs after the response is sent (BackgroundTasks), and notify_telegram
        # itself swallows every failure — Telegram being down must never affect
        # what this public endpoint returns. is_new keeps a denied address on a
        # retry timer from paging the owner forever over a submission that
        # changes nothing (see create_access_request's docstring).
        note = (body.note or "").strip()
        text = (f"ARGUS: new access request\n{email}" + (f"\n{note}" if note else "")
                + "\n\nReply with this email to approve.")
        background_tasks.add_task(notify_telegram, text)

    return {"ok": True, "status": "received"}


_TELEGRAM_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _mint_and_approve(req: dict) -> tuple[int, str, datetime]:
    """Mint a guest key for a pending request and mark it approved.

    Shared by the admin web route and the Telegram approve-by-reply handler
    below so the two paths cannot drift into different behaviour.
    """
    key_id, raw, expires = store.create_guest_key(label=req["email"])
    store.decide_access_request(req["id"], "approved")
    return key_id, raw, expires


def _handle_telegram_reply(text: str) -> None:
    """Approve-by-reply: the owner replies to the push with the email (any
    surrounding words are fine — "yes bob@x.com go ahead" works) and this
    mints the same kind of key the admin view's APPROVE button would.

    Runs on the poller's background thread (tools/notify.py), so every path
    here replies via notify_telegram rather than raising — there is no HTTP
    caller to hand an error response to. Matches ONLY pending requests: an
    already-approved or already-denied row cannot be re-matched by email,
    which is what stops a stale or duplicate reply from minting a second key
    for the same address.
    """
    m = _TELEGRAM_EMAIL_RE.search(text)
    if not m:
        return
    norm = m.group(0).strip().lower()

    try:
        pending = [r for r in store.list_access_requests("pending")
                   if r["email_norm"] == norm]
        if not pending:
            notify_telegram(f"No pending request for {norm}.")
            return
        req = pending[0]
        _, raw, expires = _mint_and_approve(req)
        notify_telegram(
            f"Approved {req['email']}.\nKey: {raw}\nExpires: {expires.isoformat()}\n"
            "Shown once, here — deliver it to them yourself."
        )
    except Exception as exc:
        logger.error("telegram approve-by-reply failed (type=%s): %s",
                     type(exc).__name__, exc, exc_info=True)
        notify_telegram(f"Approving {norm} failed — check server logs.")


# ── Admin — owner only ─────────────────────────────────────────────────────

@app.get("/api/admin/requests", dependencies=[Depends(require_owner)])
def api_admin_requests(status: str = None):
    """Every request, newest first. Denied rows are included deliberately —
    hidden, a misclicked DENY becomes an invisible permanent block."""
    try:
        return {"requests": store.list_access_requests(status)}
    except Exception as exc:
        logger.debug("api_admin_requests: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.post("/api/admin/requests/{req_id}/approve", dependencies=[Depends(require_owner)])
def api_admin_approve(req_id: int):
    """Approve a request and mint its key.

    The raw key appears in THIS RESPONSE AND NOWHERE ELSE — only its SHA-256 is
    stored. The operator copies it here and delivers it themselves.
    """
    try:
        req = store.get_access_request(req_id)
        if req is None:
            return JSONResponse(status_code=404, content={"error": "no such request"})
        key_id, raw, expires = _mint_and_approve(req)
        return {"ok": True, "guest_key": raw, "key_id": key_id,
                "expires_at": expires.isoformat(),
                "request": store.get_access_request(req_id)}
    except Exception as exc:
        logger.debug("api_admin_approve: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.post("/api/admin/requests/{req_id}/deny", dependencies=[Depends(require_owner)])
def api_admin_deny(req_id: int):
    try:
        if not store.decide_access_request(req_id, "denied"):
            return JSONResponse(status_code=404, content={"error": "no such request"})
        return {"ok": True}
    except Exception as exc:
        logger.debug("api_admin_deny: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.post("/api/admin/requests/{req_id}/reopen", dependencies=[Depends(require_owner)])
def api_admin_reopen(req_id: int):
    """Undo a denial. Without this, one misclick blackholes a person silently:
    their resubmissions collide with the UNIQUE email and change nothing."""
    try:
        if not store.reopen_access_request(req_id):
            return JSONResponse(status_code=404,
                                content={"error": "no denied request with that id"})
        return {"ok": True}
    except Exception as exc:
        logger.debug("api_admin_reopen: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.get("/api/admin/guest-keys", dependencies=[Depends(require_owner)])
def api_admin_guest_keys():
    """Issued keys. Never includes key_hash — only the short display prefix."""
    try:
        return {"keys": store.list_guest_keys()}
    except Exception as exc:
        logger.debug("api_admin_guest_keys: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.post("/api/admin/guest-keys/{key_id}/revoke", dependencies=[Depends(require_owner)])
def api_admin_revoke(key_id: int):
    try:
        if not store.revoke_guest_key(key_id):
            return JSONResponse(status_code=404,
                                content={"error": "no active key with that id"})
        return {"ok": True}
    except Exception as exc:
        logger.debug("api_admin_revoke: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.post("/api/admin/sessions/revoke-all", dependencies=[Depends(require_owner)])
def api_admin_revoke_all():
    """Kill switch for every guest at once.

    Worth knowing why this exists: rotating AGENT_SECRET does NOT log anyone
    out. Sessions are validated from the cookie before the secret is consulted,
    so a rotated secret leaves every existing session — owner and guest — alive.
    This is the actual revocation path.
    """
    try:
        return {"ok": True, "revoked": store.revoke_all_guest_access()}
    except Exception as exc:
        logger.debug("api_admin_revoke_all: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


@app.post("/api/admin/sessions/revoke-owner", dependencies=[Depends(require_owner)])
def api_admin_revoke_owner(request: Request):
    """Kill switch for every OTHER owner session — e.g. a stolen cookie.

    revoke-all above only ever touched guest sessions. There was no way to
    cut off a specific compromised owner session (device theft, a shared
    machine, a leaked cookie jar) short of editing the database directly —
    sessions last up to SESSION_DAYS with no other revocation path. The
    caller's own current session is deliberately excluded so calling this
    can never lock the operator out of the account they are using it from.
    """
    try:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return JSONResponse(status_code=401, content={"error": "no active session"})
        return {"ok": True, "revoked": store.revoke_other_owner_sessions(token)}
    except Exception as exc:
        logger.debug("api_admin_revoke_owner: error (type=%s): %s", type(exc).__name__, exc)
        return JSONResponse(status_code=500, content={"error": "internal server error"})


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


@app.post("/api/profile", dependencies=[Depends(require_owner)])
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
    sector: str = Field(default="", max_length=80)


@app.get("/api/positions", dependencies=[Depends(require_session)])
def api_positions_list():
    return {"positions": store.positions()}


@app.post("/api/positions", dependencies=[Depends(require_owner)])
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


@app.delete("/api/positions/{ticker}", dependencies=[Depends(require_owner)])
def api_positions_delete(ticker: str):
    try:
        removed = store.delete_position(ticker)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": removed, "positions": store.positions()}


@app.get("/api/watchlist", dependencies=[Depends(require_session)])
def api_watchlist_list():
    return {"watchlist": store.watchlist()}


@app.post("/api/watchlist", dependencies=[Depends(require_owner)])
def api_watchlist_add(body: WatchBody):
    try:
        t = store.add_watch(body.ticker, body.note, body.sector)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "ticker": t, "watchlist": store.watchlist()}


@app.delete("/api/watchlist/{ticker}", dependencies=[Depends(require_owner)])
def api_watchlist_delete(ticker: str):
    try:
        removed = store.delete_watch(ticker)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": removed, "watchlist": store.watchlist()}


# ── AI provider key (owner only) ────────────────────────────────────────
# Lets an already-unlocked owner connect a Groq/Perplexity key from the
# browser's CONFIG modal instead of editing .env or running the CLI wizard —
# both remain valid alternatives, this is a third path, not a replacement.

class ProviderKeyBody(BaseModel):
    provider: str = Field(max_length=16)
    key: str = Field(default="", max_length=512)


@app.post("/api/settings/provider-key", dependencies=[Depends(require_owner)])
def api_settings_provider_key(body: ProviderKeyBody):
    """Set or clear a Groq/Perplexity credential from the browser.

    Owner-only, and more strictly so than the book writes above: this sets
    the credential every future paid research call on this instance will
    use, with no per-caller budget to bound a mistake — exactly the kind of
    route require_owner exists for.

    An empty `key` is a deliberate "clear this provider" action, not an
    error — soft_validate() only runs against a non-empty value.

    The raw key is never echoed back, not even masked. Unlike the CLI
    wizard's stdout confirmation (which never leaves the machine that ran
    it), this response travels over HTTP and can land in browser history or
    devtools — so it gets the same disclosure floor /health already holds
    for these two keys: presence only, never a prefix, length, or value.

    Deliberately checks PERSISTENT_PROVIDERS, not the wider PROVIDER_KEY_NAMES
    _byok_auth() uses — Gemini is BYOK-only for now (still-beta upstream
    integration), so this route refuses it even though PROVIDER_KEY_NAMES
    itself already has a "gemini" entry for BYOK's benefit. Without this
    explicit check, the generic .get() lookup below would silently start
    accepting a persisted Gemini key the moment that dict gained the entry.
    """
    if body.provider not in PERSISTENT_PROVIDERS:
        return JSONResponse(status_code=400, content={
            "error": f"provider must be one of: {', '.join(PERSISTENT_PROVIDERS)}"})
    key_name = PROVIDER_KEY_NAMES.get(body.provider)

    if body.key:
        ok, reason = soft_validate(body.key)
        if not ok:
            return JSONResponse(status_code=400, content={"error": reason})

    try:
        write_env_key(_ENV_FILE_PATH, key_name, body.key)
    except OSError as exc:
        # A read-only mount or a permission error, not a user-input mistake —
        # 500, not 400, and never str(exc) verbatim: some OSError messages
        # embed the full filesystem path, which is server-internal detail an
        # HTTP caller has no business seeing, owner or not.
        logger.warning("api_settings_provider_key: could not write .env (%s)", exc)
        return JSONResponse(status_code=500, content={
            "error": "could not save to .env on the server — check it exists and is writable"})

    # Live immediately — _get_provider() reads os.getenv() at call time, so
    # the very next research request picks this up with no restart.
    os.environ[key_name] = body.key
    return {"ok": True, "provider": body.provider, "configured": bool(body.key)}


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


@app.post("/api/portfolio/outlook", dependencies=[Depends(require_owner)])
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
