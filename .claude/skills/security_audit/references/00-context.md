# Phase 0 — Scope and Context

**Derived from:** Trail of Bits `audit-context-building` (understand the
codebase before identifying bugs) and the release-surface step of the ECC
production-audit.

**Why this phase exists:** Every expensive false positive in an AI security
audit traces back to reading code without knowing whether it is reachable,
whether it is meant to be public, or whether validation already happened
upstream. Skipping Phase 0 does not save time — it moves the time into
arguing about findings that were never real.

**Do not report findings in this phase.** Produce a map. The map is the input
to every later phase.

---

## 0.1 Establish the release surface

- What is actually shipping? Branch, diff against the deployed ref, uncommitted
  work (`git status`, `git log --oneline -20`, `git diff --stat`).
- Is anything half-built? An unfinished auth flow in the tree is a different
  risk from a missing one.
- Is there a deploy config — Dockerfile, `fly.toml`, `vercel.json`, Procfile,
  CI workflow? Read it. It tells you the real runtime, the real env vars, and
  the real exposed ports.

## 0.2 Detect the stack

Do not assume. Detect, then load only the relevant checks in later phases.

| Signal | Stack | Pulls in |
|---|---|---|
| `package.json` + `next.config.*` | Next.js | `NEXT_PUBLIC_` leakage, server actions, middleware auth, route handlers |
| `package.json` + `vite.config.*` | Vite SPA | `VITE_` leakage, everything client-side is public |
| `package.json` + express/fastify/hono | Node API | helmet, CORS, body limits, error middleware |
| `requirements.txt` / `pyproject.toml` + FastAPI/Flask/Django | Python API | dependency injection auth, debug mode, `SECRET_KEY`, ORM query construction |
| `supabase/` dir, `@supabase/*`, `SUPABASE_URL` | Supabase | RLS on every table (the anon key is only safe with RLS), service-role key placement, storage bucket policies |
| `stripe`, `razorpay`, `@paddle/*` | Payments | server-side price authority, webhook signature verification, idempotency |
| `prisma/`, `drizzle/`, `alembic/`, `*.sql` | Owned DB | migrations, raw SQL, connection TLS |
| `openai`, `anthropic`, `@ai-sdk/*`, prompt templates | LLM integration | prompt injection, output trust, key placement, spend limits |
| `.github/workflows/` | CI | secret handling in Actions, `pull_request_target`, third-party action pinning |

Record: language(s), framework(s), package manager, lockfile present or not,
database, auth mechanism, hosting target.

## 0.3 Map the trust boundaries

This is the core deliverable. For each boundary, write down what crosses it and
who is trusted on each side.

- **Browser → server.** Every route, method, and its intended audience:
  public / authenticated / role-restricted / internal-only. Build this list
  from the router, not from the frontend — the frontend only shows what the UI
  offers, not what the server accepts.
- **Server → database.** Who constructs queries, is there an ORM, is raw SQL
  used anywhere, is row-level security in play.
- **Server → third parties.** Every outbound integration and what it receives.
- **Third party → server.** Webhooks, OAuth callbacks, cron pings. These are
  unauthenticated entry points until proven otherwise, and they are routinely
  forgotten.
- **LLM → app.** If a model's output is rendered, executed, used in a query, or
  used to pick a code path, that is an untrusted input boundary. Mark it.
- **User → storage.** File uploads, avatars, imports.

## 0.4 Inventory the entry points

Produce a table. Later phases walk it row by row.

```
METHOD  PATH                    INTENDED AUDIENCE   AUTH MECHANISM   TAKES USER ID?
GET     /api/orders/{id}        authenticated       session cookie   yes  ← IDOR candidate
POST    /api/webhook/stripe     third party         signature        no
GET     /api/admin/users        admin only          session + role   no
```

"Intended audience" comes from the developer's evident intent — route naming,
existing middleware, the surrounding code. Where intent is genuinely unclear,
mark it `UNKNOWN` and ask. An `UNKNOWN` that turns out to be public is the
single highest-yield question in the whole audit.

## 0.5 Identify what you cannot see

Write this list down now, while it is cheap:

- Code behind feature flags or env-gated branches you cannot evaluate.
- Compiled, minified, or vendored code.
- Infrastructure config living outside the repo (dashboard-configured env vars,
  cloud IAM, WAF rules, platform-level rate limits). You cannot audit what is
  not in the tree — say so rather than assuming it is absent *or* present.
- Anything requiring credentials or a running service.

This list becomes the **Not checked** section of the report verbatim.

## Exit criteria

Do not enter Phase 1 until you can state, in writing:

1. The stack.
2. The full entry-point table with an audience for each row.
3. The trust boundaries.
4. The not-visible list.

If the entry-point table has more than a couple of `UNKNOWN` audiences, stop
and ask. Guessing here poisons Phases 4 and 5.
