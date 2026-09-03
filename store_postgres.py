"""
store_postgres.py — Postgres (Cloud SQL) persistence, same public surface as
store.py.

This is NOT a shared-SQL layer with SQLite — it is a full sibling
implementation, deliberately. Two idiomatic files, each reviewable on its
own, beat one file full of `if postgres: ... else: ...` branches (the same
"swap the thing behind one seam" shape this codebase already uses for
`_get_provider()`'s override argument and `GeminiNativeClient`).

Only ever imported when `DATABASE_URL` is set — see the `sys.modules`
pre-registration in api.py's bootstrap block, which is what makes every
`import store` elsewhere in the process (tools/perplexity_research.py,
tools/stock_data.py) transparently resolve to THIS module in production,
without those files needing to know or care. `store.py` (SQLite) stays the
default, untouched, and every existing test that pokes its internals
(`store._conn`, `store.DB_PATH`, `store._lock`, `store._exec()`) keeps
working exactly as before, because this file is purely additive.

Design notes carried over unchanged from store.py:
  * config.MY_PORTFOLIO / WATCHLIST are SEED DATA, copied in once by
    bootstrap(); after that this database is authoritative.
  * Every query is parameterised (%s placeholders here, ? there) — no
    f-string SQL anywhere in this file either.
  * Every ticker write goes through validate_ticker().
  * Session tokens are stored as SHA-256 hashes only.

Where this file genuinely differs, and why:
  * No _migrate(). There is no existing Postgres data to migrate — the
    schema below ships in its final shape from CREATE TABLE day one.
  * No connection pool. This app's own concurrency model is already "one
    instance, one connection, one lock" (--workers 1, no horizontal scaling
    — CLAUDE.md's own stated hard constraint), so a pool would be new
    complexity solving a problem this app has already ruled out having.
  * A failed statement poisons a Postgres transaction ("current transaction
    is aborted") until it is rolled back — sqlite3 is more forgiving here.
    _txn() below is the one seam every multi-statement write goes through
    so a partial failure can never wedge the single shared connection for
    the next unrelated request.
"""

import hashlib
import json
import logging
import os
import secrets
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

from config import MY_PORTFOLIO, WATCHLIST
from tools.validator import validate_ticker

logger = logging.getLogger(__name__)

# Read once at import time, like store.py's own DB_PATH — tests that need a
# different target (scratch Postgres) monkeypatch this attribute and reset
# _conn to None, exactly the pattern verify_access.py already uses for
# store.DB_PATH.
DATABASE_URL = os.getenv("DATABASE_URL", "")

SESSION_DAYS = 30

_lock = threading.Lock()
_conn: "psycopg2.extensions.connection | None" = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    name        TEXT NOT NULL DEFAULT '',
    experience  TEXT NOT NULL DEFAULT '',
    risk        TEXT NOT NULL DEFAULT '',
    horizon     TEXT NOT NULL DEFAULT '',
    sectors     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    ticker     TEXT PRIMARY KEY,
    shares     DOUBLE PRECISION,
    avg_cost   DOUBLE PRECISION,
    buy_date   TEXT,
    stop_loss  DOUBLE PRECISION,
    trim_at    DOUBLE PRECISION,
    tranches   TEXT NOT NULL DEFAULT '[]',
    thesis     TEXT NOT NULL DEFAULT '',
    sector     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS watchlist (
    ticker      TEXT PRIMARY KEY,
    note        TEXT NOT NULL DEFAULT '',
    tranches    TEXT NOT NULL DEFAULT '[]',
    brent_gated BOOLEAN NOT NULL DEFAULT FALSE,
    exchange    TEXT NOT NULL DEFAULT '',
    sector      TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    tier         TEXT NOT NULL DEFAULT 'owner' CHECK (tier IN ('owner','guest')),
    guest_key_id INTEGER
);
-- Guest keys: time-boxed credentials the operator issues to other people.
-- Only the SHA-256 of the key is stored — a database read must not yield a
-- usable credential. key_prefix is kept in the clear purely so the admin
-- list can identify a row; far too short to be guessed back into a key.
CREATE TABLE IF NOT EXISTS guest_keys (
    id          SERIAL PRIMARY KEY,
    key_hash    TEXT NOT NULL UNIQUE,
    key_prefix  TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT,
    dive_ticker TEXT
);
-- One row per consumed stage of a guest's single included deep dive. The
-- PRIMARY KEY is the budget: an INSERT that violates it is a stage already
-- spent, which makes claiming a unit a single atomic statement rather than a
-- read-then-write with a race between the two.
CREATE TABLE IF NOT EXISTS guest_key_uses (
    key_id  INTEGER NOT NULL,
    module  TEXT NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY (key_id, module)
);
-- Access requests from people who want a key. Deliberately no IP column —
-- see store.py's identical comment; the reasoning is unchanged here.
CREATE TABLE IF NOT EXISTS access_requests (
    id         SERIAL PRIMARY KEY,
    email      TEXT NOT NULL,
    email_norm TEXT NOT NULL UNIQUE,
    note       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending','approved','denied')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS outlook (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Saved research artifacts — owner and guest only. See the identical
-- comment in store.py for why anonymous/BYOK research never lands here.
CREATE TABLE IF NOT EXISTS research_runs (
    id           SERIAL PRIMARY KEY,
    ticker       TEXT NOT NULL,
    mode         TEXT NOT NULL CHECK (mode IN ('full','synthesis','model_court')),
    tier         TEXT NOT NULL CHECK (tier IN ('owner','guest')),
    guest_key_id INTEGER,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_runs_scope
    ON research_runs (tier, guest_key_id, ticker, created_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Same bound as store.py's RESEARCH_HISTORY_LIMIT, kept as its own constant
# rather than importing store's (importing store.py here would defeat the
# whole point of this being an independent sibling, and would also risk
# reintroducing the sys.modules split-brain this file exists to avoid).
RESEARCH_HISTORY_LIMIT = 20

_now = lambda: datetime.now(timezone.utc).isoformat()


def _connect():
    global _conn
    if _conn is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "store_postgres imported but DATABASE_URL is not set — "
                "this module should only ever be reached via the "
                "sys.modules seam in api.py, which only fires when it is."
            )
        conn = psycopg2.connect(
            DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
        )
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
        _conn = conn
    return _conn


@contextmanager
def _txn():
    """Yield the shared connection inside `_lock`; commit on success, roll
    back on any exception.

    Every function below that needs more than one statement in one
    transaction (or an INSERT ... RETURNING id) goes through this instead of
    hand-rolling commit/rollback, so a partial failure can never leave the
    one shared connection wedged in Postgres's "current transaction is
    aborted" state for the next, unrelated request. `_rows`/`_exec` are
    themselves built on this.

    THREADING NOTE, same as store.py's: _lock is a plain, non-reentrant
    threading.Lock. Nothing here may nest a second `with _txn():` or a raw
    `with _lock:` inside a block already holding one — that deadlocks the
    first request that reaches it.
    """
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()


def _exec(sql: str, args: tuple = ()) -> None:
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)


def _loads(s: str, fallback):
    try:
        v = json.loads(s)
        return v if v is not None else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


# ── Bootstrap ─────────────────────────────────────────────────────────────

def bootstrap() -> None:
    """Seed positions/watchlist from config on first run only. See store.py's
    identical docstring — behaviour is unchanged, only the SQL dialect is."""
    _connect()
    if _rows("SELECT value FROM meta WHERE key = %s", ("seeded",)):
        return

    for ticker, cfg in MY_PORTFOLIO.items():
        try:
            t = validate_ticker(ticker)
        except ValueError:
            logger.warning("bootstrap: skipping invalid portfolio ticker %r", ticker)
            continue
        _exec(
            """INSERT INTO positions
               (ticker, shares, avg_cost, buy_date, stop_loss, trim_at, tranches, thesis, sector)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker) DO NOTHING""",
            (t, cfg.get("shares"), cfg.get("avg_cost"), None,
             cfg.get("stop_loss"), cfg.get("trim_at"),
             json.dumps(cfg.get("tranches") or []),
             cfg.get("thesis") or "", cfg.get("sector") or ""),
        )

    for ticker, cfg in WATCHLIST.items():
        try:
            t = validate_ticker(ticker)
        except ValueError:
            logger.warning("bootstrap: skipping invalid watchlist ticker %r", ticker)
            continue
        _exec(
            """INSERT INTO watchlist
               (ticker, note, tranches, brent_gated, exchange, sector, added_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ticker) DO NOTHING""",
            (t, cfg.get("thesis") or "", json.dumps(cfg.get("tranches") or []),
             bool(cfg.get("brent_gated")), cfg.get("exchange") or "",
             cfg.get("sector") or "", _now()),
        )

    _exec(
        """INSERT INTO meta (key, value) VALUES (%s,%s)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        ("seeded", _now()),
    )
    logger.info("store_postgres: seeded %d positions, %d watchlist entries from config",
                len(MY_PORTFOLIO), len(WATCHLIST))


# ── Positions ─────────────────────────────────────────────────────────────

def positions() -> dict[str, dict]:
    out = {}
    for r in _rows("SELECT * FROM positions ORDER BY ticker"):
        out[r["ticker"]] = {
            "shares": r["shares"], "avg_cost": r["avg_cost"], "buy_date": r["buy_date"],
            "stop_loss": r["stop_loss"], "trim_at": r["trim_at"],
            "tranches": _loads(r["tranches"], []),
            "thesis": r["thesis"], "sector": r["sector"],
        }
    return out


def upsert_position(ticker: str, data: dict) -> str:
    t = validate_ticker(ticker)
    existing = positions().get(t, {})
    merged = {**existing, **data}
    _exec(
        """INSERT INTO positions
             (ticker, shares, avg_cost, buy_date, stop_loss, trim_at, tranches, thesis, sector)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (ticker) DO UPDATE SET
             shares=excluded.shares, avg_cost=excluded.avg_cost, buy_date=excluded.buy_date,
             stop_loss=excluded.stop_loss, trim_at=excluded.trim_at,
             tranches=excluded.tranches, thesis=excluded.thesis, sector=excluded.sector""",
        (t, merged.get("shares"), merged.get("avg_cost"), merged.get("buy_date"),
         merged.get("stop_loss"), merged.get("trim_at"),
         json.dumps(merged.get("tranches") or []),
         merged.get("thesis") or "", merged.get("sector") or ""),
    )
    return t


def delete_position(ticker: str) -> bool:
    t = validate_ticker(ticker)
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM positions WHERE ticker = %s", (t,))
            return cur.rowcount > 0


# ── Watchlist ─────────────────────────────────────────────────────────────

def watchlist() -> dict[str, dict]:
    out = {}
    for r in _rows("SELECT * FROM watchlist ORDER BY ticker"):
        out[r["ticker"]] = {
            "thesis": r["note"], "note": r["note"],
            "tranches": _loads(r["tranches"], []) or None,
            "brent_gated": bool(r["brent_gated"]),
            "exchange": r["exchange"] or None,
            "sector": r["sector"] or None,
        }
    return out


def add_watch(ticker: str, note: str = "", sector: str = "") -> str:
    t = validate_ticker(ticker)
    _exec(
        """INSERT INTO watchlist (ticker, note, tranches, brent_gated, exchange, sector, added_at)
           VALUES (%s,%s,'[]',FALSE,'',%s,%s)
           ON CONFLICT (ticker) DO UPDATE SET
               note=excluded.note,
               sector=CASE WHEN excluded.sector != '' THEN excluded.sector
                           ELSE watchlist.sector END""",
        (t, (note or "")[:500], (sector or "")[:80], _now()),
    )
    return t


def delete_watch(ticker: str) -> bool:
    t = validate_ticker(ticker)
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist WHERE ticker = %s", (t,))
            return cur.rowcount > 0


# ── Profile ───────────────────────────────────────────────────────────────

_EXPERIENCE = {"new", "intermediate", "professional"}
_RISK       = {"conservative", "balanced", "aggressive"}
_HORIZON    = {"under1y", "1to3y", "3to5y", "over5y"}


def profile() -> dict | None:
    r = _rows("SELECT * FROM profile WHERE id = 1")
    if not r:
        return None
    p = r[0]
    return {
        "name": p["name"], "experience": p["experience"], "risk": p["risk"],
        "horizon": p["horizon"], "sectors": _loads(p["sectors"], []),
        "created_at": p["created_at"], "updated_at": p["updated_at"],
    }


def save_profile(data: dict) -> dict:
    name = str(data.get("name") or "").strip()[:60]
    exp  = data.get("experience") if data.get("experience") in _EXPERIENCE else "intermediate"
    risk = data.get("risk")       if data.get("risk")       in _RISK       else "balanced"
    hor  = data.get("horizon")    if data.get("horizon")    in _HORIZON    else "1to3y"
    sectors = [str(s)[:40] for s in (data.get("sectors") or [])][:12]

    now = _now()
    existing = profile()
    _exec(
        """INSERT INTO profile (id, name, experience, risk, horizon, sectors, created_at, updated_at)
           VALUES (1,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET
             name=excluded.name, experience=excluded.experience, risk=excluded.risk,
             horizon=excluded.horizon, sectors=excluded.sectors, updated_at=excluded.updated_at""",
        (name, exp, risk, hor, json.dumps(sectors),
         existing["created_at"] if existing else now, now),
    )
    return profile()


# ── Sessions ──────────────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(tier: str = "owner", guest_key_id: int | None = None,
                   expires_at: datetime | None = None) -> tuple[str, datetime]:
    if tier not in ("owner", "guest"):
        raise ValueError(f"unknown session tier: {tier!r}")
    token = secrets.token_urlsafe(32)
    expires = expires_at or (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS))
    _exec("INSERT INTO sessions (token_hash, created_at, expires_at, tier, guest_key_id) "
          "VALUES (%s,%s,%s,%s,%s)",
          (_hash_token(token), _now(), expires.isoformat(), tier, guest_key_id))
    return token, expires


def session_info(token: str) -> dict | None:
    if not token:
        return None
    rows = _rows(
        """SELECT s.expires_at, s.tier, s.guest_key_id,
                  g.revoked_at AS key_revoked, g.expires_at AS key_expires
             FROM sessions s
             LEFT JOIN guest_keys g ON g.id = s.guest_key_id
            WHERE s.token_hash = %s""",
        (_hash_token(token),),
    )
    if not rows:
        return None
    r = rows[0]
    now = datetime.now(timezone.utc)

    try:
        if datetime.fromisoformat(r["expires_at"]) < now:
            destroy_session(token)
            return None
    except ValueError:
        return None

    if r["tier"] == "guest":
        if r["guest_key_id"] is None or r["key_expires"] is None:
            destroy_session(token)
            return None
        if r["key_revoked"]:
            destroy_session(token)
            return None
        try:
            if datetime.fromisoformat(r["key_expires"]) < now:
                destroy_session(token)
                return None
        except ValueError:
            return None

    return {"tier": r["tier"], "guest_key_id": r["guest_key_id"],
            "expires_at": r["expires_at"]}


def destroy_session(token: str) -> None:
    if token:
        _exec("DELETE FROM sessions WHERE token_hash = %s", (_hash_token(token),))


def purge_expired_sessions() -> None:
    _exec("DELETE FROM sessions WHERE expires_at < %s", (_now(),))


# ── Guest keys ────────────────────────────────────────────────────────────

GUEST_HOURS = 24


def _expired(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso) < datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


GUEST_MODULES = ("report", "context", "policy", "patterns", "synthesis")


def create_guest_key(label: str = "", hours: int = GUEST_HOURS) -> tuple[int, str, datetime]:
    raw = "gk_" + secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO guest_keys (key_hash, key_prefix, label, created_at, expires_at) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (_hash_token(raw), raw[:6], (label or "")[:254], _now(), expires.isoformat()),
            )
            new_id = cur.fetchone()["id"]
        return new_id, raw, expires


def guest_key_for(raw_key: str) -> dict | None:
    if not raw_key:
        return None
    rows = _rows("SELECT * FROM guest_keys WHERE key_hash = %s", (_hash_token(raw_key),))
    if not rows:
        return None
    r = dict(rows[0])
    r["dead"] = None
    if r["revoked_at"]:
        r["dead"] = "revoked"
    elif _expired(r["expires_at"]):
        r["dead"] = "expired"
    return r


def guest_usage(key_id: int) -> dict:
    rows = _rows("SELECT * FROM guest_keys WHERE id = %s", (key_id,))
    if not rows:
        return {"expires_at": None, "dive_ticker": None, "modules_total": len(GUEST_MODULES),
                "modules_used": 0, "exhausted": True, "revoked": True}
    r = rows[0]
    used = _rows("SELECT module FROM guest_key_uses WHERE key_id = %s", (key_id,))
    n = len(used)
    return {
        "expires_at":    r["expires_at"],
        "dive_ticker":   r["dive_ticker"],
        "modules_total": len(GUEST_MODULES),
        "modules_used":  n,
        "exhausted":     n >= len(GUEST_MODULES),
        "revoked":       bool(r["revoked_at"]),
    }


def list_guest_keys() -> list[dict]:
    out = []
    for r in _rows("SELECT * FROM guest_keys ORDER BY created_at DESC"):
        usage = guest_usage(r["id"])
        expired = _expired(r["expires_at"])
        out.append({
            "id": r["id"], "key_prefix": r["key_prefix"], "label": r["label"],
            "created_at": r["created_at"], "expires_at": r["expires_at"],
            "revoked": bool(r["revoked_at"]), "expired": expired,
            "dive_ticker": r["dive_ticker"],
            "modules_used": usage["modules_used"], "modules_total": usage["modules_total"],
        })
    return out


def revoke_guest_key(key_id: int) -> bool:
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE guest_keys SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
                (_now(), key_id),
            )
            changed = cur.rowcount > 0
            cur.execute("DELETE FROM sessions WHERE guest_key_id = %s", (key_id,))
        return changed


def revoke_all_guest_access() -> int:
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE guest_keys SET revoked_at = %s WHERE revoked_at IS NULL", (_now(),)
            )
            n = cur.rowcount
            cur.execute("DELETE FROM sessions WHERE tier = 'guest'")
        return n


def revoke_other_owner_sessions(current_token: str) -> int:
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sessions WHERE tier = 'owner' AND token_hash != %s",
                (_hash_token(current_token),),
            )
            return cur.rowcount


# ── Guest budget ──────────────────────────────────────────────────────────

def consume_guest_unit(key_id: int, ticker: str, module: str) -> str:
    """Claim one stage of the guest's dive.

    Returns 'ok' | 'used' | 'other_ticker' | 'expired' | 'revoked' | 'missing'.

    Postgres-specific subtlety store.py's SQLite version doesn't have: a
    failed statement aborts the WHOLE surrounding transaction, not just
    itself. The INSERT below is expected to fail on a genuine race (two
    concurrent claims of the same stage) — without a SAVEPOINT around it,
    catching that failure in Python would still leave the transaction
    aborted, and the dive_ticker UPDATE a few lines above (already
    genuinely committed-worthy) would be silently discarded when this
    function's _txn() wrapper commits at the end. The SAVEPOINT scopes the
    abort to just the conflicting INSERT.
    """
    t = validate_ticker(ticker)
    if module not in GUEST_MODULES:
        raise ValueError(f"unknown guest module: {module!r}")

    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM guest_keys WHERE id = %s", (key_id,))
            row = cur.fetchone()
            if row is None:
                return "missing"
            if row["revoked_at"]:
                return "revoked"
            if _expired(row["expires_at"]):
                return "expired"

            cur.execute(
                "UPDATE guest_keys SET dive_ticker = %s WHERE id = %s AND dive_ticker IS NULL",
                (t, key_id),
            )
            cur.execute("SELECT dive_ticker FROM guest_keys WHERE id = %s", (key_id,))
            bound = cur.fetchone()["dive_ticker"]
            if bound != t:
                return "other_ticker"

            cur.execute("SAVEPOINT claim_module")
            try:
                cur.execute(
                    "INSERT INTO guest_key_uses (key_id, module, used_at) VALUES (%s,%s,%s)",
                    (key_id, module, _now()),
                )
                cur.execute("RELEASE SAVEPOINT claim_module")
                return "ok"
            except psycopg2.IntegrityError:
                cur.execute("ROLLBACK TO SAVEPOINT claim_module")
                return "used"


def refund_guest_unit(key_id: int, module: str) -> None:
    _exec("DELETE FROM guest_key_uses WHERE key_id = %s AND module = %s", (key_id, module))


# ── Access requests ───────────────────────────────────────────────────────

_MAX_PENDING = 200
_MAX_REQUESTS = 1000


def create_access_request(email: str, email_norm: str, note: str) -> tuple[str, bool]:
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM access_requests WHERE status = 'pending'")
            pending = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM access_requests")
            total = cur.fetchone()["n"]
            cur.execute("SELECT id FROM access_requests WHERE email_norm = %s", (email_norm,))
            existing = cur.fetchone()
            is_new = existing is None

            if is_new and (pending >= _MAX_PENDING or total >= _MAX_REQUESTS):
                return "closed", False

            cur.execute(
                """INSERT INTO access_requests (email, email_norm, note, status, created_at)
                   VALUES (%s,%s,%s, 'pending', %s)
                   ON CONFLICT (email_norm) DO UPDATE SET note = excluded.note
                     WHERE access_requests.status = 'pending'""",
                (email[:254], email_norm, (note or "")[:280], _now()),
            )
            return "ok", is_new


def list_access_requests(status: str | None = None) -> list[dict]:
    if status:
        rows = _rows("SELECT * FROM access_requests WHERE status = %s ORDER BY created_at DESC",
                     (status,))
    else:
        rows = _rows("SELECT * FROM access_requests ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def get_access_request(req_id: int) -> dict | None:
    rows = _rows("SELECT * FROM access_requests WHERE id = %s", (req_id,))
    return dict(rows[0]) if rows else None


def decide_access_request(req_id: int, status: str) -> bool:
    if status not in ("approved", "denied"):
        raise ValueError(f"unknown decision: {status!r}")
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE access_requests SET status = %s, decided_at = %s WHERE id = %s",
                        (status, _now(), req_id))
            return cur.rowcount > 0


def reopen_access_request(req_id: int) -> bool:
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE access_requests SET status = 'pending', decided_at = NULL "
                "WHERE id = %s AND status = 'denied'",
                (req_id,),
            )
            return cur.rowcount > 0


# ── Outlook cache ─────────────────────────────────────────────────────────

def save_outlook(payload: dict) -> None:
    _exec("""INSERT INTO outlook (id, payload, created_at) VALUES (1,%s,%s)
             ON CONFLICT (id) DO UPDATE SET payload=excluded.payload,
                                            created_at=excluded.created_at""",
          (json.dumps(payload), _now()))


def get_outlook() -> dict | None:
    r = _rows("SELECT payload, created_at FROM outlook WHERE id = 1")
    if not r:
        return None
    data = _loads(r[0]["payload"], None)
    if data is None:
        return None
    data["cached_at"] = r[0]["created_at"]
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(r[0]["created_at"])
        data["age_days"] = age.days
    except ValueError:
        data["age_days"] = None
    return data


# ── Research history ─────────────────────────────────────────────────────

def save_research_run(ticker: str, mode: str, tier: str,
                       guest_key_id: int | None, payload: dict) -> int:
    """Persist one completed research artifact and enforce the retention cap
    in the same transaction.

    `guest_key_id IS NOT DISTINCT FROM %s`, not `= %s`: standard SQL `=`
    never matches NULL against NULL, so an owner row (guest_key_id always
    NULL) would silently fail to scope against itself. This is Postgres's
    spelling of the same NULL-safe comparison store.py gets from SQLite's
    `IS ?` — Postgres does NOT accept a bound parameter on the right of a
    bare `IS` (only NULL/TRUE/FALSE/UNKNOWN are legal there), so `IS ?`
    itself does not translate; `IS NOT DISTINCT FROM` is the standard-SQL
    operator built for exactly this.
    """
    with _txn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_runs (ticker, mode, tier, guest_key_id, payload, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (ticker, mode, tier, guest_key_id, json.dumps(payload), _now()),
            )
            new_id = cur.fetchone()["id"]
            cur.execute(
                """DELETE FROM research_runs WHERE id IN (
                       SELECT id FROM research_runs
                       WHERE ticker = %s AND tier = %s
                         AND guest_key_id IS NOT DISTINCT FROM %s
                       ORDER BY created_at DESC, id DESC
                       OFFSET %s
                   )""",
                (ticker, tier, guest_key_id, RESEARCH_HISTORY_LIMIT),
            )
            return new_id


def list_research_runs(ticker: str, tier: str, guest_key_id: int | None,
                        limit: int = RESEARCH_HISTORY_LIMIT) -> list[dict]:
    rows = _rows(
        "SELECT id, ticker, mode, payload, created_at FROM research_runs "
        "WHERE ticker = %s AND tier = %s AND guest_key_id IS NOT DISTINCT FROM %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (ticker, tier, guest_key_id, limit),
    )
    return [
        {"id": r["id"], "ticker": r["ticker"], "mode": r["mode"],
         "created_at": r["created_at"], "payload": _loads(r["payload"], {})}
        for r in rows
    ]


# ── Helpers ───────────────────────────────────────────────────────────────

def parse_buy_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None
