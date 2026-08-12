"""
tools/fundamentals.py — annual financial statements, with provenance.

Why this module exists
----------------------
`yf.Ticker(t).info` is the convenient source, but it is not a reliable one for
the fields the quant engine needs:

  * `operatingIncome` and `ebit` are never populated on the tickers this
    portfolio holds — which is why ROIC never once computed.
  * `totalStockholdersEquity` is not an `info` key at all.
  * `freeCashflow` disagrees with the cash flow statement, sometimes in sign.
    MELI reports -$4.1B in `info` against +$10.77B in the statement, which made
    the engine call a strongly cash-generative business "pre-profitability".

The three statement DataFrames carry all of it, correctly, plus multiple years
of history. That history is what lets the DCF use a real trailing FCF CAGR
instead of a revenue-growth proxy.

Every value comes back with its provenance — which statement, which row label,
which fiscal period. That record is what the disclosure UI and the research
prompt use to show their work, and it is the difference between "the DCF says
$184" and "the DCF says $184, built on FY2025 FCF of $7.26B".

Security / robustness:
  * validate_ticker() before any yfinance call, as everywhere else in tools/.
  * Never raises — returns an empty dict on any failure. Callers fall back.
  * Row labels are matched against an ordered candidate list because yfinance
    renames rows between versions; a rename degrades one metric to "not
    available", it does not break the module.
"""

import logging
import math
import threading
import time

import yfinance as yf

from tools.validator import validate_ticker

logger = logging.getLogger(__name__)

# Statements are quarterly events; anything shorter than this is churn. The
# research pipeline already caches its own bundle for 120s, so this mostly
# absorbs repeat lookups across separate runs on the same ticker.
_TTL = 900.0

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# Ordered candidates per field: first match wins, so put the exact row we want
# ahead of the near-synonyms. Verified present on PLTR, MELI, ORCL and GE.
_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "operating_income": ("income_stmt", ("Operating Income", "EBIT",
                                         "Total Operating Income As Reported")),
    "pretax_income":    ("income_stmt", ("Pretax Income",)),
    "tax_provision":    ("income_stmt", ("Tax Provision",)),
    "tax_rate_calc":    ("income_stmt", ("Tax Rate For Calcs",)),
    "equity":           ("balance_sheet", ("Stockholders Equity",
                                           "Total Stockholder Equity",
                                           "Common Stock Equity")),
    "total_debt":       ("balance_sheet", ("Total Debt",)),
    "cash":             ("balance_sheet", ("Cash And Cash Equivalents",
                                           "Cash Cash Equivalents And Short Term Investments")),
    "fcf":              ("cashflow", ("Free Cash Flow",)),
    "operating_cf":     ("cashflow", ("Operating Cash Flow",)),
    "capex":            ("cashflow", ("Capital Expenditure",)),
}

_STATEMENTS = ("income_stmt", "balance_sheet", "cashflow")


def _finite(value) -> float | None:
    """Float or None. Rejects NaN/Inf, which pandas hands back for empty cells."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _find_row(df, candidates: tuple[str, ...]):
    """Return (row_label, series) for the first candidate present, else (None, None).

    Exact case-insensitive match is tried across every candidate before falling
    back to substring, so "Operating Income" is never shadowed by
    "Other Non Operating Income Expenses".
    """
    if df is None or getattr(df, "empty", True):
        return None, None

    labels = [str(i) for i in df.index]
    lowered = [lab.lower() for lab in labels]

    for cand in candidates:
        target = cand.lower()
        for lab, low in zip(labels, lowered):
            if low == target:
                return lab, df.loc[lab]

    for cand in candidates:
        target = cand.lower()
        for lab, low in zip(labels, lowered):
            if target in low:
                return lab, df.loc[lab]

    return None, None


def _period_label(columns, index: int) -> str | None:
    try:
        return str(columns[index])[:10]
    except (IndexError, TypeError):
        return None


def _fetch(ticker: str) -> dict:
    """Pull the three statements and normalise the fields we need."""
    stock = yf.Ticker(ticker)

    frames: dict[str, object] = {}
    for name in _STATEMENTS:
        try:
            frames[name] = getattr(stock, name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("fundamentals: %s unavailable for %s (type=%s): %s",
                         name, ticker, type(exc).__name__, exc)
            frames[name] = None

    out: dict = {"ticker": ticker, "fields": {}, "fcf_series": []}

    for field, (statement, candidates) in _FIELDS.items():
        df = frames.get(statement)
        label, series = _find_row(df, candidates)
        if series is None:
            continue

        value = _finite(series.iloc[0]) if len(series) else None
        if value is None:
            continue

        out["fields"][field] = {
            "value":  value,
            "period": _period_label(df.columns, 0),
            "row":    label,
            "source": statement,
        }

    # Multi-year FCF, newest first — the input to the trailing growth rate.
    cf = frames.get("cashflow")
    label, series = _find_row(cf, _FIELDS["fcf"][1])
    if series is not None:
        for i, raw in enumerate(series.values):
            value = _finite(raw)
            if value is None:
                continue
            out["fcf_series"].append({"period": _period_label(cf.columns, i), "value": value})

    return out


def get_statements(ticker: str) -> dict:
    """Annual statement figures for `ticker`, with provenance. Never raises.

    Returns:
        {
          "ticker": "PLTR",
          "fields": {"operating_income": {"value": 1.41e9, "period": "2025-12-31",
                                          "row": "Operating Income",
                                          "source": "income_stmt"}, ...},
          "fcf_series": [{"period": "2025-12-31", "value": 2.10e9}, ...],  # newest first
        }
        An empty dict if the ticker is invalid or every statement failed.
    """
    try:
        ticker = validate_ticker(ticker)
    except ValueError as exc:
        logger.warning("get_statements: invalid ticker: %s", exc)
        return {}

    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(ticker)
        if hit and (now - hit[0]) < _TTL:
            return hit[1]

    try:
        data = _fetch(ticker)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_statements: fetch failed for %s (type=%s): %s",
                     ticker, type(exc).__name__, exc)
        return {}

    if not data.get("fields") and not data.get("fcf_series"):
        # Nothing usable — don't cache an empty result for 15 minutes, the next
        # call may land after yfinance recovers.
        logger.debug("get_statements: no statement rows resolved for %s", ticker)
        return {}

    with _cache_lock:
        _cache[ticker] = (now, data)
    return data


# ── Convenience readers ────────────────────────────────────────────────────

def value_of(statements: dict, field: str) -> float | None:
    """The plain number for `field`, or None."""
    entry = (statements.get("fields") or {}).get(field)
    return entry.get("value") if entry else None


def provenance_of(statements: dict, field: str) -> dict | None:
    """The {period, row, source} record for `field`, or None."""
    entry = (statements.get("fields") or {}).get(field)
    if not entry:
        return None
    return {"period": entry.get("period"), "row": entry.get("row"), "source": entry.get("source")}


def fcf_cagr(statements: dict, max_years: int = 4) -> dict | None:
    """Trailing FCF compound growth from the statement series.

    Returns {"rate": float, "from": {...}, "to": {...}, "years": int} or None.

    Declines rather than guesses when the series cannot support a growth rate:
    fewer than three points, or a non-positive endpoint. ORCL is the live case —
    its FCF ran +8.47B → +11.81B → -0.39B → -23.69B, and any CAGR through a sign
    change is meaningless. A caller that gets None falls back to revenue growth,
    or skips the DCF and says why.
    """
    series = (statements.get("fcf_series") or [])[:max_years]
    if len(series) < 3:
        return None

    newest, oldest = series[0], series[-1]
    new_v, old_v = newest.get("value"), oldest.get("value")
    if not new_v or not old_v or new_v <= 0 or old_v <= 0:
        return None

    # Any negative year between the endpoints makes the compound rate a fiction
    # even when both ends are positive.
    if any((p.get("value") or 0) <= 0 for p in series):
        return None

    years = len(series) - 1
    try:
        rate = (new_v / old_v) ** (1.0 / years) - 1.0
    except (ValueError, ZeroDivisionError, OverflowError):
        return None

    if rate is None or math.isnan(rate) or math.isinf(rate):
        return None

    return {
        "rate":  rate,
        "from":  {"period": oldest.get("period"), "value": old_v},
        "to":    {"period": newest.get("period"), "value": new_v},
        "years": years,
    }
