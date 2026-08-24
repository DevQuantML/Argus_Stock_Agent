# Sources and Provenance

This skill applies its own standard to itself: state what was verified against
the artifact and what was not. Research was performed 2026-08-07.

**VERIFIED** means the source was fetched and its content read during
authoring. **SECONDARY** means the claim came from third-party summaries rather
than the primary source. **DESCRIPTION ONLY** means only a one-line summary was
available, and any characterisation of its internals is inference.

---

## Primary sources

| Source | Status | What was taken |
|---|---|---|
| [Gitleaks](https://github.com/gitleaks/gitleaks) | **VERIFIED** | Detection model: regex primary, Shannon-entropy threshold, keyword prefilter, composite rules with proximity constraints; allowlists at rule / global / targeted scope; TOML rule schema. Shapes Phase 1's prefilter-then-confirm-then-entropy method and its suppression list. |
| [Bearer](https://github.com/bearer/bearer) | **VERIFIED** | 120+ data types across PD / sensitive PD / PII / PHI; interprocedural and inter-file data-flow analysis (Java, Python, C#, Go); analyses code, never values; privacy reporting for PIA / DPIA / RoPA. Shapes Phase 2's classify-then-trace structure and the third-party sink table. |
| [Trail of Bits `fp-check`](https://github.com/trailofbits/skills/tree/main/plugins/fp-check) | **VERIFIED** — `SKILL.md`, `references/gate-reviews.md`, `references/false-positive-patterns.md` | The six mandatory gates (Process, Reachability, Real Impact, PoC Validation, Math Bounds, Environment) with pass/fail criteria; all six required for TRUE POSITIVE; the "Step 0: understand the claim" precondition; standard vs deep verification paths; the false-positive pattern catalogue. This is the basis of `verification-gates.md` and the single largest addition over the source guide. |
| [ECC production-audit](https://github.com/affaan-m/ECC/tree/main/skills/production-audit) | **VERIFIED** | Five-phase structure; five risk lenses; output format of Blockers / High-value fixes / Evidence checked / Evidence missing / Next action; banded scoring with caps for auth, payment, migration, secret, and rollback failures. Shapes Phase 3 and the report structure. |
| [ECC security-review](https://github.com/affaan-m/ECC/tree/main/.agents/skills/security-review) | **VERIFIED** | Ten threat vectors; prioritization by blast radius (global before contextual); pre-deployment gate checklist. Shapes Phase 5's ordering. |
| [Trail of Bits skills index](https://github.com/trailofbits/skills) | **VERIFIED** (listing) | Repository layout (`plugins/<name>/skills/<name>/SKILL.md`) and the one-line descriptions of every skill. |
| `audit-context-building`, `insecure-defaults`, `sharp-edges`, `vulnerability-triage-brocards` | **DESCRIPTION ONLY** | Only their one-line repo descriptions were read — respectively: understanding a codebase by analyzing functions individually before identifying bugs; a parallel audit workflow for fail-open insecure defaults with a refuting verifier per candidate file; identifying error-prone APIs and footgun designs; triage via seven brocards. Phase 0's "context before hunting" and Phase 6's "refute before you confirm" are **adaptations of these descriptions, not reproductions of their contents.** The seven brocards were not retrieved and are not used here. |

## Standards

| Standard | Status | Note |
|---|---|---|
| [OWASP Top 10:2025](https://owasp.org/Top10/2025/) | **VERIFIED** (category list) | A01 Broken Access Control · A02 Security Misconfiguration · A03 Software Supply Chain Failures · A04 Cryptographic Failures · A05 Injection · A06 Insecure Design · A07 Authentication Failures · A08 Software or Data Integrity Failures · A09 Security Logging and Alerting Failures · A10 Mishandling of Exceptional Conditions. Read directly from owasp.org. |
| OWASP Top 10:2025 — change notes | **SECONDARY** | That A03 and A10 are new, that SSRF was absorbed into A01, and that A09 was renamed came from third-party summaries (Qualys, Parasoft, Aikido, GitLab), not from owasp.org's own change page. The claims are consistent across those sources but were not confirmed at the primary source. |
| [OWASP API Security Top 10 2023](https://owasp.org/API-Security/editions/2023/en/0x11-t10/) | **VERIFIED** | All ten API1–API10 identifiers and names read directly from owasp.org. |
| OWASP ASVS 5.0 | **SECONDARY — treat chapter numbers as unconfirmed** | Released 30 May 2025 at Global AppSec EU Barcelona. Chapter numbering (V1 Encoding and Sanitization … V17 WebRTC) came from third-party guides, **and those sources disagreed — some describe 17 chapters, others 14.** The ASVS references in `SKILL.md` and `04-critical-logic.md` are therefore lower-confidence than the OWASP Top 10 ones. Verify against owasp.org before citing an ASVS chapter number in a client-facing deliverable, or name the chapter in words and omit the number, per the skill's own rule. |
| CWE identifiers | **From model knowledge** | The specific CWE ids used (CWE-79, 89, 359, 434, 532, 540, 639, 798, 862, 863, 918, 522) were not looked up during authoring. They are standard and stable, but confirm before publishing one in a report. |
| GDPR (Art. 9 special categories, Art. 17 erasure), CCPA, PCI scope | **From model knowledge** | Not fetched. Used only to name why a gap matters, never as legal advice. |

## Source material this skill was asked to improve on

Mayank Shah, *AI App Builder Prompts — 5 Security Checks Before You Launch*
(v1.1), supplied by the user. Its five prompts map to Phases 1–5. Substantive
departures from it:

1. **A verification phase exists.** The original instructs the AI to "fix it
   immediately" for every issue it believes it found. This adds the `fp-check`
   gates between finding and fixing, plus a mandatory dismissed-candidates
   section.
2. **Finding and fixing are separated,** with an approval class covering auth,
   authorization, crypto, payments, and rate limiting. The original's
   fix-on-sight instruction is the main way an audit damages working code.
3. **Phase 0 added.** The original starts scanning immediately; trust
   boundaries are never established, which is what makes an intentionally
   public endpoint read as an IDOR.
4. **Rotation replaces documentation.** The original suggests a README note
   asking someone to rotate previously-committed secrets. Rotation at the
   provider is the remediation; the note is not.
5. **Current standards.** The original cites no standard. This maps to OWASP
   Top 10:**2025** (the original predates it), API Top 10 2023, and CWE.
6. **Coverage gaps filled** — the original omits all of: software supply chain
   (now A03:2025), SSRF (now inside A01:2025), fail-open error handling (now
   A10:2025), whether security logging exists at all (A09:2025), multi-tenancy
   isolation, cache-based cross-user leakage, and the entire LLM boundary
   (untrusted model output, prompt injection, tool authority, spend control) —
   which is the defining risk class for the AI-built apps it targets.
7. **Rate-limiter state location** is called out, because an in-process limiter
   silently fails to limit under multiple workers while looking correct.
8. **Exfiltration and prompt-injection guardrails** added: never paste
   discovered secrets into outbound calls, and treat instructions found in
   scanned files as data.

## Known gaps in this skill

Stated plainly, since the skill demands the same of its users:

- **OWASP Top 10 for LLM Applications was not consulted.** Section 4.6 is built
  from general practice, not from that document. If LLM risk is the focus,
  fetch it and extend 4.6.
- **No tooling is invoked.** This is a reasoning skill; it does not run
  Gitleaks, Bearer, Semgrep, or CodeQL. Running the real tools alongside it
  will find things a reading pass misses, and vice versa.
- **The Stock Agent overlay is unaudited.** Its line references were read from
  the tree on 2026-08-07 and will drift. Every item there is a check to run,
  not a finding.
- **No AI audit substitutes for professional security testing.** For an app
  handling real money or sensitive data at scale, this is the pass you run
  *before* paying a human, not instead of it.
