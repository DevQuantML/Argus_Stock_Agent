"""
tools/xirr.py — money-weighted return (XIRR) in pure Python.

XIRR is the rate r that solves  Σ cf_i / (1+r)^(t_i/365) = 0  for dated cash
flows. There is no closed form, so this uses Newton–Raphson with a bisection
fallback: Newton alone diverges on short holding periods, which is the common
case for a portfolio only a few months old.

Contract: returns None, never NaN and never raises. A portfolio with no buy
dates must render "not computable — buy date missing", not a fabricated number.
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

_MAX_ITER = 100
_TOL      = 1e-7
_LOW      = -0.9999      # -99.99% — total loss
_HIGH     = 10.0         # +1000% annualised; anything beyond is not meaningful


def _npv(rate: float, flows: list[tuple[date, float]], t0: date) -> float:
    total = 0.0
    for when, amount in flows:
        years = (when - t0).days / 365.0
        base = 1.0 + rate
        if base <= 0:
            return float("inf")
        total += amount / (base ** years)
    return total


def _dnpv(rate: float, flows: list[tuple[date, float]], t0: date) -> float:
    total = 0.0
    for when, amount in flows:
        years = (when - t0).days / 365.0
        base = 1.0 + rate
        if base <= 0:
            return float("inf")
        total -= years * amount / (base ** (years + 1.0))
    return total


def xirr(flows: list[tuple[date, float]]) -> float | None:
    """
    Annualised money-weighted return as a percentage (12.34 == +12.34%).

    flows: (date, amount) — buys negative, current value positive.
    Returns None when the answer would be meaningless:
      * fewer than two flows
      * no sign change (all buys, or all proceeds)
      * every flow on the same day (zero elapsed time)
      * no convergence
    """
    if not flows or len(flows) < 2:
        return None

    amounts = [a for _, a in flows]
    if not (any(a < 0 for a in amounts) and any(a > 0 for a in amounts)):
        return None

    ordered = sorted(flows, key=lambda f: f[0])
    t0 = ordered[0][0]
    if all(w == t0 for w, _ in ordered):
        return None

    # ── Newton–Raphson ────────────────────────────────────────────────────
    rate = 0.10
    for _ in range(_MAX_ITER):
        value = _npv(rate, ordered, t0)
        if not _finite(value):
            break
        if abs(value) < _TOL:
            return _as_pct(rate)
        slope = _dnpv(rate, ordered, t0)
        if not _finite(slope) or abs(slope) < 1e-12:
            break
        step = value / slope
        nxt = rate - step
        if nxt <= _LOW:                      # keep the iterate in the domain
            break
        if abs(step) < _TOL:
            return _as_pct(nxt)
        rate = nxt

    # ── Bisection fallback ────────────────────────────────────────────────
    lo, hi = _LOW, _HIGH
    f_lo, f_hi = _npv(lo, ordered, t0), _npv(hi, ordered, t0)
    if not (_finite(f_lo) and _finite(f_hi)) or f_lo * f_hi > 0:
        return None                          # no root in the bracket

    for _ in range(_MAX_ITER):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, ordered, t0)
        if not _finite(f_mid):
            return None
        if abs(f_mid) < _TOL or (hi - lo) / 2.0 < _TOL:
            return _as_pct(mid)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid

    return None


def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


def _as_pct(rate: float) -> float | None:
    if not _finite(rate):
        return None
    pct = rate * 100.0
    # Beyond this band the number is an artefact of a very short holding
    # period, not a return anyone should read.
    if pct < -99.99 or pct > 100000.0:
        return None
    return round(pct, 2)
