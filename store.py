"""
store.py — SQLite persistence for profile, positions, watchlist, sessions.

Stdlib only (sqlite3 + hashlib.scrypt). No ORM: the queries are a dozen
one-liners and an ORM would be more code than it saves.

Design notes:
  * config.MY_PORTFOLIO / WATCHLIST are SEED DATA. bootstrap() copies them in
    on first run; after that this database is authoritative. That is what makes
    an edited or deleted position actually stick.
  * Every query is parameterised — no f-string SQL anywhere in this file.
  * Every ticker write goes through validate_ticker() so a malformed symbol
    cannot reach yfinance or a prompt via the database.
  * Session tokens are stored as SHA-256 hashes. A database read must not yield
    a usable credential.
"""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import MY_PORTFOLIO, WATCHLIST
from tools.validator import validate_ticker

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("ARGUS_DB", Path(__file__).parent / "argus.db"))

SESSION_DAYS = 30

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

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
    shares     REAL,
    avg_cost   REAL,
    buy_date   TEXT,
    stop_loss  REAL,
    trim_at    REAL,
    tranches   TEXT NOT NULL DEFAULT '[]',
    thesis     TEXT NOT NULL DEFAULT '',
    sector     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS watchlist (
    ticker      TEXT PRIMARY KEY,
    note        TEXT NOT NULL DEFAULT '',
    tranches    TEXT NOT NULL DEFAULT '[]',
    brent_gated INTEGER NOT NULL DEFAULT 0,
    exchange    TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    -- CHECKed like access_requests.status is; the same schema should not
    -- constrain one enum column and leave the security-relevant one open.
    -- Only lands on fresh databases (ADD COLUMN cannot add a CHECK), so it
    -- is belt-and-braces behind create_session's ValueError.
    tier       TEXT NOT NULL DEFAULT 'owner' CHECK (tier IN ('owner','guest')),
    guest_key_id INTEGER
);
-- Guest keys: time-boxed credentials the operator issues to other people.
-- Only the SHA-256 of the key is stored, same rule as sessions above — a
-- database read must not yield a usable credential. key_prefix is the first
-- few characters, kept in the clear purely so the admin list can identify a
-- row; it is far too short to be guessed back into a key.
CREATE TABLE IF NOT EXISTS guest_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
-- Access requests from people who want a key. Deliberately no IP column: the
-- rate limiting is in-memory anyway, and its absence is what lets the privacy
-- note promise that only an email, an optional note and a timestamp are kept.
CREATE TABLE IF NOT EXISTS access_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_now = lambda: datetime.now(timezone.utc).isoformat()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for tables that already exist.

    CREATE TABLE IF NOT EXISTS is a no-op on a table that is already there, so
    it cannot add a column to a database created by an earlier version. This
    inspects the live schema instead of tracking a version counter, which makes
    running it twice a no-op by construction.

    It lives here rather than in bootstrap() deliberately: bootstrap() returns
    early once the "seeded" meta flag is set, so a migration placed there would
    never run on exactly the established databases that need it.

    DEFAULT 'owner' is load-bearing. Every session row predating this column
    could only have been created by presenting AGENT_SECRET, so backfilling
    them as owner sessions is correct — and SQLite only accepts ADD COLUMN
    ... NOT NULL when a non-null default is supplied.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "tier" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN tier TEXT NOT NULL DEFAULT 'owner'")
    if "guest_key_id" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN guest_key_id INTEGER")
    conn.commit()


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(_SCHEMA)
        _conn.commit()
        _migrate(_conn)
    return _conn


def _rows(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return _connect().execute(sql, args).fetchall()


def _exec(sql: str, args: tuple = ()) -> None:
    with _lock:
        c = _connect()
        c.execute(sql, args)
        c.commit()


def _loads(s: str, fallback):
    try:
        v = json.loads(s)
        return v if v is not None else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


# ── Bootstrap ─────────────────────────────────────────────────────────────

def bootstrap() -> None:
    """Seed positions/watchlist from config on first run only.

    Guarded by a meta flag rather than by row count: if the user deletes every
    position on purpose, re-seeding on the next boot would resurrect them.
    """
    _connect()
    if _rows("SELECT value FROM meta WHERE key = ?", ("seeded",)):
        return

    for ticker, cfg in MY_PORTFOLIO.items():
        try:
            t = validate_ticker(ticker)
        except ValueError:
            logger.warning("bootstrap: skipping invalid portfolio ticker %r", ticker)
            continue
        _exec(
            """INSERT OR IGNORE INTO positions
               (ticker, shares, avg_cost, buy_date, stop_loss, trim_at, tranches, thesis, sector)
               VALUES (?,?,?,?,?,?,?,?,?)""",
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
            """INSERT OR IGNORE INTO watchlist
               (ticker, note, tranches, brent_gated, exchange, added_at)
               VALUES (?,?,?,?,?,?)""",
            (t, cfg.get("thesis") or "", json.dumps(cfg.get("tranches") or []),
             1 if cfg.get("brent_gated") else 0, cfg.get("exchange") or "", _now()),
        )

    _exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", ("seeded", _now()))
    logger.info("store: seeded %d positions, %d watchlist entries from config",
                len(MY_PORTFOLIO), len(WATCHLIST))


# ── Positions ─────────────────────────────────────────────────────────────

def positions() -> dict[str, dict]:
    """All holdings, shaped like config.MY_PORTFOLIO so callers are unchanged."""
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
    """Partial update: `data` must contain only the keys the caller set.

    Callers pass model_dump(exclude_unset=True) — an omitted key keeps its
    stored value rather than being overwritten with a default.
    """
    t = validate_ticker(ticker)
    existing = positions().get(t, {})
    merged = {**existing, **data}
    _exec(
        """INSERT INTO positions
             (ticker, shares, avg_cost, buy_date, stop_loss, trim_at, tranches, thesis, sector)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
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
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM positions WHERE ticker = ?", (t,))
        c.commit()
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
        }
    return out


def add_watch(ticker: str, note: str = "") -> str:
    t = validate_ticker(ticker)
    _exec(
        """INSERT INTO watchlist (ticker, note, tranches, brent_gated, exchange, added_at)
           VALUES (?,?,'[]',0,'',?)
           ON CONFLICT(ticker) DO UPDATE SET note=excluded.note""",
        (t, (note or "")[:500], _now()),
    )
    return t


def delete_watch(ticker: str) -> bool:
    t = validate_ticker(ticker)
    with _lock:
        c = _connect()
        cur = c.execute("DELETE FROM watchlist WHERE ticker = ?", (t,))
        c.commit()
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
    """Whitelist every field — free-form values must not reach the UI or prompts."""
    name = str(data.get("name") or "").strip()[:60]
    exp  = data.get("experience") if data.get("experience") in _EXPERIENCE else "intermediate"
    risk = data.get("risk")       if data.get("risk")       in _RISK       else "balanced"
    hor  = data.get("horizon")    if data.get("horizon")    in _HORIZON    else "1to3y"
    sectors = [str(s)[:40] for s in (data.get("sectors") or [])][:12]

    now = _now()
    existing = profile()
    _exec(
        """INSERT INTO profile (id, name, experience, risk, horizon, sectors, created_at, updated_at)
           VALUES (1,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
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
    """Return (raw_token, expires_at). Only the hash is persisted.

    Defaults reproduce the previous behaviour exactly — a 30-day owner session
    — so existing callers need no change.

    A guest session passes the *key's* expiry as expires_at rather than
    now + 24h. Redeeming a key in its 23rd hour must yield a one-hour session,
    never a fresh 24, or the key's lifetime would restart on every unlock.
    """
    if tier not in ("owner", "guest"):
        raise ValueError(f"unknown session tier: {tier!r}")
    token = secrets.token_urlsafe(32)
    expires = expires_at or (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS))
    _exec("INSERT INTO sessions (token_hash, created_at, expires_at, tier, guest_key_id) "
          "VALUES (?,?,?,?,?)",
          (_hash_token(token), _now(), expires.isoformat(), tier, guest_key_id))
    return token, expires


def session_info(token: str) -> dict | None:
    """Resolve a session token to {tier, guest_key_id, expires_at}, or None.

    This replaced a bool-returning session_valid(). The boolean was the whole
    problem: api.py treats any valid session as a full credential, so once
    guest keys exist a guest would be indistinguishable from the operator and
    could spend on routes that have no budget concept at all.

    A guest session is valid only while its key is *also* valid. The LEFT JOIN
    is what makes revocation and the 24-hour wall bite immediately rather than
    at the cookie's own expiry — a revoked key kills its live sessions on their
    very next request.
    """
    if not token:
        return None
    rows = _rows(
        """SELECT s.expires_at, s.tier, s.guest_key_id,
                  g.revoked_at AS key_revoked, g.expires_at AS key_expires
             FROM sessions s
             LEFT JOIN guest_keys g ON g.id = s.guest_key_id
            WHERE s.token_hash = ?""",
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
        # A guest row whose key vanished is not a usable session. Fail closed:
        # every one of these means the key is gone, dead, or past its wall.
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
        _exec("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def purge_expired_sessions() -> None:
    _exec("DELETE FROM sessions WHERE expires_at < ?", (_now(),))


# ── Guest keys ────────────────────────────────────────────────────────────
# A guest key is a 24-hour credential the operator hands to one person. It
# buys exactly one full research dive on exactly one ticker; everything free
# stays unlimited for its lifetime.
#
# THREADING NOTE for everything below: _lock is a plain, NON-reentrant
# threading.Lock, and _rows()/_exec() each acquire it. Any function here that
# holds the lock must therefore talk to the raw connection directly. Calling
# _rows() or _exec() from inside a `with _lock:` block deadlocks the process on
# the first request that reaches it. The surrounding file's idiom is the
# opposite, which is exactly why this is spelled out.

GUEST_HOURS = 24


def _expired(iso: str) -> bool:
    """True when an ISO timestamp is in the past, or unparseable.

    Fails closed on a bad value: a timestamp we cannot read is not evidence
    that a credential is still live. Every expiry test on a guest key goes
    through here so the fail-closed rule cannot differ between them.
    """
    try:
        return datetime.fromisoformat(iso) < datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


# The five paid stages of one dive: four module GETs plus the synthesis POST.
GUEST_MODULES = ("report", "context", "policy", "patterns", "synthesis")


def create_guest_key(label: str = "", hours: int = GUEST_HOURS) -> tuple[int, str, datetime]:
    """Mint a key. Returns (id, raw_key, expires_at); the raw key is shown once.

    Only the SHA-256 is persisted, the same rule the sessions table follows —
    a database read must not yield a usable credential. key_prefix keeps the
    first few characters in the clear so the operator's admin list can identify
    a row; at 6 characters it is far too short to be guessed back into a key.
    """
    raw = "gk_" + secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    with _lock:
        c = _connect()
        cur = c.execute(
            "INSERT INTO guest_keys (key_hash, key_prefix, label, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (_hash_token(raw), raw[:6], (label or "")[:254], _now(), expires.isoformat()),
        )
        c.commit()
        return cur.lastrowid, raw, expires


def guest_key_for(raw_key: str) -> dict | None:
    """Look a key up by hash. Returns the row plus a 'dead' reason, or None.

    Hash-indexed rather than a loop of constant-time comparisons over every
    stored key: any timing signal from the lookup concerns the digest, not the
    key, and turning that into a key needs a preimage attack.

    'dead' distinguishes revoked from expired so POST /api/session can say
    something true. That leaks nothing — the caller already holds the key.
    """
    if not raw_key:
        return None
    rows = _rows("SELECT * FROM guest_keys WHERE key_hash = ?", (_hash_token(raw_key),))
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
    """The guest's allowance, in the exact shape the frontend contract needs.

    modules_used is a count, never a boolean. The budget is five separately
    consumable stages, so a guest one stage in has four left — reporting that
    as "used" would tell them their dive is gone while most of it remains.
    """
    rows = _rows("SELECT * FROM guest_keys WHERE id = ?", (key_id,))
    if not rows:
        return {"expires_at": None, "dive_ticker": None, "modules_total": len(GUEST_MODULES),
                "modules_used": 0, "exhausted": True, "revoked": True}
    r = rows[0]
    used = _rows("SELECT module FROM guest_key_uses WHERE key_id = ?", (key_id,))
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
    """Admin listing. Never returns key_hash — that is the credential material."""
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
    """Revoke a key and drop its sessions in the same breath.

    session_info()'s JOIN already makes any surviving cookie dead on its next
    request; deleting the rows as well means the session does not linger in the
    table waiting for an expiry that no longer matters.
    """
    with _lock:
        c = _connect()
        cur = c.execute(
            "UPDATE guest_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (_now(), key_id),
        )
        c.execute("DELETE FROM sessions WHERE guest_key_id = ?", (key_id,))
        c.commit()
        return cur.rowcount > 0


def revoke_all_guest_access() -> int:
    """Kill switch. Rotating AGENT_SECRET logs nobody out — sessions are checked
    before the secret is ever consulted — so this is the real one."""
    with _lock:
        c = _connect()
        cur = c.execute("UPDATE guest_keys SET revoked_at = ? WHERE revoked_at IS NULL", (_now(),))
        c.execute("DELETE FROM sessions WHERE tier = 'guest'")
        c.commit()
        return cur.rowcount


# ── Guest budget ──────────────────────────────────────────────────────────

def consume_guest_unit(key_id: int, ticker: str, module: str) -> str:
    """Claim one stage of the guest's dive.

    Returns 'ok' | 'used' | 'other_ticker' | 'expired' | 'revoked' | 'missing'.

    The whole thing runs in one BEGIN IMMEDIATE transaction on the raw
    connection. Two guarantees come out of that:

      * The INSERT's PRIMARY KEY(key_id, module) is the check-and-set. There is
        no "read whether it is used, then mark it used" window for a second
        concurrent request to slip through — the four module calls of a staged
        run can and do overlap.
      * dive_ticker is bound by a conditional UPDATE (... WHERE dive_ticker IS
        NULL) and then re-read. Two simultaneous first-calls for different
        tickers cannot both win: one sets it, the other reads back the winner's
        value and is refused.
    """
    t = validate_ticker(ticker)
    if module not in GUEST_MODULES:
        raise ValueError(f"unknown guest module: {module!r}")

    with _lock:
        c = _connect()
        c.execute("BEGIN IMMEDIATE")
        # One exit point, deliberately. The outcome is computed into `result`
        # and committed once, below.
        #
        # This was `try: … return … finally: c.commit()`. The bare finally
        # committed whatever a FAILED transaction had already written — in
        # practice the conditional UPDATE binding dive_ticker — leaving a guest
        # with a key welded to a ticker it consumed no stage on, refused 409 on
        # every other symbol, and nothing logged.
        #
        # The obvious repair (`except: rollback; raise` / `else: commit`) is
        # WRONG here and silently so: an `else` clause does not run when the
        # try block exits via `return`, and every branch below returns. The
        # commit never happened, the transaction stayed open, and the next
        # BEGIN IMMEDIATE died with "cannot start a transaction within a
        # transaction" — which is exactly why the original reached for finally.
        try:
            row = c.execute("SELECT * FROM guest_keys WHERE id = ?", (key_id,)).fetchone()
            if row is None:
                result = "missing"
            elif row["revoked_at"]:
                result = "revoked"
            elif _expired(row["expires_at"]):
                result = "expired"
            else:
                c.execute(
                    "UPDATE guest_keys SET dive_ticker = ? WHERE id = ? AND dive_ticker IS NULL",
                    (t, key_id),
                )
                bound = c.execute(
                    "SELECT dive_ticker FROM guest_keys WHERE id = ?", (key_id,)
                ).fetchone()["dive_ticker"]
                if bound != t:
                    result = "other_ticker"
                else:
                    try:
                        c.execute(
                            "INSERT INTO guest_key_uses (key_id, module, used_at) "
                            "VALUES (?,?,?)",
                            (key_id, module, _now()),
                        )
                        result = "ok"
                    except sqlite3.IntegrityError:
                        result = "used"
        except Exception:
            c.rollback()
            logger.error("consume_guest_unit failed (key_id=%s ticker=%s module=%s)",
                         key_id, t, module, exc_info=True)
            raise

        c.commit()
        return result


def refund_guest_unit(key_id: int, module: str) -> None:
    """Give a stage back when it cost nothing.

    Called only when the provider was never configured, which raises before any
    network call — so no money moved and charging for it would be theft of an
    allowance. Any other failure keeps the unit: the call may well have been
    billed upstream.

    Deliberately does NOT unbind dive_ticker. "One dive, one ticker" stays a
    rule with no exceptions to explain in the interface, and a guest whose dive
    genuinely died needs a new key, not a second target.
    """
    _exec("DELETE FROM guest_key_uses WHERE key_id = ? AND module = ?", (key_id, module))


# ── Access requests ───────────────────────────────────────────────────────

_MAX_PENDING = 200
_MAX_REQUESTS = 1000


def create_access_request(email: str, email_norm: str, note: str) -> tuple[str, bool]:
    """Record a request. Returns (outcome, is_new); outcome is 'ok' or 'closed'.

    The caller must map BOTH outcomes onto responses that do not reveal
    anything about a specific address — a new email, a duplicate, an already
    approved one and a previously denied one must be indistinguishable from
    outside, or this becomes an oracle for who has asked.

    A resubmission refreshes the note of a still-pending row and otherwise
    changes nothing. Note that a denied row is therefore inert: see
    reopen_access_request(), which is the operator's way back from a misclick.

    `is_new` is True only when the address had no row at all — never for a
    note-refresh on a pending row, and never for a no-op hit against an
    approved/denied one. The caller uses it to gate the owner's Telegram
    notification: without this, a denied address retried on a timer would
    page the owner's phone forever for a submission that changes nothing.
    """
    with _lock:
        c = _connect()
        pending = c.execute(
            "SELECT COUNT(*) FROM access_requests WHERE status = 'pending'"
        ).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM access_requests").fetchone()[0]
        existing = c.execute(
            "SELECT id FROM access_requests WHERE email_norm = ?", (email_norm,)
        ).fetchone()
        is_new = existing is None

        # Caps apply to genuinely new rows; an existing requester updating their
        # note must not be turned away because the queue is long.
        if is_new and (pending >= _MAX_PENDING or total >= _MAX_REQUESTS):
            return "closed", False

        c.execute(
            """INSERT INTO access_requests (email, email_norm, note, status, created_at)
               VALUES (?,?,?, 'pending', ?)
               ON CONFLICT(email_norm) DO UPDATE SET note = excluded.note
                 WHERE access_requests.status = 'pending'""",
            (email[:254], email_norm, (note or "")[:280], _now()),
        )
        c.commit()
        return "ok", is_new


def list_access_requests(status: str | None = None) -> list[dict]:
    if status:
        rows = _rows("SELECT * FROM access_requests WHERE status = ? ORDER BY created_at DESC",
                     (status,))
    else:
        rows = _rows("SELECT * FROM access_requests ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def get_access_request(req_id: int) -> dict | None:
    rows = _rows("SELECT * FROM access_requests WHERE id = ?", (req_id,))
    return dict(rows[0]) if rows else None


def decide_access_request(req_id: int, status: str) -> bool:
    if status not in ("approved", "denied"):
        raise ValueError(f"unknown decision: {status!r}")
    with _lock:
        c = _connect()
        cur = c.execute("UPDATE access_requests SET status = ?, decided_at = ? WHERE id = ?",
                        (status, _now(), req_id))
        c.commit()
        return cur.rowcount > 0


def reopen_access_request(req_id: int) -> bool:
    """Put a denied request back in the queue.

    Without this a single misclick on DENY blackholes someone permanently and
    silently: their resubmissions match the UNIQUE email_norm, fail the
    pending-only conflict clause, and return the same cheerful response while
    the operator never sees them again.
    """
    with _lock:
        c = _connect()
        cur = c.execute(
            "UPDATE access_requests SET status = 'pending', decided_at = NULL "
            "WHERE id = ? AND status = 'denied'",
            (req_id,),
        )
        c.commit()
        return cur.rowcount > 0


# ── Outlook cache ─────────────────────────────────────────────────────────

def save_outlook(payload: dict) -> None:
    _exec("""INSERT INTO outlook (id, payload, created_at) VALUES (1,?,?)
             ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
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


# ── Helpers ───────────────────────────────────────────────────────────────

def parse_buy_date(s: str | None) -> date | None:
    """Accept YYYY-MM-DD only. Returns None rather than raising."""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None
