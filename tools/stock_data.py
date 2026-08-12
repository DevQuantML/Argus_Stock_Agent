"""
tools/stock_data.py — Current price, fundamentals, and position context.

Pulls from Yahoo Finance (free, no API key needed).
Also injects portfolio context if the ticker is a current holding.

Hardening applied:
  Task 1 — validate_ticker() guards the ticker before any API call.
  Task 2 — try/except around every external call; always returns a dict.
           None-checks on every yfinance field before arithmetic.
"""

import logging

import yfinance as yf

from config import MY_PORTFOLIO, WATCHLIST
from tools.validator import validate_ticker

logger = logging.getLogger(__name__)


def _safe_float(value, default=None):
    """Convert a value to float, returning default if value is None or invalid."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_price_and_fundamentals(ticker: str) -> dict:
    """
    Returns price, key valuation metrics, and portfolio context
    for any ticker. The agent calls this as the first tool in most research tasks.

    Works for:
    - US tickers:   PLTR, MELI, NOW, CRM, ORCL, GE
    - Indian NSE:   TATAELXSI.NS, LTTS.NS, CYIENT.NS
    - Indian BSE:   TATAELXSI.BO

    Always returns a dict — never raises.
    """
    # ── Task 1: Validate ticker before touching the network ────────────────
    try:
        ticker = validate_ticker(ticker)
    except ValueError as exc:
        logger.warning("get_price_and_fundamentals: invalid ticker: %s", exc)
        return {"ticker": ticker, "error": str(exc)}

    try:
        stock = yf.Ticker(ticker)

        # ── Task 2: Guard against unexpected response format ───────────────
        try:
            info = stock.info
        except Exception as exc:
            err_type = type(exc).__name__
            logger.debug(
                "get_price_and_fundamentals: stock.info failed (type=%s): %s",
                err_type, exc,
            )
            return {"ticker": ticker, "error": "unexpected response format"}

        if not isinstance(info, dict) or not info:
            logger.debug(
                "get_price_and_fundamentals: info is not a non-empty dict "
                "(type=%s, value=%r)", type(info), info
            )
            return {"ticker": ticker, "error": "unexpected response format"}

        # ── Current price — try multiple fields (yfinance field names vary) ─
        price = _safe_float(
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or info.get("previousClose")
        )

        # ── 52-week range ──────────────────────────────────────────────────
        high_52w = _safe_float(info.get("fiftyTwoWeekHigh"))
        low_52w  = _safe_float(info.get("fiftyTwoWeekLow"))

        pct_from_high = None
        if price is not None and high_52w is not None and high_52w != 0:
            pct_from_high = round(((price - high_52w) / high_52w) * 100, 1)

        result = {
            "ticker":   ticker,
            "name":     info.get("shortName", ticker),
            "price":    round(price, 2) if price is not None else None,
            "currency": info.get("currency", "USD"),

            # Valuation — all may be None; that is fine
            "pe_trailing":  _safe_float(info.get("trailingPE")),
            "pe_forward":   _safe_float(info.get("forwardPE")),
            "ps_ratio":     _safe_float(info.get("priceToSalesTrailing12Months")),
            "ev_ebitda":    _safe_float(info.get("enterpriseToEbitda")),

            # Growth + quality
            "revenue_growth_yoy": _safe_float(info.get("revenueGrowth")),
            "gross_margin":       _safe_float(info.get("grossMargins")),
            "operating_margin":   _safe_float(info.get("operatingMargins")),
            "net_margin":         _safe_float(info.get("profitMargins")),
            "free_cash_flow":     _safe_float(info.get("freeCashflow")),

            # Market context
            "market_cap":        _safe_float(info.get("marketCap")),
            "52w_high":          high_52w,
            "52w_low":           low_52w,
            "pct_from_52w_high": pct_from_high,

            # Analyst view
            "analyst_target": _safe_float(info.get("targetMeanPrice")),
            "analyst_rating": info.get("recommendationKey"),
        }

        # ── Inject portfolio context if we hold this stock ─────────────────
        if ticker in MY_PORTFOLIO:
            pos = MY_PORTFOLIO[ticker]
            avg_cost = _safe_float(pos.get("avg_cost"))

            current_pnl = None
            if price is not None and avg_cost is not None and avg_cost != 0:
                current_pnl = round(((price - avg_cost) / avg_cost) * 100, 1)

            stop_loss = _safe_float(pos.get("stop_loss"))
            trim_at   = _safe_float(pos.get("trim_at"))

            distance_to_stop = None
            if price is not None and stop_loss is not None and price != 0:
                distance_to_stop = round(((price - stop_loss) / price) * 100, 1)

            distance_to_trim = None
            if price is not None and trim_at is not None and price != 0:
                distance_to_trim = round(((trim_at - price) / price) * 100, 1)

            result["my_position"] = {
                "shares":             pos.get("shares"),
                "avg_cost":           avg_cost,
                "unrealized_pnl_pct": current_pnl,
                "tranches":           pos.get("tranches"),
                "thesis":             pos.get("thesis"),
                "stop_loss":          stop_loss,
                "trim_at":            trim_at,
                "distance_to_stop":   distance_to_stop,
                "distance_to_trim":   distance_to_trim,
            }

        # ── Inject watchlist context if applicable ─────────────────────────
        elif ticker in WATCHLIST:
            watch = WATCHLIST[ticker]
            result["watchlist"] = {
                "tranches":    watch.get("tranches"),
                "thesis":      watch.get("thesis"),
                "brent_gated": watch.get("brent_gated"),
                "note": "Not yet a position — monitoring for entry at tranche levels.",
            }

        return result

    except Exception as exc:  # noqa: BLE001
        err_type = type(exc).__name__
        logger.debug(
            "get_price_and_fundamentals: unhandled error (type=%s): %s", err_type, exc
        )
        return {
            "ticker": ticker,
            "error": "unexpected response format",
            "note": (
                "Yahoo Finance fetch failed. "
                "Try adding .NS for NSE or .BO for BSE suffix."
            ),
        }


# ── Price history (for frontend charts) ───────────────────────────────────

# Only these yfinance periods are accepted — anything else is rejected rather
# than passed through to yfinance, so the caller can't drive arbitrary values.
VALID_PERIODS = ("1mo", "6mo", "1y", "5y")

_MAX_POINTS = 300   # keeps the SVG path light; 5y daily is ~1250 raw rows


def get_price_history(ticker: str, period: str = "1y") -> dict:
    """
    Daily close series for charting. Downsampled to at most _MAX_POINTS.

    Always returns a dict — never raises. On failure returns
    {"ticker": ..., "error": "..."} matching the contract above.
    """
    try:
        ticker = validate_ticker(ticker)
    except ValueError as exc:
        logger.warning("get_price_history: invalid ticker: %s", exc)
        return {"ticker": ticker, "error": str(exc)}

    if period not in VALID_PERIODS:
        return {
            "ticker": ticker,
            "error": f"invalid period — must be one of {', '.join(VALID_PERIODS)}",
        }

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)

        if hist is None or hist.empty or "Close" not in hist:
            logger.debug("get_price_history: empty history for %s (%s)", ticker, period)
            return {"ticker": ticker, "error": "no price history available"}

        closes = hist["Close"].dropna()
        if closes.empty:
            return {"ticker": ticker, "error": "no price history available"}

        rows = [
            (idx.strftime("%Y-%m-%d"), _safe_float(val))
            for idx, val in closes.items()
        ]
        rows = [(d, c) for d, c in rows if c is not None]
        if not rows:
            return {"ticker": ticker, "error": "no price history available"}

        # ── Downsample by stride, always keeping the most recent point ─────
        if len(rows) > _MAX_POINTS:
            stride = (len(rows) // _MAX_POINTS) + 1
            sampled = rows[::stride]
            if sampled[-1] != rows[-1]:
                sampled.append(rows[-1])
            rows = sampled

        values = [c for _, c in rows]
        first, last = values[0], values[-1]
        change_pct = round(((last - first) / first) * 100, 2) if first else None

        # Currency comes from .info, which is a separate (slow, failure-prone)
        # call — degrade to None rather than let it break the chart.
        currency = None
        try:
            currency = stock.info.get("currency")
        except Exception:  # noqa: BLE001
            pass

        return {
            "ticker":     ticker,
            "period":     period,
            "currency":   currency,
            "points":     [{"t": d, "c": round(c, 2)} for d, c in rows],
            "first":      round(first, 2),
            "last":       round(last, 2),
            "change_pct": change_pct,
            "high":       round(max(values), 2),
            "low":        round(min(values), 2),
        }

    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "get_price_history: unhandled error for '%s' (type=%s): %s",
            ticker, type(exc).__name__, exc,
        )
        return {"ticker": ticker, "error": "price history fetch failed"}
