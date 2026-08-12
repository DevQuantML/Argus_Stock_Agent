#!/usr/bin/env python
"""
verify_quant.py — free-path verification of the statement-based quant engine.

Lives in the repo on purpose. The previous incarnation of this harness was
written into a session scratchpad, that scratchpad was cleaned up, and the
"all checks pass" claim in the handoff became unverifiable a session later.
A verification you cannot re-run is a rumour.

Spends nothing. Reads yfinance statements and prices only — no Perplexity,
no Groq, no /api/research path.

    python scripts/verify_quant.py            # portfolio + watchlist
    python scripts/verify_quant.py PLTR MELI  # only these
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The default Windows console codepage is cp1252, which has no glyph for much
# of the box-drawing and arrow punctuation used below — printing one raises
# UnicodeEncodeError and kills the run mid-report. Force UTF-8 and degrade to
# a replacement character rather than an exception.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):        # non-reconfigurable stream
        pass

from config import MY_PORTFOLIO, WATCHLIST          # noqa: E402
from tools.quant import get_quant_metrics           # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str, str]] = []


def check(name: str, ok: bool | None, why: str = "", info: str = "") -> None:
    """ok=None means the precondition was absent, which is not a failure.

    `why` is shown only when the assertion fails — it explains the failure and
    would be actively misleading printed beside a pass. `info` is the observed
    value and is shown either way.
    """
    status = SKIP if ok is None else (PASS if ok else FAIL)
    results.append((status, name, why, info))


def finite(x) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


# ── Static assertion: the bookValue trap must not come back ────────────────
def check_bookvalue_trap() -> None:
    src = (Path(__file__).resolve().parent.parent / "tools" / "quant.py").read_text(encoding="utf-8")
    # Strip comments — the fix is *documented* in a comment, which must not
    # itself trip the guard.
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    hits = re.findall(r"bookValue", code)
    check(
        "quant.py never reads info['bookValue'] (per-share; unit mismatch as equity)",
        not hits,
        why=f"found {len(hits)} live reference(s)",
    )


# ── Per-ticker assertions ──────────────────────────────────────────────────
def verify(ticker: str, held: bool) -> dict:
    q = get_quant_metrics(ticker)
    tag = f"[{ticker}]"

    if q.get("error"):
        check(f"{tag} returns metrics", False, q["error"])
        return q

    check(f"{tag} returns metrics without error", True)

    sources = q.get("sources") or {}
    flags = " ".join(q.get("quant_flags") or [])
    mismatch = q.get("currency_mismatch")

    # -- every computed metric must carry provenance --
    for metric, key in (("fcf_yield", "fcf"), ("roic", "roic"),
                        ("peg_ratio", "peg_ratio"), ("dcf_intrinsic_value", "dcf")):
        if q.get(metric) is not None:
            check(f"{tag} {metric} has a sources entry", bool(sources.get(key)),
                  f"sources[{key!r}] missing" if not sources.get(key) else "")

    # -- ROIC --
    if q.get("roic") is not None:
        s = sources.get("roic", {})
        check(f"{tag} ROIC from statements, not the info dict",
              s.get("origin") == "income statement + balance sheet", info=str(s.get("origin")))
        check(f"{tag} ROIC invested capital > 0", (s.get("invested_capital") or 0) > 0,
              info=f"invested_capital={s.get('invested_capital')}")
        check(f"{tag} ROIC cites a fiscal period",
              bool(s.get("operating_income_period") and s.get("equity_period")))
    else:
        # Absence is acceptable only if the engine says why.
        check(f"{tag} ROIC absent but a reason is stated",
              bool(q.get("quant_flags")), "no quant_flags explaining the gap")

    # -- FCF --
    if q.get("fcf_yield") is not None:
        check(f"{tag} FCF yield is finite", finite(q["fcf_yield"]), info=repr(q.get("fcf_yield")))
        check(f"{tag} FCF yield is not absurd (|yield| < 100%)",
              abs(q["fcf_yield"]) < 100,
              why="currency mismatch leaking into the ratio?",
              info=f"{q['fcf_yield']}%")

    # -- DCF + sensitivity --
    if q.get("dcf_intrinsic_value") is not None:
        grid = q.get("dcf_sensitivity") or []
        check(f"{tag} sensitivity grid has 9 cells", len(grid) == 9, info=f"{len(grid)} cells")
        check(f"{tag} every sensitivity cell is finite",
              all(finite(c.get("intrinsic")) for c in grid))
        if len(grid) == 9:
            centre = grid[4]["intrinsic"]
            head = q["dcf_intrinsic_value"]
            check(f"{tag} sensitivity centre cell == headline DCF",
                  abs(centre - head) < 0.02, info=f"centre={centre} headline={head}")
            growths = sorted({c["growth_rate"] for c in grid})
            check(f"{tag} sensitivity spans 3 distinct growth rates",
                  len(growths) == 3, info=str(growths))
        check(f"{tag} implied growth present and finite",
              finite(q.get("dcf_implied_growth_pct")) if "dcf_implied_growth_pct" in q else None,
              info=repr(q.get("dcf_implied_growth_pct")))
    else:
        check(f"{tag} DCF declined with a stated reason",
              bool(q.get("quant_flags")), why="declined silently")

    # -- currency mismatch must be CONVERTED, not suppressed --
    # This assertion used to read the other way: it required the DCF to be
    # absent whenever the statements and the listing disagreed on currency.
    # That was correct when the engine had no FX feed and declining was the
    # only honest option. tools/fx.py pulls the cross off the same yfinance
    # feed, so the units problem is now solved rather than reported, and the
    # inverse is what needs guarding — a resolved mismatch that still refuses
    # to value the company is the regression.
    if mismatch:
        # Three distinct states, and they are not the same verdict:
        #   key absent  — the engine has no FX support at all. That is the
        #                 regression this guards, so it FAILS.
        #   False       — FX support exists, the cross did not load, and the
        #                 metrics were correctly held back. Upstream weather,
        #                 not a defect; SKIP, or the suite goes flaky.
        #   True        — converted, so the outputs below must exist.
        resolved = mismatch.get("resolved")
        if resolved is False:
            verdict, why = None, ""
        elif resolved is None:
            verdict, why = False, ("engine reports no FX resolution for this listing — "
                                   "the tools/fx.py conversion is missing on this branch")
        else:
            verdict, why = True, ""

        check(f"{tag} currency mismatch resolved to an FX rate", verdict, why=why,
              info=f"{mismatch['financial']} -> {mismatch['market']}"
                   + ("  (FX cross unavailable — metrics held back)" if resolved is False else ""))

        if resolved:
            check(f"{tag} converted DCF is produced, not suppressed",
                  q.get("dcf_intrinsic_value") is not None
                  # A negative FCF declines the DCF on its own merits, and that
                  # is not the failure this guards against.
                  or (q.get("fcf_yield") or 0) <= 0,
                  why="mismatch resolved but the DCF is still absent",
                  info=f"dcf={q.get('dcf_intrinsic_value')} fcf_yield={q.get('fcf_yield')}")
            check(f"{tag} converted FCF yield is on the market-cap scale (|yield| < 100%)",
                  abs(q.get("fcf_yield") or 0) < 100,
                  why="conversion applied in the wrong direction",
                  info=f"{q.get('fcf_yield')}%")
            if q.get("dcf_intrinsic_value") is not None:
                check(f"{tag} DCF records the FX rate behind it",
                      bool(((q.get("sources") or {}).get("dcf") or {}).get("fx")),
                      why="converted value with no rate recorded is untraceable")

        check(f"{tag} currency mismatch is explained in quant_flags",
              "trades in" in flags.lower() or "reports in" in flags.lower())

    # -- PEG base-effect gate --
    if q.get("peg_ratio") is None and "PEG skipped" in flags:
        check(f"{tag} PEG skipped names the base-effect reason",
              "base-effect" in flags or "base effect" in flags)

    return q


def main() -> int:
    argv = [a.upper() for a in sys.argv[1:]]
    if argv:
        tickers = [(t, t in MY_PORTFOLIO) for t in argv]
    else:
        tickers = ([(t, True) for t in MY_PORTFOLIO]
                   + [(t, False) for t in WATCHLIST])

    print(f"verify_quant — {len(tickers)} ticker(s), free paths only, no AI spend\n")

    check_bookvalue_trap()

    snapshots: dict[str, dict] = {}
    for t, held in tickers:
        print(f"  … {t}", flush=True)
        try:
            snapshots[t] = verify(t, held)
        except Exception as exc:                     # noqa: BLE001
            check(f"[{t}] harness completed", False, f"{type(exc).__name__}: {exc}")

    # ── MELI regression: the sign-flip that started the rebuild ────────────
    meli = snapshots.get("MELI")
    if meli and not meli.get("error"):
        origin = (meli.get("sources", {}).get("fcf") or {}).get("origin")
        check("[MELI] FCF read from the cash-flow statement, not info.freeCashflow",
              origin == "cashflow statement", info=f"origin={origin!r}")
        check("[MELI] FCF yield positive (info.freeCashflow reported it negative)",
              (meli.get("fcf_yield") or 0) > 0, info=f"fcf_yield={meli.get('fcf_yield')}")

    print()
    width = max(len(n) for _, n, _, _ in results) + 2
    for status, name, why, info in results:
        mark = {PASS: "ok  ", FAIL: "FAIL", SKIP: "--  "}[status]
        note = info
        if status == FAIL and why:
            note = f"{info}  <- {why}" if info else f"<- {why}"
        print(f"  {mark} {name.ljust(width)}" + (f"  {note}" if note else ""))

    n_pass = sum(1 for s, _, _, _ in results if s == PASS)
    n_fail = sum(1 for s, _, _, _ in results if s == FAIL)
    n_skip = sum(1 for s, _, _, _ in results if s == SKIP)
    print(f"\n  {n_pass} passed · {n_fail} failed · {n_skip} not applicable "
          f"({len(results)} assertions)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
