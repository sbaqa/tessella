"""Identities: log in each configured test user and keep fresh tokens for replays."""

from __future__ import annotations

from dataclasses import dataclass, field

from tessella.config import Probe


@dataclass
class Session:
    name: str
    headers: dict[str, str] = field(default_factory=dict)


def login_all(settings: Probe, allow: list[str]) -> dict[str, Session]:
    """Return a Session per identity, plus 'anonymous' with no credentials.

    TODO(v2): POST to each identity's login_url through net.client, read the token
    from the response (configurable JSONPath), refresh on 401 during replay.
    """
    sessions = {"anonymous": Session("anonymous")}
    for name in settings.identities:
        sessions[name] = Session(name)
    return sessions
