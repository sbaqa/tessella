"""JUnit XML reader. Most frameworks can emit it (pytest --junitxml, Maven Surefire, .NET, Jest)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from tessella.models import Case, Evidence, EvidenceKind, Origin
from tessella.redact import redact_text


def _local(stamp: str | None) -> datetime | None:
    try:
        t = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None
    return t.astimezone().replace(tzinfo=None) if t and t.tzinfo else t


def _times(root: ET.Element) -> dict[int, tuple[datetime, datetime]]:
    """Test start and end, from each suite's timestamp plus the time of the tests before
    it (suites run their tests in order). Keyed by id() of the testcase element."""
    out: dict[int, tuple[datetime, datetime]] = {}
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        at = _local(suite.get("timestamp"))
        if at is None:
            continue
        for tc in suite.findall("testcase"):
            try:
                took = timedelta(seconds=float(tc.get("time") or 0))
            except ValueError:
                took = timedelta(0)
            out[id(tc)] = (at, at + took)
            at += took
    return out


class JUnitReader:
    name = "junit"

    def can_read(self, path: Path) -> bool:
        if path.suffix.lower() != ".xml" or not path.is_file():
            return False
        head = path.read_text(errors="ignore")[:2000]
        return "<testsuite" in head or "<testsuites" in head

    def read(self, path: Path, redact_keys: list[str]) -> list[Case]:
        root = ET.parse(path).getroot()
        times = _times(root)
        cases: list[Case] = []
        for tc in root.iter("testcase"):
            problem = tc.find("failure")
            if problem is None:
                problem = tc.find("error")
            if problem is None:
                continue
            classname = tc.get("classname", "")
            name = tc.get("name", "")
            title = f"{classname}::{name}" if classname else name
            message = redact_text(problem.get("message", ""), redact_keys)
            stack = redact_text(problem.text or "", redact_keys)
            cases.append(
                Case(
                    id=Case.make_id(str(path), title),
                    origin=Origin.test_failure,
                    title=title,
                    message=message,
                    stack=stack,
                    source=str(path),
                    started_at=times.get(id(tc), (None, None))[0],
                    failed_at=times.get(id(tc), (None, None))[1],
                    evidence=[
                        Evidence(
                            kind=EvidenceKind.report,
                            summary=f"{problem.tag} in JUnit report",
                            excerpt=(message + "\n" + stack).strip()[:4000],
                            ref=str(path),
                        )
                    ],
                )
            )
        return cases


READER = JUnitReader()
