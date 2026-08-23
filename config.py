"""
config.py — Your entire investment framework as data.

This file is the source of truth. Edit the Brent levels and portfolio
here; the agent and system prompt read from it automatically.
"""

import os

# ── BRENT CRUDE DEPLOYMENT FRAMEWORK ─────────────────────────────────────
# This is the macro gate for ALL position decisions.
# Adjust the price thresholds to match your actual framework.

BRENT_LEVELS = {
    "aggressive_buy": {
        "range": (0, 65),
        "signal": "AGGRESSIVE BUY",
        "action": "Deploy 25% of cash reserve. Oil crash = macro gift for tech.",
        "emoji": "🟢",
    },
    "add": {
        "range": (65, 72),
        "signal": "ADD",
        "action": "Add to existing positions. Use 10-15% of reserve.",
        "emoji": "🟡",
    },
    "hold": {
        "range": (72, 80),
        "signal": "HOLD",
        "action": "Hold current positions. No new buys until oil direction clears.",
        "emoji": "⚪",
    },
    "cautious": {
        "range": (80, 88),
        "signal": "CAUTION",
        "action": "Trim 10-15% of each position. Oil rising = margin pressure incoming.",
        "emoji": "🟠",
    },
    "defensive": {
        "range": (88, 9999),
        "signal": "DEFENSIVE",
        "action": "Raise cash. No new positions. Hedge if possible.",
        "emoji": "🔴",
    },
}

# ── ACTIVE US PORTFOLIO ───────────────────────────────────────────────────
# Placeholder seed data — safe to publish. Put your real positions in
# config_local.py (gitignored) instead of editing this block; see the
# override import at the bottom of this file.

MY_PORTFOLIO = {
    "AAPL": {
        "shares": 10,
        "avg_cost": 150.00,
        "tranches": [150.00, 135.00, 120.00],   # next buys at these levels
        "thesis": "Example placeholder position — replace via config_local.py.",
        "stop_loss": 110.00,
        "trim_at": 220.00,
        "sector": "Consumer Tech",
    },
    "MSFT": {
        "shares": 5,
        "avg_cost": 300.00,
        "tranches": [300.00, 270.00, 240.00],
        "thesis": "Example placeholder position — replace via config_local.py.",
        "stop_loss": 220.00,
        "trim_at": 450.00,
        "sector": "Enterprise SaaS / Cloud",
    },
}

# ── WATCHLIST (not held yet, monitoring for entry) ────────────────────────
# Same rule — placeholder only. Real watchlist entries belong in
# config_local.py.

WATCHLIST = {
    "GOOGL": {
        "tranches": [140.00, 125.00, 110.00],
        "thesis": "Example placeholder watchlist entry — replace via config_local.py.",
        "brent_gated": False,
        "exchange": "NASDAQ",
        "sector": "Technology",
    },
}

# ── GEOPOLITICS → PORTFOLIO TRANSMISSION MAP ─────────────────────────────
# When a geo event fires, check which positions it actually affects.
# Placeholder — matches the sample tickers above. Your real mapping belongs in
# config_local.py alongside MY_PORTFOLIO/WATCHLIST.

GEO_TRANSMISSION = {
    "middle_east_conflict":      ["all — via Brent crude spike → framework signal change"],
    "us_china_tech_tension":     ["AAPL (supply chain exposure)", "MSFT (cloud demand neutral)"],
    "russia_ukraine_escalation": ["all — via energy prices → Brent gate"],
    "us_fed_hawkish_pivot":      ["all growth equities — multiple compression risk"],
    "ai_export_controls":        ["MSFT", "GOOGL"],
}

# ── GEO EVENT LIBRARY — auto-fill source for the terminal's geo panel ────────
# GEO_TRANSMISSION above is what you wrote by hand — it always wins and is
# never touched. This library exists so the panel isn't empty just because you
# haven't hand-mapped every ticker yet: for any event below that ISN'T already
# a key in GEO_TRANSMISSION, api.py checks its "keywords" against the
# sector string on each of your actual MY_PORTFOLIO / WATCHLIST positions and
# auto-generates the transmission entry from whatever matches. No live fetch,
# no news feed, nothing sent anywhere — pure in-memory matching against config
# you already maintain. Events with zero matches are simply omitted, so the
# panel stays scoped to positions you actually hold or watch.
#
# "affects_all": True means the event transmits through the Brent gate itself
# rather than through any one sector (mirrors GEO_TRANSMISSION's own "all —"
# entries above) — it's shown whenever the book isn't empty, regardless of
# sector. Everything else matches on "keywords": each sector string is split
# into whole words and a keyword matches if some word STARTS WITH it, so
# "Technology" / "Semiconductors" / "Consumer Tech" all match "tech" /
# "semiconductor" without spelling out every inflection. This is prefix
# matching, not a bare substring check — "Fintech" does NOT match "tech",
# because "fintech" does not start with "tech" (a raw `"tech" in sector`
# check would wrongly say it did). Keywords of two characters or fewer, like
# "ai", require an exact whole-word match instead, since as a prefix "ai"
# would match "Airlines" or "Aircraft".

GEO_EVENT_LIBRARY = {
    "middle_east_conflict": {
        "affects_all": True,
        "note": "via Brent crude spike → framework signal change",
    },
    "russia_ukraine_escalation": {
        "affects_all": True,
        "note": "via energy prices → Brent gate",
    },
    "us_fed_hawkish_pivot": {
        "affects_all": True,
        "note": "multiple compression risk across growth equities",
    },
    "us_china_tech_tension": {
        "keywords": ["tech", "semiconductor", "chip", "cloud", "saas", "hardware"],
        "note": "supply chain / China revenue exposure",
    },
    "ai_export_controls": {
        "keywords": ["tech", "semiconductor", "chip", "cloud", "saas", "ai"],
        "note": "chip and AI compute export exposure",
    },
    "india_geopolitics": {
        # Deliberately narrower than "tech"/"financial" — those are too
        # generic and would flag any tech or financial name as India-linked.
        # India-specific exposure is better signalled by outsourcing/generic-
        # pharma manufacturing, which is what these keywords target.
        "keywords": ["outsourcing", "bpo", "pharma", "generic", "healthcare"],
        "note": "India-linked revenue or services exposure",
    },
    "latam_political_risk": {
        # "e-commerce" was dropped — the tokenizer splits on the hyphen, so
        # a literal keyword containing one can never equal a whole word.
        # "commerce"/"ecommerce" cover both the hyphenated and single-word
        # spellings an operator might type.
        "keywords": ["energy", "financial", "bank", "material", "consumer",
                     "latam", "fintech", "commerce", "ecommerce"],
        "note": "LatAm commodity or political-risk exposure",
    },
    "eu_auto_regulation": {
        "keywords": ["auto", "industrial", "manufactur"],
        "note": "EU regulatory or emissions exposure",
    },
}

# ── API KEYS ───────────────────────────────────────────────────────────────
# Do NOT read secrets into module-level variables here.
# Storing os.getenv() results at module scope means the secret value lives
# in any process that imports config, accessible via config.PERPLEXITY_API_KEY.
# Instead, call os.getenv("KEY_NAME") at the point of use in each module.
# This keeps secret values off the module namespace entirely.

# ── Local override ────────────────────────────────────────────────────────
# config_local.py is gitignored and not part of this repo. If it exists,
# it replaces the placeholder MY_PORTFOLIO / WATCHLIST / GEO_TRANSMISSION
# above with your real ones — see config_local.py's own docstring.
try:
    from config_local import MY_PORTFOLIO, WATCHLIST, GEO_TRANSMISSION  # noqa: F401,F811
except ImportError:
    pass
