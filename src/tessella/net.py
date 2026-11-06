"""The only HTTP client in Tessella. Enforces the network allowlist (deal-maker #1).

Every module that talks to the network (Jira, model server, logs, probe target)
must get its client from here. Any host not on the allowlist is blocked and logged.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("tessella.net")


class EgressBlocked(RuntimeError):
    pass


def _host_key(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f"{host}:{parts.port}" if parts.port else host
    return host, port


# One machine, three spellings: an allowlist entry for any of them admits the others,
# so `localhost:11435` also covers a model server configured as `http://127.0.0.1:11435`.
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _spellings(host: str) -> tuple[str, ...]:
    return LOOPBACK if host.lower() in LOOPBACK else (host,)


def is_allowed(url: str, allow: list[str]) -> bool:
    host, host_port = _host_key(url)
    port = host_port[len(host):]  # ":11435" or ""
    for name in _spellings(host):
        bracketed = f"[{name}]{port}" if ":" in name else None  # an IPv6 entry, e.g. [::1]:11435
        if name in allow or f"{name}{port}" in allow or bracketed in allow:
            return True
    return False


class GuardedTransport(httpx.HTTPTransport):
    def __init__(self, allow: list[str], **kw):
        super().__init__(**kw)
        self.allow = allow

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not is_allowed(str(request.url), self.allow):
            log.warning("blocked outbound call to %s", request.url.host)
            raise EgressBlocked(f"{request.url.host} is not in network.allow")
        return super().handle_request(request)


def client(allow: list[str], **kw) -> httpx.Client:
    """Return an httpx client that refuses hosts outside the allowlist."""
    return httpx.Client(transport=GuardedTransport(allow), **kw)


def egress_report(allow: list[str], probes: list[str] | None = None) -> list[tuple[str, bool]]:
    """Try well-known external hosts through the guard; every one must be blocked.

    Used by `tessella doctor --egress`. Returns (host, blocked) pairs.
    """
    probes = probes or [
        "https://api.openai.com",
        "https://api.anthropic.com",
        "https://example.com",
        "https://pypi.org",
    ]
    out = []
    with client(allow, timeout=3) as c:
        for url in probes:
            try:
                c.get(url)
                out.append((url, False))
            except EgressBlocked:
                out.append((url, True))
            except httpx.HTTPError:
                out.append((url, not is_allowed(url, allow)))
    return out
