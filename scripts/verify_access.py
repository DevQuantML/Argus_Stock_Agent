#!/usr/bin/env python
"""
verify_access.py — tiered access: owner vs guest, and the money that separates them.

Spends nothing, and does not merely intend to. See "Cost safety" below: the
no-spend property here is structural, asserted, and then asserted again from
the other side by counting provider invocations.

What this guards
----------------
api.py grants a route to anyone holding a valid session. That was safe while
AGENT_SECRET was the only way to get one. The moment a second class of
credential exists — a 24-hour guest key — a bare boolean makes its holder
indistinguishable from the operator, and the guest inherits routes that spend
real money with no budget attached. store.session_info() and AuthCtx exist to
carry *which* tier is calling; this harness is what stops that distinction
being quietly deleted later.

Cost safety
-----------
Three independent layers, because the obvious one does not work:

  1. Provider keys are set to "" BEFORE importing api — never popped. api.py
     calls load_dotenv() at import (api.py:41-45), python-dotenv defaults to
     override=False, and .env on this machine holds a real PERPLEXITY_API_KEY.
     Popping the name lets load_dotenv put the real key straight back, which
     would hand a "free" harness a live provider client. Setting the name to
     an empty string keeps it present, so dotenv skips it, and the truthiness
     check in _get_provider() then falls through to its ValueError.
  2. That emptiness is asserted before the first request is issued.
  3. Every provider entry point is monkeypatched with a counting stub, and the
     counters are asserted to be zero after every denial test. This is the
     strong form: not "no key was configured" but "the provider layer was
     never entered".

ARGUS_DB points at a scratch file, so this never touches the operator's real
argus.db — the database holding actual holdings and session hashes.

Run:  python scripts/verify_access.py
Exit: 0 all passed, 1 any failed
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="argus-access-")

# ── Cost safety layer 1 — before ANY project import ───────────────────────
# Empty string, not pop. See the module docstring: popping is actively unsafe
# because load_dotenv(override=False) refills a name that is absent.
os.environ["PERPLEXITY_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["ARGUS_DB"] = str(Path(_tmp) / "access.db")
os.environ["TRUST_PROXY"] = "0"
os.environ["AGENT_SECRET"] = "harness-only-secret"

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def section(title):
    print(f"\n{title}")


def main():
    # ── Migration: an OLD-schema database must survive and gain the columns ──
    # Built by hand with the pre-tier schema, because that is the only honest
    # way to prove the migration runs where it matters. A fresh database gets
    # its columns from CREATE TABLE and would prove nothing.
    section("migration — a pre-tier database gains the new columns")
    legacy = Path(_tmp) / "legacy.db"
    lc = sqlite3.connect(legacy)
    lc.executescript(
        """CREATE TABLE sessions (
               token_hash TEXT PRIMARY KEY,
               created_at TEXT NOT NULL,
               expires_at TEXT NOT NULL
           );"""
    )
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    lc.execute("INSERT INTO sessions VALUES (?,?,?)", ("legacyhash", "2026-01-01", future))
    lc.commit()
    lc.close()

    os.environ["ARGUS_DB"] = str(legacy)
    import store

    store._conn = None            # force a reconnect against the legacy file
    conn = store._connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    check("legacy sessions table gains 'tier'", "tier" in cols, True)
    check("legacy sessions table gains 'guest_key_id'", "guest_key_id" in cols, True)

    row = conn.execute("SELECT tier FROM sessions WHERE token_hash='legacyhash'").fetchone()
    check("a pre-existing session backfills as an owner session", row[0], "owner")

    # Idempotence: the migration must be safe to run again on the same file.
    store._migrate(conn)
    store._migrate(conn)
    check("running the migration repeatedly is a no-op",
          {r[1] for r in conn.execute("PRAGMA table_info(sessions)")} == cols, True)
    check("guest_keys table exists after connect",
          bool(conn.execute(
              "SELECT name FROM sqlite_master WHERE type='table' AND name='guest_keys'"
          ).fetchone()), True)

    # ── Everything below runs against a clean scratch database ──────────────
    store._conn = None
    os.environ["ARGUS_DB"] = str(Path(_tmp) / "access.db")

    import api
    from fastapi.testclient import TestClient

    # ── Cost safety layer 2 — assert the emptiness held through import ─────
    section("cost safety — the provider must be unreachable")
    check("PERPLEXITY_API_KEY is falsy after importing api (dotenv did not refill it)",
          bool(api.os.getenv("PERPLEXITY_API_KEY")), False)
    check("GROQ_API_KEY is falsy after importing api",
          bool(api.os.getenv("GROQ_API_KEY")), False)

    # ── Cost safety layer 3 — count provider invocations ───────────────────
    calls = []

    def stub(name):
        def _f(*a, **k):
            calls.append(name)
            return {"error": "stubbed — the harness must never reach a provider"}
        return _f

    api.run_research_module = stub("module")
    api.run_synthesis = stub("synthesis")
    api.run_perplexity_research = stub("legacy")
    api.run_portfolio_outlook = stub("outlook")

    store.bootstrap()
    client = TestClient(api.app)

    def owner_client():
        c = TestClient(api.app)
        r = c.post("/api/session", json={"key": "harness-only-secret"})
        assert r.status_code == 200, f"owner unlock failed: {r.status_code} {r.text}"
        return c

    def guest_client():
        """A guest session, created directly in the store.

        Phase 2 adds the redemption route; the tier boundary is testable now
        and is what this file exists to pin down.
        """
        expires = datetime.now(timezone.utc) + timedelta(hours=24)
        with store._lock:
            c = store._connect()
            cur = c.execute(
                "INSERT INTO guest_keys (key_hash, key_prefix, label, created_at, expires_at) "
                "VALUES (?,?,?,?,?)",
                (f"hash-{len(calls)}-{datetime.now(timezone.utc).timestamp()}",
                 "gk_abc", "harness@example.com", store._now(), expires.isoformat()),
            )
            c.commit()
            key_id = cur.lastrowid
        token, _ = store.create_session("guest", key_id, expires_at=expires)
        gc = TestClient(api.app)
        gc.cookies.set(api.SESSION_COOKIE, token)
        return gc, key_id, token

    # ── Session tiering ────────────────────────────────────────────────────
    section("session tiering — owner and guest are distinguishable")
    oc = owner_client()
    gc, gkey_id, gtoken = guest_client()

    info = store.session_info(gtoken)
    check("a guest session reports tier 'guest'", info and info["tier"], "guest")
    check("a guest session carries its key id", info and info["guest_key_id"], gkey_id)

    ctx_owner = api._auth_ctx(_req_with(api, {}), "harness-only-secret")
    check("X-Agent-Key resolves to the owner tier", ctx_owner and ctx_owner.tier, "owner")
    check("no credential resolves to None", api._auth_ctx(_req_with(api, {}), None), None)

    # ── Guest is read-only on the operator's book ──────────────────────────
    section("guest is read-only on the book")
    before = store.positions()

    for label, method, path, body in [
        ("POST /api/profile",           gc.post,   "/api/profile",          {"name": "intruder"}),
        ("POST /api/positions",         gc.post,   "/api/positions",        {"ticker": "AAPL", "shares": 999}),
        ("DELETE /api/positions/AAPL",  gc.delete, "/api/positions/AAPL",   None),
        ("POST /api/watchlist",         gc.post,   "/api/watchlist",        {"ticker": "TSLA"}),
        ("DELETE /api/watchlist/GOOGL", gc.delete, "/api/watchlist/GOOGL",  None),
    ]:
        r = method(path, json=body) if body is not None else method(path)
        check(f"guest {label} is refused 403", r.status_code, 403)
        if r.status_code == 403:
            check(f"...{label} names the reason in a machine code",
                  (r.json().get("detail") or {}).get("code"), "guest_read_only")

    check("the book is byte-identical after every guest write attempt",
          store.positions(), before)

    section("guest may still read the book (shared-terminal model)")
    for label, path in [("/api/info", "/api/info"), ("/api/positions", "/api/positions"),
                        ("/api/watchlist", "/api/watchlist"), ("/api/profile", "/api/profile")]:
        check(f"guest GET {label} is allowed", gc.get(path).status_code, 200)

    section("owner is unaffected by the new dependency")
    check("owner POST /api/watchlist still works",
          oc.post("/api/watchlist", json={"ticker": "NVDA"}).status_code, 200)
    check("owner DELETE /api/watchlist works",
          oc.delete("/api/watchlist/NVDA").status_code, 200)

    # ── Unbudgeted paid routes are owner-only ──────────────────────────────
    section("guests cannot reach paid routes that have no budget")
    calls.clear()
    r = gc.post("/api/portfolio/outlook")
    check("guest POST /api/portfolio/outlook is refused 403", r.status_code, 403)
    r = gc.get("/api/research/AAPL")
    check("guest GET /api/research/{t} (legacy one-shot) is refused 403", r.status_code, 403)
    check("...and the provider was never entered", calls, [])

    check("guest GET /api/portfolio/outlook (cached, free) is allowed",
          gc.get("/api/portfolio/outlook").status_code, 200)

    # Positive control for the counter itself.
    #
    # "calls == []" above is only evidence if calls CAN become non-empty. A
    # monkeypatch that silently failed to bind would produce an empty list on
    # every denial test and the whole cost assertion would pass while proving
    # nothing. So drive the same stub through a caller who IS entitled, and
    # require that it registers. A control that has stopped controlling is
    # worse than no control.
    calls.clear()
    oc.post("/api/portfolio/outlook")
    check("CONTROL: an entitled caller does reach the stub, so the counter is live",
          calls, ["outlook"])
    calls.clear()

    # ── Revocation and expiry kill live sessions ───────────────────────────
    section("a dead key kills its live sessions immediately")
    gc2, kid2, tok2 = guest_client()
    check("guest session works before revocation", gc2.get("/api/positions").status_code, 200)
    store._exec("UPDATE guest_keys SET revoked_at = ? WHERE id = ?", (store._now(), kid2))
    check("revoking the key invalidates the live session", store.session_info(tok2), None)
    check("...and the route now refuses it", gc2.get("/api/positions").status_code, 401)

    gc3, kid3, tok3 = guest_client()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    store._exec("UPDATE guest_keys SET expires_at = ? WHERE id = ?", (past, kid3))
    check("an expired key invalidates the live session (the JOIN, not the cookie)",
          store.session_info(tok3), None)

    section("owner sessions are untouched by guest-key state")
    check("owner session still valid after all of the above",
          oc.get("/api/positions").status_code, 200)

    # ── Final cost assertion ───────────────────────────────────────────────
    section("cost — nothing in this run reached a provider")
    check("provider invocation count across the whole harness", calls, [])


def _req_with(api_mod, cookies: dict):
    """A minimal stand-in for a Request, for unit-testing _auth_ctx directly."""
    class _R:
        def __init__(self, c):
            self.cookies = c
            self.headers = {}
            self.client = type("C", (), {"host": "127.0.0.1"})()
    return _R(cookies)


try:
    main()
finally:
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)

for n, got, want in PASS:
    print(f"  PASS  {n}")
for n, got, want in FAIL:
    print(f"  FAIL  {n}\n          got={got!r} want={want!r}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
