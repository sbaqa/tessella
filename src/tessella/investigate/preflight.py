"""Environment pre-flight: one 'environment down' Case instead of 200 drafts."""

from __future__ import annotations

import socket
from urllib.parse import urlsplit

import httpx

from tessella import net
from tessella.models import Evidence, EvidenceKind


def check(urls: list[str], allow: list[str]) -> list[Evidence]:
    problems: list[Evidence] = []
    with net.client(allow, timeout=5) as c:
        for url in urls:
            host = urlsplit(url).hostname or ""
            try:
                socket.gethostbyname(host)
            except OSError:
                problems.append(Evidence(kind=EvidenceKind.preflight,
                                         summary=f"DNS failed for {host}", ref=url))
                continue
            try:
                r = c.get(url)
                if r.status_code >= 500:
                    problems.append(Evidence(kind=EvidenceKind.preflight,
                                             summary=f"{url} returned {r.status_code}", ref=url))
            except (httpx.HTTPError, net.EgressBlocked) as e:
                problems.append(Evidence(kind=EvidenceKind.preflight,
                                         summary=f"{url} unreachable: {e}", ref=url))
    return problems


# TODO(v1): check test credentials (log in each identity) and certificate expiry.
