"""
tools/setup_wizard.py — guided ".env" setup for `python main.py setup`.

Split deliberately into a pure/testable core (env-file reads and writes, key
masking, format checks) and a thin interactive shell (`run_wizard`) that only
prints and prompts. The core is what scripts/verify_onboarding.py exercises
without touching stdin; the shell is not unit-tested — see that harness's
docstring for why that split is safe.

Security invariants this file exists to hold:
  - The raw key is written to .env and NOWHERE else — never printed in full,
    never logged, never returned from any function here. mask_key() is the
    only thing that ever reaches a terminal or a caller.
  - A freshly copied .env never contains a live-looking placeholder. Copying
    .env.example verbatim would leave PERPLEXITY_API_KEY=pplx-... sitting in
    .env, which _get_provider() (tools/perplexity_research.py) treats as a
    real, truthy key — the app would then fail against Perplexity's real API
    with a confusing 401 instead of the clean "no provider configured" error
    it gives today. ensure_env_file() clears that trap before anyone can hit
    it.
  - No live network call happens unless the user explicitly opts in
    (verify_key_live) — matches the project's "never spend to check
    something works" rule; the person who opted in is spending their own key
    at most a few cents, never the operator's.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
from pathlib import Path

from dotenv import dotenv_values, set_key

# Placeholders shipped in .env.example — copied verbatim, these would be
# treated as real values by the app. Every entry here MUST match that file
# exactly, or ensure_env_file() silently stops recognising it.
ENV_PLACEHOLDERS = {
    "PERPLEXITY_API_KEY": "pplx-...",
    "GROQ_API_KEY": "gsk_...",
    "AGENT_SECRET": "change-me-to-a-strong-random-string",
}

# The full canonical map — every provider this app can build a client for,
# including Gemini, which is BYOK-only (see PERSISTENT_PROVIDERS below).
# `/api/byok/*` routes check membership here, not a separately hardcoded
# tuple, so a fourth provider added later needs one entry, not one entry
# per call site scattered across the codebase.
PROVIDER_KEY_NAMES = {
    "perplexity": "PERPLEXITY_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

# The narrower subset that can be PERSISTED — via this wizard, or via
# POST /api/settings/provider-key (api.py). Gemini is deliberately absent:
# BYOK-only for now (still-beta OpenAI-compat integration on Google's side —
# see tools/perplexity_research.py's _GEMINI_EXTRA_BODY comment), never
# offered here and never accepted by that route. Promoting it to full
# parity is a later, separate decision, not an oversight.
PERSISTENT_PROVIDERS = ("perplexity", "groq")

PROVIDER_SIGNUP_URLS = {
    "perplexity": "https://www.perplexity.ai/settings/api",
    "groq": "https://console.groq.com/keys",
    "gemini": "https://aistudio.google.com/apikey",
}


def generate_agent_secret() -> str:
    """The same recipe .env.example tells a manual user to run by hand."""
    return secrets.token_hex(32)


def mask_key(value: str | None) -> str:
    """`…ab12` — enough for the user to recognise their own key, never enough
    to reconstruct it. Mirrors the guest-key display-prefix convention in
    store.py: persist/print a fragment, never the secret itself."""
    if not value:
        return "(not set)"
    if len(value) <= 4:
        return "*" * len(value)
    return f"…{value[-4:]}"


def soft_validate(value: str) -> tuple[bool, str]:
    """Format-only check — no network call, so this can never spend money.

    Deliberately does NOT check a provider-specific prefix (pplx-/gsk_):
    providers change key formats without notice, and a stale prefix check
    would reject a real, working key. This only catches mistakes that are
    wrong regardless of provider — an empty paste, a placeholder left in by
    accident, or a copy that dragged in whitespace or a stray newline.
    """
    if not value:
        return False, "nothing entered"
    if value.strip() != value or "\n" in value or "\t" in value:
        return False, "contains leading/trailing whitespace or a stray newline — check your paste"
    if value in ENV_PLACEHOLDERS.values():
        return False, "that's the placeholder from .env.example, not a real key"
    if len(value) < 20:
        return False, "too short to be a real API key"
    return True, ""


def env_is_git_tracked(env_path: Path) -> bool:
    """True only if .env is ALREADY committed to git in this repo — a real
    leak risk this wizard is about to make worse by filling it with a real
    key. Best-effort: not a git repo, git missing, or a genuinely untracked
    .env all report False alike — this is a warning nudge, not a hard gate,
    so a failure to detect must never block setup."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(env_path)],
            cwd=env_path.parent,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def gitignore_covers_env(root: Path) -> bool:
    """True if .gitignore at the repo root actually lists .env.

    env_is_git_tracked() only proves the file isn't tracked YET — it says
    nothing about whether .gitignore will keep protecting it going forward.
    If that entry were ever edited out, env_is_git_tracked() would keep
    reporting "safe" right up until the next `git add -A` commits the real
    key. This is the second half of that guarantee. Best-effort: a missing
    or unreadable .gitignore reports False rather than raising.
    """
    gitignore = root / ".gitignore"
    try:
        lines = {ln.strip() for ln in gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()}
    except OSError:
        return False
    return bool(lines & {".env", ".env.*", ".env*"})


def ensure_env_file(env_path: Path, example_path: Path) -> bool:
    """Create .env from .env.example if missing, with its placeholders
    neutralized. Returns True if a new file was created, False if .env
    already existed — in which case this function touches nothing, because
    an existing .env may hold real values that must never be overwritten
    here.

    Provider-key placeholders are cleared to "" (falsy, so _get_provider()
    correctly skips them instead of trying to authenticate as "pplx-...").
    AGENT_SECRET's placeholder is replaced with a freshly generated secret,
    so a new install is never left gated by a public, guessable string that
    ships in the repo.
    """
    if env_path.exists():
        return False
    if not example_path.exists():
        raise FileNotFoundError(f"{example_path} not found — cannot bootstrap .env")

    shutil.copyfile(example_path, env_path)
    try:
        # shutil.copyfile does not carry over permission bits — the new file
        # gets the process umask's default, commonly world-readable (644) on
        # common Linux/macOS defaults. .env is about to hold real API keys
        # and AGENT_SECRET, so lock it to the owner only. Best-effort: some
        # filesystems (certain Windows configurations, some network mounts)
        # don't honor POSIX mode bits, and a failure here must never block
        # setup over a permission cosmetic.
        env_path.chmod(0o600)
    except OSError:
        pass

    values = dotenv_values(env_path)
    for name, placeholder in ENV_PLACEHOLDERS.items():
        if values.get(name) == placeholder:
            new_value = generate_agent_secret() if name == "AGENT_SECRET" else ""
            set_key(str(env_path), name, new_value, quote_mode="never")
    return True


def write_env_key(env_path: Path, name: str, value: str) -> None:
    """Write exactly one key into .env, preserving every other line and
    comment. A thin wrapper over python-dotenv's set_key — the same library
    main.py and api.py already use to READ .env, used here to WRITE it, so
    there is exactly one parser for this file format in the whole project."""
    set_key(str(env_path), name, value, quote_mode="never")


def ensure_agent_secret(env_path: Path, example_path: Path) -> str | None:
    """Guarantee .env holds a real AGENT_SECRET, generating one if needed.

    Called from api.py at server startup so AGENT_SECRET is always safely
    self-served without a terminal wizard for the FIRST secret — starting the
    server is the one unavoidable terminal step, and this is what makes it
    also the last one. Every other key (a Groq/Perplexity credential) can
    then be added from the browser's CONFIG modal once unlocked with this.

    Returns the freshly generated secret when one was just created — the
    caller prints it exactly once, since this is the only place it will ever
    be shown. Returns None when a real secret already existed and nothing
    was touched.
    """
    if not env_path.exists():
        ensure_env_file(env_path, example_path)  # already generates AGENT_SECRET
        return dotenv_values(env_path).get("AGENT_SECRET") or None

    current = dotenv_values(env_path).get("AGENT_SECRET")
    if current and current != ENV_PLACEHOLDERS["AGENT_SECRET"]:
        return None  # already set for real — nothing to do, nothing to show

    secret = generate_agent_secret()
    write_env_key(env_path, "AGENT_SECRET", secret)
    return secret


def verify_key_live(provider: str, api_key: str) -> tuple[bool, str]:
    """One minimal call using the user's OWN key. Opt-in only — see
    run_wizard(). Never raises, never echoes the key. Costs at most a few
    cents on Perplexity, nothing on Groq's free tier — and only ever the
    caller's own money, never the operator's."""
    from openai import OpenAI

    from tools.perplexity_research import _GROQ_FAST, _SONAR

    if provider == "perplexity":
        client = OpenAI(api_key=api_key, base_url="https://api.perplexity.ai")
        model = _SONAR
    else:
        client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
        model = _GROQ_FAST

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Reply with exactly one word: OK"},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=5,
        )
        if response and response.choices and (response.choices[0].message.content or "").strip():
            return True, "key works"
        return False, "provider returned an empty response"
    except Exception as exc:  # noqa: BLE001 — this is a user-facing status line, not a crash
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg or "invalid_api_key" in msg or "invalid api key" in msg:
            return False, "provider rejected the key (check you copied it correctly)"
        return False, f"could not reach {provider} right now ({type(exc).__name__}) — this doesn't necessarily mean the key is bad"


# ── Interactive shell — not unit-tested; see module docstring ─────────────

_BANNER = """
──────────────────────────────────────────────────────────
 ARGUS SETUP — connect a research engine
──────────────────────────────────────────────────────────"""

_PROVIDER_EXPLAINER = """
Two choices — pick one to start, add the other any time later:

  [g] Groq        FREE, no card, about a minute to sign up.
                  No live web search — answers come from the
                  model's own training, which can be months
                  out of date. Every research run says so.

  [p] Perplexity  PAID (roughly $0.04-0.21 per run). Searches
                  the live web while it answers, so filings
                  and news are current — the full-quality path.
"""


def _print_next_steps() -> None:
    print("\nNext step — start the server:")
    print("  uvicorn api:app --workers 1 --no-proxy-headers")
    print("Then open http://localhost:8000 and unlock with your AGENT_SECRET.")
    print("(To add or change a key later, you don't need this wizard again —")
    print(" unlock, then open ◈ CONFIG in the app itself.)")


def run_wizard() -> None:
    """Interactive shell. Everything above this section is pure and covered
    by scripts/verify_onboarding.py; this function only orchestrates
    print()/input() around it."""
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    example_path = root / ".env.example"

    print(_BANNER)

    created = ensure_env_file(env_path, example_path)
    print(f"{'Created' if created else 'Found existing'} .env at {env_path}")

    existing = dotenv_values(env_path)
    have_pplx = bool(existing.get("PERPLEXITY_API_KEY"))
    have_groq = bool(existing.get("GROQ_API_KEY"))
    if have_pplx or have_groq:
        current = "Perplexity" if have_pplx else "Groq"
        print(f"\nA research engine is already connected: {current}.")
        again = input("Connect a different key instead? [y/N]: ").strip().lower()
        if again not in ("y", "yes"):
            print("Nothing changed.")
            _print_next_steps()
            return

    print(_PROVIDER_EXPLAINER)
    choice = input("Which will you use — [g]roq or [p]erplexity? [g]: ").strip().lower() or "g"
    provider = "perplexity" if choice.startswith("p") else "groq"
    key_name = PROVIDER_KEY_NAMES[provider]

    print(f"\nGet a key here: {PROVIDER_SIGNUP_URLS[provider]}")
    print("(Paste it here, not into the website — the browser never asks for this key.)")
    while True:
        value = input(f"Paste your {key_name}: ").strip()
        ok, reason = soft_validate(value)
        if ok:
            break
        print(f"  ✕ {reason} — try again.")

    if env_is_git_tracked(env_path):
        print("\n⚠  WARNING: .env is already tracked by git in this repository.")
        print("   Your key would be committed. Run  git rm --cached .env  first,")
        print("   or it may end up on GitHub.")
        if input("Continue anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("Stopped — nothing written.")
            return
    elif not gitignore_covers_env(root):
        print("\n⚠  WARNING: .gitignore doesn't list .env in this repository.")
        print("   It isn't committed yet, but nothing stops a future")
        print("   `git add -A` from picking it up. Add a  .env  line to")
        print("   .gitignore before you push.")

    write_env_key(env_path, key_name, value)
    print(f"  ✓ {key_name} saved as {mask_key(value)}")

    if existing.get("AGENT_SECRET") in (None, "", ENV_PLACEHOLDERS["AGENT_SECRET"]):
        secret = generate_agent_secret()
        write_env_key(env_path, "AGENT_SECRET", secret)
        print(f"  ✓ AGENT_SECRET generated — paste this into the browser's Settings drawer:")
        print(f"      {secret}")
    else:
        print("  · AGENT_SECRET already set — unchanged")

    cost_note = ("free on Groq" if provider == "groq"
                 else "a fraction of a cent on Perplexity — your own key, your own charge")
    if input(f"\nTest this key with one tiny live call now? ({cost_note}) [y/N]: ").strip().lower() in ("y", "yes"):
        print("Testing…")
        ok, msg = verify_key_live(provider, value)
        print(f"  {'✓' if ok else '✕'} {msg}")

    _print_next_steps()


if __name__ == "__main__":
    run_wizard()
