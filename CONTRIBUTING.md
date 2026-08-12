# Contributing

Thanks for looking. This is a small, opinionated codebase — the rules below exist
because each one has already cost somebody a debugging session.

## Before you open a PR

Run the free verification harnesses. All four spend nothing:

```bash
python scripts/verify_quant.py             # quant engine, 121 assertions
python scripts/verify_metric_status.py     # metric provenance, 396 assertions
python scripts/verify_ticker_validation.py # ticker guard + free routes
python scripts/verify_proxy_trust.py       # proxy trust boundary, 17 assertions
```

If you changed anything under `tools/`, say in the PR which harness you ran and
paste the summary line. "Should be fine" is not a verification.

## The rules that matter

**Never commit `config_local.py` or `.env`.** Both are gitignored. `config.py`
ships placeholder positions on purpose — put real holdings in `config_local.py`,
which overrides them automatically. A real portfolio in a public commit is very
hard to un-publish.

**Never add a code path that spends money without confirmation.** AI research
costs roughly $0.05 per module. The UI shows a cost-confirmation modal before any
paid stage; don't route around it. When testing, use the free paths — `/api/demo`,
`/api/brent`, `/api/history`, `/health` — which cover almost everything.

**Perplexity is the only research engine; Groq is the declared fallback.**
Anthropic and OpenRouter were removed deliberately. Don't reintroduce an SDK to
solve a problem a prompt change can solve. Groq has no live web search, so any
prompt depending on freshness must declare that degradation in its own output
when `provider == "groq"` — see `_policy_prompt()` for the pattern.

**Treat model output as hostile.** It goes through `renderMarkdown()` in
`static/js/md.js`, which HTML-escapes before parsing. Never `innerHTML` a model
response directly.

**`TRUST_PROXY` is a hop count, not a boolean.** `X-Forwarded-*` headers are
appended to, so the leftmost entry is written by the client and is hostile input.
The code indexes from the right. Setting the count higher than the number of
proxies you actually run silently reintroduces a rate-limiter bypass.

## Frontend specifics

- **No build step, no npm, no CDN.** Everything is same-origin — the CSP is
  `script-src 'self'` and there must be zero external network requests.
- **All colour goes through CSS custom properties.** Never hardcode a colour in a
  component rule or an SVG presentation attribute; SVG attributes don't resolve
  `var()`, so use a CSS class instead.
- **Bump the `?v=` cache-busters together** on `style.css`, `app.js` and the
  sub-module imports in `app.js` and `ui.js` whenever frontend files change.
- **Never let an animation gate the pipeline.** Background tabs clamp
  `setTimeout` to ~1s, which once turned the typewriter into an indefinite stall
  that blocked a paid run mid-flight.

## Reporting a security issue

Please don't open a public issue for anything exploitable. Open a private
security advisory through the repository's Security tab instead.

Known open items are listed honestly under **Known limitations** in the
[README](README.md) — if you're reporting one of those, no need, but a PR fixing
one is very welcome.

## Style

Match the surrounding code. Comments explain *why*, not *what* — the existing
code is dense with rationale for decisions that look arbitrary otherwise, and
that's deliberate. Keep it.
