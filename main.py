#!/usr/bin/env python3
"""
main.py — Entry point for the stock research agent.

Usage:
  python main.py setup                         # connect a research engine    (free)
  python main.py PLTR                          # research brief + bull/bear   (~$0.05)
  python main.py PLTR "is the thesis intact?"  # specific question            (~$0.05)
  python main.py scan                          # brief for every holding      (~$0.04 each)
  python main.py brent                         # oil framework signal         (free)

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
        run_wizard()
        return

    if command == "SCAN":
        cmd_scan()
        return

    if command == "BRENT":
        cmd_brent()
        return

    # Otherwise treat first arg as a ticker
    ticker   = command
    question = " ".join(args[1:]) if len(args) > 1 else None
    cmd_research(ticker, question)


if __name__ == "__main__":
    main()
