"""
tools/oil_price.py — Brent crude live price + framework signal.

This is the macro gate. The agent calls this before making any
position recommendation. Pure Python — no AI involved here.

Hardening applied:
  Task 1 — validate_ticker() guards the BZ=F symbol.
  Task 2 — try/except around every external call; always returns a dict.
"""

import logging

import yfinance as yf

from config import BRENT_LEVELS
from tools.validator import validate_ticker

logger = logging.getLogger(__name__)

_BRENT_TICKER = "BZ=F"


def get_brent_price() -> float:
    """
    Fetch current Brent crude price via Yahoo Finance.
    BZ=F = Brent crude futures (front month contract).

    Returns:
        float price on success, None on any failure.
    """
    try:
        # validate_ticker accepts BZ=F (futures format)
        validate_ticker(_BRENT_TICKER)
    except ValueError as exc:
        logger.error("get_brent_price: ticker validation failed: %s", exc)
        return None

    try:
        brent = yf.Ticker(_BRENT_TICKER)
        hist = brent.history(period="1d")

        if hist is not None and not hist.empty:
            close_val = hist["Close"].iloc[-1]
            if close_val is not None:
                return round(float(close_val), 2)

        # Fallback to info dict
        info = brent.info
        if info is None:
            logger.debug(
                "get_brent_price: brent.info returned None (type=%s)", type(info)
            )
            return None

        price = info.get("regularMarketPrice") or info.get("previousClose")
        if price is not None:
            return round(float(price), 2)

        logger.debug(
            "get_brent_price: no price field found in info dict (keys=%s)",
            list(info.keys()) if isinstance(info, dict) else type(info),
        )
        return None

    except Exception as exc:  # noqa: BLE001
        # Network errors, yfinance internal errors, etc.
        err_type = type(exc).__name__
        logger.error("get_brent_price: unexpected error (%s): %s", err_type, exc)
        return None


def get_brent_signal() -> dict:
    """
    Returns current Brent price + the framework signal.

    Called by the agent as a tool and by the /brent and /health API routes.
    Always returns a dict — never raises.

    Example return:
    {
        "brent_price": 74.30,
        "zone": "hold",
        "signal": "HOLD",
        "action": "Hold current positions. No new buys.",
        "emoji": "⚪",
        "gate_open": False,
        "framework_note": "Brent at $74.30 is in the HOLD zone (72–80). ..."
    }
    """
    try:
        price = get_brent_price()
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders
        logger.error("get_brent_signal: get_brent_price raised unexpectedly: %s", exc)
        price = None

    if price is None:
        return {
            "error": "Could not fetch Brent price. Check internet connection.",
            "gate_open": False,   # Fail safe — don't deploy if we can't check
        }

    # Walk through the levels and find the matching zone
    try:
        for zone_name, data in BRENT_LEVELS.items():
            low, high = data["range"]

            if low <= price < high:
                gate_open = price < 80   # Hard rule: no new positions above $80

                return {
                    "brent_price": price,
                    "zone": zone_name,
                    "signal": data["signal"],
                    "emoji": data["emoji"],
                    "action": data["action"],
                    "gate_open": gate_open,
                    "framework_note": (
                        f"Brent at ${price} is in the {data['signal']} zone (${low}–${high}). "
                        f"{'New positions: ALLOWED.' if gate_open else 'New positions: BLOCKED (Brent > $80).'} "
                        f"{data['action']}"
                    ),
                }
    except Exception as exc:  # noqa: BLE001
        logger.error("get_brent_signal: error walking BRENT_LEVELS: %s", exc)
        return {
            "error": "unexpected response format",
            "brent_price": price,
            "gate_open": False,
        }

    # Price somehow outside all defined ranges
    return {
        "brent_price": price,
        "zone": "unknown",
        "signal": "CHECK MANUALLY",
        "gate_open": False,
        "framework_note": (
            f"Brent at ${price} is outside defined framework ranges. Review manually."
        ),
    }
