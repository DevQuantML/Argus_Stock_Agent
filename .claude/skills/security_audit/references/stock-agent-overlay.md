# Overlay — Stock Agent / ARGUS://TERMINAL

Load this **in addition to** the phase files when working in the Stock Agent
repository. Detect it by: `api.py` + `tools/perplexity_research.py` +
`ARGUS` in `static/`.

**Everything below is a check to run, not a finding.** The line references were
read from the tree and are anchors for where to look; none of these has been
audited. Do not carry an item into a report without running it through the
gates in `verification-gates.md`.

---

## Hard constraints for this repo

**AI research spends real money — roughly $0.21 for a full 5-module run.**
Never call a paid path to verify a fix. The free paths cover almost all
verification: `/api/demo/{ticker}`, `/api/portfolio`, `/api/history/{ticker}`,
`/api/brent`, `/health`, `/api/info`. If a check genuinely requires a paid run,
stop and ask.

The frontend shows a cost-confirmation modal before any paid stage. **A finding
whose fix bypasses that modal is not a fix.** Treat any code path that reaches
a paid provider without the confirmation as a CRITICAL finding in its own
right.

Per `CLAUDE.md`: verify against the artifact, not against a previous summary.
A prior session's "done" is a claim to be tested.

## Phase 1 — the three-key separation

This repo has three credentials that are routinely conflated. Confirm each stays
on its side of the boundary.

| Key | Belongs in | Never in |
|---|---|---|
| `AGENT_SECRET` | `.env` **and** the browser Settings drawer — it is a password the user chooses | — |
| `PERPLEXITY_API_KEY` | `.env` only | Any response body, any `/static/` asset, any log line |
| `GROQ_API_KEY` | `.env` only | Same |

- `config.py:149-151` documents the rule that `os.getenv()` must be called at
  point of use rather than captured at module scope. Verify the code follows
  its own comment — grep for module-level assignments of provider keys in
  `tools/perplexity_research.py` (reads at `:74-75`) and `api.py` (`:312-313`).
- `api.py:292` `/health` and `api.py:253` `/api/info` are unauthenticated and
  report provider status. Confirm they expose *presence* of a key, never a
  prefix, length, or value.
- `.gitignore` covers `.env`, `.env.*` with `!.env.example`, and the SQLite
  triple (`argus.db`, `-wal`, `-shm`). Confirm none of them is already in
  history: `git log --all --oneline -- argus.db .env`.
- **`.claude/` is not in `.gitignore` and is untracked.** It contains
  `worktrees/pensive-shtern-4acd47/` — a second full copy of the source tree
  including its own `api.py`, `config.py`, `store.py`, and `tools/`. A
  `git add -A` would commit it. Check whether that worktree carries a `.env`,
  and decide whether `.claude/` belongs in `.gitignore`.
- `.env.example` exists — cross-check it against every `os.getenv` call in the
  tree so it is complete rather than stale.

## Phase 2 — data flow

- `store.py` backs a local SQLite DB (`store.py:33`, path overridable via
  `ARGUS_DB`) holding profile, positions, watchlist, and sessions. Confirm the
  session table stores a hash of the session token, not the token itself, and
  that sessions expire.
- The portfolio endpoints return holdings and P&L. Commit `7fe1875` claims a
  portfolio data leak was closed — re-verify that against the current code
  rather than trusting the message. Specifically: does every read scope to the
  current session, or is there a path that returns the default `MY_PORTFOLIO`
  from `config.py` to an anonymous caller?
- Prompts sent to Perplexity or Groq leave the machine. Confirm no user profile
  data, position sizes, or cost basis is interpolated into a prompt unless
  intended — check the prompt builders in `tools/perplexity_research.py`.
- `ARGUS_DB` is env-controlled; confirm it cannot be influenced by a request.

## Phase 3 — production readiness

- **The in-memory rate limiter (20 req/min/IP) only works with `--workers 1`.**
  This is documented in `CLAUDE.md` and is exactly the failure mode in
  §3.5 — correct-looking in code review, silently multiplied by worker count in
  production. Check the actual deploy command in `Dockerfile` and any platform
  config. If workers > 1, the effective limit is 20 × workers and the audit
  must say so.
- `api.py:147` gates `X-Forwarded-For` trust on `TRUST_PROXY == "1"`. Confirm
  the deployed environment sets it correctly: unset behind a proxy means every
  request is attributed to the proxy's IP and the limiter is global; set when
  *not* behind a trusted proxy means a client can spoof the header and bypass
  the limiter entirely.
- `api.py:235` refuses something when `AGENT_SECRET` is unset. Confirm the
  behaviour is fail-closed (paid routes refuse) and not fail-open (auth
  skipped). This is the A10:2025 check and it is the single highest-value line
  in the file.
- **CSP is path-aware**: pages and `/static/*` get a same-origin policy, API
  routes keep `default-src 'none'`. There must be zero external network
  requests — no CDN, no Google Fonts (the system mono stack in `--mono` exists
  because `font-src 'self'` blocks it). Verify no recent frontend change
  reintroduced an external URL.
- `/health` (`api.py:292`) always returns 200 by design, with status in the
  body. Confirm the body does not leak versions, key prefixes, or paths.
- Supply chain: `requirements.txt` — is it pinned? Run `pip-audit` and report
  only versions actually resolved.

## Phase 4 — critical logic

- **Auth coverage.** `require_auth` is defined at `api.py:213`. These routes
  declare `dependencies=[Depends(require_auth)]`:
  `/api/research/{ticker}` (`:533`), `/report` (`:579`), `/context` (`:585`),
  `/policy` (`:591`), `/patterns` (`:597`), `POST /synthesis` (`:603`), and
  `POST /api/portfolio/outlook` (`:877`).

  These do **not** declare it: `/api/session` (`:625`, `:652`, `:662`),
  `/api/profile` (`:679`, `:685`), `/api/positions` (`:712`, `:717`, `:736`),
  `/api/watchlist` (`:745`, `:750`, `:759`), `/api/portfolio` (`:432`),
  `/api/portfolio/analytics` (`:770`), `GET /api/portfolio/outlook` (`:871`).

  That asymmetry is expected — `AGENT_SECRET` gates *spend*, sessions gate
  *user data* — but it must be verified rather than assumed. For every route in
  the second list, confirm it derives identity from the session and cannot be
  induced to read or write another session's rows. Note the GET/POST split on
  `/api/portfolio/outlook`: confirm the GET reads only cached output and cannot
  trigger generation.

- **Financial DoS (API4:2023).** Every route that can reach a provider must be
  behind `require_auth` *and* rate-limited. Enumerate the call graph from each
  route into `tools/perplexity_research.py` rather than trusting the decorator
  list — an unauthenticated route that transitively reaches a paid call is
  CRITICAL.

- **`/static/{filename:path}`** (`api.py:363`) takes an unconstrained path
  parameter. This is the path-traversal check in §4.4: confirm the resolved
  path is contained within the static directory after normalisation, and that
  symlinks cannot escape it.

- **Ticker input.** `validate_ticker` and `sanitize_question` live in
  `tools/validator.py`. Confirm every route that accepts a ticker or a free
  question routes through them — `/api/demo/{ticker}`, `/api/history/{ticker}`,
  the research routes, and the positions/watchlist writers. A ticker reaches
  yfinance, a filesystem-adjacent cache, and an LLM prompt; each is a different
  sink.

- **The null-price gotcha.** yfinance returns HTTP 200 with all-null fields for
  a bogus symbol, so a null `price` is the only reliable "no such ticker"
  signal. Check this is handled at every consumer, not just one — an unhandled
  null propagating into quant math or a prompt is the A10:2025 case.

- **`guard_tool_output`** in `tools/validator.py` is the boundary for provider
  responses. Confirm every provider call passes through it.

## Phase 4.6 — the LLM boundary here

- **Model output is untrusted.** It must go through `renderMarkdown()` in
  `static/js/md.js`, which HTML-escapes before parsing. Confirm no renderer in
  `ui.js` or `views.js` assigns a model response to `innerHTML` directly. The
  typewriter reveals text nodes only, so it cannot reintroduce markup — verify
  that property still holds after the v2 frontend work.
- **Provider degradation must be declared.** Groq has no live web search. Any
  prompt depending on real-world freshness must say so in its own output when
  `provider == "groq"` — the pattern is in `_policy_prompt()`. A prompt that
  silently degrades is a correctness and trust issue; check every module in
  `tools/perplexity_research.py`.
- Ticker and question text reach a prompt. Confirm sanitisation happens before
  interpolation, and consider what a crafted ticker or question could instruct
  the model to emit.

## Phase 5 — adversarial, repo-specific

- Anonymous caller: can any route return another session's positions,
  watchlist, or profile by supplying or omitting a session identifier?
- Automation: how many free `/api/demo` and `/api/history` calls can one IP
  make, and what does each cost in upstream yfinance quota?
- Can the cost-confirmation modal be bypassed by calling the staged pipeline
  endpoints directly in a different order?
- Is there a stale deployment of an earlier ARGUS build still reachable
  (API9:2023)? Not answerable from the repo — if unchecked, put it in
  **Not checked**.

## Frontend hygiene that is also a correctness gate

Not security, but a launch blocker in this repo and cheap to check while you
are here:

- Bump `?v=` on `style.css`, `app.js`, and the sub-module imports in `app.js`
  and `ui.js` **together** whenever frontend files change. A stale cache serves
  a half-updated frontend.
- Never let an animation gate the pipeline. Background tabs clamp `setTimeout`
  to ~1s; `typeHTML()` must bail to an instant reveal on hidden tab, reduced
  motion, or a time cap, with `typeBlock()` capping again at the call site.
  A regression here once stalled a paid run mid-flight.
- Init steps are individually try/caught in `app.js`; failures must toast
  loudly rather than silently killing the command line.
- No hardcoded colour in a component rule or an SVG presentation attribute —
  SVG attributes do not resolve `var()`. Use CSS classes.
