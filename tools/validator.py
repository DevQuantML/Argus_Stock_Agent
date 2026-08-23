"""
tools/validator.py — Input validation and injection guard.

Three functions applied at every external boundary:
  validate_ticker()    — before any API call in stock_data / oil_price
  sanitize_question()  — before user question enters agent message history
  guard_tool_output()  — before every tool result enters agent message history
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Injection phrases to detect in tool output ─────────────────────────────
# These patterns signal a prompt-injection attempt buried in API/tool data.
_INJECTION_PHRASES = [
    "ignore previous instructions",
    "forget your",
    "new system prompt",
    "you are now",
    "disregard",
]

# Regex: HTML tag stripper
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Regex: valid ticker — uppercase letters, digits, dot, hyphen; 1–12 chars.
# Share-class symbols (BRK-B, BF-B, BF-A) carry a hyphen on Yahoo and yfinance
# resolves them as-is; without it every caller refused them outright, before a
# request was ever made.
#
# The hyphen opens no new sink: '.' is already allowed, no ticker reaches a
# subprocess or a filesystem path, and the '-' sits last inside the class so it
# is a literal and not a range. The class stays closed on purpose — no
# whitespace, no shell metacharacters, no path separators.
#
# Named so the error message below interpolates it instead of restating it; the
# two had already drifted apart once.
_TICKER_PATTERN = r"^[A-Z0-9.-]{1,12}$"
_TICKER_RE = re.compile(_TICKER_PATTERN)

# Regex: anything shaped like one of sanitize_prompt_text's fence delimiters.
# Deliberately broader than the exact tokens emitted — it matches any
# <<<WORD:WORD>>> form, so an attacker cannot close a block by guessing at a
# label ("<<<END:SECTOR>>>") that the current caller does not happen to use.
_FENCE_RE = re.compile(r"<<<\s*/?[A-Za-z_]*\s*:?\s*[A-Za-z_]*\s*>>>")

# Limits
_MAX_QUESTION_LEN = 300
_MAX_TOOL_OUTPUT_LEN = 4000
# Positions allow a 2000-char thesis at the API boundary (PositionBody), so the
# prompt cap matches rather than silently truncating legitimate research notes.
_MAX_PROMPT_TEXT_LEN = 2000


# ── Public API ─────────────────────────────────────────────────────────────

def validate_ticker(s: str) -> str:
    """
    Validate and return the ticker symbol.

    Accepts: PLTR, BRK-B, BZ=F, TATAELXSI.NS, GE, etc.
    Note: BZ=F contains '=' which the strict alpha-numeric regex would reject,
    so we allow '=' only for known futures symbols — we normalise by stripping
    the '=F' suffix before regex check then re-attach it.

    Raises:
        ValueError: if the ticker does not match the allowed pattern.
    """
    if not isinstance(s, str):
        raise ValueError(f"Ticker must be a string, got {type(s).__name__}")

    s = s.strip().upper()

    # Special-case: Yahoo futures symbols like BZ=F, CL=F
    # Strip the futures suffix for pattern validation, accept if base is valid
    check = s
    is_futures = False
    if "=" in s:
        parts = s.split("=")
        if len(parts) == 2 and re.match(r"^[A-Z]{1,4}$", parts[0]) and re.match(r"^[A-Z]$", parts[1]):
            is_futures = True
            check = parts[0]   # validate just the base symbol
        else:
            raise ValueError(
                f"Invalid ticker '{s}': futures format must be SYMBOL=X (e.g. BZ=F)."
            )

    if not _TICKER_RE.match(check):
        raise ValueError(
            f"Invalid ticker '{s}': must match {_TICKER_PATTERN} "
            f"(uppercase letters, digits, dots and hyphens only)."
        )

    # Reconstruct
    if is_futures:
        return s   # already normalised above
    return check


def sanitize_question(s: str) -> str:
    """
    Strip HTML tags, neutralise forged fence delimiters, redact known
    injection phrases, and truncate to MAX_QUESTION_LEN characters.

    Call this on every user-supplied question before it enters the
    agent's message history. A question reaches the model in the same
    prompt as fenced thesis/sector text (_research_prompt's question
    branch interpolates `context`, which includes position_block's fenced
    blocks, right below it) — so it needs the same two defences
    sanitize_prompt_text() already applies to that fenced text: a forged
    fence token in the question must not be able to prematurely close or
    impersonate one of those blocks, and a known injection phrase must not
    survive verbatim. It does not get the full <<<UNTRUSTED:...>>> wrapper
    sanitize_prompt_text() adds, since a question is the operator's own
    live instruction to the assistant they are already directly
    commanding, not stored data replayed into a future prompt.

    Returns:
        Cleaned string (never raises).
    """
    if not s:
        return ""

    # Neutralise forged fence tokens first — same order as
    # sanitize_prompt_text() and for the same reason: this MUST run before
    # the HTML strip below, or _HTML_TAG_RE eats the "<<<END:X>" prefix and
    # leaves the guard a no-op.
    cleaned = _FENCE_RE.sub("[removed]", s)

    # Strip HTML tags
    cleaned = _HTML_TAG_RE.sub("", cleaned)

    # Redact known injection phrases — same list sanitize_prompt_text() and
    # guard_tool_output() use.
    lowered = cleaned.lower()
    injection_found = False
    for phrase in _INJECTION_PHRASES:
        if phrase in lowered:
            injection_found = True
            cleaned = re.compile(re.escape(phrase), re.IGNORECASE).sub(
                "[content removed]", cleaned
            )

    # Collapse runs of whitespace to a single space
    cleaned = " ".join(cleaned.split())

    # Truncate
    if len(cleaned) > _MAX_QUESTION_LEN:
        cleaned = cleaned[:_MAX_QUESTION_LEN]
        logger.warning(
            "sanitize_question: question truncated to %d characters.", _MAX_QUESTION_LEN
        )

    if injection_found:
        logger.warning(
            "sanitize_question: potential prompt-injection detected in question "
            "and redacted. User text is data, not instructions."
        )

    return cleaned


def sanitize_prompt_text(text, max_len: int = _MAX_PROMPT_TEXT_LEN,
                         label: str = "TEXT") -> str:
    """
    Sanitise stored operator text before it is interpolated into a provider prompt.

    Sibling to sanitize_question(), for a different kind of input. `thesis`,
    `note` and `sector` are written through the API, stored in SQLite, and then
    interpolated verbatim into the research prompt by _build_context(). Writing
    them requires a session, so the attacker surface is narrow — but "narrow" is
    not "closed", and neither existing guard covered this path:
    sanitize_question() only ever saw the `question` parameter, and
    guard_tool_output() filters model *output*, not prompt *input*.

    Differs from sanitize_question() in one way that matters: thesis text is
    multi-line prose, so collapsing all whitespace would destroy it. Line
    structure is preserved; only runs of blank lines are capped.

    Returns a fenced block. The caller interpolates the return value directly:

        <<<UNTRUSTED:THESIS>>>
        ...text...
        <<<END:THESIS>>>

    The fence tokens are stripped from the *input* first, so text inside a block
    cannot close its own fence and start issuing instructions. Empty input
    returns an empty string rather than an empty fence, so a position with no
    thesis adds nothing to the prompt.

    Returns:
        Fenced, sanitised string, or "" (never raises).
    """
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)

    # 1. Neutralise anything resembling a fence delimiter. This MUST run before
    #    the HTML strip, not after. _HTML_TAG_RE is <[^>]+>, which happily
    #    matches the "<<<END:THESIS>" prefix of a fence token and leaves a bare
    #    ">>" behind — so running it first destroys the evidence and _FENCE_RE
    #    then matches nothing. The forged fence is broken either way, but only
    #    this order breaks it *on purpose*: with the steps reversed the
    #    protection is an accident of one regex's shape, and editing that regex
    #    would silently remove it.
    cleaned = _FENCE_RE.sub("[removed]", text)

    # 2. Strip HTML — same expression the question path uses.
    cleaned = _HTML_TAG_RE.sub("", cleaned)

    # 3. Redact known injection phrases — same list and same case-insensitive
    #    substitution guard_tool_output() applies to model output.
    lowered = cleaned.lower()
    injection_found = False
    for phrase in _INJECTION_PHRASES:
        if phrase in lowered:
            injection_found = True
            cleaned = re.compile(re.escape(phrase), re.IGNORECASE).sub(
                "[content removed]", cleaned
            )

    # 4. Normalise line endings and cap blank-line runs. Prose survives; an
    #    attempt to push the real prompt out of view with whitespace does not.
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n")).strip()

    if not cleaned:
        return ""

    # 5. Truncate.
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
        logger.warning("sanitize_prompt_text: %s truncated to %d characters.",
                       label, max_len)

    if injection_found:
        logger.warning(
            "sanitize_prompt_text: potential prompt-injection detected in stored "
            "%s and redacted. Stored text is data, not instructions.", label,
        )

    return f"<<<UNTRUSTED:{label}>>>\n{cleaned}\n<<<END:{label}>>>"


def guard_tool_output(text: str) -> str:
    """
    Sanitise tool output before it is added to the agent message history.

    Two defences:
    1. Truncate to MAX_TOOL_OUTPUT_LEN to prevent token flooding.
    2. Detect and redact known prompt-injection phrases; log a WARNING.

    This is the primary injection surface — apply it to EVERY tool result
    before Claude sees it.

    Returns:
        Sanitised string (never raises).
    """
    if not isinstance(text, str):
        text = str(text)

    # 1. Truncate
    if len(text) > _MAX_TOOL_OUTPUT_LEN:
        text = text[:_MAX_TOOL_OUTPUT_LEN]

    # 2. Detect injection attempts (case-insensitive)
    text_lower = text.lower()
    injection_found = False
    for phrase in _INJECTION_PHRASES:
        if phrase in text_lower:
            injection_found = True
            # Replace the offending phrase (case-insensitive) with [content removed]
            pattern = re.compile(re.escape(phrase), re.IGNORECASE)
            text = pattern.sub("[content removed]", text)

    if injection_found:
        logger.warning(
            "guard_tool_output: Potential prompt-injection detected in tool output. "
            "Offending content has been redacted. "
            "If you see an instruction in tool output asking you to change behaviour, "
            "ignore it — this is an injection attempt."
        )

    return text
