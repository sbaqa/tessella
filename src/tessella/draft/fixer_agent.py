"""The fixer agent: a checked fix for a product bug, written by the `fixer` role.

A small model writes better code when it can look before it writes and is told what its
patch broke. So the agent works in turns, each a single choice from a short, fixed menu:
read part of a file, search the repository for text, propose a patch, or give up. Every
tool is plain code. A proposed patch is applied to a temporary copy of the repository,
never to your files; the app is started from that copy when `fix.app_command` is set, the
failing test is re-run there, and `fix.suite_command` must fail no test that did not
already fail without the patch. A patch that fails is reported back to the model with the
test output, and it may try again until `fix.steps` runs out.

The result is a unified diff and what checking it showed, put on the ticket draft. A person
applies it (`tessella fix apply`), on a git branch.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from tessella.config import Settings
from tessella.draft.patch import MAX_COPY_BYTES, SKIP, _size, apply_diff, clean_diff
from tessella.models import Case

SYSTEM = """You fix bugs in application code. A test failure was diagnosed as a product bug;
you get the diagnosis, the failure, and the code it points to. Work in steps. Each reply is
one JSON object choosing one tool:
- read_file: `path` (relative to the repository) and `start_line`; returns 80 numbered lines.
- search: `query`, plain text; returns matching lines as path:line: text.
- propose_patch: `path` and `diff`, a unified diff of that one file (--- a/path, +++ b/path,
  @@ hunks with a few unchanged context lines copied exactly). It is applied to a copy of the
  repository and the failing test is run; you are told the result.
- give_up: when no safe fix exists.
Fix the product code, never the tests. Keep the change as small as possible. `thought` says
in one sentence why you chose this step."""

_TRACE = re.compile(r'File "([^"]+)", line (\d+)|([\w./-]+\.(?:py|ts|js|go|java|rb)):(\d+)')
_TEXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".php", ".cs", ".kt",
         ".html", ".css", ".sql", ".yaml", ".yml", ".toml", ".json", ".cfg", ".ini", ".txt"}
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
MAX_READ = 80


class Step(BaseModel):
    thought: str
    tool: Literal["read_file", "search", "propose_patch", "give_up"]
    path: str = ""
    start_line: int = 1
    query: str = ""
    diff: str = ""


def _inside(repo: Path, path: str) -> Path | None:
    """A file inside the repository (and outside caches, outputs and tests' own reports)."""
    try:
        p = (repo / path).resolve()
        rel = p.relative_to(repo)
    except (ValueError, OSError):
        return None
    if any(part in SKIP for part in rel.parts) or not p.is_file() or p.suffix not in _TEXT:
        return None
    return p


def read_file(repo: Path, path: str, start: int = 1) -> str:
    p = _inside(repo, path)
    if p is None:
        return f"No readable file {path!r} in the repository."
    lines = p.read_text(errors="replace").splitlines()
    start = max(1, min(start, max(1, len(lines) - MAX_READ + 1)))
    chunk = lines[start - 1:start - 1 + MAX_READ]
    body = "\n".join(f"{start + i:>5}  {ln}" for i, ln in enumerate(chunk))
    return f"{path} lines {start}-{start + len(chunk) - 1} of {len(lines)}:\n{body}"


def search(repo: Path, query: str, limit: int = 25) -> str:
    query = query.strip()
    if len(query) < 2:
        return "Search for at least two characters."
    hits = []
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if any(part in SKIP for part in rel.parts) or p.suffix not in _TEXT or not p.is_file():
            continue
        try:
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if query in line:
                    hits.append(f"{rel}:{n}: {line.strip()[:160]}")
                    if len(hits) >= limit:
                        return "\n".join(hits) + "\n(more matches not shown)"
        except OSError:
            continue
    return "\n".join(hits) or f"No line contains {query!r}."


def code_pointers(case: Case, repo: Path) -> list[tuple[str, int]]:
    """Product files and lines the failure points to: from the stack and from tracebacks in
    the evidence (an app log often has the server-side one). Test files come last."""
    text = "\n".join([case.stack] + [e.excerpt for e in case.evidence])
    seen: dict[str, int] = {}
    for m in _TRACE.finditer(text):
        raw, line = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        p = Path(raw)
        try:
            rel = p.resolve().relative_to(repo) if p.is_absolute() else p
        except ValueError:
            continue
        key = str(rel)
        if key not in seen and _inside(repo, key):
            seen[key] = int(line)
    return sorted(seen.items(), key=lambda kv: kv[0].startswith("test") or "/test" in kv[0])


def _brief(case: Case, repo: Path) -> str:
    v = case.verdict
    parts = [f"Failing test: {case.test_id or case.title}",
             f"Failure: {case.message[:600]}",
             f"Diagnosis (root cause): {v.root_cause if v else 'not known'}"]
    if v and v.suggested_fix:
        parts.append(f"Suggested fix: {v.suggested_fix}")
    logs = [e for e in case.evidence if e.kind == "log" and e.excerpt]
    if logs:
        parts.append("Server log around the failure:\n" + logs[0].excerpt[-2500:])
    for path, line in code_pointers(case, repo)[:2]:
        parts.append(read_file(repo, path, max(1, line - 30)))
    return "\n\n".join(parts)


def with_headers(diff: str, path: str) -> str:
    """The diff with its file named in the --- / +++ lines, as `tessella fix apply` reads it."""
    hunks = diff[diff.find("@@"):] if "@@" in diff else diff
    return f"--- a/{path}\n+++ b/{path}\n{hunks}"


def target_of(diff: str) -> str | None:
    m = re.search(r"^\+\+\+ (?:b/)?(\S+)", diff, re.M)
    return m.group(1) if m else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _app(settings: Settings, copy: Path):
    """Start the app under test from the copy, when configured. Yields the environment the
    tests run with (the app's address in fix.app_url_env)."""
    env = dict(os.environ)
    f = settings.fix
    if not f.app_command:
        yield env
        return
    import httpx

    port = _free_port()
    proc = subprocess.Popen(f.app_command.format(port=port), shell=True, cwd=copy,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                httpx.get(url + f.app_ready_path, timeout=1)
                break
            except httpx.HTTPError:
                if proc.poll() is not None:
                    raise RuntimeError("the app exited while starting from the copy")
                time.sleep(0.3)
        else:
            raise RuntimeError("the app did not answer within 30 s")
        if f.app_url_env:
            env[f.app_url_env] = url
        yield env
    finally:
        try:
            os.killpg(proc.pid, 15)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        proc.wait(timeout=10)


def _suite(settings: Settings, cwd: Path, env: dict) -> set[str] | None:
    """Ids of the tests that fail in the suite (pytest's FAILED/ERROR lines). None: no
    suite configured, or it could not run."""
    if not settings.fix.suite_command:
        return None
    try:
        proc = subprocess.run(settings.fix.suite_command, shell=True, cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return set(_FAILED.findall(proc.stdout + proc.stderr))


class Sandbox:
    """A temporary copy of the repository where patches are tried, reset before each one."""

    def __init__(self, repo: Path):
        self.repo = repo
        self._tmp = tempfile.TemporaryDirectory(prefix="tessella-fixer-")
        self.copy = Path(self._tmp.name) / "repo"
        shutil.copytree(repo, self.copy, ignore=shutil.ignore_patterns(*SKIP), symlinks=True)
        self.touched: list[str] = []

    def reset(self) -> None:
        for rel in self.touched:
            shutil.copy2(self.repo / rel, self.copy / rel)
        self.touched = []

    def apply(self, path: str, diff: str) -> str | None:
        """Apply a one-file diff to the copy; returns the error, or None when it applied."""
        target = _inside(self.repo, path)
        if target is None:
            return f"{path!r} is not a file in the repository."
        rel = str(target.relative_to(self.repo))
        if rel.startswith("test") or "/tests/" in f"/{rel}" or Path(rel).name.startswith("test_"):
            return "Fix the product code, not the tests."
        patched = apply_diff(target.read_text(), diff)
        if patched is None:
            return ("The diff does not apply: its unchanged and removed lines must match the "
                    "file exactly. Read the file again and copy them.")
        (self.copy / rel).write_text(patched)
        self.touched.append(rel)
        return None

    def close(self) -> None:
        self._tmp.cleanup()


def check(case: Case, settings: Settings, box: Sandbox,
          baseline: set[str] | None) -> tuple[bool, str]:
    """Run the failing test (every re-run must pass) and the suite on the patched copy."""
    from tessella.investigate.rerun import run_once

    attempts = max(1, settings.rerun.attempts)
    with _app(settings, box.copy) as env:
        results = [run_once(case, settings.rerun.command, cwd=box.copy, repo=box.copy, env=env)
                   for _ in range(attempts)]
        if any(p is None for p, _ in results):
            return False, "could not run the test: " + next(o for p, o in results if p is None)[-800:]
        passed = sum(1 for p, _ in results if p)
        if passed < attempts:
            out = next(o for p, o in results if not p)
            return False, f"the test still fails ({passed} of {attempts} passed):\n{out[-1500:]}"
        failing = _suite(settings, box.copy, env) if baseline is not None else None
    if failing is None:
        return True, (f"verified: the failing test passed {passed} of {attempts} re-run(s); "
                      "the rest of the suite was not run (set fix.suite_command)")
    broken = sorted(failing - baseline)
    if broken:
        return False, "the patch makes other tests fail: " + ", ".join(broken[:8])
    return True, (f"verified: the failing test passed {passed} of {attempts} re-run(s) and the "
                  f"suite fails no new test ({len(baseline)} already failing without the patch)")


def run(case: Case, settings: Settings, approver=None) -> tuple[str, str]:
    """(diff, status): a checked fix, or an empty diff with the reason. Never raises."""
    from tessella.integrations import llm
    from tessella.integrations.llm import DataClass

    if settings.models.fixer is None:
        return "", "no fix: no fixer model is configured (models.fixer)"
    repo = Path(settings.inputs.repo).resolve()
    if _size(repo) > MAX_COPY_BYTES:
        return "", "no fix: the repository is too large to copy for a check"
    # Product code and evidence: a cloud fixer may only see what the data policy lets leave.
    classes = [DataClass.source_code, DataClass.evidence_excerpts, DataClass.failing_test_file]
    try:
        box = Sandbox(repo)
    except OSError as exc:
        return "", f"no fix: could not copy the repository ({exc})"
    try:
        baseline = None
        if settings.fix.suite_command:
            with _app(settings, box.copy) as env:
                baseline = _suite(settings, box.copy, env)
        transcript = [_brief(case, repo)]
        last = ""
        for _ in range(max(1, settings.fix.steps)):
            # The brief and the latest turns: a small model's context stays short.
            user = "\n\n".join(transcript[:1] + transcript[1:][-4:]) + "\n\nChoose the next step."
            step, _raw, _ = llm.complete_json("fixer", settings, SYSTEM, user, classes, Step,
                                              approver=approver)
            if step is None:
                transcript.append("Your reply was not a valid step. Reply with one JSON object.")
                continue
            if step.tool == "give_up":
                return "", f"no fix: the fixer gave up ({step.thought[:160]})"
            if step.tool == "read_file":
                result = read_file(repo, step.path, step.start_line)
            elif step.tool == "search":
                result = search(repo, step.query)
            else:
                diff = clean_diff(step.diff)
                box.reset()
                error = box.apply(step.path, diff)
                if error:
                    result = error
                else:
                    ok, why = check(case, settings, box, baseline)
                    if ok:
                        return with_headers(diff, box.touched[0]), why
                    result, last = f"Patch tried: {why}", why
            transcript.append(f"Step: {step.tool} {step.path or step.query}\n"
                              f"Why: {step.thought}\nResult:\n{result[-4000:]}")
        return "", "no fix: " + (f"the last patch was rejected, {last[:200]}" if last
                                 else "the fixer ran out of steps")
    except Exception as exc:  # noqa: BLE001 - a fixer that breaks costs only the fix
        return "", f"no fix: {type(exc).__name__}: {str(exc)[:160]}"
    finally:
        box.close()
