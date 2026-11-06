"""Playwright HTML report reader.

The Playwright HTML report embeds its results as a base64-encoded zip inside
index.html. We read that embedded data, never the page layout.

TODO(v1): verify the embedding marker against the Playwright versions you support
and add fixtures from real reports (1.4x and 1.5x) to tests/fixtures.
"""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from pathlib import Path

from tessella.collect import attachments
from tessella.models import Case, Evidence, EvidenceKind, Origin
from tessella.redact import redact_text

_B64 = re.compile(r"playwrightReportBase64\s*=\s*['\"]data:application/zip;base64,([A-Za-z0-9+/=]+)")


def _location(test: dict) -> str:
    """`file:line` from the report, which `npx playwright test file:line` runs on its own."""
    loc = test.get("location") or {}
    return f"{loc['file']}:{loc['line']}" if loc.get("file") and loc.get("line") else ""


class PlaywrightHtmlReader:
    name = "playwright_html"

    def _index(self, path: Path) -> Path | None:
        index = path / "index.html" if path.is_dir() else path
        return index if index.is_file() and index.suffix == ".html" else None

    def can_read(self, path: Path) -> bool:
        index = self._index(path)
        return bool(index and "playwrightReportBase64" in index.read_text(errors="ignore"))

    def read(self, path: Path, redact_keys: list[str]) -> list[Case]:
        index = self._index(path)
        assert index is not None
        m = _B64.search(index.read_text(errors="ignore"))
        if not m:
            return []
        archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(m.group(1))))
        cases: list[Case] = []
        for name in archive.namelist():
            if not name.endswith(".json") or name == "report.json":
                continue
            data = json.loads(archive.read(name))
            for test in data.get("tests", []):
                cases.extend(self._failed(test, index, redact_keys))
        return cases

    def _failed(self, test: dict, index: Path, redact_keys: list[str]) -> list[Case]:
        if test.get("outcome") not in ("unexpected", "flaky"):
            return []
        title = " > ".join(test.get("path", []) + [test.get("title", "")]).strip(" >")
        errors: list[str] = []
        shots: list[Path] = []
        for result in test.get("results", []):
            for err in result.get("errors", []):
                errors.append(err if isinstance(err, str) else json.dumps(err))
            # Attachment paths are relative to the report folder, e.g. data/<sha1>.png
            for att in result.get("attachments", []):
                if att.get("path") and attachments.is_image(att.get("contentType"), att["path"]):
                    shots.append(index.parent / att["path"])
        text = redact_text("\n".join(errors), redact_keys)
        first = text.splitlines()[0] if text else ""
        return [
            Case(
                id=Case.make_id(str(index), title),
                origin=Origin.test_failure,
                title=title,
                test_id=_location(test),
                message=first,
                stack=text,
                source=str(index),
                screenshots=attachments.existing(shots),
                evidence=[
                    Evidence(
                        kind=EvidenceKind.report,
                        summary=f"Playwright outcome: {test.get('outcome')}",
                        excerpt=text[:4000],
                        ref=str(index),
                    )
                ],
            )
        ]


READER = PlaywrightHtmlReader()
