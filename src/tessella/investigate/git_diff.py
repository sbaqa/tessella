"""What changed in the code since the last green run, limited to what the failure touches.

The green side is `inputs.green_ref` when set (for example origin/main), else the commit of
the last triage that saw no failures, which triage records by itself in a git repository.
The red side is the working tree, so uncommitted changes count too. Only files named in the
failure's stack and the failing test's own file are shown; when none of those changed,
the evidence says so in one line, which is worth knowing too.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tessella.models import Case, Evidence, EvidenceKind

MAX_CHARS = 6000
_FILE = re.compile(r"""(?:File "|\bat .*\(|^\s*)([\w./\\-]+\.(?:py|js|ts|tsx|jsx|java|kt|go|rb|cs))""",
                   re.MULTILINE)


def _git(repo: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              check=False, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_repo(repo: str) -> bool:
    return _git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"


def head(repo: str) -> str | None:
    """HEAD of the repository, or None outside git."""
    sha = _git(repo, "rev-parse", "HEAD").strip()
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def changed_files(repo: str, green: str) -> list[str]:
    return [ln for ln in _git(repo, "diff", "--name-only", green).splitlines() if ln]


def files_of(case: Case, repo: str) -> list[str]:
    """Repository files the failure points at: stack frames and the test file, as paths
    relative to the repository root."""
    root = Path(repo).resolve()
    names = _FILE.findall(f"{case.stack}\n{case.test_id.split('::')[0]}")
    out = []
    for name in names:
        p = Path(name)
        p = p if p.is_absolute() else root / p
        try:
            rel = p.resolve().relative_to(root).as_posix()
        except ValueError:
            continue  # a library frame, outside the repository
        if rel not in out:
            out.append(rel)
    return out


def diff_evidence(repo: str, green: str, only: list[str] | None = None,
                  max_chars: int = MAX_CHARS) -> Evidence | None:
    changed = changed_files(repo, green)
    if not changed:
        return None
    files = [f for f in changed if f in set(only)] if only else changed
    if only and not files:
        return Evidence(kind=EvidenceKind.git_diff,
                        summary=f"{len(changed)} file(s) changed since the last green run "
                                f"({green[:8]}), none of them on this failure's stack",
                        excerpt="\n".join(changed[:30]), ref=green)
    patch = _git(repo, "diff", "--unified=3", green, "--", *files)
    return Evidence(kind=EvidenceKind.git_diff,
                    summary=f"{len(files)} file(s) on this failure's path changed since the "
                            f"last green run ({green[:8]}): {', '.join(files[:4])}",
                    excerpt=patch[:max_chars], ref=green)


def for_case(case: Case, repo: str, green: str | None, file: str | None = None) -> Evidence | None:
    """The diff for one failure, or None outside git or with nothing to compare against.
    `file` (the investigator's tool) asks for one file's diff whatever the stack says."""
    if not green or not is_repo(repo):
        return None
    if file:
        return diff_evidence(repo, green, [file.lstrip("./")])
    return diff_evidence(repo, green, files_of(case, repo))
