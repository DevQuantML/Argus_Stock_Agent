#!/usr/bin/env python
"""
verify_consistency.py — one quantity must have one value.

Spends nothing. No network, no provider calls: it drives the computations
directly and uses a throwaway SQLite file for the store.

Exists because a screenshot caught what no test did. The terminal showed MSFT
at "+64.10%" in the watchlist panel and "+64.14%" in the holdings table, on the
same screen, for the same position at the same price. Both numbers came from
real code; they disagreed because the quantity was computed twice —
tools/stock_data.py rounded to 1dp, api.py's analytics routes to 2dp, and the
frontend renders both to two places.

Underneath that was the worse defect. stock_data.py read config.MY_PORTFOLIO,
which is SEED data that bootstrap() copies into SQLite once and never consults
again, while every other reader used store.positions(). So a holding sold
through the UI still reported a live P&L, and a corrected avg_cost still showed
the original. The two panels were not rounding the same number differently —
they were reading different books.

Run:  python scripts/verify_consistency.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the store at a scratch file before anything imports it, so this never
# touches the operator's real argus.db.
_tmp = tempfile.mkdtemp(prefix="argus-consistency-")
os.environ["ARGUS_DB"] = str(Path(_tmp) / "consistency.db")

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def main():
    import config
    import store
    from tools import stock_data

    store.bootstrap()

    # ── 1. The live book, not the seed ────────────────────────────────────
    book, _watch = stock_data._live_book()
    check("_live_book returns the seeded positions",
          sorted(book), sorted(config.MY_PORTFOLIO))

    # Diverge the two deliberately: sell something. config still lists it.
    sold = sorted(config.MY_PORTFOLIO)[0]
    store.delete_position(sold)
    book, _watch = stock_data._live_book()

    check("a position sold in the UI disappears from the live book",
          sold in book, False)
    check("...while config still lists it, which is why reading config was wrong",
          sold in config.MY_PORTFOLIO, True)

    # Change a cost basis; the seed keeps the old one.
    kept = sorted(book)[0]
    store.upsert_position(kept, {"avg_cost": 999.99})
    book, _watch = stock_data._live_book()
    check("an edited avg_cost is read from the book, not the seed",
          book[kept]["avg_cost"], 999.99)
    check("...and the seed still holds the original, unchanged",
          config.MY_PORTFOLIO[kept]["avg_cost"] != 999.99, True)

    # ── 2. One rounding, not two ──────────────────────────────────────────
    # The exact case from the screenshot: MSFT, 5 @ 300, trading at 492.43.
    price, avg = 492.43, 300.0
    truth = ((price - avg) / avg) * 100          # 64.14333...

    # api.py's analytics routes, verbatim.
    api_value = round(truth, 2)
    # What stock_data.py must now produce for the same inputs.
    sd_value = round(((price - avg) / avg) * 100, 2)

    check("the two P&L computations agree to the digit", sd_value, api_value)
    check("and that value is the one the screenshot should have shown",
          sd_value, 64.14)
    check("the old 1dp rounding is what produced the mismatch",
          round(truth, 1) != api_value, True)

    # Source-level, and deliberately narrow. The defect was never "a percentage
    # rounded to 1dp" — it was a percentage rounded to 1dp *here* and 2dp in
    # api.py. pct_from_52w_high is also 1dp and is fine, because stock_data.py
    # is its only producer; everything downstream passes it through. Guarding
    # every 1dp rounding would fail on that and teach the next reader to
    # silence the harness. Guard only the quantities with two producers.
    src = (ROOT / "tools" / "stock_data.py").read_text(encoding="utf-8")
    for name, expr in [
        ("unrealized P&L %", "current_pnl = round(((price - avg_cost) / avg_cost) * 100, 2)"),
        ("distance to stop", "distance_to_stop = round(((price - stop_loss) / price) * 100, 2)"),
        ("distance to trim", "distance_to_trim = round(((trim_at - price) / price) * 100, 2)"),
    ]:
        check(f"{name} is computed at 2dp, matching api.py", expr in src, True)

    check("stock_data.py no longer reads the config seed for position context",
          "if ticker in MY_PORTFOLIO:" in src, False)

    # ── 3. One asset version, not several ─────────────────────────────────
    # Same defect class as the P&L mismatch above, in the module graph rather
    # than in arithmetic: ES modules are keyed by full URL, so api.js?v=15 and
    # api.js?v=16 are two unrelated module instances with two unrelated copies
    # of `auth`. views.js sat at ?v=15 while app.js/ui.js/index.html were at
    # ?v=16, which meant SIGN OUT in the profile view cleared one auth cache
    # while app.js kept reading the other and still believed the session was
    # live. The accent swatches had the matching bug: prefs.set() fired the
    # listener array of an instance the chart had never subscribed to, so the
    # SVG kept the old colour.
    #
    # Guard the invariant, not the number: every ?v= in the tree must agree.
    # This stays true across future bumps without editing the harness.
    versions: dict[str, list[str]] = {}
    for path in sorted((ROOT / "static").rglob("*")):
        if path.suffix not in {".js", ".html"} or not path.is_file():
            continue
        for v in re.findall(r"\?v=(\d+)", path.read_text(encoding="utf-8")):
            versions.setdefault(v, []).append(path.relative_to(ROOT).as_posix())

    check(f"every ?v= asset version in static/ agrees (found: {sorted(versions) or 'none'})",
          len(versions), 1)
    if len(versions) > 1:
        for v, files in sorted(versions.items()):
            print(f"          v={v}: {', '.join(sorted(set(files)))}")


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
