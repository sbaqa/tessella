"""HAR reader: a recorded browser or proxy session becomes a Flow."""

from __future__ import annotations

import json
from pathlib import Path

from tessella.models import Flow, Step
from tessella.redact import redact_headers, redact_text

_SKIP_TYPES = ("image/", "font/", "text/css", "javascript")


def _body(text: str | None, redact_keys: list[str] | None = None):
    if not text:
        return None
    text = redact_text(text, redact_keys or [])  # passwords in a login body, tokens in replies
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def entries_to_steps(entries: list[dict], redact_keys: list[str]) -> list[Step]:
    steps: list[Step] = []
    for entry in entries:
        req, res = entry.get("request", {}), entry.get("response", {})
        mime = (res.get("content") or {}).get("mimeType", "")
        if any(t in mime for t in _SKIP_TYPES):
            continue
        headers = {h["name"]: h["value"] for h in req.get("headers", [])}
        steps.append(
            Step(
                index=len(steps),
                method=req.get("method", "GET"),
                url=req.get("url", ""),
                headers=redact_headers(headers, redact_keys),
                body=_body((req.get("postData") or {}).get("text"), redact_keys),
                status=res.get("status"),
                response=_body((res.get("content") or {}).get("text"), redact_keys),
            )
        )
    return steps


class HarReader:
    name = "har"

    def can_read(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() == ".har"

    def read(self, path: Path, redact_keys: list[str]) -> list[Flow]:
        data = json.loads(path.read_text())
        entries = (data.get("log") or {}).get("entries", [])
        return [Flow(name=path.stem, steps=entries_to_steps(entries, redact_keys))]


READER = HarReader()
