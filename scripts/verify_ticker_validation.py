#!/usr/bin/env python
"""
Regression guard for share-class ticker symbols (BRK-B, BF-B, BF-A).

Background
----------
`validate_ticker` once used ^[A-Z0-9.]{1,12}$ -- no hyphen -- so every Yahoo
share-class symbol was rejected with HTTP 400 before yfinance was ever reached.
The v2 frontend had the mirror defect: its client-side sanitiser stripped the
hyphen, turning "BRK-B" into "BRKB", which yfinance answers with HTTP 200 and
all-null fields rather than an error (see the gotcha in CLAUDE.md).

Both halves are guarded here: the backend validator, and the character classes
in both frontends.

Cost
----
Free. No AI provider is contacted -- this exercises only the validator, the
yfinance-backed tool functions, and the unauthenticated routes. It never
touches /api/research/*.

Usage
-----
    python scripts/verify_ticker_validation.py
    python scripts/verify_ticker_validation.py --offline   # skip network checks

Exit codes
----------
    0   every check passed
    1   a check failed -- a real regression
    2   Yahoo was unreachable, so the live phase never ran (never a pass)
"""

import argparse
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_failures: list[str] = []


class YahooUnreachable(Exception):
    """
    Raised when the control symbol fails, i.e. the feed is down.

    A network outage and a rejected ticker are different outcomes and must not
    share an exit code -- otherwise a red run gets waved through as "Yahoo was
    flaky" and a real validation regression rides along behind it.
    """


def check(label: str, ok: bool, detail: str = "") -> None:
    """Record one assertion and print it in a stable column layout."""
    print(f"  [{'ok' if ok else 'FAIL'}] {label:<44} {detail}".rstrip())
    if not ok:
        _failures.append(label)


# -- 1. The validator itself -- offline, deterministic ----------------------

def verify_validator() -> None:
    from tools.validator import validate_ticker

    print("\nvalidate_ticker -- must accept")
    # BRK-B/BF-B/BF-A are the reported regression. BZ=F guards the futures
    # branch, TATAELXSI.NS the dot branch -- both predate the hyphen fix.
    for symbol in ["BRK-B", "BF-B", "BF-A", "RDS-A", "PLTR", "GE",
                   "BZ=F", "CL=F", "TATAELXSI.NS"]:
        try:
            got = validate_ticker(symbol)
            check(symbol, got == symbol, f"-> {got!r}")
        except ValueError as exc:
            check(symbol, False, str(exc))

    # Normalisation is part of the contract, not a convenience: a caller that
    # passes what a user typed must get the canonical symbol back, or the
    # hyphen fix only works for input that was already perfect.
    print("\nvalidate_ticker -- must normalise")
    for raw, want in [("brk-b", "BRK-B"), ("  BRK-B  ", "BRK-B"),
                      ("bf-a", "BF-A"), (" pltr", "PLTR")]:
        try:
            got = validate_ticker(raw)
            check(repr(raw), got == want, f"-> {got!r} (want {want!r})")
        except ValueError as exc:
            check(repr(raw), False, str(exc))

    print("\nvalidate_ticker -- must reject (the hyphen widened nothing else)")
    # Widened from the two independent versions of this guard that were written
    # in parallel: whitespace of every kind, the full shell metacharacter set,
    # both path separators, command substitution, and the caret index prefix
    # that was never accepted. One class per line, so a gap is visible.
    for symbol in ["../../etc/passwd", "a/b", "a\\b",              # path
                   "PLTR;rm -rf", "PL`TR", "PLTR&X", "PLTR|X",     # shell
                   "PLTR$X", "PLTR'X", 'PLTR"X', "$(whoami)",
                   "AB CD", "BRK\tB", "BRK\nB",                    # whitespace
                   "<script>", "^GSPC",                            # markup, index
                   "", "A" * 13, "BZ=FF", "A=B=C"]:                # shape
        try:
            got = validate_ticker(symbol)
            check(repr(symbol[:18]), False, f"LEAKED -> {got!r}")
        except ValueError:
            check(repr(symbol[:18]), True)


# -- 2. Frontend character classes -- offline, static read -----------------

def verify_frontend_regexes() -> None:
    """
    The main frontend must let '-' and '=' survive client-side sanitising.

    A stripped hyphen is the silent failure mode: the request still succeeds,
    so it surfaces as an empty panel rather than an error.

    static/v2/ was removed from the tree (the files stored AGENT_SECRET in
    localStorage and loaded external CDN scripts). The character-class regex
    guarded in the check below covers the same BRK-B / BF-B defect the v2
    sanitiser check used to assert.
    """
    print("\nfrontend sanitisers -- '-' and '=' must survive")
    # Matches the character class of any .replace(/[^...]/g, '') sanitiser.
    pattern = re.compile(r"replace\(/\[\^([A-Z0-9.=\\-]*)\]/g")

    for rel in ["static/js/app.js"]:
        path = _ROOT / rel
        if not path.exists():
            check(rel, False, "file not found")
            continue

        classes = pattern.findall(path.read_text(encoding="utf-8"))
        if not classes:
            check(rel, False, "no ticker sanitiser found -- did it move?")
            continue

        bad = [c for c in classes if "-" not in c or "=" not in c]
        check(
            rel,
            not bad,
            f"{len(classes)} sanitiser(s) ok" if not bad
            else f"{len(bad)} of {len(classes)} drop '-' or '=': {bad}",
        )


# -- 3. The four tool functions -- network (yfinance), no AI ---------------

def verify_tools(symbol: str = "BRK-B") -> None:
    from tools.fundamentals import get_statements
    from tools.quant import get_quant_metrics
    from tools.stock_data import get_price_and_fundamentals, get_price_history

    print(f"\ntool functions -- {symbol} (yfinance, no AI spend)")

    # Control first. PLTR has no hyphen, so it was resolving even before this
    # fix; if it fails now, the feed is down and nothing below is a verdict on
    # the validator.
    control = get_price_and_fundamentals("PLTR")
    if control.get("price") is None:
        raise YahooUnreachable(control.get("error") or "control symbol returned no price")

    fundamentals = get_price_and_fundamentals(symbol)
    check(
        "get_price_and_fundamentals",
        fundamentals.get("price") is not None,
        f"{fundamentals.get('name')} @ {fundamentals.get('price')}"
        if fundamentals.get("price") is not None
        else f"error={fundamentals.get('error')}",
    )

    history = get_price_history(symbol, period="6mo")
    points = history.get("points") or []
    check("get_price_history", bool(points),
          f"{len(points)} points" if points else f"error={history.get('error')}")

    quant = get_quant_metrics(symbol)
    check("get_quant_metrics", quant.get("error") is None,
          f"dcf={quant.get('dcf_intrinsic_value')} roic={quant.get('roic')}")

    statements = get_statements(symbol)
    check("get_statements", statements.get("error") is None,
          f"keys={[k for k in statements if k != 'error'][:4]}")


# -- 4. Unauthenticated routes -- in-process, no server needed -------------

def verify_routes() -> None:
    """
    Drive the real app object through TestClient so no uvicorn instance and no
    free port are required. Only unauthenticated routes are touched.
    """
    from fastapi.testclient import TestClient

    import api

    print("\nfree routes -- in-process TestClient")
    client = TestClient(api.app)

    for path in ["/api/demo/BRK-B", "/api/demo/BF-B",
                 "/api/history/BRK-B?period=6mo", "/api/demo/PLTR"]:
        response = client.get(path)
        body = response.json()
        check(path, response.status_code == 200,
              f"{response.status_code} {body.get('error', '')}".strip())

    # /v2 matches no route (returns 404). It still passes through the
    # add_security_headers middleware and receives the API's restrictive CSP,
    # because only "/" and "/static/*" match is_page. This asserts the catch-all
    # guarantee: any unrecognized path is locked down, regardless of content.
    # static/v2/ was removed from the tree; this check now documents the
    # middleware behaviour rather than a specific page.
    csp = client.get("/v2").headers.get("content-security-policy", "")
    check("/v2 CSP (catch-all: unrecognised paths get default-src 'none')",
          csp.startswith("default-src 'none'"), csp[:34])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true",
                        help="skip the yfinance and route checks")
    args = parser.parse_args()

    verify_validator()
    verify_frontend_regexes()

    if args.offline:
        print("\n(skipping network checks -- --offline)")
    else:
        try:
            verify_tools()
            verify_routes()
        except YahooUnreachable as exc:
            print(f"\n  [--] control symbol PLTR failed: {exc}")
            print("\nINCONCLUSIVE -- Yahoo unreachable, the live phase did not run.")
            print("The offline checks above still stand; re-run when the feed is back.")
            # Exit 2, never 0: an unreachable feed must not read as a pass.
            return 1 if _failures else 2

    print()
    if _failures:
        print(f"FAILED -- {len(_failures)} check(s): {', '.join(_failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
