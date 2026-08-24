# Phase 3 — Production Readiness

**Derived from:** the ECC production-audit skill — five phases (release
surface, recent changes, runtime boundaries, operational readiness, score and
recommend) with a banded ship/block score and explicit "evidence checked" /
"evidence missing" sections. Extended here with A03:2025 supply chain and
A10:2025 exceptional conditions, both of which the source material predates.

**Maps to:** A02:2025 Security Misconfiguration · A03:2025 Software Supply
Chain Failures · A08:2025 Integrity Failures · A09:2025 Logging and Alerting ·
A10:2025 Mishandling of Exceptional Conditions · API8/API9:2023 · ASVS V13.

---

## 3.1 Configuration and startup

- Every referenced env var is documented and present. Cross-check the code's
  actual reads against `.env.example`.
- **Fail fast on missing critical config.** The app refuses to start without
  its database URL, auth secret, and provider keys. The dangerous anti-pattern
  is a fallback default: `SECRET_KEY = os.getenv("SECRET_KEY", "dev")` ships a
  publicly-known signing key. Treat any secret with a literal default as
  CRITICAL.
- Debug mode off: Django `DEBUG=False`, Flask `debug=False`, Rails
  `config.consider_all_requests_local=false`, Next.js production build, no
  `--inspect`.
- `NODE_ENV` / `ENV` actually set to production in the deploy config, not just
  assumed.
- Environment separation: production is not pointed at a staging database, and
  test API keys are not in production (nor live keys in staging).

## 3.2 Debug and dead code

- Debug logging of request bodies, tokens, or SQL.
- Test-only routes: `/test`, `/debug`, `/seed`, `/admin-backdoor`, `/reset-db`,
  `/impersonate`, `/dev-login`. Any endpoint that bypasses auth "temporarily".
- Hardcoded test accounts and default credentials, especially any
  `admin`/`admin` seeded user.
- Feature flags defaulting open.
- `TODO` / `FIXME` / `HACK` comments that reference an unfinished security
  control — these are a map of known-missing defences and belong in the report.
- Commented-out auth checks. Read every commented-out block near an auth
  boundary; "temporarily disabled" checks are a recurring launch-day breach.
- API docs (Swagger/OpenAPI/GraphQL introspection) exposed publicly when they
  were meant to be internal — API9:2023.

## 3.3 Error handling — A10:2025

Two failure modes, and most audits only check the first.

**Leaking errors.** No stack trace, SQL text, file path, framework version,
internal hostname, or raw exception message reaches the client. Return a
generic message plus a correlation id; log the detail server-side. Check the
404/500 pages and the framework's default error page, not just your own
handlers.

**Fail-open errors.** The more dangerous one, and the reason A10 is new in the
2025 list. Look for:

- `try/except` (or `catch`) around an authorization or verification check whose
  handler returns success, `None`, or falls through to the protected code.
- A permission helper that returns `true` on an unexpected input rather than
  denying.
- A rate limiter, feature gate, or licence check that lets the request through
  when its backing store is unreachable.
- A signature or token verification wrapped in a broad `except Exception: pass`.
- Timeouts and partial failures leaving state half-written with no compensation.
- Bare `except:` / `catch {}` anywhere on a security path.

Default deny is the rule. If the check cannot run, the answer is no.

## 3.4 Security headers

Applies to HTML responses; API-only services need a smaller set.

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (add `preload` only if you mean it — it is hard to undo) |
| `X-Content-Type-Options` | `nosniff` |
| `Content-Security-Policy` | Restrict `script-src` to self or a nonce. `frame-ancestors 'none'` supersedes `X-Frame-Options`. Avoid `unsafe-inline`/`unsafe-eval`; if present, that is the finding. |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | Deny camera/microphone/geolocation unless used |
| Remove | `X-Powered-By`, verbose `Server` |

Node: `helmet` gets most of this. Verify the CSP is not effectively disabled by
a wildcard or by `unsafe-inline` added to silence a console error — that is the
common way a CSP ends up decorative. If the app serves both pages and APIs,
check the policy is correct for each; a single global CSP is usually wrong for
one of them.

Cookies: `Secure`, `HttpOnly`, `SameSite` — see Phase 2.

## 3.5 Rate limiting and resource consumption — API4:2023

- Authentication endpoints limited: login, signup, password reset, OTP, magic
  link, token refresh. A sane floor is ~5 login attempts/minute/IP and ~3
  password resets/hour/account.
- **Limit by account as well as by IP.** IP-only limiting is trivially defeated
  and punishes users behind shared NAT.
- **Trusted proxy configuration.** If the limiter reads `X-Forwarded-For`
  without knowing which proxy is trusted, a client can spoof the header and
  bypass the limit entirely. Confirm the app takes the correct hop.
- Expensive endpoints limited: search, export, report generation, image
  processing, and **every endpoint that spends money** — LLM calls, SMS, email.
  An unauthenticated endpoint that calls a paid API is a direct financial DoS.
- Request body size limits, upload size limits, pagination caps (a `limit`
  parameter with no maximum is a DoS), query timeouts, and depth/complexity
  limits on GraphQL.
- **Where does the limiter's state live?** An in-process dictionary is
  per-worker, so it silently multiplies by the worker count and resets on
  deploy. If the app runs more than one process, an in-memory limiter is not a
  limit — that is a finding, and it must be named explicitly because it looks
  correct in code review.

## 3.6 CORS

- Not `Access-Control-Allow-Origin: *` on anything authenticated. `*` plus
  credentials is rejected by browsers, and the usual "fix" — reflecting the
  request's `Origin` header — is worse, because it allows every origin.
- Origin allowlist is exact-match. Check the matching logic for the classic
  bugs: `startsWith("https://myapp.com")` matches
  `https://myapp.com.evil.com`; a regex with an unescaped `.` matches
  `myappXcom`; `endsWith(".myapp.com")` matches `evil-myapp.com` if the dot is
  missing.
- `Access-Control-Allow-Credentials: true` only where genuinely needed.

## 3.7 Transport and database

- HTTPS enforced; HTTP redirected. No mixed content.
- Database connections use TLS in production; certificate verification not
  disabled.
- No default credentials; the app's DB user has only the privileges it needs
  (not superuser); the database port is not publicly reachable.
- Object storage buckets are private by default. A public bucket used for user
  uploads is a data leak waiting for someone to guess a path.
- Admin interfaces (DB admin, queue dashboards, metrics) are not internet-facing
  without auth.

## 3.8 Supply chain — A03:2025 (new)

The source material this skill improves on omits this entirely, and it is now
the third-ranked category.

- **A lockfile exists and is committed.** Without it, builds are not
  reproducible and a compromised release can enter silently.
- Run the ecosystem's audit: `npm audit`, `pnpm audit`, `pip-audit`,
  `cargo audit`, `govulncheck`. Report actual findings with the resolved
  installed version — **never claim a dependency is vulnerable without checking
  the version in the lockfile**, and never invent a CVE id.
- Look for typosquats and recently-published, low-download packages, especially
  any dependency added by an AI builder. A hallucinated package name that
  someone has since registered is a live attack ("slopsquatting").
- Install scripts (`postinstall`) in dependencies.
- CI: third-party GitHub Actions pinned to a commit SHA rather than a mutable
  tag; no `pull_request_target` running untrusted code with secrets in scope;
  secrets not exposed to fork-triggered workflows.
- Any script piped from the network into a shell in build or deploy steps.
- Base image in the Dockerfile pinned and reasonably current; container not
  running as root.

## 3.9 Logging and alerting — A09:2025

Phase 2 checked that logs do not contain PII. This checks that logs exist.

- Are auth events logged at all — login success and failure, password change,
  privilege change, MFA change, token issuance?
- Are administrative and destructive actions logged with actor, target,
  timestamp, and source IP?
- Do logs persist somewhere durable, or vanish with the container?
- Is anything actually alerted on, or is it write-only? An unmonitored log is
  forensics-only, which is worth having — but say which one you have.
- Can a user tamper with or flood the log to hide activity?

## 3.10 Operational readiness

- Health check that reflects real dependency status without leaking versions,
  hostnames, or connection strings.
- Migrations are reversible, or the irreversible ones are known and stated.
- A rollback path exists and someone has described it.
- Backups exist and a restore has been tried at least once. Untested backups
  are not backups.
- Tests cover the critical paths — auth and payment at minimum. CI green.

## Output of this phase

A pass/fail line per check with the evidence that supports it, then the
findings. Every `FAIL` becomes a finding; every check you could not evaluate
goes to **Not checked** with the reason.
