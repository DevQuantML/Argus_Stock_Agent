"""
tools/gemini_native.py — Gemini's NATIVE endpoint, wearing the OpenAI client's
shape so nothing upstream has to know the difference.

Why this file exists
--------------------
Grounding with Google Search is **not available through Gemini's
OpenAI-compatibility endpoint** — confirmed three ways: every documented
`extra_body` form is rejected there (`Unknown name "google"` /
`Unknown name "google_search"` / `Invalid tool type`), other developers report
the identical errors, and a Google moderator states it outright on their own
forum. It is not a syntax we had wrong; the capability is not wired into that
endpoint. Without it Gemini answers from training data, which for an equity
research tool is the difference between a citation and a guess.

The native endpoint *does* accept grounding — verified against the live API
with a deliberately fake key before any of this was written (Google validates
request shape before the credential, so the probe cost nothing).

Why an adapter rather than a second code path
---------------------------------------------
`_call()` in perplexity_research.py speaks exactly one dialect:

    client.chat.completions.create(model=…, messages=…, max_tokens=…,
                                   extra_body=…).choices[0].message.content

Every provider reaches it through that one shape. Rather than branch `_call()`
on provider name — which would put a provider-specific `if` in the single
function every research call in the app funnels through — this exposes the
same three attributes and translates underneath. `_get_provider()` hands back
one of these instead of an `OpenAI` client for Gemini, and **every existing
call site is unchanged**.

No new dependency: stdlib `urllib` only, the same choice `tools/notify.py`
already makes for its outbound HTTPS.

Errors are re-raised as `RuntimeError` whose text mimics the OpenAI SDK's
(`"Error code: 400 - {…}"`), because `_call()`'s failure classifier reads that
string to tell auth from rate-limit from a malformed request. Matching the
format means Gemini failures classify identically to every other provider's
rather than needing their own branch.

The API key travels in the `x-goog-api-key` header, never the URL — a URL
would land the caller's key in logs and proxies. It is never logged here.
"""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_TIMEOUT_S = 90
# Enough of the provider's error body to classify and debug, not so much that
# a hostile upstream can flood a log line.
_MAX_ERR_CHARS = 600


class _Message:
    __slots__ = ("content",)

    def __init__(self, content):
        self.content = content


class _Choice:
    __slots__ = ("message",)

    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    """Only the attributes _call() actually reads, plus grounding facts.

    `search_queries` is what makes "did it really search?" answerable instead
    of assumed — the risk this whole integration was flagged for from the
    start. Empty list means the model answered without searching.
    """

    __slots__ = ("choices", "search_queries", "sources")

    def __init__(self, content, search_queries=None, sources=None):
        self.choices = [_Choice(content)] if content is not None else []
        self.search_queries = search_queries or []
        self.sources = sources or []


class _Completions:
    def __init__(self, api_key):
        self._api_key = api_key

    def create(self, model, messages, max_tokens=None, extra_body=None, **_ignored):
        payload = _build_payload(messages, max_tokens, extra_body)
        data = _post(f"{_BASE_URL}/models/{model}:generateContent",
                     payload, self._api_key)
        return _parse(data)


class _Chat:
    __slots__ = ("completions",)

    def __init__(self, api_key):
        self.completions = _Completions(api_key)


class GeminiNativeClient:
    """Duck-types the slice of `openai.OpenAI` that `_call()` uses.

    `api_key` is exposed deliberately: `_call()` redacts `client.api_key` out
    of provider error messages before logging them, and a client that hides
    the attribute would silently opt out of that redaction.
    """

    __slots__ = ("api_key", "chat", "base_url")

    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = _BASE_URL
        self.chat = _Chat(api_key)


# ── Translation ────────────────────────────────────────────────────────────

def _build_payload(messages, max_tokens, extra_body):
    """OpenAI `messages` → native `contents` + `systemInstruction`.

    The system role is not a turn in the native API — it is a separate
    top-level field. Appending it to `contents` as a user turn (the obvious
    shortcut) both weakens it and lets the model reply to it as if it were the
    question.
    """
    contents, system_parts = [], []
    for m in messages or []:
        text = m.get("content") or ""
        if not text:
            continue
        if m.get("role") == "system":
            system_parts.append(text)
            continue
        contents.append({
            "role": "model" if m.get("role") == "assistant" else "user",
            "parts": [{"text": text}],
        })

    payload = {"contents": contents}
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    if max_tokens:
        payload["generationConfig"] = {"maxOutputTokens": max_tokens}
    if extra_body:
        # Native-shaped already (e.g. {"tools": [{"google_search": {}}]}).
        # Merged at top level, not nested under a vendor key — that nesting is
        # precisely what the compat endpoint rejected.
        payload.update(extra_body)
    return payload


def _post(url, payload, api_key):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:_MAX_ERR_CHARS]
        except Exception:  # noqa: BLE001 — a body we cannot read must not mask the status
            pass
        # Mirrors the OpenAI SDK's message format on purpose: _call()'s
        # classifier reads this string, so matching it means Gemini's failures
        # sort into the same auth / ratelimit / not_found / bad_request
        # buckets as every other provider, with no Gemini-specific branch.
        raise RuntimeError(f"Error code: {exc.code} - {body}") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise RuntimeError(f"Request timed out after {_TIMEOUT_S}s") from None
        raise RuntimeError(f"Network error contacting Gemini: {reason}") from None
    except TimeoutError:
        raise RuntimeError(f"Request timed out after {_TIMEOUT_S}s") from None


def _parse(data):
    """Native response → the OpenAI-ish object, with citations appended.

    Sources are appended to the text rather than returned only as structured
    data because every consumer of a research module renders one markdown
    string. A grounded answer whose sources never reach the page is, from the
    reader's side, indistinguishable from an ungrounded one.
    """
    candidates = data.get("candidates") or []
    if not candidates:
        # No candidate at all — _call() turns an empty `choices` into its
        # "empty" reason, which is the honest reading (billed, said nothing).
        return _Response(None)

    first = candidates[0]
    parts = ((first.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))

    meta = first.get("groundingMetadata") or {}
    queries = list(meta.get("webSearchQueries") or [])

    sources, seen = [], set()
    for chunk in meta.get("groundingChunks") or []:
        web = (chunk or {}).get("web") or {}
        uri = web.get("uri")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        sources.append((web.get("title") or uri, uri))

    if sources:
        lines = "\n".join(f"- [{title}]({uri})" for title, uri in sources)
        text = f"{text}\n\n**Sources**\n{lines}"

    return _Response(text, search_queries=queries, sources=sources)
