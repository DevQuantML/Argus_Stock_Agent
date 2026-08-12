#!/usr/bin/env python
"""
verify_metric_status.py — free-path verification of gap reporting and FX.

Sits beside verify_quant.py, which asserts that the numbers are right. This one
asserts that the *absences* are right: every metric that does not compute must
name the branch that declined it and classify the absence, and the cross-listed
names must now compute rather than being declined on a units technicality.

Spends nothing. yfinance statements, prices and FX crosses only — no
Perplexity, no Groq, no /api/research path.

    python scripts/verify_metric_status.py            # the standing set
    python scripts/verify_metric_status.py SKHY BABA  # only these
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# cp1252 is the default Windows console codepage and has no glyph for the
# punctuation below; printing one would raise mid-run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from tools import fx                                        # noqa: E402
from tools.quant import (METHODOLOGICAL, STRUCTURAL,        # noqa: E402
                         TRANSIENT, UNSUPPORTED,
                         get_quant_metrics)

NATURES = {TRANSIENT, STRUCTURAL, UNSUPPORTED, METHODOLOGICAL}
METRICS = ("roic", "fcf_yield", "peg_ratio", "dcf", "sharpe", "beta")

# Cross-listed names: the whole point of the FX work is that these compute.
CROSS_LISTED = ("SKHY", "BABA", "TSM", "ASML", "NVO")
# Banks: the point here is the opposite — these must NOT compute a cash-flow
# valuation. Both are needed. JPM's free cash flow is negative, so a DCF would
# decline on the sign alone and prove nothing; BAC's is positive and used to
# produce a full $23/share intrinsic value, which is the branch worth guarding.
BANK_FILERS = ("JPM", "BAC")
# Insurance carriers, one per industry label the rule has to match: Property &
# Casualty, Life, Diversified, Reinsurance. These file an operating-income row,
# so the bank tell never fires on them and they used to price themselves —
# measured 2026-08-11, ALL returned a 14.5% FCF yield and a $2,794 intrinsic
# value against a $270 share price, PGR $2,281 against $214, MET $630 against
# $97, EG $1,738 against $369. One order of magnitude out, stated confidently.
INSURANCE_FILERS = ("ALL", "PGR", "MET", "AIG", "EG")
FINANCIAL_FILERS = BANK_FILERS + INSURANCE_FILERS
# The carve-out, and the reason the insurer rule keys on more than the word
# "Insurance". Yahoo files the broking houses as "Insurance Brokers", one
# prefix away from the carriers, but a broker earns commission on policies it
# does not carry and holds no float — AJG 2.8%, AON 4.3%, BRO 5.8% FCF yield
# against the carriers' 13.5–27.8%. An exchange and a card network are the
# wider Financial Services controls. None of these may be declined as
# unsupported; a transient feed gap is fine and is not this rule's business.
#
# Two brokers, not one. MMC has returned no sector, no bars and a 404 quote
# summary since at least 2026-08-11, so it currently proves nothing about the
# carve-out and left AJG carrying that assertion alone — and MMC is precisely
# the demonstration that a large listed name can drop out of the feed
# overnight. It stays as the dead-symbol canary (it is what surfaced an
# over-broad Sharpe assertion); AON restores the live redundancy.
FCF_MEANINGFUL = ("AJG", "AON", "MMC", "CME", "V")
# Plus a domestic control and a fund.
STANDING = (("PLTR", "MELI", "ORCL", "GE", "SPY")
            + FINANCIAL_FILERS + FCF_MEANINGFUL + CROSS_LISTED)

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool | None, note: str = "") -> None:
    results.append(("SKIP" if ok is None else ("PASS" if ok else "FAIL"), name, note))


def verify(ticker: str) -> dict:
    q = get_quant_metrics(ticker)
    tag = f"[{ticker}]"
    if q.get("error"):
        check(f"{tag} returns metrics", False, q["error"])
        return q

    status = q.get("metric_status") or {}

    # -- every metric must be accounted for, either way --
    check(f"{tag} every metric has a status entry",
          all(m in status for m in METRICS),
          f"absent: {sorted(set(METRICS) - set(status))}")

    for metric, s in status.items():
        if s.get("state") == "computed":
            check(f"{tag} {metric} computed matches a present value",
                  q.get({"fcf_yield": "fcf_yield", "peg_ratio": "peg_ratio",
                         "dcf": "dcf_intrinsic_value", "sharpe": "sharpe_ratio"}
                        .get(metric, metric)) is not None,
                  "marked computed but the value is absent")
            continue

        # -- an unavailable metric owes the reader three things --
        check(f"{tag} {metric} gap has a known nature",
              s.get("nature") in NATURES, f"nature={s.get('nature')!r}")
        check(f"{tag} {metric} gap states a reason",
              bool((s.get("reason") or "").strip()))
        check(f"{tag} {metric} gap says what would resolve it",
              bool((s.get("resolves") or "").strip()))
        # The regression that started this: a generic reason that could be
        # pasted onto any ticker is the failure mode, not a passing state.
        check(f"{tag} {metric} reason is specific, not boilerplate",
              (s.get("reason") or "").lower() not in {
                  "n/a", "not available", "missing", "no data",
                  "yahoo did not return this metric.",
              })

    # -- missing_metrics must agree with metric_status --
    declared = set(q.get("missing_metrics") or [])
    derived = {m for m, s in status.items() if s.get("state") == "unavailable"}
    check(f"{tag} missing_metrics agrees with metric_status",
          declared == derived, f"{sorted(declared)} vs {sorted(derived)}")

    # -- a currency mismatch must be converted, not declined --
    cm = q.get("currency_mismatch")
    if cm:
        check(f"{tag} currency mismatch resolved via FX",
              bool(cm.get("resolved")),
              f"{cm.get('financial')}->{cm.get('market')} unresolved (FX feed down?)")
        if cm.get("resolved"):
            check(f"{tag} FCF yield computed despite {cm['financial']}/{cm['market']}",
                  q.get("fcf_yield") is not None,
                  "still declined after conversion")
            check(f"{tag} FCF yield is plausible after conversion (|yield| < 100%)",
                  abs(q.get("fcf_yield") or 0) < 100,
                  f"{q.get('fcf_yield')}% — conversion applied in the wrong direction?")
            # Skips when the DCF was declined for a reason of its own — BABA's
            # free cash flow is -$7.5bn after conversion, so it takes the
            # negative-sign branch and there is no valuation to carry a rate.
            # That is the assertion being inapplicable, not failing; TSM, ASML
            # and NVO all compute and cover this path.
            check(f"{tag} DCF records the FX rate it used",
                  bool(((q.get("sources") or {}).get("dcf") or {}).get("fx"))
                  if q.get("dcf_intrinsic_value") is not None else None,
                  f"no DCF to carry a rate — declined "
                  f"{(status.get('dcf') or {}).get('nature')} on its own terms")

    # -- a bank or carrier must decline the cash-flow metrics, not price them --
    # Yahoo publishes a Free Cash Flow row for both, so the arithmetic succeeds
    # and prints a number that is confidently wrong: for a deposit-taking
    # institution that row tracks loan and deposit movement, and for a carrier
    # it tracks underwriting float — neither is owner's cash. An absent metric
    # with a stated reason is the correct output here.
    if ticker in FINANCIAL_FILERS:
        for metric, value_key in (("fcf_yield", "fcf_yield"),
                                  ("dcf", "dcf_intrinsic_value")):
            s = status.get(metric, {})
            check(f"{tag} {metric} is declined, not computed",
                  s.get("state") == "unavailable", f"state={s.get('state')!r}")
            check(f"{tag} {metric} gap is unsupported, not transient or structural",
                  s.get("nature") == UNSUPPORTED, f"nature={s.get('nature')!r}")
            check(f"{tag} {metric} publishes no number",
                  q.get(value_key) is None, f"{value_key}={q.get(value_key)}")
            check(f"{tag} {metric} reason names the sector it declined on",
                  (s.get("sector") or "").lower() == "financial services",
                  f"sector={s.get('sector')!r}")
            # A carrier declined on the *bank* tell would pass every assertion
            # above while proving nothing about the insurer rule — that happens
            # the moment its income statement comes back short. Naming the
            # industry is what distinguishes the branch that actually fired.
            if ticker in INSURANCE_FILERS:
                check(f"{tag} {metric} gap names the insurance industry it declined on",
                      (s.get("industry") or "").lower().startswith("insurance"),
                      f"industry={s.get('industry')!r}")

    # -- and a broker, exchange or card network must keep its cash-flow metrics --
    # The insurer rule matches on an industry label one prefix away from
    # "Insurance Brokers". Asserted as "not declined as unsupported" rather than
    # "computed": a feed gap is transient and legitimate, and MMC returned no
    # sector at all on 2026-08-11. Only an UNSUPPORTED verdict means the rule
    # reached a name whose free cash flow is real.
    if ticker in FCF_MEANINGFUL:
        for metric in ("fcf_yield", "dcf"):
            s = status.get(metric, {})
            swept = s.get("state") == "unavailable" and s.get("nature") == UNSUPPORTED
            check(f"{tag} {metric} not swept up as unsupported", not swept,
                  f"declined unsupported on sector={s.get('sector')!r} "
                  f"industry={s.get('industry')!r} — {s.get('reason')!r}")

    # -- a fund must be declared unsupported, never "the feed failed" --
    if ticker == "SPY":
        natures = {s.get("nature") for s in status.values() if s.get("state") == "unavailable"}
        check("[SPY] fund gaps are unsupported, not transient",
              natures == {UNSUPPORTED}, f"natures={sorted(natures)}")

    # -- a short-history listing must quantify the shortfall --
    # Both counts, on every transient branch including the one that fetched zero
    # rows: MMC returned no bars and a 404 quote summary on 2026-08-11, and a
    # consumer reading the structured record should not have to special-case it.
    sh = status.get("sharpe", {})
    if sh.get("state") == "unavailable" and sh.get("nature") == TRANSIENT:
        check(f"{tag} Sharpe gap cites the day count it has and needs",
              sh.get("available_days") is not None and sh.get("required_days") is not None,
              f"available={sh.get('available_days')} required={sh.get('required_days')}")

    return q


def main() -> int:
    tickers = [a.upper() for a in sys.argv[1:]] or list(STANDING)
    print(f"verify_metric_status — {len(tickers)} ticker(s), free paths only, no AI spend\n")

    # FX is the dependency the conversion rests on; prove it before blaming a
    # ticker for a gap the feed caused.
    probe = fx.rate("KRW", "USD")
    check("FX cross KRWUSD=X resolves", probe is not None,
          "yfinance FX feed unreachable — currency gaps below will read transient")
    if probe:
        check("FX rate is a plausible KRW/USD figure",
              0.0001 < probe["rate"] < 0.01, f"rate={probe['rate']}")
    check("FX identity pair short-circuits",
          (fx.rate("USD", "USD") or {}).get("rate") == 1.0)
    check("FX rejects a non-ISO code without calling out",
          fx.rate("NOTACCY", "USD") is None)

    for t in tickers:
        print(f"  … {t}", flush=True)
        try:
            verify(t)
        except Exception as exc:                    # noqa: BLE001
            check(f"[{t}] harness completed", False, f"{type(exc).__name__}: {exc}")

    print()
    width = max(len(n) for _, n, _ in results) + 2
    for status, name, note in results:
        mark = {"PASS": "ok  ", "FAIL": "FAIL", "SKIP": "--  "}[status]
        # A skip prints its note too. A bare "--" reads as a loose end, and the
        # reader cannot tell a deliberately inapplicable assertion from one that
        # quietly stopped running — which is the same failure the four natures
        # exist to prevent on the metrics themselves.
        suffix = f"  <- {note}" if status in ("FAIL", "SKIP") and note else ""
        print(f"  {mark} {name.ljust(width)}{suffix}")

    n_fail = sum(1 for s, _, _ in results if s == "FAIL")
    n_pass = sum(1 for s, _, _ in results if s == "PASS")
    n_skip = sum(1 for s, _, _ in results if s == "SKIP")
    print(f"\n  {n_pass} passed · {n_fail} failed · {n_skip} not applicable "
          f"({len(results)} assertions)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
