# Phase 4 — Critical Logic

**Derived from:** the Trail of Bits skills methodology — audit one function at a
time with full context (`audit-context-building`), hunt fail-open defaults
(`insecure-defaults`), and look for error-prone APIs and footgun designs
(`sharp-edges`). Applied here to the paths where a rapidly-built app actually
loses money and data.

**Maps to:** A01/A05/A06/A07:2025 · API1/API2/API3/API5/API7:2023 ·
ASVS V2, V6, V7, V8, V9, V10 · CWE-639, CWE-862, CWE-863, CWE-89, CWE-79,
CWE-918, CWE-434.

**Scope this phase.** Do not audit everything. Walk the entry-point table from
Phase 0 and go deep only on rows that touch identity, money, other people's
data, or the filesystem. Depth on the critical few beats breadth on everything.

---

## 4.1 Authentication — A07:2025

- Every protected route actually has the auth check applied. Enumerate from the
  router and diff against the Phase 0 table; a missing decorator or a route
  registered outside the guarded group is the classic miss. Prefer
  deny-by-default routing over per-route opt-in — with opt-in, the next route
  someone adds is unprotected.
- Session tokens from a CSPRNG, rotated on privilege change, invalidated on
  logout server-side (not just cleared client-side), and given an absolute
  timeout as well as an idle one.
- **JWT**, if used: signing algorithm pinned server-side (reject `alg: none`
  and reject an `RS256`→`HS256` swap that turns a public key into an HMAC
  secret); signature verified before any claim is read; `exp`, `iss`, and `aud`
  validated; secret is long and random, not a word. Understand that a stateless
  JWT cannot be revoked — if the app claims logout invalidates it, there must
  be a denylist or a short expiry with refresh rotation. Never put anything
  secret in a payload; it is base64, not encryption.
- Password reset: token from a CSPRNG, single-use, ≤15 min, bound to one user,
  invalidated on use and on password change, delivered out-of-band, compared in
  constant time. The reset must not reveal whether an account exists, and it
  must invalidate existing sessions.
- Registration and login do not leak account existence through different
  messages, different status codes, or a measurably different response time.
- MFA, if present: rate-limited, no bypass on a secondary path, recovery codes
  single-use and hashed at rest, TOTP window narrow, and enrolment requires
  re-authentication.
- OAuth: `state` parameter present and verified (CSRF), PKCE for public
  clients, redirect URI matched exactly against an allowlist rather than by
  prefix, and the `id_token` verified rather than trusted.

## 4.2 Authorization — A01:2025, the highest-yield area

Authentication says who you are. Authorization says what you may touch. In
rapidly-built apps the second is routinely missing while the first looks fine.

**Object-level (IDOR / BOLA — API1:2023).** For every endpoint that takes an
identifier from the client — path, query, body, or header — is there a check
that the identifier belongs to the caller? Walk the Phase 0 table's
"takes user id?" column and answer per row. The pattern to find:

```
GET /api/orders/{id}  →  SELECT * FROM orders WHERE id = :id
```

with no `AND user_id = :current_user`. Sequential integer ids make this
trivially exploitable; UUIDs make it slower, not safe — obscurity is not the
control, the ownership check is.

**Function-level (API5:2023).** Can a regular user reach an admin endpoint by
guessing its URL? Role checks must be server-side. Hiding a button is not a
control. Check for admin routes that rely on being unlinked.

**Property-level (API3:2023).** Covered in Phase 2.5 — response over-exposure
and mass assignment of `role`/`is_admin`/`credits`.

**Privilege escalation paths.** Can a user set their own role at signup or via
profile update? Can they invite themselves to another tenant? Is role read from
a client-supplied JWT claim without re-checking server-side? Is there an
impersonation feature, and is it itself restricted and logged?

**Multi-tenancy.** If the app has organisations or workspaces: is the tenant id
taken from the session, or from the request? Taking it from the request is the
bug. Every query needs a tenant scope, and one missing scope leaks across
customers.

## 4.3 Payments and money

- **The server sets the price. Always.** Never accept price, total, currency,
  discount, or quantity from the client and charge it. Look for a checkout
  handler that reads `req.body.amount`. Re-derive every figure server-side from
  the product id and quantity.
- Quantity validated as a positive integer within bounds — negative quantities
  and negative amounts turn a purchase into a refund.
- Discounts and coupons: usage capped, expiry enforced, not stackable unless
  intended, not reusable across accounts, and checked atomically so two
  concurrent requests cannot both consume the last use.
- **Webhooks verified.** Signature checked with the provider's library and the
  *raw* body — parsing the body before verifying breaks the signature and
  invites a workaround that skips verification. Timestamp checked to prevent
  replay. Events made idempotent by event id, because providers retry.
- Entitlement granted only after the server confirms payment status with the
  provider — never on a client-side "success" redirect, which the user
  controls.
- Refunds, credits, and trial resets: can a user loop them? Can they cancel and
  keep access? Can they restart a free trial by re-registering, and is that
  acceptable?
- Race conditions on balances and stock: is the update atomic or a
  read-modify-write? Two concurrent redemptions of the same credit is the
  standard exploit.
- Test/live key separation, so a test-mode payment cannot grant live access.

## 4.4 Injection — A05:2025

- **SQL:** parameterized queries or a well-used ORM everywhere. Search for
  string concatenation and f-strings inside query construction. Note that ORM
  escape hatches (`raw`, `execute`, `literal`, `extra`) reintroduce the risk,
  and that identifiers — table and column names, `ORDER BY` targets — cannot be
  parameterized and need an allowlist instead.
- **NoSQL:** an object where a string is expected turns a lookup into an
  operator (`{"$ne": null}`). Validate types before querying.
- **Command:** any shell invocation built from user input. Use the argument-array
  form, never a shell string. Same for filenames passed to converters and image
  tools.
- **Path traversal:** `../` in any user-supplied path. Resolve, then confirm the
  result is inside the intended directory — do not just strip `..`.
- **SSRF (now inside A01:2025, and API7:2023):** any place the server fetches a
  user-supplied URL — webhooks, avatar-by-URL, preview generators, PDF
  renderers, an LLM tool that browses. Allowlist destinations; block private
  ranges, loopback, and cloud metadata endpoints (169.254.169.254); re-check
  after redirects; disable redirect-following where possible. DNS rebinding
  defeats a naive pre-flight IP check.
- **Template injection** where user input reaches a template engine.
- **XSS:** does user input reach HTML without escaping? Look for `innerHTML`,
  `dangerouslySetInnerHTML`, `v-html`, `|safe`, `mark_safe`,
  `document.write`. Frameworks escape by default — the finding is where someone
  opted out. Sanitize HTML with a maintained library (DOMPurify), never a regex.
  Also check `href`/`src` sinks for `javascript:` URLs and stored XSS in fields
  that are rendered later in an admin panel.
- **CSRF:** state-changing requests protected by a framework token or by
  `SameSite` cookies plus an origin check. Not needed if auth is a bearer token
  in a header, but is needed for cookie auth — including on JSON endpoints.

## 4.5 File uploads

- Type validated server-side by content inspection, not by extension or by the
  client-supplied `Content-Type`.
- Size limited, at both the app and the reverse proxy.
- Stored outside the web root, or in object storage, with a generated filename —
  never the user's filename, which carries traversal and null-byte tricks.
- Served from a separate origin or with `Content-Disposition: attachment` and
  `X-Content-Type-Options: nosniff`, so an uploaded HTML or SVG file cannot run
  script in the app's origin. SVG is an XSS vector; treat it as active content.
- No execute permission on the upload directory.
- Access-controlled on download — an unguessable URL is not authorization.
- Archives (zip) not expanded without limits — zip bombs and path traversal via
  entry names.

## 4.6 LLM integration boundaries

Absent from the source material this skill improves on, and the defining risk
class for AI-built apps.

- **The model's output is untrusted input.** If it is rendered as HTML, run as
  code, used to build a query, used to pick a file path, or used to decide an
  authorization outcome, that is the finding. Render through an escaping
  markdown renderer; never `innerHTML` a completion.
- **Prompt injection is not solvable by prompting.** If the model reads
  attacker-influenceable content — a scraped page, an uploaded document, a user
  bio, another user's message — assume the instructions in it will be followed.
  The control is on the tool side: what can the model *do*, and with whose
  authority?
- **Tool and function calling.** Every tool the model can call runs with the
  app's privileges unless scoped. Do the tools enforce the *user's*
  authorization, or the service's? A retrieval tool that queries without a
  tenant filter leaks across users on command.
- **Spend controls.** Per-user and global caps, a token ceiling per request,
  and auth on any endpoint that triggers a paid call. An unauthenticated LLM
  endpoint is a bill someone else pays.
- **Data sent to the provider** — covered in Phase 2.3; confirm retention and
  training settings.
- **System prompt exposure.** Assume it is extractable. Nothing secret in it,
  and no reliance on it for access control.

## Method for this phase

Per candidate site: read the whole function and its callers; identify the
untrusted input and trace it to the sink; check what validation exists between
them; then decide. Note explicitly when upstream validation makes a
suspicious-looking line safe — those notes go in the dismissed list and are
what keep the report credible.
