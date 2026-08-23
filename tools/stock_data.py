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


def _live_book() -> tuple[dict, dict]:
    """The operator's CURRENT positions and watchlist.

    store.py is the source of truth: every edit made in the UI is written
    there. config.MY_PORTFOLIO / WATCHLIST are seed data only — bootstrap()
    copies them into SQLite once, on first run, and never looks at them again.

    This module used to read the config dicts directly, which meant it answered
    with the seed rather than the book. A holding sold in the UI still reported
    a live P&L here, and a corrected avg_cost still showed the original, while
    /api/portfolio/analytics — reading store — reported the real figures. Same
    screen, two answers, and the config-derived one was simply stale.

    store.positions() and store.watchlist() are documented as returning the
    config shape, so callers below are unchanged.

    Falls back to the seed only when the database cannot be read at all, which
    is the CLI path: main.py never calls store.bootstrap(), so the tables may
    not exist. Returning the seed there is better than failing outright, and it
    matches what the CLI has always shown.
    """
    try:
        import store
        return store.positions(), store.watchlist()
    except Exception as exc:  # noqa: BLE001
        logger.debug("_live_book: store unavailable (%s), using config seed",
                     type(exc).__name__)
        return MY_PORTFOLIO, WATCHLIST


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
        book, watching = _live_book()

        if ticker in book:
            pos = book[ticker]
            avg_cost = _safe_float(pos.get("avg_cost"))

            # 2dp, matching api.py's analytics routes exactly. At 1dp this
            # returned 64.1 for a position api.py reported as 64.14, and the
            # frontend renders both to two places — so the same holding showed
            # "+64.10%" in the watchlist and "+64.14%" in the holdings table on
            # one screen. Two roundings of one quantity is one too many.
            current_pnl = None
            if price is not None and avg_cost is not None and avg_cost != 0:
                current_pnl = round(((price - avg_cost) / avg_cost) * 100, 2)

            stop_loss = _safe_float(pos.get("stop_loss"))
            trim_at   = _safe_float(pos.get("trim_at"))

            distance_to_stop = None
            if price is not None and stop_loss is not None and price != 0:
                distance_to_stop = round(((price - stop_loss) / price) * 100, 2)

            distance_to_trim = None
            if price is not None and trim_at is not None and price != 0:
                distance_to_trim = round(((trim_at - price) / price) * 100, 2)

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
        elif ticker in watching:
            watch = watching[ticker]
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


# ── Free extras: earnings date and news headlines ─────────────────────────
# Both come off the same yfinance feed as everything else here, so they cost
# nothing per call. Both are also the first place in this file where THIRD-PARTY
# text (headlines, publisher names, URLs) reaches the response, so both are
# whitelist-mapped rather than passed through: yfinance returns a large nested
# blob whose shape changes between releases, and forwarding it wholesale would
# ship whatever the upstream happens to include today.

_NEWS_LIMIT = 8
_TITLE_CAP = 200


def _first(d: dict, *names):
    """Return the first present, non-empty key. yfinance renamed several of
    these between versions, and a missing headline is not worth an exception."""
    for n in names:
        v = d.get(n)
        if v:
            return v
    return None


def get_next_earnings(ticker: str) -> dict:
    """Next scheduled earnings date, best-effort.

    Yahoo publishes this two different ways — `calendar` (which may carry a
    date RANGE when only the week is known) and `get_earnings_dates()` — and
    either can be missing entirely for a given symbol. A missing date is a
    normal outcome, not an error, so it returns None rather than raising.
    """
    try:
        t = validate_ticker(ticker)
    except ValueError as exc:
        return {"ticker": ticker, "error": str(exc)}

    try:
        tk = yf.Ticker(t)
        date = None

        try:
            cal = tk.calendar
            if isinstance(cal, dict):
                v = _first(cal, "Earnings Date", "EarningsDate")
                if isinstance(v, (list, tuple)) and v:
                    v = v[0]
                date = v
        except Exception:  # noqa: BLE001 — one shape failing is not fatal
            pass

        if date is None:
            try:
                df = tk.get_earnings_dates(limit=8)
                if df is not None and not df.empty:
                    import pandas as pd  # noqa: PLC0415 — already a yfinance dep
                    now = pd.Timestamp.now(tz=df.index.tz)
                    future = df[df.index >= now]
                    if not future.empty:
                        date = future.index.min()
            except Exception:  # noqa: BLE001
                pass

        iso = None
        if date is not None:
            try:
                iso = date.date().isoformat() if hasattr(date, "date") else str(date)[:10]
            except Exception:  # noqa: BLE001
                iso = None

        return {"ticker": t, "next_earnings": iso}

    except Exception as exc:  # noqa: BLE001
        logger.debug("get_next_earnings: error for '%s' (type=%s): %s",
                     ticker, type(exc).__name__, exc)
        return {"ticker": ticker, "next_earnings": None}


def get_news(ticker: str, limit: int = _NEWS_LIMIT) -> dict:
    """Recent headlines, whitelist-mapped to four fields.

    Every value here is written by someone else. Two rules follow:

      * Only title, publisher, link and published_at survive; everything else
        in the upstream payload is dropped.
      * A link is kept ONLY if it parses as http or https. Dropping a
        `javascript:` or `data:` URL at the boundary means the frontend is not
        the only thing standing between a hostile URL and an anchor's href.

    Newer yfinance nests the interesting fields under `content`, older versions
    keep them flat; both shapes are read.
    """
    try:
        t = validate_ticker(ticker)
    except ValueError as exc:
        return {"ticker": ticker, "error": str(exc)}

    try:
        from urllib.parse import urlparse  # noqa: PLC0415

        raw = yf.Ticker(t).news or []
        items = []

        for entry in raw[: max(1, int(limit))]:
            if not isinstance(entry, dict):
                continue
            body = entry.get("content") if isinstance(entry.get("content"), dict) else entry

            title = _first(body, "title", "headline")
            if not title:
                continue

            link = _first(body, "link", "canonicalUrl", "clickThroughUrl") or ""
            if isinstance(link, dict):
                link = link.get("url") or ""
            try:
                if urlparse(str(link)).scheme not in ("http", "https"):
                    link = ""
            except Exception:  # noqa: BLE001
                link = ""

            pub = _first(body, "publisher", "provider") or ""
            if isinstance(pub, dict):
                pub = pub.get("displayName") or pub.get("name") or ""

            items.append({
                "title":        str(title)[:_TITLE_CAP],
                "publisher":    str(pub)[:80],
                "link":         str(link)[:500],
                "published_at": str(_first(body, "pubDate", "providerPublishTime",
                                           "displayTime") or "")[:40],
            })

        return {"ticker": t, "items": items}

    except Exception as exc:  # noqa: BLE001
        logger.debug("get_news: error for '%s' (type=%s): %s",
                     ticker, type(exc).__name__, exc)
        return {"ticker": t if "t" in dir() else ticker, "items": []}
