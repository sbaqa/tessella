"""pytest-html report reader.

pytest-html 4.x stores the results as JSON in the `data-jsonblob` attribute of
the `#data-container` element. We read that JSON, not the rendered table.

TODO(v1): add fixtures from pytest-html 3.x (no JSON blob) and decide whether to
support it via selectolax scraping as a last resort.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from tessella.collect import attachments
from tessella.models import Case, Evidence, EvidenceKind, Origin
from tessella.redact import redact_text


def _screenshots(run: dict, report: Path) -> list[str]:
    """pytest-html extras hold an image as a path (relative to the report) or as base64."""
    paths: list[Path] = []
    for extra in run.get("extras") or []:
        if str(extra.get("format_type", "")).lower() != "image":
            continue
        content = str(extra.get("content", ""))
        candidate = report.parent / content
        if len(content) < 1024 and candidate.is_file():
            paths.append(candidate)
        else:
            written = attachments.write_embedded(content, "." + extra.get("extension", "png"))
            if written:
                paths.append(written)
    return attachments.existing(paths)

_BLOB = re.compile(r'data-jsonblob="([^"]+)"')
_TAGS = re.compile(r"<[^>]+>")


class PytestHtmlReader:
    name = "pytest_html"

    def can_read(self, path: Path) -> bool:
        return path.is_file() and path.suffix == ".html" and "data-jsonblob" in path.read_text(
            errors="ignore"
        )

    def read(self, path: Path, redact_keys: list[str]) -> list[Case]:
        m = _BLOB.search(path.read_text(errors="ignore"))
        if not m:
            return []
        blob = json.loads(html.unescape(m.group(1)))
        cases: list[Case] = []
        for test_id, runs in (blob.get("tests") or {}).items():
            for run in runs:
                if str(run.get("result", "")).lower() not in ("failed", "error"):
                    continue
                log = _TAGS.sub("", html.unescape(run.get("log", "")))
                log = redact_text(log, redact_keys)
                first = next((ln for ln in log.splitlines() if ln.startswith("E ")), "")
                cases.append(
                    Case(
                        id=Case.make_id(str(path), test_id),
                        origin=Origin.test_failure,
                        title=test_id,
                        test_id=test_id,  # pytest-html keys results by node id
                        message=first.removeprefix("E ").strip(),
                        stack=log,
                        source=str(path),
                        screenshots=_screenshots(run, path),
                        evidence=[
                            Evidence(
                                kind=EvidenceKind.report,
                                summary=f"pytest-html result: {run.get('result')}",
                                excerpt=log[:4000],
                                ref=str(path),
                            )
                        ],
                    )
                )
        return cases


READER = PytestHtmlReader()
