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
api.py                        FastAPI app — routes, tiered auth, rate limit, CSP
config.py                     MY_PORTFOLIO, WATCHLIST, BRENT_LEVELS, GEO_TRANSMISSION,
                               GEO_EVENT_LIBRARY
main.py                       CLI: python main.py PLTR | scan | brent
store.py                      SQLite — positions, watchlist, sessions, profile,
                              guest keys + dive budget, access requests
tools/perplexity_research.py  the ONLY AI path — prompts, modules, synthesis
tools/quant.py                DCF, ROIC, FCF yield, PEG, Sharpe, Beta (pure Python)
tools/fundamentals.py         annual statements with provenance (imported by quant.py)
tools/fx.py                   currency conversion for foreign-listed names (ADRs)
tools/stock_data.py           yfinance prices + live-book context
tools/oil_price.py            Brent futures → macro gate signal
tools/validator.py            validate_ticker, guard_tool_output, sanitize_question
tools/xirr.py                 money-weighted return for the portfolio analytics route
tools/notify.py               best-effort Telegram push (access-request alerts)
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
| a **guest key** (`gk_…`) | issued by the owner, stored **hashed** in `guest_keys` | A 24-hour credential handed to one other person. Redeemed at `POST /api/session` like `AGENT_SECRET`, but yields a `tier='guest'` session: read-only on the book, and worth exactly **one** 5-stage research dive on **one** ticker. The raw key exists only in the approve response — the database holds SHA-256 and a 6-char display prefix. |

## Cost

**AI research spends real money — roughly $0.21 for a full 5-module run.**

Never trigger a paid research call to "check something works". The free paths
(`/api/demo`, `/api/portfolio`, `/api/history`, `/api/brent`, `/api/meta`,
`/api/news`, `/api/earnings`, `/health`) cover almost all verification. The frontend deliberately shows a cost-confirmation
modal before any paid stage; do not add a code path that bypasses it.

## Access tiers

Three of them, and every gate keys off the tier rather than "has a session":

| Tier | How they arrive | What they get |
|---|---|---|
| **visitor** | "Continue without a key" on the landing — no session at all | Quant, charts, history, news, earnings, the Brent gate. Frontend-only tier; the server never issues it. |
| **guest** | A 24-hour `gk_…` key the owner issued | Visitor features, plus **read-only** sight of the owner's book, plus exactly one 5-stage dive on one ticker. |
| **owner** | `AGENT_SECRET` | Everything, plus the admin view. |

`app.js` **derives** the tier — `const tier = () => api.auth.effectiveTier()` —
and never stores it. It was a `state.tier` field kept in step by a `syncTier()`
call after every refresh, and one call site forgot: unlocking through the config
modal left the mirror reading `visitor`, so the real owner's `add`/`rm` were
refused as a stranger's. A getter cannot drift from what it reads, and
`verify_consistency.py` fails the build if a `state.tier` field or `syncTier`
reappears.

The guest allowance is rendered as a **count** (`PLTR 2/5`), never a
spent/unspent flag — the budget is five separately consumable stages, and a
guest one stage in still has four.

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
js/api.js             fetch layer, tiered auth state, error normalisation
js/copy.js            disclaimer / privacy / freshness — defined once, imported everywhere
js/tour.js            five-step coach-mark orientation (no imports, no network)
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
- Bump the `?v=` on `style.css`, `index.html` and **every** sub-module import
  — `app.js`, `ui.js` and `views.js` each carry their own — together whenever
  frontend files change. `grep -rn '?v=' static/` must show exactly one number,
  and `verify_consistency.py` fails the build if it does not. This is not
  housekeeping: two versions means two module instances and two `auth` objects,
  which shipped once already (see the gotcha below).

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
- **Everything caller-supplied that reaches a provider prompt is fenced**, and
  there are now three such inputs: stored `thesis`/`note`/`sector`, the posted
  synthesis body, and the `question` query parameter. All go through
  `sanitize_prompt_text()` and arrive inside `<<<UNTRUSTED:…>>>` markers the
  text cannot forge. Inside that function, fence stripping runs *before* the
  HTML strip: `_HTML_TAG_RE` is `<[^>]+>` and will eat a `<<<END:X>` prefix,
  which destroys the evidence and makes the real guard a no-op.
  `scripts/verify_hardening.py` asserts it.

  **That ordering is why `question` is fenced with ONE call, not
  `sanitize_prompt_text(sanitize_question(q))`.** Chaining them puts the HTML
  strip first, so a forged `<<<END:QUESTION>>>` is eaten down to a bare `>>`
  before the fence-stripper ever sees it. The forged fence is broken either
  way — but chained it is broken by accident of a regex's shape, and editing
  that regex silently removes the protection. `sanitize_prompt_text` takes the
  same length cap as a parameter (`max_len=_MAX_QUESTION_LEN`), so nothing is
  lost by dropping the chain. The harness asserts both the neutralisation and
  the absence of the chained form.

  `question` is fenced inside `run_research_module`, not at the API boundary,
  so the route and the CLI are both covered — a guard that runs for only some
  callers is a guard waiting to be bypassed.
- **A session is a tier, never a boolean.** `store.session_info()` returns
  `{tier, guest_key_id, expires_at}`; `api._auth_ctx()` turns that into an
  `AuthCtx`. The old `session_valid() -> bool` was the whole vulnerability:
  `require_auth` accepted *any* valid session, so the moment a second credential
  class existed its holder was indistinguishable from the operator and inherited
  `POST /api/portfolio/outlook` — ~$0.06 a call with no budget concept at all.
  **Never reintroduce a bare-boolean credential check.** `require_owner` guards
  three kinds of route — the five book-mutating ones, the two unbudgeted paid
  ones, and the whole `/api/admin/*` surface (seven routes) — and answers a
  guest with **403, not 401** — a 401 sends the frontend to the unlock screen and
  asks a guest for an owner secret they were never given.
- **The guest budget is per-module, and the PRIMARY KEY is the lock.** A dive is
  five stages (4 module GETs + synthesis). A ticker-only budget would still let a
  guest re-fire `/report` a hundred times. `guest_key_uses` has
  `PRIMARY KEY (key_id, module)`, and `consume_guest_unit()` claims a stage with
  one `INSERT` inside `BEGIN IMMEDIATE` — there is no read-then-write window,
  which matters because the four module calls of a staged run overlap.
  `scripts/verify_access.py` races 8 threads at one stage and asserts exactly one
  winner. **Everything in that function talks to the raw connection**: `_lock` is
  a plain non-reentrant `threading.Lock` and `_rows`/`_exec` both acquire it, so
  calling either from inside the lock deadlocks on the first guest research call.
  The surrounding file's idiom is the opposite.
- **Refund only what provably cost nothing.** `_refund_if_free()` keys off a
  structured `cost_incurred: False` set by the layer that made — or did not make
  — the network call, never off the wording of an error string. Three cases
  qualify: no provider configured, a 401, and a 429; all are rejected before any
  work is billed.
  Any other failure keeps the stage: a mid-flight timeout may already have been
  billed, and refunding it would be unlimited retries of a paid operation. A
  refund never unbinds `dive_ticker`, so "one dive, one ticker" stays a rule with
  no exception to explain in the UI.
- **The sessions migration lives in `_connect()`, not `bootstrap()`.**
  `bootstrap()` returns early once the `seeded` meta flag is set, so a migration
  placed there would never run on exactly the established databases that need it.
  `_migrate()` inspects `PRAGMA table_info` rather than tracking a version, so
  running it twice is a no-op by construction.
- **A "free" harness must set provider keys to `""`, never pop them.** `api.py`
  calls `load_dotenv()` at import with the default `override=False`, which fills
  any name *absent* from the environment — so popping `PERPLEXITY_API_KEY` and
  then importing `api` hands the harness a live provider client off the real
  `.env`. Setting the name to an empty string keeps it present, dotenv skips it,
  and `_get_provider()` falls through to its `ValueError`. `verify_access.py`
  does this, asserts the emptiness *after* import, and additionally counts
  provider invocations — with a positive control proving the counter can fire, so
  "never entered" is evidence rather than a monkeypatch that silently missed.
- **A blank line in `.env` is not the same as an absent one, and `ARGUS_DB`
  used to get this wrong.** `.env.example` documents `ARGUS_DB=` (and
  `TRUST_PROXY=`, `ALPHA_VANTAGE_KEY=`) as "leave blank for the default" — but
  `load_dotenv(override=False)` only skips a name already present in the
  process environment; a `KEY=` line with nothing after the `=` still SETS
  that key, to `""`. `os.getenv("ARGUS_DB", default)` only falls back when the
  key is *absent*, so a present-but-empty value silently wins over the
  default, `Path("")` resolves to the cwd, and `sqlite3.connect()` on a
  directory fails with an opaque "unable to open database file" that gives no
  hint the cause is an env var. This is exactly what a fresh
  `cp .env.example .env` produces — it is not a hypothetical. `DB_PATH` is now
  `Path(os.getenv("ARGUS_DB") or (Path(__file__).parent / "argus.db"))`; `or`
  treats "unset" and "set to empty" as the same request, matching what the
  comment already promised. `TRUST_PROXY` never had this bug — `_trusted_hops()`
  wraps the `int()` conversion in a `try/except ValueError` and fails closed to
  0, so an empty string degrades safely there by accident of unrelated code,
  not by the same guarantee.
- **`POST /api/research/{t}/synthesis` fences its body.** `_synthesis_prompt()`
  interpolates the four posted module strings and `run_synthesis` fires three
  paid calls off them. While the only caller was the operator's own browser
  echoing back model output that was a trusted loop; guest access makes it
  untrusted input steering three calls on the *owner's* provider account. Every
  field goes through `sanitize_prompt_text()` for **every tier** — a guard that
  runs for only some callers is a guard waiting to be bypassed.
- **`/api/meta` is public; `/api/info` is not, and the difference is
  `GEO_TRANSMISSION`.** `BRENT_LEVELS` is never overridden by `config_local.py`
  (the override imports only `MY_PORTFOLIO`, `WATCHLIST`, `GEO_TRANSMISSION`), so
  the Brent ladder is always the published framework and is safe to serve to a
  keyless visitor. `GEO_TRANSMISSION` *is* overridden and its values name real
  holdings by construction (`"AAPL (supply chain exposure)"`), which makes the
  geo map a description of the book. **Do not loosen `/api/info` to feed visitor
  mode** — add to `/api/meta` instead. `verify_access.py` plants a sentinel
  ticker in `GEO_TRANSMISSION` and asserts it never reaches `/api/meta`.
- **The tour sits at z-index 55 — below the modal layer at 60.** The
  cost-confirmation dialog lives at 60 and is the project's guarantee against
  unintended spend. Above it, `#tour` would dim that dialog and, since its root
  takes pointer events across the viewport, swallow the click on CANCEL.
  `startTour()` additionally refuses to run while any `.ov` or the gate is open.
- **Request emails and notes are attacker-supplied and land in the owner's
  authenticated page.** Everything in the admin view is written with
  `textContent`/`el(tag, cls, text)` — never interpolated into a template,
  escaped or not. The CSP would stop an injected `<script>` executing, but markup
  injection could still deface the view, and there is no reason to lean on the
  CSP for a value that has no business being markup.
- **A denied access request is inert.** `email_norm` is UNIQUE and the conflict
  clause only updates *pending* rows, so a denied person's resubmissions change
  nothing and return the same cheerful body forever. That is deliberate
  anti-enumeration — but it means one misclick is a silent permanent block, so
  denied rows stay **visible and reopenable** in the admin view.
- **The owner's Telegram push fires only on `is_new`, not on every 200.**
  `POST /api/access-request` returns the identical body for a new address, a
  pending resubmission, and a no-op hit on an approved/denied row — that's the
  anti-enumeration guarantee above. Notifying on every one of those would let a
  denied address on a retry loop page the owner's phone forever over a
  submission that changes nothing in the database. `store.create_access_request`
  now returns `(outcome, is_new)`; `api_access_request` only schedules
  `notify_telegram` when `is_new` is True. The push itself runs via FastAPI
  `BackgroundTasks` **after** the response is sent, and `tools/notify.py`
  swallows every failure (unset token, network error, Telegram outage) — a
  side-channel notification must never affect what the public endpoint returns.
  No `parse_mode` is set on the Telegram call, so the caller-supplied email/note
  reach Telegram as literal text with no Markdown/HTML injection surface.
- **The owner can approve a request by replying to the Telegram push with the
  email — no webhook, and it works identically on localhost or deployed.**
  `tools.notify.start_telegram_reply_listener()` long-polls Telegram's
  `getUpdates` from a daemon thread rather than registering a webhook: a
  webhook needs a public HTTPS URL Telegram can call back into, and polling
  needs only outbound requests, which this process can always make. It starts
  from `api.py`'s FastAPI `lifespan`, not at import time — a bare `import api`
  or the bare `TestClient(api.app)` this project's whole harness suite uses
  (no `with` block) never runs `lifespan`, so importing or testing api.py
  starts no thread and touches no network. Only `with TestClient(app) as c:`
  or a real `uvicorn.run()` triggers it.
  **The chat-id check in `_process_update` is the entire authorization
  boundary** — a message from any chat other than the configured
  `TELEGRAM_CHAT_ID` is dropped before it ever reaches the handler. The bot is
  not a public interface; anyone who starts a chat with it and replies gets
  silently ignored, not an error.
  `api._handle_telegram_reply()` matches the reply's email against **pending**
  requests only (`store.list_access_requests("pending")`), which is what stops
  a stale or duplicate reply from minting a second key for an address already
  approved or denied — the email simply will not be found there any more.
  Approval itself goes through `api._mint_and_approve()`, shared with the
  admin web route's APPROVE button, so the two paths cannot drift into
  different behaviour. On first start the listener discards whatever backlog
  `getUpdates` returns before processing anything — a server restart must not
  replay a reply from days ago and auto-approve something stale.
- **Every `?v=` in `static/` must agree.** ES modules are keyed by full URL, so
  `api.js?v=15` and `api.js?v=16` are two module instances with two separate
  `auth` objects. This actually shipped: `views.js` sat a version behind, so SIGN
  OUT in the profile view cleared one auth cache while `app.js` read the other
  and still believed the session was live, and the accent swatches fired a
  listener array the chart had never subscribed to. `verify_consistency.py` now
  asserts agreement (not a specific number, so future bumps need no edit) — and
  caught the same drift recurring during the very next change.

- **`TRUST_PROXY` is a hop count, not a boolean.** It says how many reverse
  proxies you control sit in front of the app; 0/unset means none. `_client_ip()`
  and `_is_https()` index `X-Forwarded-*` that many entries *from the right*,
  because those headers are appended to — the leftmost entry is written by the
  client and is hostile input. Setting it higher than the number of proxies you
  actually run reads into the client-controlled part and silently restores the
  rate-limiter bypass. `scripts/verify_proxy_trust.py` asserts the boundary.

## Verification harnesses

Every one of these spends nothing. The quant pair reads yfinance statements,
prices and FX crosses; the rest use `TestClient` against a scratch `ARGUS_DB`
with the provider keys blanked. **Run them all before any PR.**

```
scripts/verify_quant.py           the numbers are right
scripts/verify_metric_status.py   the absences are right, and FX converts
scripts/verify_access.py          owner vs guest, the one-dive budget, request flow
scripts/verify_hardening.py       limiter budgets, backstop, prompt fencing, launch flags
scripts/verify_proxy_trust.py     TRUST_PROXY hop-count boundary
scripts/verify_consistency.py     one quantity, one value — P&L rounding and ?v=
scripts/verify_ticker_validation.py  every ticker write goes through the validator
scripts/verify_docs.py            this file has not drifted from the tree
```

One line to run the free set:

```
python scripts/verify_access.py && python scripts/verify_hardening.py && python scripts/verify_proxy_trust.py && python scripts/verify_consistency.py && python scripts/verify_docs.py && python scripts/verify_ticker_validation.py
```

`verify_access.py` is the one to extend when touching auth. Its cost safety is
structural rather than intentional — see the `.env` re-injection gotcha above —
and it holds a **positive control** that drives an entitled caller through the
same stubbed provider, so a zero invocation count is evidence rather than a
monkeypatch that silently failed to bind. A control that has stopped
controlling is worse than no control.

`verify_quant.py`'s `currency mismatch` assertion was inverted as part of the FX
work: it
used to require `dcf_intrinsic_value is None` whenever the currencies
disagreed, which was right when declining was the only honest option and is
wrong now that `tools/fx.py` converts. It now asserts the opposite, and reads
`currency_mismatch["resolved"]` three ways — **absent fails** (the FX feature
has been removed), **False skips** (the cross did not load, and holding the
metrics back is correct behaviour), **True asserts** the converted DCF, the
FCF yield scale, and that `sources.dcf.fx` records the rate. Do not collapse
those three into a boolean; a plain truth test either goes flaky on an
upstream outage or goes blind to the regression.
