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
    sector      TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
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


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Built in a local variable and only published to the module global
        # on full success — if any step below raises, _conn stays None and
        # the next call retries initialisation from scratch, instead of
        # every future caller silently reusing a half-set-up connection.
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
        _conn = conn
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table's CREATE TABLE IF NOT EXISTS ran.

    IF NOT EXISTS only creates a table on a fresh database — it is a no-op
    against an existing file, so a new column needs an explicit ALTER TABLE
    here or it silently never appears on any database that predates it.

    The ALTER is wrapped because _connect() has no lock of its own; two
    threads can both observe _conn is None and both reach this line before
    either commits. Whichever loses that race hits "duplicate column name"
    — which only ever means the other thread just added it — so it's caught
    and ignored rather than left to fail the request. Only the thread that
    actually adds the column runs the backfill below, so it never runs
    twice.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(watchlist)")}
    if "sector" not in cols:
        try:
            conn.execute("ALTER TABLE watchlist ADD COLUMN sector TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            return

        # bootstrap() only seeds sector on first run and never re-runs, so a
        # watchlist row that existed before this column did would otherwise
        # be stuck at '' forever. Backfill from config for anything that
        # matches and is still blank; anything already set (or not in
        # config) is left alone.
        for ticker, cfg in WATCHLIST.items():
            sector = cfg.get("sector") or ""
            if sector:
                conn.execute(
                    "UPDATE watchlist SET sector = ? WHERE ticker = ? AND sector = ''",
                    (sector, ticker),
                )


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
               (ticker, note, tranches, brent_gated, exchange, sector, added_at)
               VALUES (?,?,?,?,?,?,?)""",
            (t, cfg.get("thesis") or "", json.dumps(cfg.get("tranches") or []),
             1 if cfg.get("brent_gated") else 0, cfg.get("exchange") or "",
             cfg.get("sector") or "", _now()),
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
            "sector": r["sector"] or None,
        }
    return out


def add_watch(ticker: str, note: str = "", sector: str = "") -> str:
    t = validate_ticker(ticker)
    _exec(
        """INSERT INTO watchlist (ticker, note, tranches, brent_gated, exchange, sector, added_at)
           VALUES (?,?,'[]',0,'',?,?)
           ON CONFLICT(ticker) DO UPDATE SET note=excluded.note""",
        (t, (note or "")[:500], (sector or "")[:80], _now()),
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


def create_session() -> tuple[str, datetime]:
    """Return (raw_token, expires_at). Only the hash is persisted."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    _exec("INSERT INTO sessions (token_hash, created_at, expires_at) VALUES (?,?,?)",
          (_hash_token(token), _now(), expires.isoformat()))
    return token, expires


def session_valid(token: str) -> bool:
    if not token:
        return False
    rows = _rows("SELECT expires_at FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    if not rows:
        return False
    try:
        if datetime.fromisoformat(rows[0]["expires_at"]) < datetime.now(timezone.utc):
            destroy_session(token)
            return False
    except ValueError:
        return False
    return True


def destroy_session(token: str) -> None:
    if token:
        _exec("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def purge_expired_sessions() -> None:
    _exec("DELETE FROM sessions WHERE expires_at < ?", (_now(),))


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
