"""Playwright trace.zip reader: the network log of a test becomes a Flow.

This is what lets probe work without anyone recording a HAR by hand:
the team's own test suite already records every request it makes.

Request and response bodies are not inline in the network log: each names a file under
resources/ (`_file` in current Playwright, `_sha1` in older versions), read from the zip.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from tessella.collect.har import entries_to_steps
from tessella.models import Flow


def _resource(z: zipfile.ZipFile, holder: dict | None) -> None:
    """Inline the body a HAR-like entry keeps in the zip's resources/ folder."""
    if not holder or holder.get("text"):
        return
    name = holder.get("_file") or (f"resources/{holder['_sha1']}" if holder.get("_sha1") else None)
    if not name:
        return
    try:
        holder["text"] = z.read(name).decode(errors="ignore")
    except KeyError:
        pass


class TraceReader:
    name = "playwright_trace"

    def can_read(self, path: Path) -> bool:
        return path.is_file() and path.name.endswith("trace.zip")

    def read(self, path: Path, redact_keys: list[str]) -> list[Flow]:
        entries: list[dict] = []
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.endswith(".network"):
                    continue
                for line in z.read(name).decode(errors="ignore").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "resource-snapshot" and "snapshot" in event:
                        entry = event["snapshot"]  # HAR-like
                        _resource(z, (entry.get("request") or {}).get("postData"))
                        _resource(z, (entry.get("response") or {}).get("content"))
                        entries.append(entry)
        return [Flow(name=path.parent.name, steps=entries_to_steps(entries, redact_keys))]


READER = TraceReader()
