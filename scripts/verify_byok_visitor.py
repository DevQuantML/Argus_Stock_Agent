#!/usr/bin/env python
"""
verify_byok_visitor.py — anonymous self-funded research: opt-in, key-scoped,
never persisted. Also covers Model Court (section 7): session-gated,
dual-key, graceful degradation.

What this guards
-----------------
GET /api/byok/{ticker} lets an anonymous visitor paste their OWN
Groq/Perplexity key and run one research call funded by their own money —
no AGENT_SECRET, no guest key, no session. Three ways this could go wrong,
each with its own section below:

  1. The feature must default OFF. ALLOW_BYOK_VISITORS unset must refuse
     the route outright, before the key header is even inspected, on an
     instance that never opted in.
  2. A malformed key must never reach the provider layer — soft_validate()
     runs first, and the stub below proves it with a call counter, not just
     an assumption from reading the code.
  3. The visitor's key must never be persisted or echoed anywhere: not
     os.environ, not .env, not the response body, not a log line.
     _get_provider(override=...) building the client from the header value
     rather than any stored state is what this whole feature stands on.

POST /api/model-court/{ticker} (section 7) is a different shape: it
requires a session (owner OR guest — require_session, not require_owner)
AND two caller-supplied keys (X-Perplexity-Key, X-Gemini-Key), run in
parallel, never one. Guards: anonymous is refused before either key is
read; a guest session works (not owner-only); either missing/malformed
key is 400 before any provider call; one provider failing degrades to a
200 with the working side intact and comparison explicitly skipped,
never silently dropped or promoted to an error; both succeeding reaches
the comparison stage exactly once. Because run_model_court() calls
run_perplexity_research/_call as module-global names from inside
tools/perplexity_research.py itself, section 7's stubs patch that module
object directly (`pr.run_perplexity_research`, `pr._call`) rather than
api.py's imported reference — patching api.run_perplexity_research, as
section 3 does for the one-shot route, would not be seen by code running
inside tools/perplexity_research.py's own namespace.

Cost safety
-----------
Same structural guarantee as every other harness here: PERPLEXITY_API_KEY
and GROQ_API_KEY are blanked (never popped) before api is ever imported.
run_perplexity_research is additionally monkeypatched to a counting stub —
the actual OpenAI client is never constructed with a real network call in
this file, even for the "success" path, which needs its return value
controlled anyway to assert the response shape without spending anything.
Section 7 additionally stubs tools.perplexity_research._call, since
run_model_court's comparison stage calls it directly rather than going
through run_perplexity_research.

Run:  python scripts/verify_byok_visitor.py
Exit: 0 all passed, 1 any failed
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="argus-byok-")

# ── Cost safety — before ANY project import ────────────────────────────────
os.environ["PERPLEXITY_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["ARGUS_DB"] = str(Path(_tmp) / "byok.db")
os.environ["TRUST_PROXY"] = "0"
os.environ["AGENT_SECRET"] = "harness-only-secret"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["ALLOW_BYOK_VISITORS"] = ""   # section 1 needs it unset first

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def section(title):
    print(f"\n{title}")


def main():
    import tools.perplexity_research as pr_mod
    from tools.perplexity_research import _get_provider

    # ── 1. _get_provider(override=...) — precedence, and no-override intact ─
    # _get_provider() returns a 5-tuple now (extra_body added for Gemini's
    # grounding) — every unpack below reflects that; if a future edit drops
    # back to 4, this whole section fails loudly with a ValueError, not
    # silently.
    section("_get_provider override — precedence, extra_body, and byte-for-byte no-override behavior")
    client, main_model, debate_model, name, extra_body = _get_provider(("groq", "gsk_" + "z" * 40))
    check("override provider name is honored", name, "groq")
    check("override picks the Groq model pair", (main_model, debate_model),
          ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"))
    check("Groq's extra_body is None — no provider ever needed it before Gemini",
          extra_body, None)

    client2, _, _, name2, extra_body2 = _get_provider(("perplexity", "pplx-" + "z" * 40))
    check("override provider name is honored (perplexity)", name2, "perplexity")
    check("Perplexity's extra_body is also None — the new 5th element "
          "didn't silently change either pre-existing provider",
          extra_body2, None)

    # ── Gemini — the actual new mechanic this section exists to prove ──────
    client_g, main_g, debate_g, name_g, extra_body_g = _get_provider(("gemini", "AIza" + "g" * 35))
    check("override provider name is honored (gemini)", name_g, "gemini")
    # "gemini-3-flash" is NOT a real model id — it shipped here and made every
    # Gemini call fail, misreported as "the key was rejected". Pinned so the
    # bogus string cannot come back.
    check("gemini uses a REAL Flash model id, not the bogus 'gemini-3-flash'",
          (main_g, debate_g), ("gemini-3.7-flash", "gemini-3.7-flash"))
    # The NATIVE base, not the /openai/ compat base — the compat endpoint is
    # exactly the one that cannot ground.
    check("gemini's client points at Google's native base, not the /openai/ compat one",
          str(client_g.base_url), "https://generativelanguage.googleapis.com/v1beta")
    # Grounding is in Gemini's NATIVE shape, merged at the payload's top level.
    # The OpenAI-compat forms are all rejected by the live endpoint (probed
    # with a fake key: `Unknown name "google"` / `Unknown name "google_search"`
    # / `Invalid tool type`), and a Google moderator confirms grounding is
    # unavailable in compatibility mode at all — hence the native client below.
    check("gemini requests Google Search grounding, natively shaped",
          extra_body_g, {"tools": [{"google_search": {}}]})
    check("...and it is NOT the compat form that the endpoint rejects",
          "google" in extra_body_g, False)
    check("Perplexity and Groq still send no extra_body",
          (extra_body, extra_body2), (None, None))

    # The client for Gemini is deliberately NOT an OpenAI client — that is the
    # whole mechanism by which grounding works. If this ever reverts to
    # OpenAI(...), grounding silently dies and Gemini answers from training
    # data while the UI still promises live search.
    from tools.gemini_native import GeminiNativeClient
    check("gemini uses the native client, not the OpenAI compat client",
          isinstance(client_g, GeminiNativeClient), True)
    check("...while perplexity and groq still use the OpenAI client",
          (type(client2).__name__, type(client).__name__), ("OpenAI", "OpenAI"))
    # _call() redacts client.api_key out of provider errors before logging.
    # A client that hid the attribute would silently opt out of that.
    check("...and the native client still exposes api_key so _call can redact it",
          getattr(client_g, "api_key", None), "AIza" + "g" * 35)

    # Gemini grounds, so it must NOT be declared search-less; Groq still is.
    check("only groq is declared as having no live web search",
          tuple(pr_mod._NO_LIVE_SEARCH), ("groq",))

    try:
        _get_provider()
        no_override_raised = False
    except ValueError as exc:
        no_override_raised = True
        check("no-override, no env: the exact same message as before (unchanged)",
              "No AI provider configured" in str(exc), True)
    check("no override + nothing configured still raises — existing behavior untouched",
          no_override_raised, True)

    os.environ["GROQ_API_KEY"] = "gsk_" + "e" * 40
    client3, _, _, name3, extra_body3 = _get_provider()  # no override — must use env, exactly as before
    check("no override, env set: still reads os.environ exactly as before", name3, "groq")
    check("no override, env set: extra_body is None — there is no GEMINI_API_KEY "
          "env branch, by construction (BYOK-only)",
          extra_body3, None)
    os.environ["GROQ_API_KEY"] = ""

    # A GEMINI_API_KEY in the environment must do NOTHING — Gemini is
    # BYOK-only, reachable exclusively through override. This is the
    # concrete proof of that, not just an absence of an env branch in the
    # code someone has to trust by reading it.
    os.environ["GEMINI_API_KEY"] = "AIza" + "h" * 35
    try:
        _get_provider()
        gemini_env_raised = False
    except ValueError:
        gemini_env_raised = True
    check("a GEMINI_API_KEY in the environment is completely ignored — "
          "still raises exactly as if nothing were set",
          gemini_env_raised, True)
    os.environ["GEMINI_API_KEY"] = ""

    # ── 1b. The native Gemini adapter's translation ─────────────────────────
    # Grounding only pays off if the request is shaped right and the citations
    # survive back out. Pure translation, no network.
    section("gemini native adapter — payload translation and grounding parse")
    from tools.gemini_native import _build_payload, _parse

    _p = _build_payload(
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "Q"}],
        2500, {"tools": [{"google_search": {}}]})
    # The system role is a separate top-level field natively. Folding it into
    # `contents` as a user turn both weakens it and invites the model to
    # answer it instead of the question.
    check("system role becomes systemInstruction, NOT a user turn",
          _p["systemInstruction"]["parts"][0]["text"], "SYS")
    check("...and only the real user turn is in contents",
          [c["parts"][0]["text"] for c in _p["contents"]], ["Q"])
    check("max_tokens maps to generationConfig.maxOutputTokens",
          _p["generationConfig"]["maxOutputTokens"], 2500)
    check("grounding is merged at the payload TOP level, not nested under a "
          "vendor key (the nesting is what compat mode rejected)",
          _p.get("tools"), [{"google_search": {}}])

    _r = _parse({"candidates": [{
        "content": {"parts": [{"text": "Answer."}]},
        "groundingMetadata": {
            "webSearchQueries": ["asml q3"],
            "groundingChunks": [
                {"web": {"uri": "https://ex.com/a", "title": "Ex A"}},
                {"web": {"uri": "https://ex.com/a", "title": "dupe"}},
            ]}}]})
    check("the model's text survives parsing",
          _r.choices[0].message.content.startswith("Answer."), True)
    check("citations are appended so they actually reach the reader",
          "[Ex A](https://ex.com/a)" in _r.choices[0].message.content, True)
    check("...deduped by URI, not repeated per chunk", len(_r.sources), 1)
    # This is what makes "did it really search?" answerable rather than
    # assumed — the risk this integration was flagged for from the start.
    check("the search queries are surfaced, so grounding is checkable",
          _r.search_queries, ["asml q3"])
    check("a response with no candidates yields no choices, so _call() can "
          "report it as empty rather than as a successful blank answer",
          _parse({"candidates": []}).choices, [])

    # ── 2. Rate-limit bucket registered ─────────────────────────────────────
    section("rate-limit bucket — /api/byok lands in its own bucket, not the default")
    import api
    bucket = api._budget_for("/api/byok/AAPL")
    check("byok bucket exists", bucket is not None, True)
    check("byok bucket is (byok, 20, 300) — sized for a 5-request staged run, "
          "still anonymous-reachable so well under market's effective 5-min rate",
          bucket, ("byok", 20, 300))

    # ── 3. Route — stub run_perplexity_research, count every call ──────────
    section("provider-key route — disabled, malformed, and never-reaches-the-stub")
    from fastapi.testclient import TestClient

    calls = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "ticker": args[0] if args else kwargs.get("ticker"),
            "mode": "full",
            "report": "STUBBED REPORT — never a real provider call",
            "bull_case": "stubbed bull",
            "bear_case": "stubbed bear",
        }

    api.run_perplexity_research = stub
    client_t = TestClient(api.app)

    # 3a. Disabled instance — the default state until this section flips it on.
    r_off = client_t.get("/api/byok/AAPL", headers={
        "X-Provider": "groq", "X-Provider-Key": "gsk_" + "a" * 40})
    check("disabled instance refuses with 403", r_off.status_code, 403)
    check("...and the stub was never reached", len(calls), 0)

    # 3b. Enabled from here on.
    os.environ["ALLOW_BYOK_VISITORS"] = "1"

    r_bad_provider = client_t.get("/api/byok/AAPL", headers={
        "X-Provider": "openai", "X-Provider-Key": "sk-" + "b" * 40})
    check("an unrecognised provider is rejected with 400", r_bad_provider.status_code, 400)
    check("...and the stub was never reached", len(calls), 0)

    r_missing_header = client_t.get("/api/byok/AAPL", headers={
        "X-Provider-Key": "gsk_" + "c" * 40})
    check("a missing X-Provider header is rejected with 400", r_missing_header.status_code, 400)
    check("...and the stub was never reached", len(calls), 0)

    r_short_key = client_t.get("/api/byok/AAPL", headers={
        "X-Provider": "groq", "X-Provider-Key": "too-short"})
    check("a too-short key is rejected with 400 via the shared soft_validate",
          r_short_key.status_code, 400)
    check("...and the stub was never reached — validated before any provider call",
          len(calls), 0)

    r_placeholder = client_t.get("/api/byok/AAPL", headers={
        "X-Provider": "groq", "X-Provider-Key": "gsk_..."})
    check("the shipped .env.example placeholder is rejected, not treated as a real key",
          r_placeholder.status_code, 400)
    check("...and the stub was never reached", len(calls), 0)

    # ── 4. The one real, valid call ─────────────────────────────────────────
    section("a valid request — reaches the provider layer exactly once, leaks nothing")
    good_key = "gsk_" + "d" * 40
    r_ok = client_t.get("/api/byok/MSFT", headers={
        "X-Provider": "groq", "X-Provider-Key": good_key})
    check("a valid provider + key succeeds", r_ok.status_code, 200)
    check("the stub was reached exactly once", len(calls), 1)

    call_args, call_kwargs = calls[0]
    override_used = call_kwargs.get("override") or (call_args[2] if len(call_args) > 2 else None)
    check("run_perplexity_research received the override as (provider, key)",
          override_used, ("groq", good_key))

    body_text = r_ok.text
    check("the raw key never appears anywhere in the response body",
          good_key in body_text, False)
    check("the stubbed report made it through to the response",
          r_ok.json().get("report"), "STUBBED REPORT — never a real provider call")

    # Same route, Gemini — proves the route-level chain, not just the
    # _get_provider() unit above. Same stub, so this doesn't touch a real
    # Google endpoint either.
    gemini_key = "AIza" + "k" * 35
    r_gemini_ok = client_t.get("/api/byok/GOOGL", headers={
        "X-Provider": "gemini", "X-Provider-Key": gemini_key})
    check("a valid Gemini provider + key succeeds at the route level", r_gemini_ok.status_code, 200)
    gemini_call_args, gemini_call_kwargs = calls[-1]
    gemini_override_used = gemini_call_kwargs.get("override")
    check("run_perplexity_research received Gemini's override as (provider, key)",
          gemini_override_used, ("gemini", gemini_key))
    check("the raw Gemini key never appears anywhere in the response body",
          gemini_key in r_gemini_ok.text, False)

    # ── 5. Nothing persisted, anywhere ──────────────────────────────────────
    section("the key is never persisted — os.environ, .env, or the database")
    check("GROQ_API_KEY in os.environ is untouched by a visitor's own key",
          os.environ.get("GROQ_API_KEY", ""), "")
    check("PERPLEXITY_API_KEY in os.environ is untouched",
          os.environ.get("PERPLEXITY_API_KEY", ""), "")
    check("GEMINI_API_KEY in os.environ is untouched by the Gemini request above",
          os.environ.get("GEMINI_API_KEY", ""), "")
    # api._ENV_FILE_PATH is the real project .env in this process (never
    # monkeypatched in this file) — asserting its content is unchanged would
    # require reading the operator's real file, which this harness must
    # never do. The os.environ assertions above are the load-bearing check:
    # write_env_key() is never even imported into this file, let alone
    # called — the byok route path has no code reference to it at all.
    import inspect
    src = inspect.getsource(api.api_byok_research)
    check("api_byok_research's own source never calls write_env_key",
          "write_env_key" in src, False)
    check("api_byok_research's own source never assigns os.environ",
          "os.environ[" in src, False)

    # ── 6. The staged pipeline — same guarantees, four modules + synthesis ──
    # Everything above proved the one-shot route. The staged routes share
    # _byok_auth() (same function, not a re-implementation), but the actual
    # provider entry points are different functions — run_research_module and
    # run_synthesis — so each gets its own stub and its own call count,
    # exactly the way verify_access.py keeps module/synthesis/legacy/outlook
    # as four separate counters rather than one shared one.
    section("staged pipeline — report/context/policy/patterns/synthesis")
    # This section alone issues 21 requests against the 20/300s byok bucket
    # to exercise every failure mode thoroughly — a volume no real single
    # session would produce, but exactly what a rate-limiter test does
    # everywhere else in this repo (see verify_hardening.py). Cleared here,
    # not loosened in api.py: 20/300s stays the real production bound.
    api._rate_limit_store.clear()
    module_calls = []

    def module_stub(*args, **kwargs):
        module_calls.append((args, kwargs))
        return {
            "ticker": args[0] if args else kwargs.get("ticker"),
            "module": args[1] if len(args) > 1 else kwargs.get("module"),
            "mode": "module",
            "output": "STUBBED MODULE OUTPUT",
        }

    api.run_research_module = module_stub

    # Disabled first — mirrors section 3a for the one-shot route, now for
    # each staged endpoint individually. Every one must refuse identically.
    os.environ["ALLOW_BYOK_VISITORS"] = ""
    for stage in ("report", "context", "policy", "patterns"):
        r = client_t.get(f"/api/byok/AAPL/{stage}", headers={
            "X-Provider": "groq", "X-Provider-Key": "gsk_" + "x" * 40})
        check(f"disabled instance refuses {stage} with 403", r.status_code, 403)
    check("...and run_research_module was never reached while disabled",
          len(module_calls), 0)

    os.environ["ALLOW_BYOK_VISITORS"] = "1"

    # A malformed key must be caught before run_research_module for every
    # single stage, not just the first one tried.
    for stage in ("report", "context", "policy", "patterns"):
        r = client_t.get(f"/api/byok/AAPL/{stage}", headers={
            "X-Provider": "groq", "X-Provider-Key": "too-short"})
        check(f"a too-short key is rejected with 400 on {stage}", r.status_code, 400)
    check("...and run_research_module was never reached for any of them",
          len(module_calls), 0)

    good_key2 = "gsk_" + "f" * 40
    for stage in ("report", "context", "policy", "patterns"):
        r = client_t.get(f"/api/byok/AAPL/{stage}", headers={
            "X-Provider": "groq", "X-Provider-Key": good_key2})
        check(f"a valid request to {stage} succeeds", r.status_code, 200)
        check(f"the raw key never appears in the {stage} response body",
              good_key2 in r.text, False)
    check("run_research_module was reached exactly 4 times — one per stage",
          len(module_calls), 4)

    last_args, last_kwargs = module_calls[-1]
    last_override = last_kwargs.get("override")
    check("run_research_module received the override as (provider, key)",
          last_override, ("groq", good_key2))

    # A real provider failure (not the disabled/malformed cases above) must
    # surface the SPECIFIC reason, not run_research_module's own generic,
    # owner/guest-safe text — this is the caller's own key, so "the key was
    # rejected" is the actionable, correct thing to show. Fixed after a live
    # report of exactly this: a real (bad) Gemini key produced nothing more
    # useful than "the research provider is unavailable right now" for every
    # stage, with no way to tell a bad key from a rate limit from anything
    # else. _byok_module_response now translates via _PROVIDER_REASON_TEXT.
    def failing_module_stub(*args, **kwargs):
        module_calls.append((args, kwargs))
        return {
            "error": "the research provider is unavailable right now",
            "reason": "auth", "cost_incurred": False,
            "ticker": args[0] if args else kwargs.get("ticker"),
            "module": args[1] if len(args) > 1 else kwargs.get("module"),
        }

    api.run_research_module = failing_module_stub
    r_module_fail = client_t.get("/api/byok/AAPL/report", headers={
        "X-Provider": "groq", "X-Provider-Key": good_key2})
    check("a real provider failure on a module stage is 502", r_module_fail.status_code, 502)
    check("...with the specific translated reason, not the generic backend text",
          r_module_fail.json().get("error"), "the key was rejected")
    check("...and still carries the machine-readable code the staged loop keys on",
          r_module_fail.json().get("code"), "provider_unavailable")
    api.run_research_module = module_stub  # restore the success stub for what follows

    # ── Synthesis — its own function, its own fencing, its own stub ────────
    synthesis_calls = []

    def synthesis_stub(*args, **kwargs):
        synthesis_calls.append((args, kwargs))
        return {
            "ticker": args[0] if args else kwargs.get("ticker"),
            "mode": "synthesis",
            "synthesis": "STUBBED VERDICT",
            "bull_case": "stubbed bull",
            "bear_case": "stubbed bear",
        }

    api.run_synthesis = synthesis_stub

    os.environ["ALLOW_BYOK_VISITORS"] = ""
    r_syn_off = client_t.post("/api/byok/AAPL/synthesis", json={
        "report": "r", "context": "c", "policy": "p", "patterns": "pt"},
        headers={"X-Provider": "groq", "X-Provider-Key": good_key2})
    check("disabled instance refuses synthesis with 403", r_syn_off.status_code, 403)
    check("...and run_synthesis was never reached", len(synthesis_calls), 0)

    os.environ["ALLOW_BYOK_VISITORS"] = "1"
    r_syn_bad = client_t.post("/api/byok/AAPL/synthesis", json={
        "report": "r", "context": "c", "policy": "p", "patterns": "pt"},
        headers={"X-Provider": "groq", "X-Provider-Key": "too-short"})
    check("a too-short key is rejected with 400 on synthesis", r_syn_bad.status_code, 400)
    check("...and run_synthesis was never reached", len(synthesis_calls), 0)

    r_syn_ok = client_t.post("/api/byok/AAPL/synthesis", json={
        "report": "the actual report text", "context": "c", "policy": "p", "patterns": "pt"},
        headers={"X-Provider": "groq", "X-Provider-Key": good_key2})
    check("a valid synthesis request succeeds", r_syn_ok.status_code, 200)
    check("run_synthesis was reached exactly once", len(synthesis_calls), 1)
    check("the raw key never appears in the synthesis response body",
          good_key2 in r_syn_ok.text, False)
    check("the stubbed verdict made it through",
          r_syn_ok.json().get("synthesis"), "STUBBED VERDICT")

    syn_args, syn_kwargs = synthesis_calls[0]
    check("run_synthesis received the override as (provider, key)",
          syn_kwargs.get("override"), ("groq", good_key2))
    # syn_args[1] is the `modules` dict the route builds after fencing —
    # confirms the same sanitize_prompt_text() call this file's own
    # docstring promises runs on every tier, not skipped for an anonymous one.
    posted_modules = syn_args[1] if len(syn_args) > 1 else syn_kwargs.get("modules")
    check("the posted report text reached run_synthesis (fenced, not dropped)",
          "the actual report text" in (posted_modules or {}).get("report", ""), True)

    # Same reason-translation fix as the module stages above, for synthesis.
    def failing_synthesis_stub(*args, **kwargs):
        synthesis_calls.append((args, kwargs))
        return {
            "error": "the research provider is unavailable right now",
            "reason": "ratelimit", "cost_incurred": False,
            "ticker": args[0] if args else kwargs.get("ticker"),
        }

    api.run_synthesis = failing_synthesis_stub
    r_syn_fail = client_t.post("/api/byok/AAPL/synthesis", json={
        "report": "r", "context": "c", "policy": "p", "patterns": "pt"},
        headers={"X-Provider": "groq", "X-Provider-Key": good_key2})
    check("a real provider failure on synthesis is 502", r_syn_fail.status_code, 502)
    check("...with the specific translated reason, not the generic backend text",
          r_syn_fail.json().get("error"), "rate limit hit — wait a moment and retry")

    # ── Rate-limit bucket covers the staged paths too — same prefix match ──
    for stage_path in ("report", "context", "policy", "patterns", "synthesis"):
        b = api._budget_for(f"/api/byok/AAPL/{stage_path}")
        check(f"/api/byok/AAPL/{stage_path} lands in the byok bucket, not default",
              b, ("byok", 20, 300))

    # ── 7. Model Court — session-gated, dual BYOK key, graceful degradation ──
    # POST /api/model-court/{ticker} is a DIFFERENT shape from everything above:
    # it requires a session (owner or guest — require_session, not require_owner)
    # AND two caller-supplied keys, never one. run_model_court() lives in
    # tools/perplexity_research.py and calls the module-global names
    # run_perplexity_research/_call directly, so the stubs here patch the
    # `pr` module object itself — patching api.run_perplexity_research (as
    # section 3 does for the one-shot route) would NOT be seen by code
    # running inside tools/perplexity_research.py's own module namespace.
    section("Model Court — require_session gate, dual-key validation, degraded/success paths")
    import store
    import tools.perplexity_research as pr

    api._rate_limit_store.clear()

    mc_pplx_calls, mc_gemini_calls, mc_comparison_calls = [], [], []

    def mc_research_stub(ticker, question=None, override=None, status=None):
        # Mirrors run_perplexity_research's REAL failure contract, not a
        # convenient fake one: a provider failure (bad key, rate limit) is
        # signalled ONLY through `status`, never a top-level "error" key in
        # the returned dict — the dict looks exactly as "successful"-shaped
        # as a real report, with a friendly failure string in `report`. This
        # is deliberate: an earlier version of this stub returned
        # {"error": ...} directly on failure, which let run_model_court's
        # OWN status-folding logic (the actual fix for a real bug found via
        # live verification — a bad key used to slip through as pplx_ok=True,
        # wasting a third paid comparison call on two failure strings) go
        # completely unexercised. If that fold ever regresses, this stub
        # exercises the exact shape that would silently break.
        provider, key = override
        record = (ticker, question, override)
        (mc_pplx_calls if provider == "perplexity" else mc_gemini_calls).append(record)
        if "FAILME" in key:
            if status is not None:
                status.update(failed=True, reason="auth")
            return {
                "ticker": ticker, "mode": "full",
                "report": f"Research generation failed — check your {provider.upper()} key and try again.",
                "bull_case": "Bull case generation failed.", "bear_case": "Bear case generation failed.",
            }
        return {
            "ticker": ticker, "mode": "full",
            "report": f"STUBBED {provider.upper()} REPORT",
            "bull_case": "stubbed bull", "bear_case": "stubbed bear",
        }

    def mc_call_stub(prompt, model=None, max_tokens=None, client=None,
                      system=None, status=None, extra_body=None):
        mc_comparison_calls.append((prompt, model, system))
        return "STUBBED COMPARISON — A caught X, B caught Y"

    pr.run_perplexity_research = mc_research_stub
    pr._call = mc_call_stub

    mc_anon = TestClient(api.app)
    mc_owner = TestClient(api.app)
    mc_owner.post("/api/session", json={"key": "harness-only-secret"})
    guest_raw = store.create_guest_key("model-court@example.com")[1]
    mc_guest = TestClient(api.app)
    mc_guest.post("/api/session", json={"key": guest_raw})

    good_pplx = "pplx-" + "p" * 40
    good_gemini = "AIza" + "g" * 35
    fail_pplx = "pplx-FAILME" + "0" * 30
    fail_gemini = "AIzaFAILME" + "0" * 30

    # 7a. Anonymous — no session at all — is refused before either key is read.
    r_anon = mc_anon.post("/api/model-court/AAPL",
                           headers={"X-Perplexity-Key": good_pplx, "X-Gemini-Key": good_gemini})
    check("an anonymous caller (no session) is refused with 401 — require_session, "
          "not a looser check", r_anon.status_code, 401)
    check("...and neither provider stub was reached",
          (len(mc_pplx_calls), len(mc_gemini_calls)), (0, 0))

    # 7b. A guest session — proves the gate is require_session (owner OR guest),
    # not require_owner narrowed to the operator alone. Also the success path.
    r_guest = mc_guest.post("/api/model-court/AAPL",
                             headers={"X-Perplexity-Key": good_pplx, "X-Gemini-Key": good_gemini})
    check("a guest session is accepted — require_session covers guest, not just owner",
          r_guest.status_code, 200)
    check("...and both provider stubs were reached for the guest call",
          (len(mc_pplx_calls), len(mc_gemini_calls)), (1, 1))
    check("...and the comparison stub was reached once too (both sides succeeded)",
          len(mc_comparison_calls), 1)
    mc_comparison_calls.clear()  # 7e/7f below count comparison calls from zero again

    # From here on, use the owner session — same gate, exercised the other way.
    # 7c. Missing either header → 400, stub never reached.
    r_no_gemini = mc_owner.post("/api/model-court/MSFT",
                                 headers={"X-Perplexity-Key": good_pplx})
    check("missing X-Gemini-Key is rejected with 400", r_no_gemini.status_code, 400)
    check("...naming the missing header", "X-Gemini-Key" in r_no_gemini.text, True)

    r_no_pplx = mc_owner.post("/api/model-court/MSFT",
                               headers={"X-Gemini-Key": good_gemini})
    check("missing X-Perplexity-Key is rejected with 400", r_no_pplx.status_code, 400)
    check("...naming the missing header", "X-Perplexity-Key" in r_no_pplx.text, True)
    check("neither stub was reached by either missing-header request",
          (len(mc_pplx_calls), len(mc_gemini_calls)), (1, 1))  # unchanged since 7b

    # 7d. Malformed key on either side → 400, neither stub reached.
    r_bad_pplx_fmt = mc_owner.post("/api/model-court/MSFT", headers={
        "X-Perplexity-Key": "too-short", "X-Gemini-Key": good_gemini})
    check("a too-short X-Perplexity-Key is rejected with 400", r_bad_pplx_fmt.status_code, 400)

    r_bad_gemini_fmt = mc_owner.post("/api/model-court/MSFT", headers={
        "X-Perplexity-Key": good_pplx, "X-Gemini-Key": "too-short"})
    check("a too-short X-Gemini-Key is rejected with 400", r_bad_gemini_fmt.status_code, 400)
    check("format-invalid requests never reach either provider stub",
          (len(mc_pplx_calls), len(mc_gemini_calls)), (1, 1))  # still unchanged

    # 7e. One provider fails, one succeeds → degraded 200, comparison skipped.
    r_degraded = mc_owner.post("/api/model-court/NVDA", headers={
        "X-Perplexity-Key": fail_pplx, "X-Gemini-Key": good_gemini})
    check("one bad + one good provider still returns 200 (degraded, not an error status)",
          r_degraded.status_code, 200)
    degraded_body = r_degraded.json()
    check("the failed side's error is visible in the response — folded in from "
          "`status` by run_model_court, since the stub's own dict carries no "
          "'error' key (matching run_perplexity_research's real contract)",
          "error" in degraded_body.get("perplexity", {}), True)
    check("...translated to a short human phrase, not the bare 'auth' status code",
          degraded_body.get("perplexity", {}).get("error"), "the key was rejected")
    check("the working side's report made it through",
          degraded_body.get("gemini", {}).get("report"), "STUBBED GEMINI REPORT")
    check("master analysis was skipped — needs both sides",
          degraded_body.get("analysis"), None)
    check("...with a note explaining why, naming the failed provider",
          "Perplexity" in (degraded_body.get("analysis_note") or ""), True)
    check("no comparison call was attempted for the degraded request",
          len(mc_comparison_calls), 0)

    # 7f. Both succeed → both stubs reached, comparison reached exactly once,
    # neither raw key anywhere in the response.
    mc_pplx_calls.clear(); mc_gemini_calls.clear()
    r_full = mc_owner.post("/api/model-court/NVDA", headers={
        "X-Perplexity-Key": good_pplx, "X-Gemini-Key": good_gemini})
    check("both keys good — 200", r_full.status_code, 200)
    check("Perplexity stub reached exactly once", len(mc_pplx_calls), 1)
    check("Gemini stub reached exactly once", len(mc_gemini_calls), 1)
    check("the comparison stub was reached exactly once", len(mc_comparison_calls), 1)
    full_body = r_full.json()
    check("the master analysis text made it into the response",
          full_body.get("analysis"), "STUBBED COMPARISON — A caught X, B caught Y")
    check("no analysis_note on a fully successful run",
          full_body.get("analysis_note"), None)
    check("the raw Perplexity key never appears anywhere in the response body",
          good_pplx in r_full.text, False)
    check("the raw Gemini key never appears anywhere in the response body",
          good_gemini in r_full.text, False)

    # 7g. Both fail → run_model_court's own contract ("always a dict, never
    # raises") holds at the route level too: neither an exception nor a 500,
    # just an honest error-shaped 200 with both failures visible.
    r_both_fail = mc_owner.post("/api/model-court/NVDA", headers={
        "X-Perplexity-Key": fail_pplx, "X-Gemini-Key": fail_gemini})
    check("both providers failing does not crash the route", r_both_fail.status_code, 200)
    both_fail_body = r_both_fail.json()
    check("the top-level error names both providers failed",
          both_fail_body.get("error"), "both providers failed")

    # ── Rate-limit bucket ────────────────────────────────────────────────────
    mc_bucket = api._budget_for("/api/model-court/AAPL")
    check("model-court reuses the existing 'heavy' bucket (10, 60) — session-"
          "gated, not anonymous, so no new bucket was invented",
          mc_bucket, ("heavy", 10, 60))

    # ── Nothing persisted ────────────────────────────────────────────────────
    check("PERPLEXITY_API_KEY in os.environ is untouched by any Model Court call",
          os.environ.get("PERPLEXITY_API_KEY", ""), "")
    check("GEMINI_API_KEY in os.environ is untouched by any Model Court call",
          os.environ.get("GEMINI_API_KEY", ""), "")
    mc_src = inspect.getsource(api.api_model_court)
    check("api_model_court's own source never calls write_env_key",
          "write_env_key" in mc_src, False)
    check("api_model_court's own source never assigns os.environ",
          "os.environ[" in mc_src, False)


try:
    main()
finally:
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)

for n, got, want in PASS:
    print(f"  PASS  {n}")
for n, got, want in FAIL:
    print(f"  FAIL  {n}\n          got={got!r} want={want!r}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
