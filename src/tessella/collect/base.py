"""Reader interface and dispatch. A new framework = one new reader module."""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Protocol

from tessella.models import Case, Flow


class ReportReader(Protocol):
    name: str

    def can_read(self, path: Path) -> bool: ...

    def read(self, path: Path, redact_keys: list[str]) -> list[Case]: ...


class FlowReader(Protocol):
    name: str

    def can_read(self, path: Path) -> bool: ...

    def read(self, path: Path, redact_keys: list[str]) -> list[Flow]: ...


def _report_readers() -> list[ReportReader]:
    from tessella.collect import allure, findings, junit, playwright_html, pytest_html

    # Order matters: the most specific reader that claims a path wins (a Schemathesis
    # report is JUnit XML too).
    return [playwright_html.READER, allure.READER, pytest_html.READER, findings.SCHEMATHESIS,
            findings.ZAP, junit.READER]


def _flow_readers() -> list[FlowReader]:
    from tessella.collect import har, traces

    return [traces.READER, har.READER]


def expand(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pat in patterns:
        matches = glob.glob(pat, recursive=True) or ([pat] if Path(pat).exists() else [])
        paths.extend(Path(m) for m in matches)
    return sorted(set(paths))


def collect_cases(patterns: list[str], redact_keys: list[str]) -> list[Case]:
    cases: list[Case] = []
    for path in expand(patterns):
        for reader in _report_readers():
            if reader.can_read(path):
                cases.extend(reader.read(path, redact_keys))
                break
    return cases


def collect_flows(patterns: list[str], redact_keys: list[str]) -> list[Flow]:
    flows: list[Flow] = []
    for path in expand(patterns):
        for reader in _flow_readers():
            if reader.can_read(path):
                flows.extend(reader.read(path, redact_keys))
                break
    return flows
