"""Re-run a failing test with the team's own command to separate flaky from real."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from tessella.models import Case, Evidence, EvidenceKind

# pytest exit codes that mean "the command never ran the test": interrupted, internal error,
# usage error (e.g. a node id that doesn't exist), no tests collected.
_PYTEST_NOT_RUN = {2, 3, 4, 5}
_NOTHING_RAN = re.compile(
    r"no tests ran|no tests found|not found: |ERROR: file or directory not found", re.IGNORECASE)
_NODE_PATH = re.compile(r"^[\w./\\-]+\.(?:py|ts|js|mjs|cjs|tsx|jsx)(?:::|:\d+|$)")


def test_id_for(case: Case, repo: str | Path = ".") -> str | None:
    """The id to put in {test_id}: the reader's own id, a title that already is a path-based
    id, or a JUnit `dotted.module::name` title mapped to the pytest file found under `repo`.
    None when no runnable id can be derived; the caller then skips the re-run."""
    if case.test_id:
        return case.test_id
    title = case.title.strip()
    if _NODE_PATH.match(title):
        return title
    dotted, sep, name = title.partition("::")
    if not sep or not dotted or "/" in dotted or " " in dotted:
        return None
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):  # longest module path that exists; the rest are classes
        module = "/".join(parts[:cut]) + ".py"
        if (Path(repo) / module).is_file():
            return "::".join([module, *parts[cut:], name])
    return None


def _never_ran(cmd: list[str], proc: subprocess.CompletedProcess) -> bool:
    if proc.returncode == 0:
        return False
    if "pytest" in " ".join(cmd[:3]) and proc.returncode in _PYTEST_NOT_RUN:
        return True
    return bool(_NOTHING_RAN.search((proc.stdout or "") + (proc.stderr or "")))


def run_once(case: Case, command: str, extra_args: str = "", cwd: str | Path | None = None,
             timeout: int = 600, repo: str | Path = ".") -> tuple[bool | None, str]:
    """One run of the test: (passed, last lines of output). passed is None when the
    command could not run the test at all."""
    test_id = test_id_for(case, repo)
    if test_id is None:
        return None, "no runnable test id"
    cmd = shlex.split(command.format(test_id=test_id)) + shlex.split(extra_args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                              check=False)
    except OSError as exc:
        return None, f"could not start: {exc}"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout} s"
    output = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-80:])
    if _never_ran(cmd, proc):
        return None, f"exit {proc.returncode}, the test did not run\n{output}"
    return proc.returncode == 0, output


def rerun(case: Case, command: str, attempts: int = 2, timeout: int = 600,
          repo: str | Path = ".") -> Evidence:
    """Run `command` (with {test_id}) `attempts` times; passes on retry = flaky candidate.
    A command that could not run the test (no id, unknown id, missing binary) is reported
    as such, never as a consistent failure."""
    test_id = test_id_for(case, repo)
    if test_id is None:
        return Evidence(kind=EvidenceKind.rerun,
                        summary="Not re-run: the report gives no runnable test id",
                        excerpt=case.title)
    cmd = shlex.split(command.format(test_id=test_id))
    results: list[bool] = []
    problems: list[str] = []
    for _ in range(attempts):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except OSError as exc:
            problems.append(f"could not start: {exc}")
            continue
        except subprocess.TimeoutExpired:
            results.append(False)  # it ran and did not pass in time: counts as a failure
            continue
        if _never_ran(cmd, proc):
            problems.append(f"exit {proc.returncode}, the test did not run")
            continue
        results.append(proc.returncode == 0)
    if not results:
        return Evidence(kind=EvidenceKind.rerun,
                        summary=f"Could not re-run: {problems[-1]}",
                        excerpt="\n".join(problems), ref=" ".join(cmd))
    runs, passed = len(results), sum(results)
    if passed == 0:
        summary = f"Failed on all {runs} re-runs: consistent failure"
    elif passed == runs:
        summary = f"Passed on all {runs} re-runs: likely flaky or environment"
    else:
        summary = f"Passed {passed} of {runs} re-runs: flaky"
    return Evidence(kind=EvidenceKind.rerun, summary=summary, excerpt=str(results),
                    ref=" ".join(cmd))
