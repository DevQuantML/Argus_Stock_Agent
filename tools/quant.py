"""
tools/quant.py — Quantitative metrics computed from yfinance data.

Pure Python math — no new API, no new packages beyond what is already used.

Inputs come from two places, in this order of trust:
  1. the annual statements (tools/fundamentals.py) — authoritative
  2. the yfinance `info` dict — convenient, but wrong often enough to matter

That order is not stylistic. `info["operatingIncome"]` is never populated on the
tickers this portfolio holds, and `info["totalStockholdersEquity"]` is not an
`info` key at all, so ROIC could never compute. `info["freeCashflow"]` disagrees
with the cash flow statement — MELI reported -$4.1B there against +$10.77B in
the statement, which made the engine call a business generating ten billion
dollars of cash "pre-profitability", and skip its DCF on that basis. Statements
first, `info` only as a declared fallback.

Metrics:
  DCF            — 5-year discounted cash flow → intrinsic value per share
  Implied growth — reverse DCF: the growth the current price already assumes
  Sensitivity    — 3×3 grid over discount rate and growth
  ROIC           — Return on Invested Capital (quality gate)
  FCF Yield      — free cash flow / market cap
  PEG Ratio      — forward PE / earnings growth rate
  Sharpe Ratio   — risk-adjusted 1-year return (annualised)
  Beta           — market sensitivity (from yfinance)

Every metric records where its inputs came from in `sources`, down to the fiscal
period. A number the user cannot trace is a number they have to take on faith,
which is the opposite of what a deterministic engine is for.

A metric that does NOT compute records why, in `metric_status`, with the same
seriousness. "N/A" on its own is the least useful thing a research tool can
say: the reader cannot tell whether to wait, to look elsewhere, or to conclude
something about the business. See the four natures below — they exist because
those three reactions are different, and the old single missing_metrics list
could not tell them apart. It produced "Forward P/E not reported" on MELI,
whose forward P/E is reported perfectly well; the actual reason was that its
earnings are contracting, which is a fact about the company, not the feed.

Security:
  Task 1 — validate_ticker() before any yfinance call
  Task 2 — _safe_float() on every field; division-by-zero guards everywhere
           DCF only runs on positive FCF; growth rate capped 0–30%
           Always returns a dict, never raises
"""

import logging
import math

import yfinance as yf

from tools import fx
from tools.fundamentals import fcf_cagr, get_statements, provenance_of, value_of
from tools.validator import validate_ticker

logger = logging.getLogger(__name__)

# ── Why a metric is absent ─────────────────────────────────────────────────
# Four natures, because they ask the reader to do four different things.
#
#   TRANSIENT      wait — the input is missing from the feed right now
#   STRUCTURAL     read it — the business itself is why the metric won't form
#   UNSUPPORTED    stop — the metric is undefined for this kind of instrument
#   METHODOLOGICAL trust the refusal — it would compute, but it would mislead
#
# Only TRANSIENT is a data problem. The other three are answers.
TRANSIENT      = "transient"
STRUCTURAL     = "structural"
UNSUPPORTED    = "unsupported"
METHODOLOGICAL = "methodological"

# Instruments with no company behind them. A fund's "ROIC" is not a hard
# number to obtain, it is a category error, and saying "Yahoo did not return
# it" invites the reader to retry forever.
_NON_OPERATING_QUOTE_TYPES = {
    "ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "OPTION",
}

# The one Financial Services industry that begins with "Insurance" and is not
# an underwriter. Yahoo files every carrier as "Insurance - <line>" — the four
# labels seen live on 2026-08-11 are Property & Casualty, Life, Diversified and
# Reinsurance — and the broking houses as "Insurance Brokers". So a prefix match
# on "Insurance" alone would take AJG, AON, BRO and WTW down with the carriers.
# It must not: a broker earns commission on policies it does not carry, holds no
# reserves and no float, and its free cash flow is a real valuation input —
# AJG 2.8%, AON 4.3%, WTW 4.9%, BRO 5.8%, against 13.5–27.8% for the carriers.
#
# Excluding the broker rather than listing the carriers is deliberate: an
# underwriting line this list had never seen would otherwise be handed a DCF,
# and being wrongly declined with a stated reason is the cheaper error.
#
# Matched as a prefix rather than an equality so a re-spelling on Yahoo's side
# ("Insurance Brokerage", "Insurance Brokers & Services") keeps the exclusion.
_INSURANCE_BROKING_PREFIX = "insurance broker"

_SHARPE_MIN_DAYS = 30   # daily bars needed before an annualised Sharpe means anything

# ── DCF constants ──────────────────────────────────────────────────────────
_WACC              = 0.10   # fallback discount rate when beta is unavailable
_TERMINAL_GROWTH   = 0.03   # 3% perpetuity growth (≈ long-run GDP)
_PROJECTION_YEARS  = 5
_MAX_GROWTH_RATE   = 0.30   # cap at 30% — no company compounds FCF forever
_RISK_FREE_RATE    = 0.05   # 5% ≈ current US T-bill rate
_EQUITY_RISK_PREM  = 0.05   # equity risk premium for the CAPM discount rate
_WACC_FLOOR        = 0.07   # keep CAPM output inside a defensible band
_WACC_CEILING      = 0.15
_MAX_PEG_GROWTH    = 1.00   # 100%/yr — beyond this, reported growth is almost
                            # always a base-effect artifact (a company recently
                            # turned profitable, or is recovering from a near-
                            # zero prior-year base) rather than a durable rate.
                            # PEG at that growth degenerates toward 0.0 and
                            # reads as "deeply undervalued" when the ratio is
                            # just meaningless — SKHY's 1273%/yr earnings
                            # growth produced PEG 0.0.
_MIN_MEANINGFUL_PEG = 0.05  # the same degeneracy reached from the numerator:
                            # a low forward multiple against growth just under
                            # the cap rounds to 0.0 and reads as the strongest
                            # possible buy. Below this the ratio is degenerate,
                            # not cheap.


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe_float(value, default=None):
    """Return float or default if value is None / unconvertible."""
    if value is None:
        return default
    try:
        f = float(value)
        # Reject NaN and Inf — yfinance occasionally returns these
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _pct(value):
    """Format a float as a percentage string, or 'N/A'."""
    if value is None:
        return "N/A"
    return f"{value:.1f}%"


def _flag(condition: bool, msg_true: str, msg_false: str) -> str:
    return msg_true if condition else msg_false


def _norm_industry(*values) -> str:
    """First non-empty industry label, lowercased with punctuation flattened.

    Yahoo ships the same industry under two spellings — `info["industry"]` is
    "Insurance - Property & Casualty" and `info["industryKey"]` is
    "insurance-property-casualty". Collapsing both to "insurance property
    casualty" means the test that reads them needs one form, not two, and it
    survives a dash becoming an en-dash or the spacing around it changing.
    """
    for value in values:
        text = str(value or "").strip()
        if text:
            flat = "".join(c if c.isalnum() else " " for c in text.lower())
            return " ".join(flat.split())
    return ""


# ── Absence bookkeeping ────────────────────────────────────────────────────

class _Gaps:
    """Collects, per metric, whether it computed and — if not — why.

    Holds the legacy `missing_metrics` list in step with the structured record
    so nothing downstream has to change at once. `reason` is one sentence the
    UI prints verbatim; `resolves` says what would make the metric appear, and
    is the part that tells the reader whether waiting is worth anything.
    """

    def __init__(self) -> None:
        self.status: dict[str, dict] = {}
        self.missing: list[str] = []

    def ok(self, metric: str, **detail) -> None:
        entry = {"state": "computed"}
        entry.update({k: v for k, v in detail.items() if v is not None})
        self.status[metric] = entry

    def gap(self, metric: str, nature: str, reason: str, resolves: str, **detail) -> None:
        entry = {
            "state":    "unavailable",
            "nature":   nature,
            "reason":   reason,
            "resolves": resolves,
        }
        entry.update({k: v for k, v in detail.items() if v is not None})
        self.status[metric] = entry
        if metric not in self.missing:
            self.missing.append(metric)

    def natures(self) -> set[str]:
        return {e["nature"] for e in self.status.values() if e["state"] == "unavailable"}


# ── Discount rate ──────────────────────────────────────────────────────────

def _compute_wacc(beta: float | None) -> tuple[float, dict]:
    """CAPM discount rate from beta, clamped to a defensible band.

    A flat 10% for every name prices a utility and a hypergrowth software
    company identically. Beta is already fetched for its own metric, so the
    cost of using it here is zero.
    """
    if beta is None:
        return _WACC, {"method": "fallback", "reason": "beta not reported", "beta": None}

    raw = _RISK_FREE_RATE + beta * _EQUITY_RISK_PREM
    wacc = min(max(raw, _WACC_FLOOR), _WACC_CEILING)
    return wacc, {
        "method":   "capm",
        "beta":     beta,
        "risk_free": _RISK_FREE_RATE,
        "equity_risk_premium": _EQUITY_RISK_PREM,
        "uncapped": round(raw, 4),
        "clamped":  raw != wacc,
    }


# ── DCF ────────────────────────────────────────────────────────────────────

def _intrinsic(fcf: float, growth: float, wacc: float,
               net_debt: float, shares: float) -> float | None:
    """Intrinsic value per share for one set of assumptions.

    Shared by the forward DCF, the reverse DCF and the sensitivity grid so all
    three are guaranteed to be the same model.
    """
    if wacc <= _TERMINAL_GROWTH or shares <= 0:
        return None

    pv = 0.0
    fcf_t = fcf
    for year in range(1, _PROJECTION_YEARS + 1):
        fcf_t *= (1 + growth)
        pv += fcf_t / (1 + wacc) ** year

    terminal = fcf_t * (1 + _TERMINAL_GROWTH) / (wacc - _TERMINAL_GROWTH)
    pv += terminal / (1 + wacc) ** _PROJECTION_YEARS

    value = (pv - net_debt) / shares
    return None if math.isnan(value) or math.isinf(value) else value


def _compute_dcf(fcf: float, growth_rate: float, wacc: float,
                 net_debt: float, shares: float) -> dict | None:
    """5-year DCF → intrinsic value per share, with every assumption recorded."""
    g = min(max(growth_rate, 0.0), _MAX_GROWTH_RATE)
    value = _intrinsic(fcf, g, wacc, net_debt, shares)
    if value is None:
        return None

    return {
        "intrinsic_value": round(value, 2),
        "assumptions": {
            "fcf_used":         round(fcf),
            "growth_rate_used": round(g, 4),
            "growth_capped":    growth_rate > _MAX_GROWTH_RATE,
            "growth_uncapped":  round(growth_rate, 4),
            "discount_rate":    round(wacc, 4),
            "terminal_growth":  _TERMINAL_GROWTH,
            "projection_years": _PROJECTION_YEARS,
            "net_debt_used":    round(net_debt),
            "shares_used":      round(shares),
        },
    }


def _implied_growth(price: float, fcf: float, wacc: float,
                    net_debt: float, shares: float) -> float | None:
    """Reverse DCF — the 5-year FCF growth the current price already assumes.

    This is the more useful direction. "DCF says $26 against a $162 price" tells
    you the model disagrees with the market; "the market is pricing 47% annual
    FCF growth, and the trailing three years delivered 125%" tells you what you
    would have to believe, which is something you can actually assess.

    Intrinsic value rises monotonically with growth for positive FCF, so a
    bisection converges. Returns None when the price sits outside the bracket
    rather than reporting a bound as if it were a solution.
    """
    lo, hi = -0.50, 1.00

    v_lo = _intrinsic(fcf, lo, wacc, net_debt, shares)
    v_hi = _intrinsic(fcf, hi, wacc, net_debt, shares)
    if v_lo is None or v_hi is None or not (v_lo <= price <= v_hi):
        return None

    for _ in range(80):
        mid = (lo + hi) / 2
        v = _intrinsic(fcf, mid, wacc, net_debt, shares)
        if v is None:
            return None
        if v < price:
            lo = mid
        else:
            hi = mid

    rate = (lo + hi) / 2
    return None if math.isnan(rate) or math.isinf(rate) else round(rate * 100, 1)


def _sensitivity(fcf: float, growth: float, wacc: float, net_debt: float,
                 shares: float, price: float | None) -> list[dict]:
    """3×3 grid over discount rate (±2pts) and growth (±5pts).

    One scenario reported alone reads as a verdict. Nine of them read as what a
    DCF actually is — a function of two assumptions the user is free to reject.

    Growth is deliberately NOT clamped to _MAX_GROWTH_RATE here. That cap stops
    the headline projection running away on a trailing rate like MELI's 63%;
    applying it inside the grid collapsed the top column back onto the centre,
    so the table showed 25% / 30% / 30% with two identical columns and implied
    that 35% growth changes nothing. A negative growth point is legitimate too —
    a business whose cash flow shrinks is a scenario worth pricing. Only the
    discount-rate floor survives, because w <= terminal growth is division by a
    non-positive number, not a policy choice.
    """
    grid = []
    for w in (wacc - 0.02, wacc, wacc + 0.02):
        for g in (growth - 0.05, growth, growth + 0.05):
            w_c = max(w, _TERMINAL_GROWTH + 0.01)
            value = _intrinsic(fcf, g, w_c, net_debt, shares)
            if value is None:
                continue
            cell = {
                "discount_rate": round(w_c, 4),
                "growth_rate":   round(g, 4),
                "intrinsic":     round(value, 2),
            }
            if price and price > 0:
                cell["upside_pct"] = round(((value - price) / price) * 100, 1)
            grid.append(cell)
    return grid


# ── Sharpe ─────────────────────────────────────────────────────────────────

def _compute_sharpe(ticker_obj) -> tuple[float | None, dict]:
    """Annualised Sharpe from 1 year of daily closes, plus why it failed.

    Returns (sharpe, meta). The meta half exists because "Not enough price
    history" is not actionable and "22 of the 30 sessions needed — this is a
    recently listed line" is. SKHY is the live case: its ADR had 22 daily bars,
    so the shortfall is eight sessions and it clears itself.
    """
    meta: dict = {"required_days": _SHARPE_MIN_DAYS}
    try:
        hist = ticker_obj.history(period="1y")
        if hist is None or hist.empty or "Close" not in hist:
            meta.update(available_days=0, cause="no_history")
            return None, meta

        close = hist["Close"].dropna()
        meta["available_days"] = len(close)

        if len(close) < _SHARPE_MIN_DAYS:
            meta["cause"] = "short_history"
            return None, meta

        daily_returns = close.pct_change().dropna()
        if len(daily_returns) < 20:
            meta["cause"] = "short_history"
            return None, meta

        annual_return = float(daily_returns.mean()) * 252
        annual_vol    = float(daily_returns.std()) * (252 ** 0.5)

        if annual_vol == 0:
            meta["cause"] = "zero_volatility"
            return None, meta

        sharpe = (annual_return - _RISK_FREE_RATE) / annual_vol
        if math.isnan(sharpe) or math.isinf(sharpe):
            meta["cause"] = "not_finite"
            return None, meta

        return round(sharpe, 2), meta

    except Exception as exc:  # noqa: BLE001
        logger.debug("_compute_sharpe: error (type=%s): %s", type(exc).__name__, exc)
        meta["cause"] = "fetch_failed"
        return None, meta


# ── Effective tax rate ─────────────────────────────────────────────────────

def _effective_tax_rate(stmts: dict, info: dict) -> tuple[float, str]:
    """Return (rate, source_label). Clamped 0–50%.

    Preference order: the rate the filings actually imply, then yfinance's
    pre-computed rate, then the US statutory rate as a last resort.
    """
    pretax = value_of(stmts, "pretax_income")
    tax    = value_of(stmts, "tax_provision")
    if pretax and pretax > 0 and tax is not None:
        return min(max(tax / pretax, 0.0), 0.5), "tax provision / pretax income"

    calc = value_of(stmts, "tax_rate_calc")
    if calc is not None:
        return min(max(calc, 0.0), 0.5), "statement tax rate"

    info_rate = _safe_float(info.get("effectiveTaxRate"))
    if info_rate is not None:
        return min(max(info_rate, 0.0), 0.5), "info.effectiveTaxRate"

    return 0.21, "US statutory default (21%)"


# ── Main public function ───────────────────────────────────────────────────

def get_quant_metrics(ticker: str) -> dict:
    """
    Compute quantitative metrics for a ticker.

    Statements are the primary source; the yfinance info dict fills gaps and is
    labelled as such in `sources`. Always returns a dict — never raises.
    """
    # ── Task 1: Validate ticker ────────────────────────────────────────────
    try:
        ticker = validate_ticker(ticker)
    except ValueError as exc:
        logger.warning("get_quant_metrics: invalid ticker: %s", exc)
        return {"ticker": ticker, "error": str(exc)}

    try:
        stock = yf.Ticker(ticker)

        try:
            info = stock.info
        except Exception as exc:
            logger.debug("get_quant_metrics: stock.info failed (type=%s): %s", type(exc).__name__, exc)
            return {"ticker": ticker, "error": "unexpected response format"}

        if not isinstance(info, dict) or not info:
            return {"ticker": ticker, "error": "unexpected response format"}

        stmts = get_statements(ticker)

        # ── Raw fields ─────────────────────────────────────────────────────
        market_cap     = _safe_float(info.get("marketCap"))
        shares         = _safe_float(info.get("sharesOutstanding"))
        price          = _safe_float(info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose"))
        rev_growth     = _safe_float(info.get("revenueGrowth"))
        forward_pe     = _safe_float(info.get("forwardPE"))
        raw_eps_growth = _safe_float(info.get("earningsGrowth"))
        eps_growth     = raw_eps_growth if raw_eps_growth else rev_growth
        growth_field   = "earnings growth" if raw_eps_growth else "revenue growth"
        beta           = _safe_float(info.get("beta"))

        quote_type = str(info.get("quoteType") or "").upper()
        is_fund    = quote_type in _NON_OPERATING_QUOTE_TYPES

        result = {
            "ticker":       ticker,
            "data_quality": "full",
            "quant_flags":  [],
            "sources":      {},
        }
        sources = result["sources"]
        gaps = _Gaps()

        # Whether the statements loaded at all decides, everywhere below,
        # between "the feed is short" and "the filing has no such line".
        fields = (stmts or {}).get("fields") or {}
        income_loaded  = any(k in fields for k in ("pretax_income", "tax_provision", "operating_income"))
        balance_loaded = any(k in fields for k in ("equity", "total_debt", "cash"))
        cashflow_loaded = any(k in fields for k in ("fcf", "operating_cf", "capex"))

        if not stmts and not is_fund:
            result["quant_flags"].append(
                "Annual statements unavailable — metrics fall back to Yahoo summary fields, "
                "which are less reliable for cash flow and operating income."
            )

        # ── Currency: convert rather than decline ──────────────────────────
        # An ADR files in its home currency and trades in another. SKHY files
        # in KRW and quotes in USD, so a KRW cash flow over a USD market cap
        # used to print a 2500% FCF yield — the engine's answer was to decline
        # both FCF yield and DCF and call the two scales incomparable. They are
        # comparable at a rate, and the rate comes from the feed already in use.
        # Five of twelve large caps probed hit this, so declining is not a
        # niche loss. ROIC, PEG, Sharpe and beta are drawn wholly from one side
        # and are untouched by any of it.
        market_ccy = info.get("currency")
        fin_ccy    = info.get("financialCurrency")
        ccy_mismatch = bool(market_ccy and fin_ccy and market_ccy != fin_ccy)
        fx_rate = None

        if ccy_mismatch:
            result["currency_mismatch"] = {"market": market_ccy, "financial": fin_ccy}
            conv = fx.rate(fin_ccy, market_ccy)
            if conv:
                fx_rate = conv["rate"]
                result["currency_mismatch"].update(
                    rate=fx_rate, pair=conv["pair"], origin=conv["origin"], resolved=True)
                sources["fx"] = conv
                result["quant_flags"].append(
                    f"Reports in {fin_ccy} but trades in {market_ccy} — statement cash flow "
                    f"converted at {fx_rate:.6g} {market_ccy}/{fin_ccy} so FCF yield and DCF "
                    f"stay on the same scale as the price."
                )
            else:
                result["currency_mismatch"]["resolved"] = False
                result["quant_flags"].append(
                    f"Reports in {fin_ccy} but trades in {market_ccy}, and the "
                    f"{fin_ccy}/{market_ccy} rate did not load — cash-flow valuation is "
                    f"held back until it does. ROIC and the risk metrics are unaffected."
                )

        def to_market(value: float | None) -> float | None:
            """Statement-currency money expressed in the trading currency."""
            if value is None:
                return None
            return value if fx_rate is None else value * fx_rate

        # A single sentence reused wherever a fund hits a company metric.
        fund_reason = (
            f"{ticker} is quoted as {quote_type.lower() or 'a fund'}, not an operating "
            f"company, so it files no financial statements."
        )
        fund_resolves = "Nothing will change this — the metric does not apply to this instrument."

        # ── Free cash flow: statement first, info second ───────────────────
        fcf = value_of(stmts, "fcf")
        if fcf is not None:
            prov = provenance_of(stmts, "fcf") or {}
            sources["fcf"] = {
                "origin": "cashflow statement",
                "row":    prov.get("row"),
                "period": prov.get("period"),
            }
        else:
            fcf = _safe_float(info.get("freeCashflow"))
            if fcf is not None:
                sources["fcf"] = {"origin": "info.freeCashflow",
                                  "note": "statement unavailable — summary field is less reliable"}

        # ── Balance sheet inputs ───────────────────────────────────────────
        equity     = value_of(stmts, "equity")
        total_debt = value_of(stmts, "total_debt")
        cash       = value_of(stmts, "cash")
        if total_debt is None:
            total_debt = _safe_float(info.get("totalDebt"), default=0.0)
        if cash is None:
            cash = _safe_float(info.get("totalCash"), default=0.0)
        net_debt = (total_debt or 0.0) - (cash or 0.0)

        op_income = value_of(stmts, "operating_income")

        # ── The financial-filer signatures ─────────────────────────────────
        # Two different institutions, two different tells, one conclusion: free
        # cash flow is not a valuation input here, so neither FCF yield nor a
        # DCF may be published. The sector label has to agree before anything is
        # withheld. On its own it would be far too broad — Visa, Mastercard and
        # the asset managers all sit under Financial Services and their free cash
        # flow is a perfectly good input (measured live at 3.2%, 3.3%, 1.9%).
        sector   = str(info.get("sector") or "").strip()
        industry = str(info.get("industry") or "").strip()
        is_financial_sector = sector.lower() == "financial services"

        # (a) The bank tell — an income statement that loads cleanly and carries
        # no operating-income line. That is how a deposit-taking institution
        # files: there is no "operations" line to separate from financing when
        # financing IS the operation. ROIC already declines on this signal below.
        #
        # The same fact disqualifies the cash-flow metrics. Yahoo does publish a
        # Free Cash Flow row for a bank, so the arithmetic succeeds — but for a
        # bank that row is dominated by period swings in loans, deposits and
        # trading books, which measure balance-sheet movement rather than cash
        # the owners could take out. JPM computed -15.5% and only escaped a DCF
        # because that figure is negative. BAC, same signature, computed +2.8%
        # and a full DCF at $23/share — confidently wrong rather than absent,
        # which is the shape this whole metric_status effort exists to remove.
        # The missing row is the signal; the sector is the corroboration that
        # stops an industrial with a feed gap being told its cash flow does not
        # apply to it.
        is_bank_filer = op_income is None and income_loaded and is_financial_sector

        # (b) The insurer tell — the industry label, because the bank tell does
        # not fire here. A carrier DOES file an operating-income row, so it slid
        # past (a) and priced itself: measured live on 2026-08-11, ALL returned a
        # 14.5% FCF yield and a $2,794 intrinsic value against a $270 share
        # price, PGR 13.9% and $2,281 against $214, MET 27.8% and $630 against
        # $97. An order of magnitude out, stated with total confidence.
        #
        # The cause is the same shape as the bank's. An insurer collects premiums
        # years before it pays the claims they cover, and that underwriting float
        # lands in operating cash flow while remaining money held for
        # policyholders. The free-cash-flow line therefore tracks reserve build,
        # not cash available to owners — so it must not be capitalised.
        #
        # See _INSURANCE_BROKING_PREFIX above for why brokers are carved out.
        norm_industry = _norm_industry(info.get("industry"), info.get("industryKey"))
        is_insurance_underwriter = (
            is_financial_sector
            and norm_industry.startswith("insurance")
            and not norm_industry.startswith(_INSURANCE_BROKING_PREFIX)
        )

        is_financial_filer = is_bank_filer or is_insurance_underwriter

        # Shared tail — both cash-flow metrics decline for the same reason, and
        # each filer names the tell it actually tripped. A carrier told its
        # income statement carries no operating-income line would be reading a
        # sentence that is false about it, and the whole point of metric_status
        # is that the engine states the branch it really took.
        if is_insurance_underwriter:
            filer_signature = (
                f"files as a {sector} company in {industry or 'insurance underwriting'} — "
                f"an insurance carrier, not a broker"
            )
            fin_cause = (
                "premiums are collected years before the claims they cover are paid, and that "
                "underwriting float runs through operating cash flow while still being money "
                "held for policyholders, so the free-cash-flow line measures reserve build "
                "rather than cash available to owners."
            )
            fin_resolves = (
                "Nothing here will change it — the metric does not describe this kind of "
                "business. Insurers are read on book value, return on equity and the "
                "combined ratio instead."
            )
        else:
            filer_signature = (
                f"files as a {sector} company and its income statement carries no "
                f"operating-income line — the bank signature"
            )
            fin_cause = (
                "operating cash flow is dominated by swings in loans, deposits and reserves, "
                "so the free-cash-flow line measures balance-sheet movement rather than cash "
                "available to owners."
            )
            fin_resolves = (
                "Nothing here will change it — the metric does not describe this kind of "
                "business. Banks are read on book value, return on equity and net interest "
                "margin instead."
            )

        # ── Discount rate ──────────────────────────────────────────────────
        wacc, wacc_meta = _compute_wacc(beta)
        sources["discount_rate"] = wacc_meta

        # ── ROIC ───────────────────────────────────────────────────────────
        # Statements only. The old code fell back to info["bookValue"], which is
        # book value PER SHARE — adding it to absolute net debt produced an
        # invested-capital figure off by the share count. It never fired because
        # operating income was always missing, but it must not come back.
        if is_fund:
            gaps.gap("roic", UNSUPPORTED, fund_reason, fund_resolves)
        elif op_income is None or equity is None:
            # Which input is missing, and whether the statement loaded at all,
            # are different diagnoses. JPM's income statement loads fine and
            # simply has no operating-income row — that is how banks file, and
            # no amount of retrying produces one.
            #
            # The sector has to agree before that sentence is printed. Gated on
            # the missing row alone, this branch told ANY filer short of an
            # operating-income line that the absence was "normal for banks and
            # insurers" — a confident explanation of the wrong business, which
            # is the same failure metric_status exists to remove, just in prose
            # rather than in a number. is_bank_filer carries the corroboration.
            if is_bank_filer:
                gaps.gap("roic", UNSUPPORTED,
                         "Yahoo's income statement for this filer carries no operating-income "
                         "line, which is normal for banks and insurers — their statements are "
                         "not structured around one.",
                         "Nothing here will change it; ROIC for this business needs a source "
                         "that models financials separately.",
                         sector=sector)
            elif op_income is None and income_loaded:
                # Statement present, row absent, and nothing says this is a
                # financial. Structural, not unsupported: ROIC is perfectly
                # well-defined for whatever this is — the line is just not
                # there under a label the engine knows.
                gaps.gap("roic", STRUCTURAL,
                         "The income statement loaded but carries no operating-income line "
                         "under any label this engine recognises, and this filer is not "
                         "classified as a financial.",
                         "Clears if the next annual filing reports operating income or EBIT "
                         "under a standard label.")
            elif equity is None and balance_loaded:
                gaps.gap("roic", STRUCTURAL,
                         "The balance sheet loaded but carries no shareholders-equity line "
                         "under any label this engine recognises.",
                         "Clears if the next annual filing reports equity under a standard label.")
            else:
                gaps.gap("roic", TRANSIENT,
                         "Yahoo returned no annual statements for this ticker, so operating "
                         "income and equity could not be read.",
                         "Usually a feed gap — the statement cache refreshes every 15 minutes.")
        else:
            tax, tax_source = _effective_tax_rate(stmts, info)
            nopat = op_income * (1 - tax)
            invested_capital = equity + net_debt
            if invested_capital and invested_capital > 0:
                roic = round((nopat / invested_capital) * 100, 1)
                result["roic"] = roic
                oi_prov = provenance_of(stmts, "operating_income") or {}
                eq_prov = provenance_of(stmts, "equity") or {}
                sources["roic"] = {
                    "operating_income": op_income,
                    "operating_income_period": oi_prov.get("period"),
                    "equity": equity,
                    "equity_period": eq_prov.get("period"),
                    "net_debt": round(net_debt),
                    "invested_capital": round(invested_capital),
                    "tax_rate": round(tax, 4),
                    "tax_rate_source": tax_source,
                    "origin": "income statement + balance sheet",
                }
                # A carrier keeps its ROIC — unlike its cash-flow metrics, the
                # inputs here are real: a filed operating-income row over filed
                # equity. Measured 2026-08-11 it lands within a few points of
                # reported ROE (PGR 31.1 vs 34.9, AIG 7.0 vs 7.2, CB 12.7 vs
                # 14.8), so it is approximately the right number — but under a
                # slightly wrong name. Invested capital is equity plus net debt,
                # which excludes the policyholder float that earned much of the
                # numerator, so the ratio reads as a lightly-levered ROE rather
                # than a return on operating capital. That matters because the
                # threshold flag below calls >15% "quality cleared"; a reader
                # taking that as operating efficiency would be reading it wrong.
                roic_caveat = (
                    "for an insurance carrier this reads as a lightly-levered return on equity, "
                    "not a return on operating capital: invested capital is equity plus net debt "
                    "and excludes the policyholder float that earned much of the operating income"
                ) if is_insurance_underwriter else None
                gaps.ok("roic", origin="income statement + balance sheet",
                        caveat=roic_caveat)
                if roic >= 15:
                    result["quant_flags"].append(f"ROIC {roic}% — quality threshold cleared (>15%)")
                else:
                    result["quant_flags"].append(f"ROIC {roic}% — below quality threshold (<15%); capital efficiency weak")
                if roic_caveat:
                    result["quant_flags"].append(f"ROIC {roic}% — {roic_caveat}")
            else:
                gaps.gap("roic", STRUCTURAL,
                         f"Invested capital works out to {round(invested_capital):,} — zero or "
                         f"negative, so return on it has no meaning.",
                         "Clears when equity plus net debt turns positive; typical of a business "
                         "carrying more cash and buybacks than book equity.")

        # ── FCF Yield ──────────────────────────────────────────────────────
        fcf_market = to_market(fcf)
        if is_fund:
            gaps.gap("fcf_yield", UNSUPPORTED, fund_reason, fund_resolves)
        elif is_financial_filer:
            # Ahead of every data-availability branch on purpose. Whether the
            # feed supplied a free-cash-flow row is beside the point when the
            # metric would not mean anything for this filer either way.
            gaps.gap("fcf_yield", UNSUPPORTED,
                     f"{ticker} {filer_signature}. For such a filer {fin_cause}",
                     fin_resolves,
                     sector=sector,
                     industry=industry or None)
        elif fcf is None:
            if cashflow_loaded:
                gaps.gap("fcf_yield", STRUCTURAL,
                         "The cash-flow statement loaded but carries no free-cash-flow line, "
                         "and Yahoo's summary field did not supply one either.",
                         "Clears if the next annual filing reports operating cash flow and "
                         "capital expenditure under standard labels.")
            else:
                gaps.gap("fcf_yield", TRANSIENT,
                         "Yahoo returned no cash-flow statement for this ticker.",
                         "Usually a feed gap — the statement cache refreshes every 15 minutes.")
        elif ccy_mismatch and fx_rate is None:
            gaps.gap("fcf_yield", TRANSIENT,
                     f"Cash flow is reported in {fin_ccy} against a {market_ccy} market cap, and "
                     f"the {fin_ccy}/{market_ccy} rate did not load, so the two cannot be put on "
                     f"one scale.",
                     "Clears as soon as the FX quote returns; the cash-flow figure itself is fine.")
        elif not market_cap:
            gaps.gap("fcf_yield", TRANSIENT,
                     "Yahoo did not report a market capitalisation for this listing, so there is "
                     "nothing to divide the cash flow by.",
                     "Usually returns on the next quote refresh.")
        else:
            fcf_yield = round((fcf_market / market_cap) * 100, 2)
            result["fcf_yield"] = fcf_yield
            gaps.ok("fcf_yield", converted=bool(fx_rate))
            if fcf_yield >= 4:
                result["quant_flags"].append(f"FCF yield {fcf_yield}% — attractive (>4%)")
            elif fcf_yield >= 2:
                result["quant_flags"].append(f"FCF yield {fcf_yield}% — modest; growth premium must be justified")
            elif fcf_yield > 0:
                result["quant_flags"].append(f"FCF yield {fcf_yield}% — thin; pure growth story, no income floor")
            else:
                result["quant_flags"].append(f"FCF yield {fcf_yield}% — negative or near-zero FCF; pre-profitability")

        # ── PEG Ratio ──────────────────────────────────────────────────────
        # Every branch here used to collapse into one silent `missing.append`,
        # and the UI filled the silence with a guess: "Forward P/E not reported,
        # or growth is a base-effect spike". On MELI both halves were false —
        # its forward P/E is 31.7 and its earnings are *shrinking*, which is the
        # actual answer and a far more interesting one. Each branch now carries
        # the reason that produced it.
        if is_fund:
            gaps.gap("peg_ratio", UNSUPPORTED, fund_reason, fund_resolves)
        elif forward_pe is None and eps_growth is None:
            gaps.gap("peg_ratio", TRANSIENT,
                     "Yahoo published neither a forward P/E nor a growth rate for this listing.",
                     "Appears once analyst estimates are published; thinly covered names may "
                     "never get them.")
        elif forward_pe is None:
            gaps.gap("peg_ratio", TRANSIENT,
                     "Yahoo published no forward P/E — there is no next-year earnings consensus "
                     "to price the growth against.",
                     "Appears once analysts publish a forward estimate; thinly covered names may "
                     "never get one.")
        elif eps_growth is None:
            gaps.gap("peg_ratio", TRANSIENT,
                     "Yahoo published a forward P/E but no growth rate to divide it by.",
                     "Appears on the next fundamentals refresh, once a year-on-year rate exists.")
        elif eps_growth <= 0:
            gaps.gap("peg_ratio", STRUCTURAL,
                     f"Reported {growth_field} is {eps_growth * 100:.1f}% — PEG divides by growth, "
                     f"and dividing by a negative rate produces a negative ratio that reads as "
                     f"cheap when the earnings are contracting.",
                     "Returns when year-on-year growth turns positive again. The contraction is "
                     "the signal here, not a data gap.")
        elif eps_growth > _MAX_PEG_GROWTH:
            # Capping would still print a number, and PEG has no equivalent of
            # "cap and carry on" the way the DCF projection does.
            gaps.gap("peg_ratio", METHODOLOGICAL,
                     f"Reported {growth_field} is {eps_growth * 100:.0f}%/yr — a recovery off a "
                     f"small or negative prior-year base, not a rate that repeats. PEG at that "
                     f"denominator collapses toward 0.0 and would read as deeply undervalued.",
                     f"Returns once growth settles below {_MAX_PEG_GROWTH * 100:.0f}%/yr and "
                     f"reflects a run-rate rather than a base effect.")
            result["quant_flags"].append(
                f"PEG skipped — reported growth is {eps_growth * 100:.0f}%/yr, almost certainly "
                f"a base-effect artifact (a small or negative prior-year base), not a durable rate; "
                f"PEG at that growth is not a meaningful signal"
            )
        elif forward_pe <= 0:
            gaps.gap("peg_ratio", STRUCTURAL,
                     f"Forward P/E is {forward_pe:.1f} — the consensus expects a loss next year, "
                     f"so there are no forward earnings to price.",
                     "Returns when consensus forward earnings turn positive.")
        elif (forward_pe / (eps_growth * 100)) < _MIN_MEANINGFUL_PEG:
            # The growth cap above catches the usual base effect, but not a low
            # forward multiple against growth just under the ceiling: a forward
            # P/E of 3 at 80% growth gives 0.04, which rounds to 0.0 and prints
            # as "undervalued relative to growth (<1.0)". A ratio that small is
            # degenerate, not cheap — the same failure the cap exists to stop,
            # arriving from the numerator instead of the denominator.
            gaps.gap("peg_ratio", METHODOLOGICAL,
                     f"Forward P/E {forward_pe:.1f} against {eps_growth * 100:.0f}% growth gives "
                     f"{forward_pe / (eps_growth * 100):.4f} — a ratio that rounds to zero rather "
                     f"than to a cheap valuation, and would be read as the strongest possible buy.",
                     f"Returns when the ratio clears {_MIN_MEANINGFUL_PEG}, which needs either a "
                     f"higher forward multiple or a growth rate that is not near the cap.")
        else:
            peg = round(forward_pe / (eps_growth * 100), 2)
            result["peg_ratio"] = peg
            sources["peg_ratio"] = {"forward_pe": forward_pe, "growth": eps_growth,
                                    "origin": f"info.forwardPE / info.{growth_field.replace(' ', '')}"}
            gaps.ok("peg_ratio")
            if peg < 1.0:
                result["quant_flags"].append(f"PEG {peg} — undervalued relative to growth (<1.0)")
            elif peg < 2.0:
                result["quant_flags"].append(f"PEG {peg} — fair value territory (1–2)")
            else:
                result["quant_flags"].append(f"PEG {peg} — expensive vs growth (>2); requires acceleration to justify")

        # ── DCF ────────────────────────────────────────────────────────────
        # Growth comes from the trailing FCF series when that series can carry
        # it. Revenue growth is a proxy, used only when it must be, and labelled.
        growth = None
        growth_source = None
        cagr = fcf_cagr(stmts)
        if cagr:
            growth = cagr["rate"]
            growth_source = {
                "origin": "trailing FCF CAGR",
                "years":  cagr["years"],
                "from":   cagr["from"],
                "to":     cagr["to"],
            }
        elif rev_growth is not None:
            growth = rev_growth
            growth_source = {"origin": "info.revenueGrowth",
                             "note": "FCF series could not support a growth rate (too few years, "
                                     "or it crosses zero)"}

        # Both money inputs cross into the trading currency together, so the
        # per-share value comes out in the same units as the price it is
        # compared against. The growth rate is a ratio and does not convert.
        net_debt_market = to_market(net_debt) or 0.0

        if is_fund:
            gaps.gap("dcf", UNSUPPORTED, fund_reason, fund_resolves)
        elif is_financial_filer:
            # Before the fcf <= 0 branch, so a bank is declined for being a bank
            # rather than for the sign its cash flow happened to print this year.
            gaps.gap("dcf", UNSUPPORTED,
                     f"A DCF discounts free cash flow, and {ticker} {filer_signature}. For "
                     f"such a filer {fin_cause} Discounting it would return a per-share value "
                     f"with no economic meaning behind it.",
                     fin_resolves,
                     sector=sector,
                     industry=industry or None)
        elif ccy_mismatch and fx_rate is None:
            gaps.gap("dcf", TRANSIENT,
                     f"Cash flow and net debt are in {fin_ccy} while the shares trade in "
                     f"{market_ccy}, and the {fin_ccy}/{market_ccy} rate did not load, so a "
                     f"per-share value cannot be stated in the currency of the price.",
                     "Clears as soon as the FX quote returns; every other DCF input is present.")
        elif fcf is None:
            gaps.gap("dcf", TRANSIENT if not cashflow_loaded else STRUCTURAL,
                     "No free-cash-flow figure could be read, so there is nothing to discount.",
                     "Usually a feed gap — the statement cache refreshes every 15 minutes."
                     if not cashflow_loaded else
                     "Clears if the next annual filing reports cash flow under standard labels.")
        elif fcf <= 0:
            gaps.gap("dcf", STRUCTURAL,
                     f"Free cash flow is {fcf_market:,.0f} {market_ccy or ''} — negative. "
                     f"Discounting a negative cash flow forward compounds the loss and returns "
                     f"a confident-looking negative valuation.".strip(),
                     "Returns the first year free cash flow turns positive. Until then the "
                     "negative cash flow is the finding.")
        elif not shares or shares <= 0:
            gaps.gap("dcf", TRANSIENT,
                     "Yahoo did not report a share count, so enterprise value cannot be reduced "
                     "to a per-share figure.",
                     "Usually returns on the next fundamentals refresh.")
        elif growth is None:
            gaps.gap("dcf", STRUCTURAL,
                     "No defensible growth rate: the trailing free-cash-flow series crosses zero, "
                     "so a compound rate through it would be fiction, and no revenue growth was "
                     "reported as a fallback.",
                     "Returns once three consecutive years of positive free cash flow are on "
                     "file, or Yahoo publishes a revenue growth rate.")
        else:
            # Clamp once and reuse, so the sensitivity grid's centre cell is
            # built from the identical float as the headline value rather than
            # a rounded copy of it.
            g_applied = min(max(growth, 0.0), _MAX_GROWTH_RATE)
            dcf_result = _compute_dcf(fcf_market, growth, wacc, net_debt_market, shares)
            if dcf_result:
                intrinsic = dcf_result["intrinsic_value"]
                result["dcf_intrinsic_value"] = intrinsic
                result["dcf_assumptions"]     = dcf_result["assumptions"]
                sources["dcf"] = {
                    "fcf": sources.get("fcf"),
                    "growth": growth_source,
                    "discount_rate": wacc_meta,
                    "net_debt": round(net_debt_market),
                    "fx": sources.get("fx"),
                }
                gaps.ok("dcf", converted=bool(fx_rate))

                result["dcf_sensitivity"] = _sensitivity(
                    fcf_market, g_applied, wacc, net_debt_market, shares, price)

                if price and price > 0:
                    upside = round(((intrinsic - price) / price) * 100, 1)
                    result["dcf_upside_pct"] = upside

                    implied = _implied_growth(price, fcf_market, wacc, net_debt_market, shares)
                    if implied is not None:
                        result["dcf_implied_growth_pct"] = implied
                        trailing = round(growth * 100, 1)
                        result["quant_flags"].append(
                            f"Reverse DCF: price implies {implied}% annual FCF growth for "
                            f"{_PROJECTION_YEARS} years — trailing rate was {trailing}%"
                        )

                    if upside > 30:
                        result["quant_flags"].append(
                            f"DCF: {upside}% upside to intrinsic ${intrinsic} — potential undervaluation"
                        )
                    elif upside > 0:
                        result["quant_flags"].append(
                            f"DCF: {upside}% upside to intrinsic ${intrinsic} — slight margin of safety"
                        )
                    elif upside > -30:
                        result["quant_flags"].append(
                            f"DCF: {abs(upside)}% downside to intrinsic ${intrinsic} — growth priced in"
                        )
                    else:
                        result["quant_flags"].append(
                            f"DCF: {abs(upside)}% downside to intrinsic ${intrinsic} — significant premium to DCF value"
                        )
            else:
                gaps.gap("dcf", STRUCTURAL,
                         f"The model returned no finite value at a {wacc * 100:.1f}% discount "
                         f"rate against {_TERMINAL_GROWTH * 100:.0f}% terminal growth.",
                         "Clears if beta moves the discount rate back above terminal growth.")

        # ── Sharpe Ratio ───────────────────────────────────────────────────
        sharpe, sharpe_meta = _compute_sharpe(stock)
        if sharpe is not None:
            result["sharpe_ratio"] = sharpe
            gaps.ok("sharpe", days=sharpe_meta.get("available_days"))
            if sharpe >= 1.0:
                result["quant_flags"].append(f"Sharpe {sharpe} — strong risk-adjusted return (>1.0)")
            elif sharpe >= 0.5:
                result["quant_flags"].append(f"Sharpe {sharpe} — acceptable risk-adjusted return")
            else:
                result["quant_flags"].append(f"Sharpe {sharpe} — weak risk-adjusted return; high vol relative to return")
        else:
            cause = sharpe_meta.get("cause")
            have  = sharpe_meta.get("available_days") or 0
            need  = sharpe_meta["required_days"]
            if cause == "short_history":
                # The shortfall is the useful number. "Not enough price history"
                # gave the reader nothing to act on; "22 of 30 sessions, so about
                # eight more" tells them it fixes itself and roughly when.
                gaps.gap("sharpe", TRANSIENT,
                         f"This listing has {have} daily closes on file and an annualised Sharpe "
                         f"needs {need} — the line is too recently traded to measure.",
                         f"Resolves on its own after about {need - have} more trading sessions. "
                         f"Nothing is wrong with the data.",
                         available_days=have, required_days=need)
            elif cause == "zero_volatility":
                gaps.gap("sharpe", STRUCTURAL,
                         "Daily returns show zero volatility over the window, so the ratio would "
                         "divide by zero — the quote is stale or the line barely trades.",
                         "Returns once the listing prints moving prices again.",
                         available_days=have, required_days=need)
            else:
                # Every Sharpe gap carries both counts, including this one where
                # the prose has no number to quote. A consumer that reads the
                # structured record should not have to special-case the branch
                # that happened to fetch zero rows.
                gaps.gap("sharpe", TRANSIENT,
                         "Yahoo returned no usable price history for this listing, so there is "
                         "nothing to measure return or volatility against.",
                         "Usually a feed gap or a rate limit — retry in a few minutes.",
                         available_days=have, required_days=need)

        # ── Beta ───────────────────────────────────────────────────────────
        if beta is not None:
            result["beta"] = beta
            gaps.ok("beta")
            if beta > 2.0:
                result["quant_flags"].append(f"Beta {beta} — very high market sensitivity; size positions accordingly")
            elif beta > 1.0:
                result["quant_flags"].append(f"Beta {beta} — above-market volatility")
            else:
                result["quant_flags"].append(f"Beta {beta} — low market correlation; defensive characteristic")
        elif is_fund:
            gaps.gap("beta", UNSUPPORTED,
                     f"Yahoo publishes no beta for {quote_type.lower() or 'this instrument'} "
                     f"listings.",
                     fund_resolves)
        else:
            gaps.gap("beta", TRANSIENT,
                     "Yahoo has not published a beta for this listing — it is computed from a "
                     "multi-year price history that this line does not yet have.",
                     "Appears once the listing has traded long enough for Yahoo to fit one, "
                     "typically after a few years.")

        # ── Structural gaps are findings, so they travel ───────────────────
        # A metric the fundamentals refused to form says something about the
        # business, and quant_flags is what reaches the research prompt. MELI's
        # PEG used to vanish here in silence; "earnings are contracting 10.9%"
        # is precisely the sort of thing the brief should be reasoning about.
        # Transient and unsupported gaps are deliberately excluded — a feed
        # hiccup is not a finding, and neither is an ETF having no ROIC.
        for metric, s in gaps.status.items():
            if s.get("nature") == STRUCTURAL:
                label = metric.replace("_", " ")
                result["quant_flags"].append(f"{label} not formed — {s['reason']}")

        # ── Data quality flag ──────────────────────────────────────────────
        result["metric_status"] = gaps.status
        if gaps.missing:
            result["data_quality"] = "partial"
            result["missing_metrics"] = gaps.missing
            # Only a transient gap is a data problem. A DCF declined because
            # cash flow is negative is the engine working, and labelling the
            # whole report "degraded" for it trains the reader to ignore the
            # banner that matters.
            result["gap_natures"] = sorted(gaps.natures())

        if not result["quant_flags"]:
            result["quant_flags"].append("Insufficient data to compute quant flags")

        return result

    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "get_quant_metrics: unhandled error (type=%s): %s", type(exc).__name__, exc
        )
        return {
            "ticker": ticker,
            "error": "unexpected response format",
            "note": "Quant computation failed — fundamentals data may be unavailable for this ticker.",
        }
