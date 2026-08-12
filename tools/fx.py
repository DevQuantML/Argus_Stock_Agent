"""
tools/fx.py — currency conversion for cross-listed equities.

Why this module exists
----------------------
An ADR reports its statements in the home currency and trades in another. SKHY
files in KRW and quotes in USD; BABA in CNY, TSM in TWD, ASML in EUR, NVO in
DKK — all quoted in USD. Dividing a KRW cash flow by a USD market cap gives a
2500% "FCF yield", so the engine used to decline both FCF yield and DCF on any
such listing and say the two scales were not comparable.

They are comparable; they just need a rate. Five of the twelve large caps
probed while writing this hit the mismatch, which makes it a routine case
rather than an edge one, and declining a DCF on a $700B semiconductor company
because of a units problem is a worse answer than converting the units.

The rate comes from the same yfinance feed already in use — no new package, no
new credential, no new failure domain. Yahoo publishes direct crosses as
`{FROM}{TO}=X`; `KRWUSD=X` returns USD per KRW.

Never raises. A caller that gets None must report the affected metric as
temporarily unavailable — guessing a rate would be worse than saying so.
"""

import logging
import math
import re
import threading
import time

import yfinance as yf

logger = logging.getLogger(__name__)

# FX moves far too little over an hour to matter to a five-year DCF, and the
# statement cache next door already holds its inputs for 15 minutes.
_TTL = 3600.0

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# ISO 4217 shape. The pair symbol is built from two of these and nothing else,
# so no caller-supplied string is ever concatenated into a Yahoo symbol —
# validate_ticker() is not used here because it rejects a six-letter base
# before the `=X` suffix, which is exactly the shape every FX cross has.
_CCY_RE = re.compile(r"^[A-Z]{3}$")


def _finite_positive(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f <= 0:
        return None
    return f


def _quote(pair: str) -> tuple[float | None, str | None]:
    """Spot rate for a Yahoo FX symbol, and where it came from.

    `info` is tried first because it is the live quote, then a short history
    window, because the two fail independently — during one probe `info`
    returned an empty dict for a pair whose history was fine.
    """
    ticker = yf.Ticker(pair)

    try:
        info = ticker.info or {}
        rate = _finite_positive(info.get("regularMarketPrice") or info.get("previousClose"))
        if rate is not None:
            return rate, "spot quote"
    except Exception as exc:  # noqa: BLE001
        logger.debug("fx: info failed for %s (type=%s): %s", pair, type(exc).__name__, exc)

    try:
        hist = ticker.history(period="5d")
        if hist is not None and not hist.empty and "Close" in hist:
            closes = hist["Close"].dropna()
            if len(closes):
                rate = _finite_positive(closes.iloc[-1])
                if rate is not None:
                    return rate, "last daily close"
    except Exception as exc:  # noqa: BLE001
        logger.debug("fx: history failed for %s (type=%s): %s", pair, type(exc).__name__, exc)

    return None, None


def rate(from_ccy: str | None, to_ccy: str | None) -> dict | None:
    """Units of `to_ccy` per one unit of `from_ccy`.

    Returns {"rate": float, "pair": "KRWUSD=X", "from": "KRW", "to": "USD",
             "origin": "yfinance FX cross — spot quote"} or None.

    An identical pair returns rate 1.0 without a network call, so callers can
    hand it any two currencies without special-casing the common one.
    """
    if not isinstance(from_ccy, str) or not isinstance(to_ccy, str):
        return None

    src, dst = from_ccy.strip().upper(), to_ccy.strip().upper()
    if not _CCY_RE.match(src) or not _CCY_RE.match(dst):
        logger.debug("fx: rejecting non-ISO currency pair %r -> %r", from_ccy, to_ccy)
        return None

    if src == dst:
        return {"rate": 1.0, "pair": None, "from": src, "to": dst,
                "origin": "same currency — no conversion applied"}

    now = time.monotonic()
    key = (src, dst)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _TTL:
            return hit[1]

    pair = f"{src}{dst}=X"
    try:
        value, how = _quote(pair)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fx: unhandled error on %s (type=%s): %s", pair, type(exc).__name__, exc)
        return None

    if value is None:
        # Not cached. A failed lookup is the transient case by definition, and
        # holding the failure for an hour would turn a blip into an outage.
        logger.debug("fx: no rate available for %s", pair)
        return None

    result = {
        "rate":   value,
        "pair":   pair,
        "from":   src,
        "to":     dst,
        "origin": f"yfinance FX cross — {how}",
    }
    with _cache_lock:
        _cache[key] = (now, result)
    return result
