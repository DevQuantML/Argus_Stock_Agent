#!/usr/bin/env python
"""
verify_postgres_store.py — store_postgres.py against a REAL local Postgres.

Not free-as-in-zero-setup like every other harness in this repo — it needs
Docker (or any reachable Postgres) and is flagged as such deliberately,
matching how verify_quant.py is already the one harness that hits live
yfinance. Costs no money either way; "not free" here means "needs a service
running," not "spends."

Run against a scratch Postgres:
    docker run --rm -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16
    python scripts/verify_postgres_store.py

Or point DATABASE_URL at any reachable Postgres 13+ before running — CI does
this via a GitHub Actions `postgres:16` service container (see
.github/workflows/verify.yml). If DATABASE_URL is not set and localhost:5432
is not reachable, this exits 0 with a single SKIPPED line rather than
failing the whole suite for lack of a local Postgres — it does not silently
report the backend as verified either way.

What this mirrors from verify_access.py, translated to the Postgres backend
directly (imports store_postgres, never store — see api.py's sys.modules
seam for why the two are never mixed in one running process):
  - sessions: create/resolve/expire/destroy, guest tier tied to its key
  - guest budget atomicity: concurrent claims of the same stage race safely
    (the SAVEPOINT-wrapped INSERT in consume_guest_unit is the thing this
    proves — a bug here would show up as either a double-charge or a wedged
    transaction, not a clean 500)
  - research-run retention cap and (tier, guest_key_id) scoping, including
    the IS NOT DISTINCT FROM NULL-safety for owner rows
  - access-request anti-enumeration (new vs resubmission vs closed)

`store.py` (SQLite) needs none of this re-tested here — it already has its
own harness and is untouched by this migration.

Exit: 0 all passed (or skipped), 1 any failed.
"""
import os
import secrets
import socket
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def section(title):
    print(f"\n{title}")


def _postgres_reachable(url: str) -> bool:
    """A cheap TCP probe, not a real connection attempt — good enough to
    decide 'skip' vs 'try and let a real failure show a real error'."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host, port = p.hostname or "localhost", p.port or 5432
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


_DEFAULT_LOCAL = "postgresql://postgres:test@localhost:5432/postgres"
_DB_URL = os.getenv("DATABASE_URL") or _DEFAULT_LOCAL

if not _postgres_reachable(_DB_URL):
    print(f"SKIPPED — no Postgres reachable at {_DB_URL!r}.")
    print("Run: docker run --rm -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16")
    print("...then re-run this script, or set DATABASE_URL yourself.")
    sys.exit(0)

# A dedicated schema-per-run would be cleaner isolation, but this backend has
# no schema/search_path concept exposed anywhere else in the app — matching
# store_postgres.py's own "don't invent structure the app doesn't already
# have" bias. Each table is dropped and recreated instead, same effect.
os.environ["DATABASE_URL"] = _DB_URL

import store_postgres as store  # noqa: E402  (see ROOT insert above)


def _reset_schema():
    """Drop every table this app owns and let bootstrap()/_connect() recreate
    them clean — the Postgres equivalent of verify_access.py's scratch
    SQLite file, since there is no throwaway-file option here."""
    store._conn = None
    conn = store._connect()
    with conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS research_runs, guest_key_uses, guest_keys, "
            "sessions, access_requests, watchlist, positions, profile, "
            "outlook, meta CASCADE"
        )
    conn.commit()
    store._conn = None
    store._connect()  # recreate the schema from _SCHEMA


def main():
    _reset_schema()

    # ── Bootstrap ────────────────────────────────────────────────────────
    section("bootstrap — seeds once, ON CONFLICT DO NOTHING on a re-run")
    store.bootstrap()
    n_positions_first = len(store.positions())
    store.bootstrap()  # must be a no-op the second time
    check("bootstrap is idempotent (position count unchanged on re-run)",
          len(store.positions()), n_positions_first)

    # ── Sessions ─────────────────────────────────────────────────────────
    section("sessions — create, resolve, expire, destroy")
    token, expires = store.create_session("owner")
    info = store.session_info(token)
    check("a fresh owner session resolves", info is not None, True)
    check("...with tier owner", info["tier"] if info else None, "owner")

    store.destroy_session(token)
    check("a destroyed session no longer resolves", store.session_info(token), None)

    past = datetime.now(timezone.utc) - timedelta(days=1)
    expired_token, _ = store.create_session("owner", expires_at=past)
    check("an already-expired session resolves to None",
          store.session_info(expired_token), None)

    # ── Guest keys + budget atomicity ───────────────────────────────────
    section("guest keys — mint, redeem, one dive one ticker")
    key_id, raw_key, gexp = store.create_guest_key("pg-harness")
    looked_up = store.guest_key_for(raw_key)
    check("a minted key looks itself up", looked_up["id"] if looked_up else None, key_id)
    check("a fresh key is not dead", looked_up["dead"] if looked_up else "?", None)

    outcome = store.consume_guest_unit(key_id, "PLTR", "report")
    check("first claim of a stage succeeds", outcome, "ok")
    outcome2 = store.consume_guest_unit(key_id, "PLTR", "report")
    check("re-claiming the SAME stage is refused", outcome2, "used")
    outcome3 = store.consume_guest_unit(key_id, "AAPL", "context")
    check("a second ticker on the same key is refused (one dive, one ticker)",
          outcome3, "other_ticker")

    section("guest budget — concurrent claims of one stage race safely")
    key_id2, raw2, _ = store.create_guest_key("pg-race")
    results = []
    lock = threading.Lock()

    def _claim():
        r = store.consume_guest_unit(key_id2, "MSFT", "policy")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("exactly one of 8 concurrent claims wins", results.count("ok"), 1)
    check("the other 7 are refused as already used", results.count("used"), 7)
    usage = store.guest_usage(key_id2)
    check("guest_usage reflects exactly one consumed stage", usage["modules_used"], 1)

    revoked = store.revoke_guest_key(key_id2)
    check("revoking a live key succeeds", revoked, True)
    check("a revoked key's claim is refused",
          store.consume_guest_unit(key_id2, "MSFT", "patterns"), "revoked")

    # ── Research history — retention cap + NULL-safe scoping ───────────
    section("research history — retention cap and (tier, guest_key_id) scoping")
    for i in range(store.RESEARCH_HISTORY_LIMIT + 5):
        store.save_research_run("NVDA", "synthesis", "owner", None, {"i": i})
    owner_runs = store.list_research_runs("NVDA", "owner", None, limit=100)
    check("owner history is capped at RESEARCH_HISTORY_LIMIT",
          len(owner_runs), store.RESEARCH_HISTORY_LIMIT)
    check("the newest run survives the prune",
          owner_runs[0]["payload"]["i"], store.RESEARCH_HISTORY_LIMIT + 4)

    store.save_research_run("NVDA", "synthesis", "guest", key_id, {"i": "guest-run"})
    check("a guest's saved run does not appear in the owner's history "
          "(guest_key_id IS NOT DISTINCT FROM NULL correctly excludes it)",
          any(r["payload"].get("i") == "guest-run" for r in
              store.list_research_runs("NVDA", "owner", None, limit=100)),
          False)
    guest_runs = store.list_research_runs("NVDA", "guest", key_id, limit=100)
    check("...but it appears in that guest's own scoped history",
          any(r["payload"].get("i") == "guest-run" for r in guest_runs), True)

    # ── Access requests — anti-enumeration ──────────────────────────────
    section("access requests — new vs resubmission vs decided-is-inert")
    outcome, is_new = store.create_access_request("a@example.com", "a@example.com", "hi")
    check("a fresh address is new", (outcome, is_new), ("ok", True))
    outcome, is_new = store.create_access_request("a@example.com", "a@example.com", "hi again")
    check("a resubmission of a pending address is not new", (outcome, is_new), ("ok", False))

    req = store.list_access_requests("pending")[0]
    store.decide_access_request(req["id"], "denied")
    outcome, is_new = store.create_access_request("a@example.com", "a@example.com", "please?")
    check("resubmitting a DENIED address changes nothing and is not new",
          (outcome, is_new), ("ok", False))
    check("...and the row is still denied, not reopened by the resubmission",
          store.get_access_request(req["id"])["status"], "denied")

    reopened = store.reopen_access_request(req["id"])
    check("the operator can explicitly reopen a denied request", reopened, True)
    check("...which puts it back in pending",
          store.get_access_request(req["id"])["status"], "pending")


try:
    main()
finally:
    # Leave the scratch schema in place for inspection on failure; drop it
    # on a clean pass so repeated local runs start from the same state.
    if not FAIL:
        try:
            conn = store._connect()
            with conn.cursor() as cur:
                cur.execute(
                    "DROP TABLE IF EXISTS research_runs, guest_key_uses, guest_keys, "
                    "sessions, access_requests, watchlist, positions, profile, "
                    "outlook, meta CASCADE"
                )
            conn.commit()
        except Exception:
            pass

for n, got, want in PASS:
    print(f"  PASS  {n}")
for n, got, want in FAIL:
    print(f"  FAIL  {n}\n          got={got!r} want={want!r}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
