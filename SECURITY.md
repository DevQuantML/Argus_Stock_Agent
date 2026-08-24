# Security Policy

Argus is a single-maintainer personal research tool, not a versioned
library — there is no supported-versions matrix here. `main` is always the
supported line.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for a security report — the
repo may hold real portfolio and API-key configuration in `config_local.py`
and `.env`, which are gitignored but a report describing an exploit path
against them deserves a private channel regardless.

Use GitHub's private reporting instead: **Security tab → Report a
vulnerability** on this repository. That opens a draft security advisory
visible only to the maintainer until it's resolved.

## Scope

In scope: anything reachable through `api.py`'s routes, the auth/session
model (`AGENT_SECRET`, guest keys), rate limiting, and the frontend's
handling of untrusted (LLM-generated) content. Out of scope: the third-party
data providers (Perplexity, Groq, yfinance) themselves.
