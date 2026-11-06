"""Allure report reader.

A generated Allure report folder keeps one JSON per test under data/test-cases/.
Raw allure-results (*-result.json) are read the same way.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tessella.collect import attachments
from tessella.models import Case, Evidence, EvidenceKind, Origin
from tessella.redact import redact_text

_FAILED = {"failed", "broken"}


def _screenshots(data: dict, report_root: Path, result_file: Path) -> list[str]:
    """Generated reports keep files in data/attachments/; raw results keep them beside the JSON."""
    folders = [report_root / "data" / "attachments", result_file.parent]
    paths: list[Path] = []
    for att in attachments.walk(data):
        source = att.get("source")
        if source and attachments.is_image(att.get("type"), source):
            paths.extend(folder / source for folder in folders)
    return attachments.existing(paths)


def _pytest_node_id(data: dict) -> str:
    """allure-pytest results carry the module, class and test name as labels; together they
    are the node id `pytest` re-runs. Other frameworks: empty (rerun falls back)."""
    labels = {lb.get("name"): lb.get("value") for lb in data.get("labels") or []}
    module, name = labels.get("package"), data.get("name")
    if labels.get("framework") != "pytest" or not module or not name:
        return ""
    klass = [labels["testClass"]] if labels.get("testClass") else []
    return "::".join([module.replace(".", "/") + ".py", *klass, name])


def _times(data: dict) -> tuple[datetime | None, datetime | None]:
    """Start and stop as this machine's local time: generated reports keep them under
    `time`, raw results at the top level, both in epoch milliseconds."""
    t = data.get("time") or data

    def at(ms):
        return datetime.fromtimestamp(ms / 1000) if isinstance(ms, (int, float)) and ms > 0 else None
    return at(t.get("start")), at(t.get("stop"))


class AllureReader:
    name = "allure"

    def _files(self, path: Path) -> list[Path]:
        if not path.is_dir():
            return []
        generated = path / "data" / "test-cases"
        if generated.is_dir():
            return sorted(generated.glob("*.json"))
        return sorted(path.glob("*-result.json"))

    def can_read(self, path: Path) -> bool:
        return bool(self._files(path))

    def read(self, path: Path, redact_keys: list[str]) -> list[Case]:
        cases: list[Case] = []
        for f in self._files(path):
            data = json.loads(f.read_text())
            if data.get("status") not in _FAILED:
                continue
            title = data.get("fullName") or data.get("name", f.stem)
            # Raw allure-results nest these under statusDetails; generated reports
            # (data/test-cases/*.json) hoist them to top-level statusMessage/statusTrace.
            details = data.get("statusDetails") or {}
            raw_message = details.get("message") or data.get("statusMessage", "")
            raw_trace = details.get("trace") or data.get("statusTrace", "")
            message = redact_text(raw_message, redact_keys)
            trace = redact_text(raw_trace, redact_keys)
            started, stopped = _times(data)
            cases.append(
                Case(
                    id=Case.make_id(str(path), title),
                    origin=Origin.test_failure,
                    title=title,
                    test_id=_pytest_node_id(data),
                    message=message.splitlines()[0] if message else "",
                    stack=trace or message,
                    source=str(f),
                    started_at=started,
                    failed_at=stopped,
                    screenshots=_screenshots(data, path, f),
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.report,
                            summary=f"Allure status: {data.get('status')}",
                            excerpt=(message + "\n" + trace).strip()[:4000],
                            ref=str(f),
                        )
                    ],
                )
            )
        return cases


READER = AllureReader()
