"""The failing test's own source, around the line it failed on.

Local models may see everything, and the test code is often what separates a test bug
(a renamed locator, a too-tight timeout) from a product bug. No model needed: the file comes
from the case's test id or title, the line from the stack (or a Playwright `file:line` id).
"""

from __future__ import annotations

import re
from pathlib import Path

from tessella.models import Case, Evidence, EvidenceKind
from tessella.redact import redact_text

_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
_JS_FRAME = re.compile(r"([\w./\\-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py)):(\d+)")
_DOTTED = re.compile(r"^[\w.]+$")


def _test_file(case: Case, repo: Path) -> Path | None:
    candidates = []
    head = (case.test_id or case.title).split("::")[0].split(" > ")[0].strip()
    candidates.append(re.sub(r":\d+$", "", head))  # Playwright file:line
    if _DOTTED.match(head):
        candidates.append(head.replace(".", "/") + ".py")  # JUnit classname
    root = repo.resolve()
    for c in candidates:
        path = (repo / c).resolve()
        if path.is_file() and path.is_relative_to(root):  # never read outside the repo
            return path
    return None


def _failing_line(case: Case, path: Path) -> int | None:
    name = path.name
    py = [int(line) for f, line in _PY_FRAME.findall(case.stack) if Path(f).name == name]
    if py:
        return py[-1]  # innermost frame in the test file
    js = [int(line) for f, line in _JS_FRAME.findall(case.stack) if Path(f).name == name]
    if js:
        return js[0]
    m = re.search(r":(\d+)$", case.test_id)
    if m:
        return int(m.group(1))
    test_name = re.sub(r"\[.*\]$", "", (case.test_id or case.title).split("::")[-1])
    lines = path.read_text(errors="ignore").splitlines()
    for i, text in enumerate(lines, 1):
        if re.search(rf"\bdef {re.escape(test_name)}\b", text):
            return i
    return None


def test_source_evidence(case: Case, repo: str | Path = ".", context: int = 20,
                         redact_keys: list[str] | None = None) -> Evidence | None:
    repo = Path(repo)
    path = _test_file(case, repo)
    if path is None:
        return None
    line = _failing_line(case, path)
    if line is None:
        return None
    return _excerpt(path, repo, line, context, redact_keys,
                    "the line the test failed on")


def around(case: Case, repo: str | Path, line: int, context: int = 30,
           redact_keys: list[str] | None = None) -> Evidence | None:
    """Any part of the failing test's file, for the investigator's test_source tool."""
    repo = Path(repo)
    path = _test_file(case, repo)
    if path is None:
        return None
    return _excerpt(path, repo, line, context, redact_keys, "as the investigator asked")


def _excerpt(path: Path, repo: Path, line: int, context: int, redact_keys: list[str] | None,
             why: str) -> Evidence | None:
    lines = path.read_text(errors="ignore").splitlines()
    if not 1 <= line <= len(lines):
        return None
    lo, hi = max(1, line - context), min(len(lines), line + context)
    width = len(str(hi))
    excerpt = "\n".join(
        f"{'>' if n == line else ' '} {n:>{width}} | {lines[n - 1]}" for n in range(lo, hi + 1))
    rel = path.relative_to(repo.resolve())
    return Evidence(kind=EvidenceKind.test_source,
                    summary=f"Test source around {rel}:{line}, {why}",
                    excerpt=redact_text(excerpt, redact_keys or []), ref=f"{rel}:{line}")
