#!/usr/bin/env python
"""
verify_proxy_trust.py — free-path verification of the X-Forwarded-* trust boundary.

Spends nothing. No network, no API calls, no server — it drives _client_ip(),
_is_https() and the failed-auth backstop directly with crafted headers.

Why this file exists: X-Forwarded-For is *appended* to, never replaced, so the
leftmost entry is written by the client and the rightmost by your own proxies.
Reading from the left — which this code did until the fix — let a caller rotate
that field for a fresh rate-limit bucket per request, so the 20/min cap never
fired and POST /api/session was brute-forceable. The regression is silent: every
route still returns 200 and the limiter still *looks* installed. These
assertions are the only thing that notices.

Run:  python scripts/verify_proxy_trust.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeClient:
    def __init__(self, host): self.host = host


class FakeURL:
    def __init__(self, scheme): self.scheme = scheme


class FakeRequest:
    """Minimal stand-in for starlette's Request — headers, client, url.scheme."""
    def __init__(self, headers=None, peer="203.0.113.9", scheme="http"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        # starlette headers are case-insensitive; emulate .get() on lowercase
        self.headers = _CIDict(self.headers)
        self.client = FakeClient(peer)
        self.url = FakeURL(scheme)


class _CIDict(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


def run():
    import importlib
    import api
    importlib.reload(api)

    # ── hops = 1 (Railway/Fly/Render: one proxy you control) ──────────────
    os.environ["TRUST_PROXY"] = "1"
    importlib.reload(api)

    # The attack: client writes a fake IP; the proxy appends the real one.
    r = FakeRequest({"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})
    check("spoofed prefix ignored, proxy-written entry used",
          api._client_ip(r), "198.51.100.7")

    # Rotating the spoofed prefix must NOT change the bucket — this is the
    # whole vulnerability: a changing bucket means the limiter never fires.
    a = api._client_ip(FakeRequest({"X-Forwarded-For": "9.9.9.9, 198.51.100.7"}))
    b = api._client_ip(FakeRequest({"X-Forwarded-For": "8.8.8.8, 198.51.100.7"}))
    check("rotating the spoofed prefix yields a STABLE bucket", a == b, True)

    # A single entry at hops=1 is the NORMAL case, not an attack: the client
    # sent no X-Forwarded-For and the one trusted proxy appended what it saw.
    # That entry is proxy-written, so it is the correct bucket.
    check("single entry at hops=1 is the proxy-written value",
          api._client_ip(FakeRequest({"X-Forwarded-For": "1.2.3.4"}, peer="203.0.113.9")),
          "1.2.3.4")

    check("no header falls back to socket peer",
          api._client_ip(FakeRequest({}, peer="203.0.113.9")), "203.0.113.9")

    # Proto: a client-supplied "https" must not flip the cookie Secure flag.
    check("spoofed proto prefix cannot force https",
          api._is_https(FakeRequest({"X-Forwarded-Proto": "https, http"})), False)
    check("proxy-written https is honoured",
          api._is_https(FakeRequest({"X-Forwarded-Proto": "http, https"})), True)

    # ── hops = 0 (default, no proxy) ──────────────────────────────────────
    os.environ["TRUST_PROXY"] = "0"
    importlib.reload(api)
    check("hops=0 ignores X-Forwarded-For entirely",
          api._client_ip(FakeRequest({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, peer="203.0.113.9")),
          "203.0.113.9")
    check("hops=0 ignores X-Forwarded-Proto entirely",
          api._is_https(FakeRequest({"X-Forwarded-Proto": "https"}, scheme="http")), False)

    # ── unset (must behave as 0) ──────────────────────────────────────────
    os.environ.pop("TRUST_PROXY", None)
    importlib.reload(api)
    check("unset TRUST_PROXY ignores the header",
          api._client_ip(FakeRequest({"X-Forwarded-For": "1.2.3.4"}, peer="203.0.113.9")),
          "203.0.113.9")

    # ── garbage value must fail closed, not crash ─────────────────────────
    os.environ["TRUST_PROXY"] = "yes"
    importlib.reload(api)
    check("non-numeric TRUST_PROXY fails closed to socket peer",
          api._client_ip(FakeRequest({"X-Forwarded-For": "1.2.3.4"}, peer="203.0.113.9")),
          "203.0.113.9")

    # ── hops = 2 (two proxies) ────────────────────────────────────────────
    os.environ["TRUST_PROXY"] = "2"
    importlib.reload(api)
    check("hops=2 indexes two back from the right",
          api._client_ip(FakeRequest({"X-Forwarded-For": "1.2.3.4, 198.51.100.7, 10.0.0.1"})),
          "198.51.100.7")

    # NOW the guard matters: two hops declared, only one entry present. The
    # request did not traverse the topology the operator described, so reading
    # any position would be guessing at attacker-supplied data.
    check("chain shorter than declared hops falls back to socket peer",
          api._client_ip(FakeRequest({"X-Forwarded-For": "1.2.3.4"}, peer="203.0.113.9")),
          "203.0.113.9")
    check("hops=2 with an empty-ish chain falls back",
          api._client_ip(FakeRequest({"X-Forwarded-For": " , "}, peer="203.0.113.9")),
          "203.0.113.9")

    # ── failed-auth backstop ──────────────────────────────────────────────
    os.environ["TRUST_PROXY"] = "1"
    importlib.reload(api)
    peer = "203.0.113.55"
    check("budget not exhausted at start", api._auth_failures_exceeded(peer), False)
    for _ in range(api._AUTH_FAIL_MAX):
        api._record_auth_failure(peer)
    check("budget exhausted after _AUTH_FAIL_MAX failures",
          api._auth_failures_exceeded(peer), True)
    check("a DIFFERENT peer is unaffected",
          api._auth_failures_exceeded("203.0.113.56"), False)

    # The key property: spoofing X-Forwarded-For does not escape the backstop,
    # because the counter is keyed on the socket peer.
    spoofed = FakeRequest({"X-Forwarded-For": "1.1.1.1, 198.51.100.7"}, peer=peer)
    check("spoofed header cannot escape the failed-auth backstop",
          api._auth_failures_exceeded(api._socket_ip(spoofed)), True)


run()

for n, got, want in PASS:
    print(f"  PASS  {n}")
for n, got, want in FAIL:
    print(f"  FAIL  {n}\n          got={got!r} want={want!r}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
