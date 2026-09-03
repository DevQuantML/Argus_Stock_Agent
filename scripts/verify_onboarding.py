#!/usr/bin/env python
"""
verify_onboarding.py — the setup wizard's .env writer, and what /health leaks.

What this guards
-----------------
`python main.py setup` (tools/setup_wizard.py) is the guided path a cloner
uses to connect their own PERPLEXITY_API_KEY or GROQ_API_KEY. Two ways that
path could go wrong, both silent unless a harness catches them:

  1. A naive copy of .env.example would leave a live-looking placeholder
     (PERPLEXITY_API_KEY=pplx-...) sitting in .env. _get_provider() treats
     any non-empty string as a real key, so the app would then fail against
     Perplexity's real API with a confusing 401 instead of the clean
     "no provider configured" error it gives today. ensure_env_file() exists
     specifically to neutralize this before it can happen — this harness
     proves it does, using the REAL .env.example shipped in this repo (not a
     hand-written stand-in), so drift between the two is caught here.
  2. The safe-write path (write_env_key / dotenv.set_key) must never corrupt
     or duplicate lines in an existing .env — that file also holds
     TRUST_PROXY, ARGUS_DB and Telegram settings a careless rewrite could
     silently drop.

It also asserts the boundary on the OTHER side: what /health is allowed to
say about provider configuration. app.js's boot sequence reads
checks.perplexity_key / checks.groq_key / checks.ai_provider to decide which
of three status lines to show — those booleans are the entire contract, and
this harness proves no more than that leaks (no key fragment, no provider
name beyond the boolean) and that the keyless path still returns 200.

Cost safety
-----------
Same structural guarantee as verify_access.py: PERPLEXITY_API_KEY and
GROQ_API_KEY are set to "" (never popped — see that script's docstring for
why popping is actively unsafe with load_dotenv(override=False)) before api
is ever imported. The env-writer tests below operate entirely on scratch
files under a tempdir; the real project .env is never opened, read, or
written by this script.

Run:  python scripts/verify_onboarding.py
Exit: 0 all passed, 1 any failed
"""
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="argus-onboarding-")

# ── Cost safety — before ANY project import ────────────────────────────────
os.environ["PERPLEXITY_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["ARGUS_DB"] = str(Path(_tmp) / "onboarding.db")
os.environ["TRUST_PROXY"] = "0"
os.environ["AGENT_SECRET"] = "harness-only-secret"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def section(title):
    print(f"\n{title}")


def main():
    from dotenv import dotenv_values

    from tools.setup_wizard import (
        ENV_PLACEHOLDERS,
        ensure_agent_secret,
        env_is_git_tracked,
        ensure_env_file,
        gitignore_covers_env,
        mask_key,
        soft_validate,
        write_env_key,
    )

    # ── 0. The constant matches the file it exists to protect against ──────
    # If .env.example's placeholder text ever changes without this constant
    # changing too, ensure_env_file() silently stops recognising it and the
    # whole neutralization step becomes a no-op — the exact failure mode
    # this harness exists to catch, so it is asserted first.
    section("drift — ENV_PLACEHOLDERS matches the real .env.example")
    example_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name, placeholder in ENV_PLACEHOLDERS.items():
        check(f"'{placeholder}' ({name}) appears verbatim in .env.example",
              placeholder in example_text, True)

    # ── 1. mask_key never returns the full secret ──────────────────────────
    section("mask_key — never the full secret")
    real_key = "gsk_" + secrets.token_hex(20)
    masked = mask_key(real_key)
    check("masked output does not contain the real key", real_key in masked, False)
    check("masked output keeps only the last 4 characters", masked, f"…{real_key[-4:]}")
    check("empty key masks to a plain status string", mask_key(""), "(not set)")
    check("None masks to a plain status string", mask_key(None), "(not set)")
    check("a key shorter than 5 chars fully redacts", mask_key("abcd"), "****")

    # ── 2. soft_validate — format only, no network, no false accepts ───────
    section("soft_validate — catches format mistakes without a network call")
    check("empty string rejected", soft_validate("")[0], False)
    check("the shipped Perplexity placeholder rejected",
          soft_validate(ENV_PLACEHOLDERS["PERPLEXITY_API_KEY"])[0], False)
    check("the shipped Groq placeholder rejected",
          soft_validate(ENV_PLACEHOLDERS["GROQ_API_KEY"])[0], False)
    check("leading/trailing whitespace rejected", soft_validate("  gsk_" + "a" * 30)[0], False)
    check("a stray newline rejected", soft_validate("gsk_" + "a" * 30 + "\n")[0], False)
    check("a too-short string rejected", soft_validate("short")[0], False)
    check("a plausible key accepted", soft_validate("gsk_" + "a" * 40)[0], True)

    # ── 3. env_is_git_tracked — both directions, inside a real git repo ────
    # A warning that never fires is worse than no warning, so this proves
    # the detector actually flags a tracked file, not just that it stays
    # quiet on an untracked one.
    section("env_is_git_tracked — positive and negative control")
    repo = Path(_tmp) / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    tracked = repo / ".env"
    tracked.write_text("PERPLEXITY_API_KEY=real-looking-value\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env"], cwd=repo, capture_output=True)
    check("a git-added .env is detected as tracked", env_is_git_tracked(tracked), True)

    untracked = repo / ".env.local"
    untracked.write_text("GROQ_API_KEY=other-value\n", encoding="utf-8")
    check("a file never git-added is NOT flagged as tracked",
          env_is_git_tracked(untracked), False)

    no_repo_dir = Path(_tmp) / "no-repo"
    no_repo_dir.mkdir()
    outside_repo = no_repo_dir / ".env"
    outside_repo.write_text("x=1\n", encoding="utf-8")
    check("a path with no git repo at all reports False, not an exception",
          env_is_git_tracked(outside_repo), False)

    # ── 3b. gitignore_covers_env — the OTHER half of the same guarantee ────
    # env_is_git_tracked only proves the file isn't tracked YET; this proves
    # .gitignore will keep it that way. Positive and negative control, plus
    # a check against the REAL .gitignore this repo actually ships.
    section("gitignore_covers_env — positive and negative control")
    covered_dir = Path(_tmp) / "covered"
    covered_dir.mkdir()
    (covered_dir / ".gitignore").write_text(".env\n*.log\n", encoding="utf-8")
    check("a .gitignore listing .env is detected as covering it",
          gitignore_covers_env(covered_dir), True)

    uncovered_dir = Path(_tmp) / "uncovered"
    uncovered_dir.mkdir()
    (uncovered_dir / ".gitignore").write_text("*.log\nnode_modules/\n", encoding="utf-8")
    check("a .gitignore that never mentions .env is NOT flagged as covering it",
          gitignore_covers_env(uncovered_dir), False)

    missing_dir = Path(_tmp) / "no-gitignore"
    missing_dir.mkdir()
    check("a missing .gitignore reports False, not an exception",
          gitignore_covers_env(missing_dir), False)

    check("this repo's own .gitignore actually covers .env (the real invariant)",
          gitignore_covers_env(ROOT), True)

    # ── 4. ensure_env_file — the real .env.example, placeholders cleared ───
    section("ensure_env_file — placeholders neutralized, comments preserved")
    scratch = Path(_tmp) / "scratch"
    scratch.mkdir()
    example_path = scratch / ".env.example"
    shutil.copyfile(ROOT / ".env.example", example_path)
    env_path = scratch / ".env"

    created = ensure_env_file(env_path, example_path)
    check("a missing .env is created", created, True)

    # POSIX only — Windows' chmod only toggles the read-only bit, not real
    # owner/group/other permission bits, so this assertion is meaningless
    # there and is skipped rather than made to pass by accident.
    if os.name != "nt":
        mode = env_path.stat().st_mode & 0o777
        check("a freshly created .env is not group/other readable or writable",
              mode & 0o077, 0)

    written_text = env_path.read_text(encoding="utf-8")
    for placeholder in ENV_PLACEHOLDERS.values():
        check(f"placeholder '{placeholder}' does not survive into .env",
              placeholder in written_text, False)

    values = dotenv_values(env_path)
    check("PERPLEXITY_API_KEY cleared to empty (falsy) rather than left as a placeholder",
          values.get("PERPLEXITY_API_KEY"), "")
    check("GROQ_API_KEY cleared to empty (falsy) rather than left as a placeholder",
          values.get("GROQ_API_KEY"), "")
    secret = values.get("AGENT_SECRET") or ""
    check("AGENT_SECRET was regenerated, not left as the public placeholder",
          secret != ENV_PLACEHOLDERS["AGENT_SECRET"] and len(secret) == 64, True)

    # A comment untouched by this feature's own .env.example edit must survive
    # the copy — proves the whole file was preserved, not just the three keys.
    check("an unrelated comment survives the copy",
          "TRUST_PROXY is a HOP COUNT" in written_text, True)

    # Calling again on an existing .env must not touch it — this is what stops
    # a second `python main.py setup` run from silently discarding a key the
    # user already set by hand between runs.
    write_env_key(env_path, "GROQ_API_KEY", "gsk_" + "b" * 40)
    before_second_call = env_path.read_text(encoding="utf-8")
    created_again = ensure_env_file(env_path, example_path)
    check("ensure_env_file on an existing .env reports no file was created",
          created_again, False)
    check("ensure_env_file never rewrites an existing .env",
          env_path.read_text(encoding="utf-8"), before_second_call)

    # ── 5. write_env_key — one line changes, everything else is untouched ──
    section("write_env_key — surgical, idempotent, no collateral edits")
    write_env_key(env_path, "PERPLEXITY_API_KEY", "pplx-" + "c" * 40)
    after_pplx = dotenv_values(env_path)
    check("the written key is present", after_pplx.get("PERPLEXITY_API_KEY"), "pplx-" + "c" * 40)
    check("a sibling key set earlier survives an unrelated write",
          after_pplx.get("GROQ_API_KEY"), "gsk_" + "b" * 40)
    check("AGENT_SECRET survives an unrelated write", after_pplx.get("AGENT_SECRET"), secret)

    raw = env_path.read_text(encoding="utf-8")
    check("exactly one PERPLEXITY_API_KEY line exists after a rewrite",
          raw.count("PERPLEXITY_API_KEY="), 1)

    write_env_key(env_path, "PERPLEXITY_API_KEY", "pplx-" + "d" * 40)
    raw2 = env_path.read_text(encoding="utf-8")
    check("writing the same key again is idempotent, not additive",
          raw2.count("PERPLEXITY_API_KEY="), 1)
    check("the second write's value took effect",
          dotenv_values(env_path).get("PERPLEXITY_API_KEY"), "pplx-" + "d" * 40)

    # ── 5b. ensure_agent_secret — the auto-bootstrap api.py runs at startup ─
    # This is what lets a fresh clone skip the CLI wizard entirely for the
    # FIRST secret: api.py calls this before anything else can reach owner
    # routes. Three cases, matching the three branches in api.py's own
    # startup check (not os.getenv("AGENT_SECRET")).
    section("ensure_agent_secret — fresh / already-real / placeholder cases")
    fresh_dir = Path(_tmp) / "agent-secret-fresh"
    fresh_dir.mkdir()
    fresh_example = fresh_dir / ".env.example"
    shutil.copyfile(ROOT / ".env.example", fresh_example)
    fresh_env = fresh_dir / ".env"

    generated = ensure_agent_secret(fresh_env, fresh_example)
    check("a missing .env yields a freshly generated 64-hex secret",
          bool(generated) and len(generated) == 64, True)
    check("that secret is the one actually written to the new .env",
          dotenv_values(fresh_env).get("AGENT_SECRET"), generated)

    real_dir = Path(_tmp) / "agent-secret-real"
    real_dir.mkdir()
    real_env = real_dir / ".env"
    real_env_text = "AGENT_SECRET=already-a-real-secret-value-here\nOTHER=1\n"
    real_env.write_text(real_env_text, encoding="utf-8")
    result_real = ensure_agent_secret(real_env, fresh_example)
    check("an existing real secret returns None — nothing to display", result_real, None)
    check("...and the file itself is byte-for-byte unchanged",
          real_env.read_text(encoding="utf-8"), real_env_text)

    ph_dir = Path(_tmp) / "agent-secret-placeholder"
    ph_dir.mkdir()
    ph_env = ph_dir / ".env"
    ph_env.write_text(f"AGENT_SECRET={ENV_PLACEHOLDERS['AGENT_SECRET']}\n", encoding="utf-8")
    regenerated = ensure_agent_secret(ph_env, fresh_example)
    check("a placeholder AGENT_SECRET is regenerated, not left as the public value",
          bool(regenerated) and regenerated != ENV_PLACEHOLDERS["AGENT_SECRET"], True)
    check("...and the regenerated value is what's actually in the file",
          dotenv_values(ph_env).get("AGENT_SECRET"), regenerated)

    # ── 6. /health — the exact signal the boot sequence consumes ───────────
    # Keys are set on os.environ directly (not via the wizard) because
    # _get_provider() and /health both read os.environ at call time; this
    # section is about what the route reports, not the wizard's file I/O.
    section("/health — reports presence only, leaks nothing else, stays 200")
    import api
    from fastapi.testclient import TestClient

    client = TestClient(api.app)

    r_none = client.get("/health")
    check("/health with no provider configured returns 200", r_none.status_code, 200)
    checks_none = r_none.json().get("checks", {})
    check("ai_provider is False with nothing configured", checks_none.get("ai_provider"), False)
    check("perplexity_key is False with nothing configured", checks_none.get("perplexity_key"), False)
    check("groq_key is False with nothing configured", checks_none.get("groq_key"), False)

    os.environ["GROQ_API_KEY"] = "gsk_" + "e" * 40
    r_groq = client.get("/health")
    checks_groq = r_groq.json().get("checks", {})
    check("groq_key True once GROQ_API_KEY is set", checks_groq.get("groq_key"), True)
    check("perplexity_key stays False — presence is per-key, not shared",
          checks_groq.get("perplexity_key"), False)
    check("ai_provider True once any one provider key is set",
          checks_groq.get("ai_provider"), True)
    check("the raw Groq key value never appears in the /health response body",
          os.environ["GROQ_API_KEY"] in r_groq.text, False)

    os.environ["PERPLEXITY_API_KEY"] = "pplx-" + "f" * 40
    r_both = client.get("/health")
    checks_both = r_both.json().get("checks", {})
    check("perplexity_key True once PERPLEXITY_API_KEY is also set",
          checks_both.get("perplexity_key"), True)
    check("the raw Perplexity key value never appears in the /health response body",
          os.environ["PERPLEXITY_API_KEY"] in r_both.text, False)

    # Restore the blanked state so nothing below this line runs against a
    # "configured" provider by accident.
    os.environ["GROQ_API_KEY"] = ""
    os.environ["PERPLEXITY_API_KEY"] = ""

    # ── 7. Regression — keyless public paths still work ────────────────────
    # /api/demo/AAPL makes a real (free, non-AI) yfinance call — same
    # tolerance verify_hardening.py already accepts for this exact endpoint.
    section("regression — keyless public paths still return 200")
    check("/health stays 200 with no provider (never 500, per api.py's own rule)",
          client.get("/health").status_code, 200)
    r_demo = client.get("/api/demo/AAPL")
    check("/api/demo/AAPL still works with no AI provider configured",
          r_demo.status_code, 200)

    # ── 8. POST /api/settings/provider-key — the new browser-entry route ───
    # api._ENV_FILE_PATH is monkeypatched to a scratch file for this section
    # only — this route writes to whatever that constant points at, and the
    # constant is exactly what makes the route testable without ever risking
    # the real project .env. Restored in `finally` regardless of outcome.
    section("provider-key route — auth boundary, validation, live effect")
    import store

    scratch_env = Path(_tmp) / "settings-route.env"
    scratch_env.write_text("", encoding="utf-8")
    prev_env_path = api._ENV_FILE_PATH
    api._ENV_FILE_PATH = scratch_env

    try:
        anon = TestClient(api.app)
        r_anon = anon.post("/api/settings/provider-key",
                            json={"provider": "groq", "key": "gsk_" + "x" * 40})
        check("anonymous caller gets 401, not a silent pass", r_anon.status_code, 401)

        guest_key = store.create_guest_key("settings-guest@example.com")[1]
        gc = TestClient(api.app)
        gc.post("/api/session", json={"key": guest_key})
        r_guest = gc.post("/api/settings/provider-key",
                           json={"provider": "groq", "key": "gsk_" + "y" * 40})
        check("a guest session gets 403 (entitled, not authorized), not 401",
              r_guest.status_code, 403)

        owner = TestClient(api.app)
        r_login = owner.post("/api/session", json={"key": "harness-only-secret"})
        check("owner login for this section succeeds", r_login.status_code, 200)

        r_bad_provider = owner.post("/api/settings/provider-key",
                                     json={"provider": "openai", "key": "sk-" + "z" * 40})
        check("an unrecognised provider name is rejected with 400",
              r_bad_provider.status_code, 400)

        # Gemini is a real entry in PROVIDER_KEY_NAMES (BYOK's benefit) but
        # NOT in PERSISTENT_PROVIDERS — this is what proves the BYOK-only
        # boundary from Phase 4 is enforced here, not just documented.
        # Format-valid key on purpose: a rejection for being too-short would
        # prove nothing about which provider check actually fired.
        r_gemini_via_config = owner.post("/api/settings/provider-key",
                                          json={"provider": "gemini", "key": "AIza" + "y" * 35})
        check("Gemini is refused by CONFIG's route even with a format-valid key — BYOK-only",
              r_gemini_via_config.status_code, 400)
        check("...and nothing was ever written for it",
              "GEMINI_API_KEY" in scratch_env.read_text(encoding="utf-8"), False)

        r_bad_key = owner.post("/api/settings/provider-key",
                                json={"provider": "groq", "key": "too-short"})
        check("a too-short key is rejected with 400 via the shared soft_validate",
              r_bad_key.status_code, 400)
        check("...and the rejected write never touches the scratch file",
              scratch_env.read_text(encoding="utf-8"), "")

        good_key = "gsk_" + "a" * 40
        r_ok = owner.post("/api/settings/provider-key",
                           json={"provider": "groq", "key": good_key})
        check("a valid key from the owner succeeds", r_ok.status_code, 200)
        body_ok = r_ok.json()
        check("response reports configured=true", body_ok.get("configured"), True)
        check("the raw key never appears anywhere in the response body",
              good_key in r_ok.text, False)
        check("the key actually landed in the (scratch) .env file",
              dotenv_values(scratch_env).get("GROQ_API_KEY"), good_key)
        check("the key is applied live to THIS process immediately — no restart",
              os.environ.get("GROQ_API_KEY"), good_key)

        r_clear = owner.post("/api/settings/provider-key",
                              json={"provider": "groq", "key": ""})
        check("an empty key is treated as a deliberate clear — 200, not 400",
              r_clear.status_code, 200)
        check("response reports configured=false after clearing",
              r_clear.json().get("configured"), False)
        check("the scratch .env reflects the clear",
              dotenv_values(scratch_env).get("GROQ_API_KEY"), "")
    finally:
        api._ENV_FILE_PATH = prev_env_path
        os.environ["GROQ_API_KEY"] = ""
        os.environ["PERPLEXITY_API_KEY"] = ""


try:
    main()
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

for n, got, want in PASS:
    print(f"  PASS  {n}")
for n, got, want in FAIL:
    print(f"  FAIL  {n}\n          got={got!r} want={want!r}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
