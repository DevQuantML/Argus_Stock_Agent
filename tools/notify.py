"""Telegram push notifications — optional, best-effort, stdlib only.

Deliberately no new dependency: the Telegram Bot API is one JSON POST, and
this project ships no HTTP client beyond what `openai` and `yfinance` already
pull in. `urllib.request` is enough.
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_S = 5


def notify_telegram(text: str) -> None:
    """Best-effort push to the owner's Telegram.

    No-ops silently if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset — the
    feature is optional per .env.example. Any failure (network, bad token,
    Telegram outage) is swallowed and logged at debug level: this fires from
    inside a public, unauthenticated request handler, and a notification
    side-channel must never affect the response the caller gets. No
    parse_mode is set, so the request's email/note reach Telegram as literal
    text — there is no Markdown/HTML injection surface to worry about.

    Failures are logged at WARNING rather than debug: this project sets no
    logging config, so debug output is discarded and a permanently broken token
    would be invisible forever.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        _SEND_URL.format(token=token),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=_TIMEOUT_S)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # WARNING, not debug. Nothing in this project calls logging.basicConfig,
        # so the root logger sits at WARNING under uvicorn and every debug line
        # is discarded before it is formatted — which would make a wrong token
        # or a revoked bot indistinguishable from "nobody has requested access
        # yet". The operator would simply never be pinged again and never learn
        # why. Access requests are capped at 5/hour/IP, so this cannot be noisy.
        logger.warning("notify_telegram: push failed (type=%s): %s",
                       type(exc).__name__, exc)
