# Phase 6 — Verification Gates

**Derived from:** Trail of Bits `fp-check` — "systematic false positive
verification for security bug analysis with mandatory gate reviews", whose
gate table and false-positive pattern catalogue are reproduced and adapted
below, plus the refuting-verifier approach from `insecure-defaults`.

**Why this phase exists.** An audit's credibility is set by its worst finding,
not its best. A report with two real bugs and eight confident-sounding
non-bugs teaches the reader to ignore all ten. Worse, in an agentic setting,
each non-bug becomes an unnecessary edit to working security code — which is
its own way of introducing a vulnerability.

**Every candidate from Phases 1–5 passes through here before it is reported or
fixed. No exceptions, including for findings that seem obvious.**

---

## The gates

All six must pass. Failure of any single gate makes the candidate a **FALSE
POSITIVE**, and it moves to the dismissed list with its gate number.

| # | Gate | Question | Passes when | Fails when |
|---|---|---|---|---|
| 1 | **Process** | Was the finding reached with documented evidence, not pattern-matching? | Concrete evidence exists for the claim — file, line, and the reasoning | The claim rests on "this pattern is usually bad" |
| 2 | **Reachability** | Can an attacker reach and control the input? | A named entry point carries attacker-controlled data to this code | You cannot show attacker control, or the path is unreachable |
| 3 | **Real impact** | Does exploitation produce data disclosure, privilege escalation, code execution, or financial loss? | A concrete impact scenario with concrete values | It is only a robustness, style, or defence-in-depth issue |
| 4 | **Demonstration** | Can the attack path be shown end to end? | A request, input, or sequence that shows control → trigger → impact | You cannot describe a specific attack, only a category |
| 5 | **Bounds** | Do the numbers, types, and conditions actually permit it? | The condition is achievable given the validation present | Validation or typing makes the condition impossible |
| 6 | **Environment** | Do existing protections fail to block it? | Protections are absent or bypassable, and you say how | A framework default, header, platform control, or upstream layer fully blocks it |

Gate 6 has a caveat worth stating in the report: "the platform probably handles
it" is not a pass *or* a fail — it is an unknown. Verify the control exists, or
move the item to **Not checked**.

## Before the gates: state the claim

Write it out before evaluating. Vague claims pass gates they should not.

```
Vulnerability:  What is wrong, in one sentence.
Location:       file:line, and every other affected site.
Root cause:     Why it is wrong.
Trigger:        What an attacker sends or does.
Entry point:    Where that input enters the system.
Impact:         What they get.
Class:          IDOR / injection / auth bypass / ...  + CWE
Context:        Does this run in production? Behind auth? Behind a flag?
```

If you cannot fill in **Entry point** and **Impact** from the code, the
candidate fails at gate 2 or 3 and there is no point continuing.

## Refute before you confirm

For each candidate, spend one honest pass trying to *kill* it. Ask directly:
what would make this not a bug? Then go look for that thing.

- Is there validation earlier in the call chain? Read the callers, not just the
  function.
- Does the framework already handle this — ORM parameterization, template
  auto-escaping, CSRF middleware, a body size limit at the proxy?
- Is this path reachable in production, or only in tests, seeds, a dev-only
  branch, or behind a flag that is off?
- Is the "user input" actually user input, or does it come from a trusted
  internal caller?
- Does a type system or schema validator constrain it before it arrives?
- Is the dangerous-looking API actually safe by contract — a bounded write, a
  fixed set of allowed values?

**A candidate you did not try to refute has not been verified.** Record what
you checked; that record is what makes a CONFIRMED verdict mean something.

## False-positive patterns to recognize

Adapted from the `fp-check` catalogue. If a candidate matches one of these,
raise the bar before reporting.

**Pattern-based.** Judging isolated code without tracing preceding validation.
Claiming a race without showing the value can change between check and use.
Treating a defensive assertion as a vulnerability. Assuming network data
reaches a dangerous function without tracing the path. Flagging a function
because it is on a "dangerous" list. Reporting issues in error-handling or
cleanup paths that are unreachable by an attacker.

**Context-blind.** Reading fragments disconnected from the architecture.
Missing that earlier validation makes the code unreachable. Missing that the
code runs only during trusted setup, a migration, or a CLI task. Missing a
framework or language guarantee. Treating a debug-only path as production.

**Bounds.** Claiming overflow, underflow, or an off-by-one without proving the
condition can occur. Missing the conditional that makes the scenario
impossible. Flagging a buffer or index access that validation already
guarantees is in range.

**API contract.** Claiming a violation the API prevents by construction.
Confusing "this parameter is modified" with "this is unsafe".

**Additions for this class of audit:**

- **Dependency claims without a version.** "This package has a known CVE" is
  not a finding until you have read the resolved version from the lockfile and
  confirmed it is in range. Never state a CVE id you have not verified, and
  never invent one.
- **Config asserted from absence.** "There is no CSP" is false if the CSP is set
  by the platform, a proxy, or a middleware you did not read. Check the response,
  or say you could not.
- **Best practice reported as vulnerability.** "Should use argon2 instead of
  bcrypt" is hardening, not a finding. bcrypt at a sane cost factor is fine.
  Severity inflation is the fastest way to lose a reader.
- **Duplicate findings.** One root cause across twelve call sites is one
  finding with twelve locations, not twelve findings.

## Verdicts

- **CONFIRMED** — all six gates pass. Report it, with the evidence.
- **DISMISSED** — a gate failed. Goes in the dismissed section with the gate
  number and one line. Do not delete it; the dismissed list is what proves the
  audit had a standard.
- **UNVERIFIABLE** — cannot be settled without something you do not have
  (production access, a credential, a running dependency, a non-repo config).
  Goes in **Not checked** with what would settle it. Never promote an
  UNVERIFIABLE to CONFIRMED because it seems likely, and never quietly drop it.

## Calibrate the effort

Gate depth should follow blast radius, not uniformity.

- **Anything CRITICAL or HIGH, and anything you intend to fix in auth,
  authorization, payments, or crypto** — full six gates, written out, plus the
  refutation pass.
- **MEDIUM** — gates 2, 3, and 6 explicitly (reachable? real impact? not
  already blocked?), the rest as a judgement call.
- **LOW / hardening** — no gate ceremony, but say plainly that it is hardening
  rather than a vulnerability, so it is not read as one.

## Scoring the release

After verdicts, produce the report per the SKILL.md structure. Apply these caps
to the verdict line:

- `DO NOT SHIP` — any CONFIRMED CRITICAL; any live credential leaked to a
  pushed remote and not yet rotated; any unauthenticated path to another user's
  data or to money.
- `SHIP WITH CAVEATS` (ceiling) — any CONFIRMED HIGH remaining; a payment or
  money-moving path that was not exercised; Phase 0 could not establish trust
  boundaries; CI failing or critical-path tests absent.
- `SHIP` — no CRITICAL or HIGH, phases completed, and the **Not checked**
  list contains nothing that could hide one.

State the cap that applied and why. A verdict without its reason is not
actionable.
