# Phase 2 — Personal Data Flow

**Derived from:** Bearer (github.com/bearer/bearer) — a SAST tool that
classifies 120+ data types across PD / sensitive PD / PII / PHI and performs
interprocedural, inter-file data-flow analysis to see where classified data
travels. Bearer analyses code, never values. Do the same: trace the flow, never
dump the data.

**Maps to:** A01:2025 Broken Access Control · A04:2025 Cryptographic Failures ·
A09:2025 Security Logging and Alerting Failures · API3:2023 Broken Object
Property Level Authorization · CWE-359 · CWE-532 (info in logs) · CWE-522.

**Guardrail:** do not query the production database, do not read real user
records, and do not print any real personal data you encounter in fixtures or
logs. Work from code.

---

## 2.1 Classify before you trace

Tag each piece of data the app touches. Severity of a leak follows the class.

- **Identifiers:** email, phone, username, device id, IP address, cookie id.
- **Government / financial:** national id, passport, tax id, bank account, card
  number (PAN), IBAN. If the app stores a raw PAN, that is a finding on its own
  — PCI scope belongs with the payment provider, not your database.
- **Credentials:** passwords, password hashes, reset tokens, session tokens,
  API keys, MFA seeds, recovery codes.
- **Special category:** health, biometric, genetic, precise geolocation, sexual
  orientation, religion, political opinion, union membership, race. Under GDPR
  Art. 9 these carry a higher bar; under HIPAA, health data has its own regime.
- **Derived / behavioural:** search history, chat transcripts, prompts sent to
  an LLM, uploaded documents. Users rarely expect these to be shared, and in AI
  apps they are the most commonly over-shared class.

Also note the **data subjects**: end users, employees, and — easy to miss —
third parties whose data a user uploads (a CRM import, a contact list, a
document containing someone else's details).

## 2.2 Map collection points

Every place data enters: signup and profile forms, OAuth profile scopes,
file/CSV imports, webhooks, URL parameters, analytics auto-capture, and
anything an LLM is asked to extract. For each: what is collected, is it
actually used, and is it retained.

The most valuable question in this phase is **"why is this field collected at
all?"** Data minimisation removes whole classes of risk. A date of birth
collected for an age gate can usually be an `is_over_18` boolean.

## 2.3 Trace to sinks

For each classified item, follow it to every sink. This is the part that needs
real tracing across function and file boundaries, not a single grep.

| Sink | What to check |
|---|---|
| **Logs** | Structured loggers, `console.log`, `print`, exception handlers that log the full request or the whole user object. Request/response middleware logging bodies. Check log *destinations* too — a log shipper sends this off-box. |
| **Analytics / product telemetry** | What is in the event properties? Auto-capture tools record form field contents and URLs by default; URLs carrying tokens or ids end up in a third-party system permanently. |
| **Error monitoring** | Breadcrumbs, request headers, local variable capture on exception. Configure scrubbing explicitly rather than relying on defaults. |
| **LLM providers** | The whole prompt is sent off-box, plus system prompt and retrieved context. Is user PII in it? Is the provider's zero-retention / no-training setting on? Is user content used for training by default? |
| **Email / SMS providers** | Template variables sent to the provider, and whether the provider retains message bodies. |
| **Payment providers** | Should receive payment data *directly from the client* via their SDK. If card data touches your server, that is a finding. |
| **API responses** | See 2.5. |
| **Client storage** | `localStorage`, `sessionStorage`, IndexedDB, cookies. |
| **Backups / exports** | Do CSV exports and DB dumps carry more than the requester should see? |

For each third-party sink produce a one-line record: **service · data classes
sent · purpose · is a DPA in place**. That table is most of a GDPR Record of
Processing Activities, and it is far easier to write now than after launch.

## 2.4 Credentials and cryptography

- Passwords hashed with **argon2id, scrypt, or bcrypt** — a memory-hard or
  deliberately slow function with a per-user salt. Plain SHA-256 or MD5, salted
  or not, is a finding. So is a fast HMAC used as a password hash.
- Password never logged, never returned, never stored in plaintext, never sent
  anywhere but the hashing function.
- Reset and verification tokens: generated from a **CSPRNG** (not `Math.random`,
  not a timestamp, not a sequential id), single-use, short-lived (15 minutes is
  a good default), bound to one user, invalidated on use and on password change,
  and compared in constant time.
- Sensitive data at rest: is disk/DB encryption on? Is any field-level
  encryption using a key that is itself in the repo?
- Secrets compared with a constant-time comparison, not `==`.

## 2.5 Response filtering — the most common real leak

Serialising a database row straight to JSON is how password hashes, internal
flags, other users' fields, and soft-deleted records reach the client. This is
API3:2023 Broken Object Property Level Authorization.

Check every endpoint that returns an object:

- Is the response built from an explicit allowlist of fields, or by dumping the
  model? Dumping is the finding.
- Does an "include related" / `expand` / `?include=` parameter let a caller pull
  fields or relations they should not see?
- Do list endpoints leak more per item than the list needs?
- Does an error path return a fuller object than the success path?
- **Mass assignment in reverse:** does a create/update endpoint accept fields
  the user should not set — `role`, `is_admin`, `credits`, `verified`,
  `stripe_customer_id`? Bind to an explicit input schema.

## 2.6 Client-side storage

- Session tokens in cookies with `HttpOnly`, `Secure`, and `SameSite` (`Lax`
  minimum; `Strict` for anything sensitive). A token in `localStorage` is
  readable by any XSS on the page — flag it, and note the mitigation is
  `HttpOnly` cookies, not "we will prevent all XSS".
- Cookie scope: no over-broad `Domain` that shares the cookie with subdomains
  you do not control.
- No PII cached in `localStorage` for convenience.
- If a service worker or offline cache stores responses, does it cache
  authenticated responses that persist after logout?

## 2.7 Retention and deletion

- Is there any deletion path at all? GDPR Art. 17 and CCPA both require one;
  most rapidly-built apps have none.
- Does deletion cascade — auth provider, database, object storage, search
  index, analytics, email provider, backups, LLM provider logs?
- Are soft-deleted records excluded from every query, including admin and
  export paths?
- Is there a stated retention period, or does everything live forever by
  default?
- Is there an export path (data portability)?

Missing deletion is a real compliance gap; report it as MEDIUM with the
regulation named, and do not overstate it as a breach.

## Output of this phase

A flow map — **data class → collected at → stored where → sent to whom →
retained how long** — plus the third-party table from 2.3, plus findings. The
map is a deliverable in its own right; keep it even when nothing is wrong.
