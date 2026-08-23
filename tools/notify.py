"""Telegram push notifications — optional, best-effort, stdlib only.

Deliberately no new dependency: the Telegram Bot API is one JSON POST, and
this project ships no HTTP client beyond what `openai` and `yfinance` already
pull in. `urllib.request` is enough.

Two directions, both optional and both gated on the same two env vars:
  * notify_telegram()            — outbound push (api.py calls this).
  * start_telegram_reply_listener() — inbound: long-polls Telegram for
    replies from the owner, so an access request can be approved by
    replying with the email instead of opening the admin view.

The inbound side deliberately uses long-polling (getUpdates), not a webhook.
A webhook needs a public HTTPS URL Telegram can call back into; polling only
needs this process to make outbound requests, which works identically on
localhost and behind a real deployment, and needs no new inbound port or
infrastructure.
"""

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT_S = 5
_POLL_TIMEOUT_S = 25   # Telegram-side long-poll wait, per getUpdates call
_POLL_HTTP_TIMEOUT_S = 35
_RETRY_DELAY_S = 5


def notify_telegram(text: str) -> None:
    """Best-effort push to the owner's Telegram.

    No-ops silently if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset — the
    feature is optional per .env.example. Any failure (network, bad token,
    Telegram outage) is swallowed: this fires from inside a public,
    unauthenticated request handler, and a notification side-channel must never
    affect the response the caller gets. No parse_mode is set, so the request's
    email/note reach Telegram as literal text — there is no Markdown/HTML
    injection surface to worry about.

    Swallowed, but logged at WARNING rather than debug: this project sets no
    logging config, so debug output is discarded and a permanently broken token
    would be invisible forever.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        _API_BASE.format(token=token, method="sendMessage"),
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


def _get_updates(token: str, offset: int | None, timeout_s: int) -> list[dict]:
    """One raw getUpdates call. Raises on any transport failure — the poller
    loop is what decides how to react, not this function."""
    params = {"timeout": timeout_s}
    if offset is not None:
        params["offset"] = offset
    url = _API_BASE.format(token=token, method="getUpdates") + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_POLL_HTTP_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("result", []) if data.get("ok") else []


def _process_update(update: dict, want_chat_id: int, handler) -> None:
    """Chat-id filter plus text extraction for one update, factored out of
    the polling loop so it is testable without a real thread or network call.

    The chat-id check is the ENTIRE authorization boundary for the inbound
    side: the bot is not a public interface, and a message from any chat
    other than the configured TELEGRAM_CHAT_ID is silently dropped rather
    than handed to `handler`.
    """
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    if msg.get("chat", {}).get("id") != want_chat_id:
        return
    text = msg.get("text", "")
    if not text:
        return
    try:
        handler(text)
    except Exception:
        logger.exception("telegram reply listener: handler raised on message")


def start_telegram_reply_listener(handler) -> threading.Event | None:
    """Long-poll Telegram for replies from the owner and hand each message's
    text to `handler(text)`. Returns None (does nothing) if TELEGRAM_BOT_TOKEN
    / TELEGRAM_CHAT_ID are unset — same optionality as notify_telegram().
    Otherwise starts a daemon thread and returns a threading.Event that stops
    it when set.

    The first getUpdates call uses timeout_s=0 and its result is discarded
    after computing the next offset — a restart must not replay a reply from
    days ago and re-trigger `handler` on something stale. Every call after
    that long-polls for _POLL_TIMEOUT_S seconds, so a reply is picked up
    within roughly that long, not on a fixed short-interval timer hammering
    Telegram's API.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return None

    try:
        want_chat_id = int(chat_id)
    except ValueError:
        logger.warning("start_telegram_reply_listener: TELEGRAM_CHAT_ID %r is not "
                       "numeric — refusing to start (would filter on nothing)", chat_id)
        return None

    stop = threading.Event()

    def _run():
        offset = None
        try:
            backlog = _get_updates(token, offset=None, timeout_s=0)
            if backlog:
                offset = backlog[-1]["update_id"] + 1
        except Exception as exc:
            logger.warning("telegram reply listener: initial getUpdates failed "
                           "(type=%s): %s", type(exc).__name__, exc)

        while not stop.is_set():
            try:
                updates = _get_updates(token, offset=offset, timeout_s=_POLL_TIMEOUT_S)
            except Exception as exc:
                logger.warning("telegram reply listener: getUpdates failed "
                               "(type=%s): %s", type(exc).__name__, exc)
                stop.wait(_RETRY_DELAY_S)
                continue

            for upd in updates:
                offset = upd["update_id"] + 1
                _process_update(upd, want_chat_id, handler)

    threading.Thread(target=_run, name="telegram-reply-listener", daemon=True).start()
    return stop
