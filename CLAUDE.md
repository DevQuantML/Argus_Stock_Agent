# Stock Agent

Brent-crude-gated equity research agent. FastAPI backend, vanilla-JS frontend,
no build step.

## Working rules

These are production rules. They apply to every task in this repo, including
small ones.

### 1. Define before acting

Do not plan, research, reason deeply, or execute tools until the problem has
been explicitly defined and validated.

If the problem definition is incomplete or ambiguous, **ask clarifying
questions instead of proceeding on assumption**. A wrong assumption acted on
early costs more than a question asked up front. Questions are cheap; a
confidently-built wrong thing is not.

This does not mean stalling on trivia. Make routine judgment calls. Ask when
different readings of the request would lead to materially different work.

### 2. Re-verify before reporting

Before presenting any result, check the delivered work against what was
actually agreed, and state plainly which items were achieved and which were
not.

- Verify against the **artifact**, not against intent, memory, or a previous
  summary. Read the file; run the command; query the endpoint.
- A prior session's "done" is a claim to be tested, not a fact to inherit.
- If something was skipped, deferred, or only partly done, say so explicitly
  and say why. Do not let it be discovered later.
- Never report success for a path that was not exercised. If a code path
  could not be tested, name it as untested.

## Architecture

```
api.py                        FastAPI app — routes, auth, rate limit, CSP
config.py                     MY_PORTFOLIO, WATCHLIST, BRENT_LEVELS, GEO_TRANSMISSION
main.py                       CLI: python main.py PLTR | scan | brent
store.py                      SQLite — positions, watchlist, sessions, profile
tools/perplexity_research.py  the ONLY AI path — prompts, modules, synthesis
tools/quant.py                DCF, ROIC, FCF yield, PEG, Sharpe, Beta (pure Python)
tools/fundamentals.py         annual statements with provenance (imported by quant.py)
tools/fx.py                   currency conversion for foreign-listed names (ADRs)
tools/stock_data.py           yfinance prices + live-book context
tools/oil_price.py            Brent futures → macro gate signal
tools/validator.py            validate_ticker, guard_tool_output, sanitize_question
tools/xirr.py                 money-weighted return for the portfolio analytics route
static/                       frontend — index.html, style.css, js/*.js (ES modules)
scripts/verify_*.py           free harnesses — run before any PR
docs/HANDOFF.md               design decisions, open items, verification log
```

### Provider policy

**Perplexity is the only research engine, Groq is the declared fallback.**
`_get_provider()` in `tools/perplexity_research.py` picks between them:
Perplexity when `PERPLEXITY_API_KEY` is set, else Groq. Anthropic and
OpenRouter were removed deliberately — do not reintroduce an SDK to solve a
problem that a prompt change can solve.

Groq has **no live web search**. Any prompt that depends on real-world
freshness must declare that degradation in its own output when
`provider == "groq"` — see `_policy_prompt()` for the pattern.

### Three different keys — do not conflate them

| Key | Lives in | Purpose |
|---|---|---|
| `AGENT_SECRET` | `.env` **and** the browser Settings drawer | A password you choose. Gates `/api/research/*` so nobody else spends your credits. |
| `PERPLEXITY_API_KEY` | `.env` only | Provider credential. Never sent to the browser. |
| `GROQ_API_KEY` | `.env` only | Fallback provider credential. Never sent to the browser. |

## Cost

**AI research spends real money — roughly $0.21 for a full 5-module run.**

Never trigger a paid research call to "check something works". The free paths
(`/api/demo`, `/api/portfolio`, `/api/history`, `/api/brent`, `/health`) cover
almost all verification. The frontend deliberately shows a cost-confirmation
modal before any paid stage; do not add a code path that bypasses it.

## Frontend — ARGUS://TERMINAL

A dense Bloomberg-style research terminal: boot sequence, ticker tape, command
line, three-column grid (Brent + geo · watchlist + commands · research output),
inspector drawer. Command-driven with click fallbacks on every action.

```
index.html            boot overlay + terminal shell
js/theme-boot.js      accent/tempo/CRT before first paint (plain, blocking)
js/theme.js           preference store + typing tempo
js/app.js             boot, state, command parser, staged pipeline
js/ui.js              every renderer — panels, pipeline, verdict, modals
js/chart.js           SVG price chart + sparklines
js/md.js              XSS-safe markdown
js/api.js             fetch layer, agent-key storage, error normalisation
```

- **No build step, no npm, no CDN.** Everything is same-origin — the CSP is
  `script-src 'self'` and there must be zero external network requests. The
  source design pulled JetBrains Mono from Google Fonts; that is replaced by a
  system mono stack (`--mono`) because `font-src 'self'` blocks it.
- **CSP is path-aware** (`api.py`): pages and `/static/*` get a same-origin
  policy, API routes keep `default-src 'none'`. Inline `<script>` is blocked,
  which is why `theme-boot.js` is an external blocking script.
- **All colour goes through CSS custom properties.** `--accent` and its
  derivatives are swapped by `[data-accent]` on `<html>`. Never hardcode a
  colour in a component rule or an SVG presentation attribute — SVG attributes
  do not resolve `var()`, so use CSS classes (see `.ref-dcf`, `.spark-pos`).
- **`--txt-muted` is deliberately brighter than the source design.** Most muted
  text is 8–9px; the original `#4A6070` measured 2.84:1 and was unreadable at
  that size.
- **LLM output is untrusted.** It goes through `renderMarkdown()` in `md.js`,
  which HTML-escapes before parsing. Never `innerHTML` a model response
  directly. The typewriter reveals text nodes only, so it cannot reintroduce
  markup.
- **Never let an animation gate the pipeline.** Background tabs clamp
  `setTimeout` to ~1s, which once turned the typewriter into an indefinite
  stall that blocked a paid run mid-flight. `typeHTML()` bails to an instant
  reveal on hidden tab / reduced motion / a time cap, and `typeBlock()` caps it
  again at the call site.
- **Init steps are individually try/caught** in `app.js`. A single missing
  alias in `wire()` once silently killed the whole command line while every
  panel still looked healthy — failures now toast loudly.
- Bump the `?v=` on `style.css`, `app.js`, and the sub-module imports in
  `app.js` and `ui.js` together whenever frontend files change.

## Gotchas

- **An unknown ticker returns HTTP 200 with all-null fields.** yfinance does
  not error on a bogus symbol, so a null `price` is the only reliable
  "no such ticker" signal.
- **`/health` always returns 200**, never 500 — cloud platforms treat a 500
  health check as a dead instance. Status lives in the body.
- Quant metrics are conditionally present. Every one of them — computed or
  not — has an entry in `quant["metric_status"]`. A metric that did not
  compute carries a `nature`, a `reason` and a `resolves`, and the frontend
  prints those verbatim. **Never write an absence reason in the frontend.**
  The card strings that used to live in `METRICS[x].absent` drifted away from
  what the engine actually decided: MELI's PEG card read "Forward P/E not
  reported" while its forward P/E was 31.7 and the real cause was contracting
  earnings. The engine knows which branch it took; only it may say.
- The four natures are not decoration. `transient` is the only one that means
  "a data problem" — the other three (`structural`, `unsupported`,
  `methodological`) are findings, and the heads-up strip stays grey for them
  so the amber one keeps its meaning.
- **A currency mismatch is converted, not declined.** An ADR files in its home
  currency and trades in another (SKHY/KRW, BABA/CNY, TSM/TWD, ASML/EUR,
  NVO/DKK — five of twelve large caps probed). `tools/fx.py` pulls the cross
  from the same yfinance feed and FCF yield and DCF are computed on the
  trading-currency figures. ROIC stays in the reporting currency because both
  its inputs come off the same statements — do not convert it.
- **Banks and insurance carriers decline the cash-flow metrics, and not
  because the feed is short.** Two different tells, both gated on
  `info["sector"] == "Financial Services"` — the sector alone would be far too
  broad, since Visa, Mastercard and the asset managers all sit under it and
  their free cash flow is a real valuation input (3.2%, 3.3%, 1.9%).
  - **Banks** — an income statement that loads cleanly while carrying no
    operating-income row. Yahoo *does* publish a Free Cash Flow row for a
    bank, so the metrics used to compute: JPM printed a -15.5% FCF yield and
    BAC a +2.8% one with a $23/share DCF behind it. For a deposit-taking
    institution that row tracks loan and deposit movement, not owner's cash.
    Catches JPM, BAC, GS, WFC, C; leaves V, MA, BLK computing. **These decline
    all three — ROIC too**, since ROIC needs the same missing row. ROIC's
    branch is gated on `is_bank_filer`, not on the missing row alone: gated on
    the row, it told *any* filer short of an operating-income line that the
    absence was "normal for banks and insurers", which is a confident
    explanation of the wrong business. A non-financial missing the row now
    declines `structural` and says so. No live ticker reaches that branch —
    a 27-name scan found only HSBC missing the row, and it is a bank — so it
    is covered by a stub, not by the standing set.
  - **Insurance carriers** — the industry label, because the bank tell does
    not fire here: a carrier *does* file an operating-income row, so it slid
    past and priced itself. Measured 2026-08-11, ALL returned a 14.5% FCF
    yield and a $2,794 intrinsic value against a $270 share price, PGR $2,281
    against $214, MET $630 against $97, EG $1,738 against $369 — an order of
    magnitude out, stated with total confidence. Premiums are banked years
    before the claims they cover are paid, so that float runs through
    operating cash flow while remaining money held for policyholders.
    **These decline FCF yield and DCF only — ROIC still computes, with a
    caveat attached.** The asymmetry with the banks is deliberate: a carrier's
    ROIC is built from a real filed operating-income row over real filed
    equity, and it lands within a few points of reported ROE (PGR 31.1 vs
    34.9, AIG 7.0 vs 7.2, CB 12.7 vs 14.8, EG 9.7 vs 12.6) — approximately the
    right number, not the order-of-magnitude artifact free cash flow was. But
    invested capital is `equity + net debt`, which excludes the policyholder
    float that earned much of the numerator, so the ratio reads as a
    lightly-levered ROE rather than a return on operating capital. Since the
    threshold flag calls >15% "quality cleared", carriers get a second
    `quant_flag` saying exactly that, and `metric_status["roic"]["caveat"]`
    carries it for any other consumer. **Do not silently drop that caveat** —
    without it the engine asserts operating efficiency it did not measure.
- **The insurer rule must not match on the word "Insurance" alone.** Yahoo
  files carriers as `Insurance - <line>` (Property & Casualty, Life,
  Diversified and Reinsurance are the four labels seen live) and the broking
  houses as `Insurance Brokers` — one prefix apart. A broker earns commission
  on policies it does not carry and holds no float, and its cash flow is a
  real input: AJG 2.8%, AON 4.3%, WTW 4.9%, BRO 5.8%, against the carriers'
  13.5–27.8%. So the rule is "starts with insurance, does not start with
  insurance broker" — excluding the broker rather than listing the carriers,
  so an underwriting line never seen before is declined rather than handed a
  DCF. The label is normalised by `_norm_industry()` so `info["industry"]` and
  `info["industryKey"]` collapse to one form. Exchanges (CME, ICE) are never
  at risk — they file as `Financial Data & Stock Exchanges`.
  `scripts/verify_metric_status.py` guards both directions: `INSURANCE_FILERS`
  must decline `unsupported` and name the insurance industry they declined on
  (a carrier caught by the *bank* tell, after a short statement feed, would
  otherwise pass while proving nothing), and `FCF_MEANINGFUL` must never be
  declined `unsupported` at all. That set holds **two** brokers on purpose:
  **`MMC` has returned no sector, no bars and a 404 quote summary since at
  least 2026-08-11**, so it proves nothing about the carve-out and left AJG
  carrying the assertion alone. MMC stays as the dead-symbol canary — it is
  what surfaced an over-broad Sharpe assertion — and AON restores the live
  redundancy. A control that has quietly stopped controlling anything is worse
  than no control, so check MMC before trusting it.
- **Start the server with `--workers 1 --no-proxy-headers`. Both flags fail
  silently.** The in-memory rate limiter needs a single worker. And uvicorn
  enables `--proxy-headers` *by default*, which rewrites `request.client` from
  the client-supplied `X-Forwarded-For` before any app code runs — overriding
  `TRUST_PROXY` entirely and poisoning `request.client.host`, the value the
  failed-unlock backstop trusts *because* it is meant to be unforgeable. Leave
  it on and a caller rotates the header for a fresh rate-limit bucket per
  request. `TestClient` does not run that middleware, so no in-process test can
  catch it; `scripts/verify_hardening.py` guards the launch commands instead.
- **Every `/api/*` path is metered in middleware**, classified by
  `_budget_for()` in `api.py`. Anything matching no prefix falls through to a
  default budget, so a new route is limited from the moment it exists. `/health`,
  `/` and `/static/*` are exempt deliberately — a 429 on `/health` reads as a
  dead instance to a cloud platform, and one page load pulls several assets.
  Buckets are namespaced, so the middleware and `require_auth` never charge the
  same request to one budget.
- **Stored `thesis` / `note` / `sector` reach the provider prompt**, so they go
  through `sanitize_prompt_text()` and arrive fenced in `<<<UNTRUSTED:…>>>`
  markers that the text inside cannot forge. Inside that function, fence
  stripping runs *before* the HTML strip: `_HTML_TAG_RE` is `<[^>]+>` and will
  eat a `<<<END:X>` prefix, which destroys the evidence and makes the real guard
  a no-op. `scripts/verify_hardening.py` asserts it.
- **`TRUST_PROXY` is a hop count, not a boolean.** It says how many reverse
  proxies you control sit in front of the app; 0/unset means none. `_client_ip()`
  and `_is_https()` index `X-Forwarded-*` that many entries *from the right*,
  because those headers are appended to — the leftmost entry is written by the
  client and is hostile input. Setting it higher than the number of proxies you
  actually run reads into the client-controlled part and silently restores the
  rate-limiter bypass. `scripts/verify_proxy_trust.py` asserts the boundary.

## Verification harnesses

Both spend nothing — yfinance statements, prices and FX crosses only.

```
scripts/verify_quant.py           the numbers are right
scripts/verify_metric_status.py   the absences are right, and FX converts
```

`verify_quant.py` lives on `fix/slide-in-panel-overlay`, not on this branch.
Its `currency mismatch` assertion was inverted there as part of this work: it
used to require `dcf_intrinsic_value is None` whenever the currencies
disagreed, which was right when declining was the only honest option and is
wrong now that `tools/fx.py` converts. It now asserts the opposite, and reads
`currency_mismatch["resolved"]` three ways — **absent fails** (the FX feature
has been removed), **False skips** (the cross did not load, and holding the
metrics back is correct behaviour), **True asserts** the converted DCF, the
FCF yield scale, and that `sources.dcf.fx` records the rate. Do not collapse
those three into a boolean; a plain truth test either goes flaky on an
upstream outage or goes blind to the regression.
