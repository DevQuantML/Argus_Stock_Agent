# ARGUS://TERMINAL — Developer Handoff

Last updated: 2026-08-08

> **Most recent change (2026-08-08): the auth boundary moved.** Every route that
> reads or writes the book now requires a session. If you are holding a mental
> model where `/api/positions` and `/api/portfolio` are open, it is out of date —
> see §2 and §4. Sections are dated because "this session" in an undated handoff
> is what caused the last two rounds of confusion.

---

## 1. Project Architecture

```
api.py                        FastAPI app — routes, auth (AGENT_SECRET), rate-limit (20 req/min/IP),
                              CSP (script-src 'self' for pages, default-src 'none' for API routes).
                              TWO auth dependencies, do not conflate:
                                require_auth    — paid routes, metered on every call
                                require_session — user-data routes, meters failures only
config.py                     MY_PORTFOLIO, WATCHLIST, BRENT_LEVELS, GEO_TRANSMISSION
main.py                       CLI: python main.py PLTR | scan | brent
tools/
  fundamentals.py             NEW — annual statement reader with TTL=900s cache and full provenance
  quant.py                    DCF, reverse DCF, sensitivity grid, ROIC, FCF yield, PEG, Sharpe, Beta
  perplexity_research.py      Only AI path — prompts, modules, synthesis, _build_context()
  stock_data.py               yfinance price history + fundamentals
  oil_price.py                Brent futures → macro gate signal
  validator.py                validate_ticker, guard_tool_output, sanitize_question
  fx.py                       Currency conversion for foreign-listed names, so a
                              statement in KRW/DKK/INR is comparable to a USD market cap
  xirr.py                     XIRR calculation for portfolio analytics
store.py                      SQLite over argus.db — positions, watchlist, profile,
                              sessions, outlook. Persistent state already lives here.
scripts/                      All free — no Perplexity, no Groq, no API spend.
  verify_quant.py             Quant engine end to end, 121 assertions. In the repo
                              deliberately — the previous scratchpad harness was lost.
  verify_metric_status.py     Metric provenance and gap reasons, 396 assertions
  verify_ticker_validation.py Ticker guard incl. hyphenated classes (BRK-B), free routes
  verify_proxy_trust.py       X-Forwarded-* trust boundary, 17 assertions. No network —
                              drives _client_ip()/_is_https() with crafted headers.
static/
  index.html                  Boot overlay + terminal shell (cache-bust v=13)
  style.css                   All layout + theming via CSS custom properties (cache-bust v=13)
  js/
    theme-boot.js             Applies accent/tempo/CRT before first paint (blocking, non-module)
    theme.js                  Preference store + typing tempo
    app.js                    Boot, state, command parser, staged pipeline (v=13)
    ui.js                     Every renderer — panels, pipeline, verdict, modals, DCF disclosure (v=13)
    chart.js                  SVG price chart + sparklines
    md.js                     XSS-safe markdown renderer (HTML-escapes before parsing)
    api.js                    Fetch layer, session handling, error normalisation
    views.js                  Onboarding, unlock, profile views (v=13)
  v2/                         Landing-page source. NOT SERVED — the /v2 route was removed
                              2026-08-08. See §4 before re-wiring it.
```

### Provider policy

**Perplexity is the only research engine; Groq is the declared fallback.** `_get_provider()` in
`tools/perplexity_research.py` selects between them. Groq has no live web search — prompts that
need freshness declare the degradation in output when `provider == "groq"`. Anthropic and OpenRouter
were deliberately removed; do not reintroduce an SDK.

### Three keys — never conflate

| Key | Where | Purpose |
|---|---|---|
| `AGENT_SECRET` | `.env` + browser Settings drawer | Password gating `/api/research/*` **and, since 2026-08-08, every route that reads or writes the book**. Exchanged for an HttpOnly session cookie by `POST /api/session`. |
| `PERPLEXITY_API_KEY` | `.env` only | Provider credential — never sent to browser |
| `GROQ_API_KEY` | `.env` only | Fallback provider credential — never sent to browser |

---

## 2. Completed Features

### Pre-deploy security pass (2026-08-08 session)

A six-phase audit before a public deploy. One CRITICAL finding, reproduced
against a running instance before anything was changed.

**The finding.** Twelve routes read *and wrote* the book with no auth:
`/api/positions`, `/api/watchlist`, `/api/profile`, `/api/portfolio`,
`/api/portfolio/analytics`, `GET /api/portfolio/outlook`, and `/api/info`.
An anonymous `curl` returned share counts, average cost, buy dates, stop-loss
and trim levels, and private thesis text; `POST` and `DELETE` let any caller
rewrite or erase holdings. `store.py` is single-tenant, so there was no identity
to scope by and no gate in front of it.

The sensitivity was already known — the comment explaining why CORS was removed
names these exact fields — but dropping CORS only stops a browser on another
origin. It does nothing about a direct request, and this app is built to deploy
publicly (`0.0.0.0` bind, cloud `PORT` injection, `railway.app` in the docstring).

**Fixed:**

- **`require_session` added to all twelve routes.** Same credentials as
  `require_auth`; the difference is that a *valid* credential is not metered.
  `require_auth`'s 20/min budget is sized for paid runs, and these routes are hit
  several times per page load, so sharing it would lock a live session out of its
  own dashboard. Failed attempts are still metered, which is what stops the key
  being brute-forced through this door.
- **`_credential_ok()`** factored out; both dependencies go through it, so the
  fail-closed behaviour when `AGENT_SECRET` is unset cannot drift apart.
- **`/api/info` gated, not trimmed.** The frontend reads `info.portfolio` and
  `info.watchlist`; removing those fields would have broken the geo panel and the
  watchlist for no gain that gating does not already give.
- **`/v2` route removed** (source kept). See §4.
- **`.dockerignore`** now excludes `argus.db`, `-wal`, `-shm` and `.claude`.
  `COPY . .` was baking the development book — positions, profile, session
  hashes — into image layers.
- **`.env.example`** documents `TRUST_PROXY` and `ARGUS_DB`, both previously
  read by the code and undocumented.
- **Boot gate** (`app.js`) requires a session on every visit, not just a fresh
  install. The old condition also tested `!profile`, which only looked correct
  while the data routes were open.
- **`aria-expanded="false"` + `aria-controls`** on the Brent and watchlist
  toggles. `setPanel` already kept the value in sync; it simply did not exist
  before the first toggle.
- Cache-bust v12 → v13 across all five files.

**Regression caught during verification, then fixed:** gating the routes made
the boot-time profile fetch 401, which made the app read every returning user as
a brand-new install and force them through onboarding after each unlock. The
profile read now happens inside `enterOrOnboard()`, after auth is established.

### Tier 0 — Quant engine rebuild (2026-08-05 session)

The core problem: `yf.Ticker(t).info` does not populate `operatingIncome`, `ebit`, or
`totalStockholdersEquity` for any ticker in the portfolio. ROIC was returning `None` on every
holding. `info.freeCashflow` for MELI reported -$4.1B against the statement's +$10.77B (sign
flip), causing the engine to flag a cash-generative business as "pre-profitability" and skip DCF.

**`tools/fundamentals.py` (new)**
- TTL-cached (900s) accessor over `income_stmt`, `balance_sheet`, `cashflow` DataFrames
- Ordered candidate row labels per field (handles yfinance renames)
- Returns per-field `{value, period, row, source}` — full provenance for disclosure UI
- Exports `fcf_series` (4-year history) for trailing CAGR
- Never raises; returns empty dict on any failure

**`tools/quant.py` (major rewrite)**
- ROIC: reads statements only; `bookValue` fallback deleted (it was per-share, unit mismatch)
- FCF: statement first, `info.freeCashflow` as labelled fallback — MELI fix
- DCF growth: trailing FCF CAGR from `fcf_series` (≥3 points, both endpoints positive)
- WACC: CAPM — risk-free + beta × 5%, clamped [7%, 15%], falls back to 10% when beta absent
- Reverse DCF: bisection solving intrinsic(g) = price → `dcf_implied_growth_pct`
- 3×3 sensitivity grid: WACC ±2 pts × growth ±5 pts → 9 intrinsic values
- `sources` dict: per-metric inputs + fiscal period for every computed value
- Currency mismatch detection: KRW-statement / USD-quote ADRs decline FCF yield and DCF
- PEG: skips if growth > 100%/yr (`_MAX_PEG_GROWTH = 1.00`) — prevents ~0.0 on base-effect spikes
- Adds `dcf_implied_growth_pct` and `dcf_sensitivity` to the return dict

**`tools/perplexity_research.py` (modified)**
- `_assumption_block(quant)` generates an explicit "MODEL ASSUMPTIONS AND SOURCES" block
- `_build_context()` includes it in the paid prompt, alongside implied growth
- Paid runs now state: FCF with fiscal period, growth rate and source, CAPM discount rate, net debt, shares

**`static/js/ui.js` (significant additions)**
- `countCompact()` — share counts without `$` prefix
- `fmtPct1()` — single-decimal percent formatter
- `sensitivityTable(grid, price, ccy)` — 3×3 HTML table, centre cell highlighted
- `renderDcfDisclosure(container, quant, stock)` — full disclosure panel (implied growth + sensitivity)
- `openInspector()` — now includes implied growth row, ROIC INPUTS section with provenance, DCF disclosure
- Absent-text updates: ROIC, DCF, FCF, PEG all carry accurate "why missing" copy
- Currency mismatch surfaced in the partial-data note

**`static/style.css` (additions)**
- `.qdisc*` classes for DCF disclosure panel
- `.qdisc-imp*` classes for implied growth section
- `.qdisc-a` grid for assumptions key-value pairs
- `.qsens` table styles with `.mid` highlighted centre cell

**Verification harness**
- `scratchpad/verify_tier0.py` — 66 assertions over the full book + watchlist, all free paths
- Covers: ROIC on all holdings, MELI FCF sign, ORCL graceful decline, sensitivity completeness,
  provenance on every metric, `bookValue` trap regression, PEG base-effect gate

### Earlier features (pre-session)

- Boot sequence with live data verification
- ARGUS://TERMINAL theme — accent/CRT/tempo preferences, dark-first, `data-accent` attribute theming
- Command-driven interface: `research`, `quant`, `chart`, `scan`, `brent`, `pos`, `add`, `rm`,
  `portfolio`, `profile`, `focus`, `clear`, `help`
- Staged research pipeline with cost-confirmation modal before paid stages
- Inspector drawer (`#insp`) with position details and quant metrics
- Portfolio view: P&L, XIRR, weighted outlook
- Profile/onboarding view
- Watchlist persistence (server-side, survives browser wipe)
- Ticker tape with pause control
- Price chart with SVG sparklines
- Session auth via `AGENT_SECRET` with browser Settings drawer
- `v2/` frontend directory committed (landing-page variant) — **never wired to the
  backend, and no longer served since 2026-08-08; see §4 before reviving it**

---

## 3. Pending Tasks

Status re-verified against the code on 2026-08-13, not inherited from the previous
handoff. Of the four items from the 2026-08-08 audit, **two are now closed and two
remain open.** Each open one touches auth or rate-limit design, where a careless
patch is worse than the finding.

### RESOLVED 2026-08-13 — `X-Forwarded-For` spoofing defeated the rate limiter (was HIGH)

`_client_ip()` read `X-Forwarded-For.split(",")[0]`. The *first* entry is the one
a client writes — a proxy appends, it does not replace. So with `TRUST_PROXY=1`,
which you need behind Railway/Fly/Render, rotating that header gave every request
a fresh bucket, the 20/min limit never fired, and `POST /api/session` was
brute-forceable without limit. `_is_https()` had the same defect on
`X-Forwarded-Proto`, where a client-supplied `https` could flip the session
cookie's Secure flag.

**Fixed as proposed.** `TRUST_PROXY` is now a hop *count*; `_forwarded_value()`
indexes `parts[-hops]` so the client-written prefix is never read. A chain shorter
than the declared hop count falls back to the socket peer rather than guessing, and
a non-numeric value fails closed to 0. `TRUST_PROXY=1` still means one proxy, so
existing deploys are unaffected.

A failed-unlock backstop (`_auth_failures_exceeded` / `_record_auth_failure`) is
keyed on `request.client.host` — the TCP peer, unspoofable at any distance — and
bounds brute force even if the hop count is misconfigured. It counts failures
only, so correct sign-ins never touch it.

Verified by `scripts/verify_proxy_trust.py`: 17 assertions, free path, no network.
The load-bearing one is that rotating a spoofed prefix now yields a **stable**
bucket.

**Still worth doing, independently:** rotate `AGENT_SECRET` to
`python -c "import secrets; print(secrets.token_hex(32))"`. A long random secret
removes the brute-force risk regardless of what the limiter does.

### OPEN · MEDIUM — public routes are unmetered

Confirmed still open. `_check_rate_limit` has three call sites — `require_auth`,
`api_session_create`, and the info route — none of them middleware. `/api/demo`,
`/api/history` and `/api/brent` have no limit at all, and `/api/portfolio` fans
out several concurrent yfinance calls per request, so N requests become many times
N upstream calls. **Fix:** apply the limiter in the `add_security_headers`
middleware with a per-path-class budget, so a route added later cannot silently
miss it.

### OPEN · MEDIUM — stored thesis text reaches the prompt unsanitised

Confirmed still open; `sanitize_prompt_text` does not exist in the codebase.
`thesis` and `note` are interpolated verbatim into the provider prompt
(`_build_context`, `_outlook_prompt`). `sanitize_question` covers only the
`question` parameter, and `guard_tool_output` filters model *output*, not prompt
*input*. The attacker surface narrowed when writes started requiring a session,
but the input is still untreated. **Fix:** a `sanitize_prompt_text()` sibling,
plus explicit delimiters around untrusted blocks.

### RESOLVED 2026-08-12 — dependencies unpinned (was LOW)

Closed by `69a2038`. `requirements.txt` now pins every dependency to an exact
version rather than a `>=` floor. The previous entry here claimed this was open
after it had been fixed — the hazard of a status line that outlives its check.

---

### RESOLVED 2026-08-07 — Slide-in panels covered the header and command line (was HIGH)

Kept as a record because the root cause was not what the handoff before it
diagnosed. Re-verified independently on 2026-08-08 — see §8.

**Diagnosed symptom (previous handoff):** no visible close affordance on `#col2` (≤1180px) or
`#col1` (≤820px); only `Escape` worked.

**Actual root cause:** `#grid` was `position:static`. `#col1`/`#col2` become
`position:absolute; top:0; bottom:0` at their breakpoints, so with no positioned ancestor they
resolved against the **viewport**, not the grid. Measured at 780×800: the open panel occupied
`top:0 … height:800` while `#grid` starts at `y=108` — covering 108px of chrome. Consequences:

- `elementFromPoint` over `#btn-watch` returned the panel's own `.blk-h`, not the button — the
  toggle that opened the panel was unreachable.
- At ≤820px the open Brent panel also buried `#cmd`. In a command-driven terminal that left
  Escape — undiscoverable — as the only recovery.

The missing ✕ was a symptom. A close button alone would have shipped a panel that still covered
the command line.

**Fix applied:**

1. `#grid{position:relative}` — panels now anchor to the grid and stop at its top edge.
2. `✕` close buttons (`#col1-close`, `#col2-close`, class `.col-close`), shown only inside each
   column's own breakpoint.
3. Scrim (`#scrim`) as the last child of `#grid`, covering the grid only so the command line
   stays live while a panel is open.
4. `setPanel()` / `closePanels()` in `app.js` — single path for all four dismiss routes (toggle,
   ✕, scrim, Escape), maintaining `aria-expanded`. Opening one panel closes the other.
5. Cache-bust v11 → v12 across all five files.

**Design note — why the scrim is CSS-driven.** The first attempt synced the scrim from JS and
closed stale panels on a `matchMedia` change listener. Testing showed that under CDP viewport
override **neither `matchMedia` change nor `window.resize` fires**, so that path was unverifiable
here. It was replaced with `#col2.open ~ #scrim{display:block}` *inside* each breakpoint's media
query. Widening past a breakpoint now retires the scrim automatically even though the `.open`
class goes stale — correct by construction, no listener to fail. Verified: at 1400px with
`col2.open` still set, the scrim is `display:none` and the grid is fully interactive.

---

## 4. Important Design Decisions

| Decision | Rationale |
|---|---|
| **Statements over `info` dict** | `info` fields are structurally absent for operating income and equity; statement DataFrames are reliable. |
| **`bookValue` fallback deleted** | yfinance `bookValue` is per-share; using it as total equity produces garbage ROIC. Deleted, not fixed. |
| **FCF CAGR as DCF growth proxy** | Revenue growth (old proxy) has no relationship to capital returns; trailing FCF CAGR is what the DCF actually values. Requires ≥3 points with positive endpoints; declines gracefully when unavailable. |
| **Reverse DCF (implied growth)** | Inverts the question: instead of "what is fair value?", asks "what growth does the current price imply?". Pure Python bisection, no extra dependency. |
| **3×3 sensitivity grid** | One scenario masquerades as a verdict. Nine cells make the assumption dependence visible. Growth axis NOT clamped inside sensitivity (only the headline DCF clamps to 30%). |
| **Currency mismatch detection** | Korean ADRs (SKHY) report KRW financials against a USD-quoted price. Without the mismatch guard, FCF yield prints 2262%. |
| **PEG cap at 100%/yr** | PEG ≈ 0 from a 1273% growth rate (SKHY) or 215% (PLTR) is a base-effect artefact that reads as "undervalued". Metric skipped; reason stated. |
| **CSP `script-src 'self'`** | Zero external network requests enforced. No CDN, no npm. Everything same-origin. Inline `<script>` blocked — use external `.js` files. SVG presentation attributes cannot use `var()`, always use CSS classes. |
| **No build step** | Vanilla JS, ES modules, `?v=N` cache-busting. Bump all four import sites together on any static change. |
| **Perplexity only, Groq fallback** | No Anthropic or OpenRouter SDK. Groq has no web search — prompts declare degradation in output. |
| **`/health` always 200** | Cloud platforms kill instances on 500. Status in body. |
| **Typewriter bail-out** | Background tabs clamp `setTimeout` to ~1s; typewriter bails to instant reveal on hidden tab / reduced motion / time cap to prevent indefinite stalls blocking paid runs. |
| **In-memory rate limiter** | 20 req/min/IP, but only works with `--workers 1`. Not a distributed guard. |
| **Two auth dependencies, not one** | `require_auth` protects *spend* and meters every call. `require_session` protects *data* and meters only failures — the data routes are read several times per page load, so sharing the paid budget would lock a live session out of its own dashboard. Both call `_credential_ok()`, so the fail-closed rule cannot drift between them. |
| **`/api/info` gated rather than trimmed** | It returns the holdings list and watchlist thesis text, but the frontend genuinely reads `info.portfolio` and `info.watchlist`. Trimming would break the geo panel and watchlist; gating closes the leak with no feature loss. |
| **`/v2` is not served** | The source keeps `AGENT_SECRET` in `localStorage` (the HttpOnly cookie exists to prevent exactly that), loads `marked` and Google Fonts from CDNs (breaking the zero-external-request rule), and pipes model output through `marked` into `innerHTML` — `marked` does not escape HTML, so that is an XSS sink. None of it was reachable, because `/v2` matched neither arm of the `is_page` CSP test and so got `default-src 'none'`, which stopped the page working at all. **Do not "fix" that by adding `/v2` to `is_page`** — it switches all three on at once. Rewrite against the session cookie and `md.js` first. |
| **Session gate on every visit** | The boot gate tests `!api.auth.has()` alone. It used to also test `!profile`, so a returning browser walked straight in — which only looked correct while the data routes were open to anyone. |

---

## 5. Known Bugs

| Severity | Bug | Location |
|---|---|---|
| ~~HIGH~~ | ~~Slide-in panels covered the header/command line~~ — **fixed 2026-08-07**, see §3 | `index.html`, `style.css`, `app.js` |
| ~~CRITICAL~~ | ~~Twelve user-data routes had no auth — anonymous read and write of the book~~ — **fixed 2026-08-08**, see §2 | `api.py` |
| **HIGH** | `X-Forwarded-For` spoofing bypasses the rate limiter, making `AGENT_SECRET` brute-forceable — **open**, see §3 | `api.py` `_client_ip` |
| MEDIUM | Public routes are unmetered; `/api/portfolio` amplifies one request into 8 yfinance calls — **open**, see §3 | `api.py` |
| MEDIUM | Stored `thesis`/`note` reach the provider prompt unsanitised — **open**, see §3 | `tools/perplexity_research.py` |
| LOW | Unknown ticker returns HTTP 200 with all-null fields — `null` price is the only reliable signal | `tools/stock_data.py`, callers |
| LOW | Rate limiter (20 req/min) only works with `--workers 1` — multi-worker deploys have no guard | `api.py` |
| LOW | `requirements.txt` unpinned (`>=` floors, no lockfile); `pip-audit` never run | `requirements.txt` |
| INFO | ROIC, FCF, and DCF are annual (as-of last fiscal close), not TTM — disclosed in UI but may surprise | `tools/fundamentals.py` |
| INFO | `fcf_cagr()` declines with <3 data points or any sign change in the series — some tickers get no DCF | `tools/fundamentals.py` |

---

## 6. Next Implementation Steps

### Immediate (one session) — before any public deploy
1. **Close the `X-Forwarded-For` bypass** (§3, HIGH) and **rotate `AGENT_SECRET`**.
   The rotation is a one-liner and removes the risk on its own; do it even if the
   code change waits.
2. **Meter the public routes** (§3, MEDIUM).

### Short term (Tier 1 from the plan)
3. **Citation persistence** — `research_runs` table or file store so the user can inspect the
   Perplexity citations that drove a verdict. Requires a one-off ~$0.04 sonar-pro call to confirm
   the citation field shape in the response.
4. **Determinism audit** — log the full `_build_context()` output per run so two runs on the same
   ticker are comparable.

### Medium term (Tier 2 from the plan)
5. **Brent regime history** — chart the BRENT_LEVELS thresholds over time, with rationale for each
   level. Decided: no DXY/10Y/HY panel, keep the single-signal story.

### Backend / infra
6. Add a persistent `research_runs` table. **Correction to the previous handoff:** it claimed
   "`api.py` uses in-memory state only". That is false — `store.py` (13 KB) already runs SQLite
   against `argus.db` with `bootstrap()`, `positions`, `watchlist`, `profile`, `sessions` and
   `outlook` tables. This is a new table in an existing store, not new infrastructure.

---

## 7. Files Modified

### 2026-08-08 session — security pass (commit `3749ea2`)

| File | Status | Why |
|---|---|---|
| `api.py` | **MODIFIED** | `require_session` + `_credential_ok`; the dependency applied to 12 user-data routes; `/v2` route removed; endpoint descriptions and section comments corrected |
| `static/js/app.js` | **MODIFIED** | Boot gate requires a session every visit; profile read moved into `enterOrOnboard()` after auth; cache v→13 |
| `static/index.html` | **MODIFIED** | `aria-expanded="false"` + `aria-controls` on the panel toggles; cache v→13 |
| `static/js/ui.js` | **MODIFIED** | Cache-bust v→13 only |
| `static/js/views.js` | **MODIFIED** | Cache-bust v→13 only |
| `.dockerignore` | **MODIFIED** | Exclude `argus.db`/`-wal`/`-shm` and `.claude` |
| `.env.example` | **MODIFIED** | Document `TRUST_PROXY` (both failure directions) and `ARGUS_DB` |

Not modified: `store.py`, `config.py`, `main.py`, `tools/*`, `static/style.css`,
`static/v2/*` (source kept, route gone).

### 2026-08-05 session — Tier 0 quant rebuild

| File | Status | Why |
|---|---|---|
| `tools/fundamentals.py` | **NEW** | Cached statement accessor — the missing layer that makes ROIC and correct FCF possible |
| `tools/quant.py` | **REWRITTEN** | Read statements via `fundamentals.py`; add CAPM WACC, reverse DCF, 3×3 sensitivity, `sources` dict, currency mismatch guard, PEG cap |
| `tools/perplexity_research.py` | **MODIFIED** | `_assumption_block()` + include implied growth in `_build_context()` so paid prompts state their assumptions |
| `static/js/ui.js` | **MODIFIED** | DCF disclosure panel, sensitivity table, implied growth row, ROIC provenance in inspector, updated absent-text copy; cache v→11 |
| `static/style.css` | **MODIFIED** | `.qdisc*`, `.qdisc-imp*`, `.qdisc-a`, `.qsens` classes for disclosure UI; responsive slide-in CSS for `#col1`/`#col2`; cache v→11 |
| `static/index.html` | **MODIFIED** | Cache-bust version bump v→11 only |
| `static/js/app.js` | **MODIFIED** | Cache-bust v→11; added Escape handler closing `#col1`/`#col2`; `#btn-brent`/`#btn-watch` toggle wiring |
| `static/js/views.js` | **MODIFIED** | Cache-bust v→11 only |
| `static/v2/` | **COMMITTED** | Landing-page frontend variant (not yet wired to backend) |

### Files not modified by the 2026-08-05 session (verify before assuming)
- `tools/stock_data.py` — provides `get_price_and_fundamentals()` used by portfolio
- `tools/oil_price.py` — Brent signal unchanged
- `tools/validator.py` — `validate_ticker()` called by `fundamentals.py`
- `config.py` — portfolio and watchlist config unchanged
- `main.py` — CLI path unchanged

`api.py` was untouched **by that session**, but was substantially changed on
2026-08-08 — see the table above.

---

## 8. Verification Status

All verification, in every session below, was done via **free paths only** — no
Perplexity or Groq spend at any point.

### 2026-08-07 — quant engine + panel fix

> **The previous `verify_tier0.py` no longer exists.** It was written into a session scratchpad
> that has since been cleaned up, so the "66 assertions PASS" claim below could not be re-run and
> was not inherited. It was rebuilt as **`scripts/verify_quant.py` — in the repo this time** — and
> re-run from scratch on 2026-08-07: **110 assertions, 0 failures.**

Re-verified 2026-08-07 (`python scripts/verify_quant.py`, 110 assertions, 0 failed):

| Check | Status |
|---|---|
| ROIC from statements on every holding, with fiscal period + invested capital > 0 | PASS |
| MELI FCF read from cash-flow statement (`origin='cashflow statement'`), yield **+11.7%** | PASS |
| ORCL declines DCF with a stated reason (FCF yield −5.68%) | PASS |
| `bookValue` never referenced in live code (comment-stripped grep) | PASS |
| PEG skipped for SKHY and PLTR, reason names the base effect | PASS |
| Sensitivity 9-cell, all finite, centre cell == headline DCF, 3 distinct growth rates | PASS |
| Every computed metric carries a `sources` entry | PASS |
| SKHY currency mismatch (KRW→USD) suppresses DCF and is explained | PASS |

Frontend, verified in a live browser against the running server:

| Check | Status |
|---|---|
| Panels anchor to grid top (y=108) at 780 / 900 / 1100px; header, toggle and `#cmd` all reachable | PASS |
| Close via ✕ / scrim / Escape / toggle — all four, both columns, `aria-expanded` tracks | PASS |
| Scrim retires itself at 1400px despite stale `.open`; grid interactive | PASS |
| 900px band: docked `#col1` with stale `.open` does **not** raise the scrim | PASS |
| Scrim stacks under the panel (hit-test inside → panel, outside → scrim) | PASS |
| `quant MELI` end-to-end through the real command line — sensitivity + disclosure render | PASS |
| Zero console errors; `/health`, `/api/info`, `/api/brent`, `/api/portfolio`, `/api/demo/MELI` → 200 | PASS |

**Not exercised:** every `/api/research/*` path (paid). No Perplexity or Groq call was made in
this session, so the staged pipeline, synthesis and citation handling remain untested here.

### 2026-08-08 — security pass

Free paths only; no provider call at any point.

| Check | Status |
|---|---|
| Finding reproduced **before** the fix: anonymous `GET /api/positions` returned the full book; anonymous `POST`/`DELETE` mutated it (throwaway ticker, book restored) | CONFIRMED |
| All 12 user-data routes → 401 without a credential | PASS |
| Same 13 → 200 with a session cookie, and with `X-Agent-Key` | PASS |
| Wrong `X-Agent-Key` → 401 | PASS |
| Paid `/api/research/*` and `POST /api/portfolio/outlook` → 401 before reaching a provider (no spend) | PASS |
| Public `/health`, `/api/brent`, `/api/demo/PLTR`, `/api/history` → 200, unchanged | PASS |
| `GET /v2` → 404 | PASS |
| Fail-closed with `AGENT_SECRET` unset: data routes, paid routes and `POST /api/session` all 401; `/health` still 200 (throwaway instance, scratch DB) | PASS |
| Book intact after all probing — 5 positions, no residue | PASS |
| Boot with no session lands on unlock, not onboarding; only `/api/session` is called pre-auth | PASS |
| `aria-expanded="false"` + `aria-controls` present on load, before any interaction | PASS |
| Panel CSS at 1400 / 1100 / 900 / 780px: closed panels sit fully off-screen (x=-347), scrim tracks `.open` per breakpoint, stale `col1.open` at 900px does not raise the scrim, panel top == grid top at every width | PASS |

**Not exercised, and why:**

- **The panel click-through.** The ✕/scrim/Escape handlers wire only after
  `startTerminal()`, which needs an unlocked session. The CSS layer was driven
  directly and the JS wiring read, but the interactive path was not executed
  here. The 2026-08-07 session did exercise it; treat that as the evidence.
- **Screenshots.** The browser pane was not displayed, so the page composited no
  frames — which also froze CSS transitions mid-flight and briefly looked like a
  bug until isolated by disabling the transition.
- **The Docker image.** Docker is not installed on this machine, so
  `.dockerignore` is correct by inspection but no image was built.
- **`pip-audit`.** Not run; no lockfile, so resolved versions are unknown. No CVE
  is claimed anywhere in this document.
- **Platform controls** at the deploy target (WAF, platform rate limits, TLS
  termination, whether `TRUST_PROXY` is actually set) are outside the repo.

---

## 9. Running the Project

```bash
# Install dependencies
pip install -r requirements.txt

# Start server (single worker — rate limiter requires it)
uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1

# CLI (free path — no AI spend)
python main.py PLTR

# Verification harness (free — reads statements, no Perplexity)
python scripts/verify_quant.py            # portfolio + watchlist
python scripts/verify_quant.py PLTR MELI  # a subset
```

Required `.env` keys:

```
AGENT_SECRET=<your-password>   # now gates the book too, not just spend
PERPLEXITY_API_KEY=<key>       # optional — falls back to Groq
GROQ_API_KEY=<key>             # optional — needed only as fallback
TRUST_PROXY=                   # 1 ONLY behind a proxy you control — read .env.example first
ARGUS_DB=                      # optional — point at a mounted volume in a container
```

Without `AGENT_SECRET` the app is fail-closed: every gated route returns 401 and
the terminal cannot be unlocked. `/health` and the market-data routes still serve.

The UI requires an unlock on every visit. That is deliberate — see §4.
