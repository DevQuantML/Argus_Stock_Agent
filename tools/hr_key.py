"""
hr_key.py — derives the HR/hiring-manager preview key from AGENT_SECRET.

Stdlib only, no store import: both api.py (to validate a redemption attempt)
and main.py (the `hrkey` CLI command, so the operator can read the key off
without starting the server) need this with no import-order dependency on
either.

The key is deliberately NOT stored anywhere — SQLite already holds only a
SHA-256 of every session/guest credential, and this goes one step further:
there is nothing to look up at all. Anyone who can read AGENT_SECRET can
re-derive the identical hr_ string at any time, and rotating AGENT_SECRET
changes the derived value on the next call with zero migration — the
"revoke everything and start over" lever this feature needed, for free.

HMAC, not a bare hash: hashing AGENT_SECRET directly (or concatenating it
into a string first) is length-extension-adjacent and mixes the "prove you
hold the secret" step with the domain-separation step. HMAC keeps those
separate the standard way. The domain-separation label is fixed so that (a)
a future second derived-key kind does not collide with this one, and (b) the
value is stable across releases as long as AGENT_SECRET does not change.
"""

import base64
import hashlib
import hmac

_HR_KEY_LABEL = b"argus-hr-access-v1"
_HR_KEY_PREFIX = "hr_"


def derive_hr_key(secret: str) -> str:
    """Deterministically derive the HR preview key from AGENT_SECRET.

    Returns "" for an empty secret — mirrors _agent_key_ok's "no secret
    configured, nothing can be valid" fail-closed rule, and gives callers an
    unambiguous falsy value to gate on instead of a derived string built from
    nothing.
    """
    if not secret:
        return ""
    digest = hmac.new(secret.encode("utf-8"), _HR_KEY_LABEL, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return _HR_KEY_PREFIX + token[:32]


def hr_key_ok(candidate: str, secret: str) -> bool:
    """Constant-time check that `candidate` is the current derived HR key.

    hmac.compare_digest, not `==` — the same timing-leak reasoning
    _agent_key_ok documents applies here: a short-circuiting string compare
    would leak the matching prefix length to an attacker probing the endpoint.
    """
    if not candidate or not secret:
        return False
    expected = derive_hr_key(secret)
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
