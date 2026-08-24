# Phase 1 — Secret Exposure

**Derived from:** Gitleaks (github.com/gitleaks/gitleaks) — its detection model
is regex + Shannon-entropy threshold + keyword prefilter, with allowlists at
rule, global, and targeted scope. This phase applies that model by hand and
then goes where Gitleaks does not: the client bundle and the runtime.

**Maps to:** A02:2025 Security Misconfiguration · A04:2025 Cryptographic
Failures · CWE-798 (hardcoded credentials) · CWE-540 (source code info leak).

**Guardrail:** never print a discovered secret value. Location and type only.
Redact to first 4 chars + `••••`. Do not paste a secret into a web search to
"check if it is real" — that is exfiltration.

---

## 1.1 Where secrets hide

Search the whole tree, not just source. In practice the hits come from:

- Source files and config (`.py`, `.ts`, `.js`, `.go`, `.rb`, `.java`).
- `.env`, `.env.local`, `.env.production`, `.env.backup`, `.env.old`.
- JSON/YAML/TOML config, `docker-compose.yml`, Kubernetes manifests, Helm
  values, Terraform `.tfvars`.
- CI workflow files with inline values instead of secret references.
- Notebooks (`.ipynb`) — outputs cached in the file, not just the source cells.
- Test fixtures, seed scripts, `README` code samples, and commented-out code.
- Editor/agent artifacts: `.vscode/`, `.idea/`, `.claude/`, worktree copies,
  `*.bak`, `*.orig`, `*.rej`, editor swap files.
- Build output and lockfile-adjacent junk: `dist/`, `.next/`, `build/`,
  source maps (`*.map`) — these embed whatever the bundler saw.

## 1.2 High-signal patterns

Prefilter on keyword, confirm on shape, then sanity-check entropy — a
40-character string of one repeated character is a placeholder, not a key.

| Provider / type | Recognizable shape |
|---|---|
| AWS access key | `AKIA` / `ASIA` + 16 uppercase alnum; secret is 40 base64-ish chars |
| Stripe | `sk_live_`, `rk_live_`, `whsec_` (secret) · `pk_live_` (publishable, client-safe) |
| OpenAI / Anthropic | `sk-`, `sk-ant-` |
| Google / Firebase | `AIza` + 35 chars |
| GitHub | `ghp_`, `gho_`, `ghs_`, `github_pat_` |
| Slack | `xoxb-`, `xoxp-`, `xapp-` |
| JWT | three base64url segments split by `.` — decode the header/payload only, never trust `alg` |
| Private key | `-----BEGIN (RSA\|EC\|OPENSSH\|PGP) PRIVATE KEY-----` |
| DB connection string | `postgres://`, `mysql://`, `mongodb+srv://` with credentials inline |
| Supabase service role | JWT whose decoded payload has `"role":"service_role"` |
| Generic | assignment to a name containing `secret`, `token`, `passwd`, `password`, `apikey`, `api_key`, `credential`, `private_key`, `auth` where the value is a literal of length ≥ 16 and entropy ≥ 3.5 |

**Suppress these — they are the standard false positives:** values that are
obviously placeholders (`your-api-key-here`, `xxx`, `changeme`, `example`,
`test`, all-same-character), values in `.env.example`, documented public test
keys (Stripe's `pk_test_`/`sk_test_` sample keys), fixture data in test
directories, and hashes/checksums in lockfiles. Note them as suppressed rather
than silently dropping them.

## 1.3 Client-side exposure — the one that actually burns people

Anything the browser can download is public. Check, per stack:

- **Next.js:** every `NEXT_PUBLIC_*` var. Is any of them a secret? Also check
  for secrets read in a component that is not marked server-only — a
  server-intended value referenced in a client component gets inlined into the
  bundle.
- **Vite / CRA:** every `VITE_*` / `REACT_APP_*` var, same question.
- **Any SPA:** grep the built bundle in `dist/` or `.next/` for the patterns in
  1.2. If a build exists, this is the ground truth and beats reading source.
- **Source maps** shipped to production re-expose the original source. Check
  whether `productionBrowserSourceMaps` / `build.sourcemap` is on.

**Supabase specifically:** the anon key is *designed* to sit in the client, but
only if Row Level Security is enabled and enforced on every table. An anon key
plus one RLS-less table equals full public read (and often write) of that
table. So: enumerate the tables, confirm RLS is enabled on each, and confirm
the policies actually restrict rather than being `USING (true)`. The
service-role key bypasses RLS entirely and must never appear in client code,
in a `NEXT_PUBLIC_` var, or in an edge function that echoes it.

**Stripe specifically:** `pk_*` in the client is correct. `sk_*` or `whsec_*`
in the client is a CRITICAL finding, no further analysis needed.

## 1.4 Git history

A secret removed in the working tree is still in history and still leaked.

```bash
git log --all --oneline -- .env
```

```bash
git log -p --all -S 'sk_live' --oneline
```

Also check: has the repo ever been public, is there a fork, is it pushed to a
remote at all? A secret in the history of a never-pushed local repo is a very
different severity from one in a public GitHub repo.

**The remediation is rotation, not documentation.** The source guide this skill
improves on suggests adding a README note asking someone to rotate later. That
is not a fix. If a live credential was ever committed:

1. Rotate it at the provider — now, before anything else.
2. Then, optionally, rewrite history (`git filter-repo`, BFG) — and understand
   this does not help if the repo was ever public or cloned.
3. Then check the provider's audit log for use you did not authorize.

Report an unrotated live credential in a pushed repo as **CRITICAL**, and it
caps the verdict at `DO NOT SHIP`.

## 1.5 Runtime leakage

Secrets escape at runtime as often as at rest:

- `console.log` / `print` / `logger.*` of a whole config object, request
  headers, or an error object that carries the request.
- Error responses that echo the connection string or the outbound request.
- A `/debug`, `/env`, `/config`, or framework introspection route that dumps
  environment.
- Client-side error reporting (Sentry and similar) capturing request headers
  including `Authorization`.
- The `Server`/`X-Powered-By` headers and verbose framework error pages.

## 1.6 Hygiene

- `.env` (and variants) in `.gitignore`, with `!.env.example` if an example is
  tracked.
- `.env.example` exists, lists every variable the app reads, and contains no
  real values. Cross-check against an actual grep for `process.env.` /
  `os.getenv(` / `os.environ` so the example is complete rather than stale.
- The app fails fast and loudly at startup when a critical variable is missing,
  rather than silently defaulting. A missing `AUTH_SECRET` that falls back to
  `"dev"` is a CRITICAL auth bypass, not a config nit.
- Ignore files cover agent/editor scratch directories and worktrees, which
  routinely contain a second full copy of the source.

## Output of this phase

A table: **type · location (file:line) · client-exposed? · in git history? ·
rotated? · severity**. Plus the suppressed-placeholder list. No values.
