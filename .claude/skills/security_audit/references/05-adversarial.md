# Phase 5 — Adversarial Pass

**Derived from:** the ECC security-review skill — enumerate threat vectors,
prioritize by blast radius (global before contextual, data theft before abuse),
and gate deployment on a concrete checklist.

**The shift in this phase:** Phases 1–4 ask "is this code correct?". This phase
asks "what would I do if I wanted to hurt this app?" and works backwards. It
finds a different class of bug — the ones where every function is individually
correct and the *system* is still exploitable.

**Rules of engagement.** Reason about attacks and demonstrate them against a
local instance you control. Do not attack production, do not test against real
user accounts, do not trigger paid calls, and do not send real mail or messages.
If a proof requires any of that, describe the proof precisely instead of running
it and mark it unverified.

---

## 5.1 Adopt attacker personas

Run the app's surface past each. Different starting privileges surface
different bugs.

| Persona | Starts with | Goes after |
|---|---|---|
| **Anonymous** | A URL | Any endpoint that answers without auth; enumerable data; the signup flow |
| **Legitimate user** | One valid account | Other users' data; admin functions; free credits; other tenants |
| **Malicious content author** | Ability to store text/files others will view | Stored XSS; prompt injection into an admin's LLM session; SSRF via an embed |
| **Departed insider** | A revoked account, an old token, an old API key | Tokens that outlive revocation; an integration key nobody rotated; a cached session |
| **Automation** | Scripts, many IPs | Rate limits; mass signup; scraping; resource exhaustion; race conditions |

## 5.2 Attack path — read other people's data

The highest-value path and the one to run first.

- Take every endpoint from the Phase 0 table with an id in it. For each, state
  what happens if the id is changed to one owned by someone else. If the answer
  is not "the ownership check on line N rejects it", it is a finding.
- Are ids sequential? Then enumeration is free and severity rises.
- Do list endpoints, search, exports, or `?include=` relations return records
  the caller does not own?
- Is there a tenant boundary, and does every query carry the scope?
- Do error messages and status codes differentiate "does not exist" from
  "exists but not yours"? That difference is an enumeration oracle.
- Does a cache — CDN, framework, service worker — serve one user's
  authenticated response to another? Check `Cache-Control` on authenticated
  responses; a `public` on a personalized route is a real leak.

## 5.3 Attack path — get in without credentials

- Which endpoints answer with no token? Compare against the intended-audience
  column and explain each difference.
- Malformed, expired, unsigned, `alg: none`, and other-user tokens — all
  rejected?
- Is there a route registered outside the guarded group — a webhook handler,
  a health check, a static file route, a legacy alias, a trailing-slash or
  case variant that the middleware's path matcher misses?
- Default or seeded accounts. Any `admin@example.com` left in a migration.
- Does a password reset, magic link, or OAuth callback let you land in an
  authenticated session for an account you do not own?
- Is there an unauthenticated internal endpoint that "is not linked anywhere"?

## 5.4 Attack path — become someone more important

- Can role be set or edited by the user — at signup, in a profile update, in an
  invite acceptance, in a JWT claim the server trusts?
- Are admin capabilities enforced server-side, or only hidden in the UI?
- Does an invite or team-join flow let you attach to an organisation you were
  not invited to, by changing an id?
- Does impersonation or "view as" exist, and can a non-admin reach it?
- Does the file upload or import path let you overwrite a config file, a
  template, or another user's asset?

## 5.5 Attack path — abuse what works

Nothing is "broken" here; the feature simply has no cost.

- Signup: mass account creation, disposable domains, no verification.
- Messaging, comments, invites, share-by-email: spam relay through your domain
  and your sending reputation.
- Uploads: fill the storage bill.
- Any paid call — LLM, SMS, geocoding, third-party API: run up the invoice.
  Check whether the endpoint requires auth and whether spend is capped.
- Referral and promo systems: self-referral, loops, unlimited redemption,
  concurrent redemption of a single-use code.
- Free trials: restart by re-registering, or by changing an id.
- Expensive queries: unbounded pagination, deep GraphQL nesting, regex-driven
  search over user input (ReDoS), report generation with no queue.
- Password reset flooding a target's inbox.

## 5.6 Attack path — inject content

Put a payload in every field that is stored and later rendered — username,
display name, bio, comment, filename, search term, org name, support ticket,
tag, webhook label, and any field an LLM later reads.

Ask where each field is rendered: the app UI, an admin dashboard, an email
template, a PDF export, a Slack notification, a CSV. **A field that is escaped
in the app and interpolated raw into an admin email is still stored XSS**, and
it targets the highest-privilege user. CSV exports have their own variant —
formula injection via a leading `=`, `+`, `-`, or `@`.

Also: injection through search filters and sort parameters, which often bypass
the ORM; and prompt injection through any field an LLM consumes.

## 5.7 Attack path — find what should not be exposed

- `/.env`, `/.git/config`, `/.git/HEAD`, `/config.json`, `/backup.sql`,
  `/.DS_Store` reachable over HTTP. A served `.git` directory means the whole
  source and history are downloadable.
- Source maps in production.
- `/swagger`, `/openapi.json`, `/graphql` with introspection on, `/actuator`,
  `/metrics`, `/debug/pprof`, framework debug panels.
- Admin tools on a subdomain with no auth.
- Directory listing enabled on any static path.
- Verbose health checks leaking versions, hostnames, or dependency URLs.
- Stale hosts: an old staging deployment on a forgotten subdomain, still
  running last month's code against the production database. This is
  API9:2023 and it is invisible from inside the repo — if you cannot check it,
  put it in **Not checked** and say why it matters.

## 5.8 Attack path — break the logic

The flaws no scanner finds, because the code does exactly what it says.

- Negative or zero values where only positive make sense — amount, quantity,
  duration, transfer.
- Numeric overflow, float rounding used for money, unit confusion between
  cents and units.
- **Time-of-check to time-of-use.** Between "is the coupon valid?" and "consume
  the coupon", can a second request slip in? Test with concurrent requests, not
  sequential ones. Applies to balances, stock, seat counts, single-use tokens,
  and idempotency keys.
- Multi-step flows: can a step be skipped or replayed? Can you jump to the
  confirmation step with a crafted request and get the outcome without paying?
- State machines: can an order go from `cancelled` back to `paid`? Can a
  subscription be reactivated without charge?
- Does the app trust a client-supplied timestamp, sequence number, or "already
  verified" flag?

## Prioritization

Report in blast-radius order, not discovery order:

1. Unauthenticated access to other users' data or to money.
2. Authenticated privilege escalation or cross-tenant access.
3. Stored injection reaching a privileged viewer.
4. Abuse with real financial cost.
5. Information disclosure that assists the above.
6. Everything else.

## Output of this phase

Per attack path: the persona, the steps, the observed result, and — for a
confirmed finding — the concrete exploit narrative the report needs. Paths you
tried that failed are worth one line each; they are evidence the surface was
actually walked.

Every candidate from this phase still goes through the gates in
`verification-gates.md`. An adversarial mindset generates the most findings and
the most false positives.
