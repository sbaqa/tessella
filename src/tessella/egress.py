"""Offline mode: proof, not promise, that Tessella talks to nothing outside this machine.

`net.client` already refuses hosts outside `network.allow`, but that only covers code that
uses it. Offline mode watches the interpreter itself: a Python audit hook sees every socket
connection and every DNS lookup made by any code in the process, Tessella's or a
library's, and refuses anything that is not loopback (or a host you allowed). Each refused
attempt is recorded, so a CI job can fail on the first one.

Turn it on with `tessella --offline ...` or TESSELLA_OFFLINE=1. Child processes (a test
re-run, git) are separate programs the hook cannot see; `scripts/zero-egress.sh` runs the
whole triage inside a network namespace with no route out to cover them too.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import threading

from tessella.net import LOOPBACK, EgressBlocked

ENV = "TESSELLA_OFFLINE"

_state = {"active": False, "installed": False, "allow": frozenset()}
_lock = threading.Lock()
attempts: list[str] = []  # every refused destination, in order


def _loopback(host: str) -> bool:
    if host.lower() in LOOPBACK or host == "":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _allowed(host: str, port: int | None = None) -> bool:
    allow = _state["allow"]
    return _loopback(host) or host in allow or (port is not None and f"{host}:{port}" in allow)


def _refuse(target: str) -> None:
    with _lock:
        attempts.append(target)
    raise EgressBlocked(f"offline mode: connection to {target} refused")


def _hook(event: str, args: tuple) -> None:
    if not _state["active"]:
        return
    if event == "socket.connect":
        sock, address = args[0], args[1]
        if getattr(sock, "family", None) == socket.AF_UNIX or not isinstance(address, tuple):
            return
        host, port = str(address[0]), address[1] if len(address) > 1 else None
        if not _allowed(host, port):
            _refuse(f"{host}:{port}")
    elif event == "socket.getaddrinfo":
        host = args[0]
        if isinstance(host, bytes):
            host = host.decode(errors="replace")
        if host is not None and not _allowed(str(host), args[1] if len(args) > 1 else None):
            _refuse(str(host))
    elif event in ("socket.sendto", "socket.sendmsg") and len(args) > 1:
        address = args[1]
        if isinstance(address, tuple) and not _allowed(str(address[0]), address[1]):
            _refuse(f"{address[0]}:{address[1]}")


def _resolve(allow: list[str]) -> frozenset[str]:
    """The allowed names plus the addresses they resolve to, looked up now (before the hook
    is active), so a connection to the server's IP is recognised as the allowed host."""
    out = set(allow)
    for entry in allow:
        host = entry.rsplit(":", 1)[0] if entry.count(":") == 1 else entry
        out.add(host)
        try:
            out.update(str(info[4][0]) for info in socket.getaddrinfo(host, None))
        except OSError:
            pass
    return frozenset(out)


def enable(allow: list[str] | None = None) -> None:
    """Refuse every non-loopback connection from now on. `allow` may name extra hosts
    inside your network (`network.offline_allow`, e.g. a team model server); loopback is
    always allowed."""
    _state["active"] = False  # resolve the allowed names without tripping the hook
    resolved = _resolve(allow or [])
    with _lock:
        _state["allow"] = resolved
        _state["active"] = True
        if not _state["installed"]:
            sys.addaudithook(_hook)  # cannot be removed; `disable` makes it a no-op
            _state["installed"] = True
    os.environ[ENV] = "1"  # a `tessella` started by this one (a re-run) is offline too


def disable() -> None:
    _state["active"] = False
    os.environ.pop(ENV, None)


def active() -> bool:
    return _state["active"]


def requested() -> bool:
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes", "on")
