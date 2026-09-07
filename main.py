#!/usr/bin/env python3
"""
main.py — Entry point for the stock research agent.

Usage:
  python main.py setup                         # connect a research engine    (free)
  python main.py PLTR                          # research brief + bull/bear   (~$0.05)
  python main.py PLTR "is the thesis intact?"  # specific question            (~$0.05)
  python main.py scan                          # brief for every holding      (~$0.04 each)
  python main.py brent                         # oil framework signal         (free)
  python main.py hrkey                         # print the HR preview key     (free)

Research runs on Perplexity, or Groq when only GROQ_API_KEY is set.
Everything except `setup` and `brent` spends API credits.
"""

# Load .env before anything else. Must come after the module docstring —
# any statement above it would demote the docstring to a bare expression and
# `python main.py` with no args would print None instead of this usage text.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional — fall back to real env vars

import sys

# ── Windows UTF-8 fix ──────────────────────────────────────────────────────
# The default Windows terminal codec (cp1252) can't encode emoji characters
# used in the Brent signal output (🔴, 🟡, ⚪, etc.).
# Reconfigure stdout/stderr to UTF-8 only when needed.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf-8-sig"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tools.perplexity_research import run_perplexity_research, run_research_module
from tools.oil_price import get_brent_signal
from tools.hr_key import derive_hr_key
from config import MY_PORTFOLIO, WATCHLIST


def print_separator(title: str = ""):
    print("\n" + "=" * 60)
    if title:
        print(f"  {title}")
        print("=" * 60)


def cmd_research(ticker: str, question: str = None):
    """Run full research on a single ticker — report plus bull/bear debate."""
    print_separator(f"RESEARCH: {ticker}")
    print("Running — live web search, typically 30-60s...\n")

    result = run_perplexity_research(ticker, question)
    if "error" in result:
        print(f"Error: {result['error']}")
        return

    print(result.get("report", ""))
    print_separator("BULL CASE")
    print(result.get("bull_case", ""))
    print_separator("BEAR CASE")
    print(result.get("bear_case", ""))


def cmd_scan():
    """Quick thesis check across all portfolio positions.

    Uses the single `report` module rather than the full pipeline — a scan is
    6 tickers, and the debate plus the other modules would triple the cost for
    a question that only needs the brief.
    """
    print_separator("PORTFOLIO SCAN")
    print("Checking all positions...\n")

    for ticker in MY_PORTFOLIO:
        print(f"\n{'─' * 40}")
        print(f"  {ticker}")
        print('─' * 40)
        result = run_research_module(
            ticker,
            "report",
            question="One paragraph: is my thesis intact? Any new risks?",
        )
        print(result.get("output") or f"Error: {result.get('error', 'research failed')}")


def cmd_hrkey():
    """Print the current HR preview key, derived from AGENT_SECRET.

    Offline — no server, no network call, no database. This is the CLI twin
    of GET /api/admin/hr-key (require_owner): both call derive_hr_key() on
    whatever AGENT_SECRET this process's .env resolves to, so the two agree
    by construction. The key is never generated ahead of time or written
    anywhere — it exists only as this deterministic function of the current
    password, which is also why rotating the password produces a different
    key with no migration step.
    """
    import os
    secret = os.getenv("AGENT_SECRET") or ""
    if not secret:
        print("AGENT_SECRET is not set — run `python main.py setup` first, "
              "or check your .env file.")
        return
    print_separator("HR PREVIEW KEY")
    print(f"\n  {derive_hr_key(secret)}\n")
    print("  Give this to a reviewer to sign in with. It carries owner-level")
    print("  READ access (your book, charts, history) and NO write access,")
    print("  and is good for 5 research runs total, in either mode, on any")
    print("  ticker. Rotating AGENT_SECRET invalidates it and mints a new one.")


def cmd_brent():
    """Show current Brent price and framework signal."""
    print_separator("BRENT CRUDE SIGNAL")
    signal = get_brent_signal()

    if "error" in signal:
        print(f"Error: {signal['error']}")
        return

    print(f"\n  Brent price:  ${signal['brent_price']}")
    print(f"  Zone:         {signal['emoji']} {signal['signal']}")
    print(f"  Action:       {signal['action']}")
    print(f"  Gate open:    {'Yes — new positions allowed' if signal['gate_open'] else 'No — Brent > $80, no new positions'}")


def main():
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        return

    command = args[0].upper()

    # Special commands
    if command == "SETUP":
        from tools.setup_wizard import run_wizard
        # run_wizard() makes several blocking input() calls. Ctrl+C there
        # raises KeyboardInterrupt; a closed/piped stdin (e.g.
        # `python main.py setup < /dev/null`) raises EOFError. Neither is
        # caught inside the wizard itself, so left unhandled here they'd
        # dump a raw traceback instead of the clean "cancelled" every other
        # entry point in this project gives (run_perplexity_research,
        # run_research_module, get_price_and_fundamentals — all documented
        # "always returns, never raises"). This is that same discipline
        # reaching the one place it didn't before: the CLI's own top level.
        try:
            run_wizard()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
        return

    if command == "SCAN":
        cmd_scan()
        return

    if command == "BRENT":
        cmd_brent()
        return

    if command == "HRKEY":
        cmd_hrkey()
        return

    # Otherwise treat first arg as a ticker
    ticker   = command
    question = " ".join(args[1:]) if len(args) > 1 else None
    cmd_research(ticker, question)


if __name__ == "__main__":
    main()
