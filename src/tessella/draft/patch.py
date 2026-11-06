"""Test patches for test bugs (locator, wait, assertion), written by the `fixer` role and
checked before anyone sees them.

"Local reads, cloud writes": a cloud fixer receives only data classes the policy lets leave,
the redacted summary the local reader produced and the failing test file. A local fixer may
see everything, so it also gets the evidence pack (stack, log lines, test source).

A proposed diff is applied to a temporary copy of the repository, never to your files, and
the failing test is re-run there. The patch is kept only when the test passes on every
re-run; the review queue shows it with that result, and a person applies it.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from tessella.config import Settings
from tessella.models import Case, Draft, DraftKind
from tessella.verdict.prompts import redacted_summary

SYSTEM = """You fix automated tests. You get a summary of a test failure that was already
diagnosed as a test bug, and the test file. Reply with ONLY a unified diff of the test file
that fixes the test. Do not change product code. If you cannot fix it safely, reply NO_FIX."""

_PY_FILE = re.compile(r"^([\w./\\-]+\.(?:py|ts|js|java))(?:::|$)")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Left out of the temporary copy: history, caches, outputs, environments.
SKIP = (".git", ".tessella", "node_modules", "venv", ".venv", "__pycache__", ".pytest_cache",
        "test-results", "playwright-report", "reports", ".mypy_cache", ".ruff_cache", "dist",
        "build")
MAX_COPY_BYTES = 300 * 2**20


def test_file_of(case: Case, repo: str) -> Path | None:
    """Best effort: the file part of a pytest/Playwright test id."""
    head = (case.test_id or case.title).split("::")[0].split(" > ")[0].strip()
    head = re.sub(r":\d+$", "", head)
    if not _PY_FILE.match(head):
        head = head.replace(".", "/") + ".py"  # JUnit classname tests.test_x -> tests/test_x.py
    path = Path(repo) / head
    return path if path.is_file() else None


def propose(case: Case, settings: Settings, approver=None) -> Draft | None:
    from tessella.integrations import llm
    from tessella.integrations.llm import DataClass

    fixer = settings.models.fixer
    if fixer is None:
        return None
    test_file = test_file_of(case, settings.inputs.repo)
    if test_file is None:
        return None
    classes = [DataClass.redacted_summary, DataClass.failing_test_file]
    user = (f"{redacted_summary(case)}\n\nTest file {test_file.name}:\n"
            f"```\n{test_file.read_text()[:12000]}\n```")
    if not fixer.is_cloud:
        from tessella.verdict.pack import budget_chars, evidence_pack

        user = f"{evidence_pack(case, budget_chars(fixer) // 2)}\n\n{user}"
        classes.append(DataClass.evidence_excerpts)
    reply, _ = llm.complete("fixer", settings, SYSTEM, user, data_classes=classes,
                            approver=approver)
    if "NO_FIX" in reply or "@@" not in reply:
        return None
    return Draft(kind=DraftKind.test_patch, title=f"Fix test: {case.title}",
                 body=clean_diff(reply))


def clean_diff(reply: str) -> str:
    """The diff without the code fences or prose a model sometimes wraps it in."""
    lines = reply.strip().splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(("--- ", "@@"))), 0)
    body = [ln for ln in lines[start:] if not ln.startswith("```")]
    return "\n".join(body).strip() + "\n"


def apply_diff(original: str, diff: str) -> str | None:
    """Apply a single-file unified diff. Each hunk's old lines must appear in the file
    (anywhere, nearest to the stated line first); returns None when one does not."""
    src = original.splitlines()
    out_lines = list(src)
    hunks: list[tuple[int, list[str], list[str]]] = []
    current: tuple[int, list[str], list[str]] | None = None
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if m:
            current = (int(m.group(1)), [], [])
            hunks.append(current)
            continue
        if current is None or line.startswith(("--- ", "+++ ")):
            continue
        tag, text = (line[:1], line[1:]) if line else (" ", "")
        if tag == " ":
            current[1].append(text)
            current[2].append(text)
        elif tag == "-":
            current[1].append(text)
        elif tag == "+":
            current[2].append(text)
    if not hunks:
        return None
    shift = 0
    for start, old, new in hunks:
        want = start - 1 + shift
        spots = [i for i in range(len(out_lines) - len(old) + 1)
                 if out_lines[i:i + len(old)] == old] if old else [max(0, want)]
        if not spots:
            return None
        at = min(spots, key=lambda i: abs(i - want))
        out_lines[at:at + len(old)] = new
        shift += len(new) - len(old)
    return "\n".join(out_lines) + ("\n" if original.endswith("\n") else "")


def _size(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        if any(part in SKIP for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            total += p.stat().st_size
            if total > MAX_COPY_BYTES:
                break
    return total


def verify(case: Case, settings: Settings, diff: str) -> tuple[bool | None, str]:
    """(True, why) when the test passes on every re-run in a patched temporary copy,
    (False, why) when it does not, (None, why) when it could not be checked."""
    from tessella.investigate.rerun import run_once

    repo = Path(settings.inputs.repo).resolve()
    test_file = test_file_of(case, str(repo))
    if test_file is None:
        return None, "the test file was not found"
    patched = apply_diff(test_file.read_text(), diff)
    if patched is None:
        return False, "the diff does not apply to the current test file"
    if _size(repo) > MAX_COPY_BYTES:
        return None, "the repository is too large to copy for a check"
    attempts = max(1, settings.rerun.attempts)
    with tempfile.TemporaryDirectory(prefix="tessella-fix-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(repo, copy, ignore=shutil.ignore_patterns(*SKIP), symlinks=True)
        (copy / test_file.resolve().relative_to(repo)).write_text(patched)
        results = [run_once(case, settings.rerun.command, cwd=copy, repo=copy)
                   for _ in range(attempts)]
    if any(passed is None for passed, _ in results):
        return None, "could not run the test: " + next(o for p, o in results if p is None)[:200]
    passed = sum(1 for p, _ in results if p)
    if passed == attempts:
        return True, f"verified: the test passed {passed} of {attempts} re-run(s) with this patch"
    return False, f"the test still failed with this patch ({passed} of {attempts} passed)"


def fix(case: Case, settings: Settings, rerun: bool = True, approver=None) -> tuple[str, str]:
    """(patch, status) for a test bug: a checked patch, or an empty patch with the reason.
    Never raises: a fixer that is down or wrong costs only the patch."""
    try:
        draft = propose(case, settings, approver)
    except Exception as exc:  # noqa: BLE001
        return "", f"no patch: the fixer call failed ({type(exc).__name__})"
    if draft is None:
        return "", "no patch: the fixer did not propose one"
    if not rerun:
        return draft.body, "not verified: re-runs are off (--no-rerun)"
    try:
        ok, why = verify(case, settings, draft.body)
    except Exception as exc:  # noqa: BLE001
        return draft.body, f"not verified: {type(exc).__name__}: {str(exc)[:120]}"
    if ok is False:
        return "", f"a patch was proposed and rejected: {why}"
    return draft.body, why if ok else f"not verified: {why}"
