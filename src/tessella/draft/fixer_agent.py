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
from tessella.draft.patch import MAX_COPY_BYTES, SKIP, _size
from tessella.models import Case

SYSTEM = """You fix bugs in application code. A test failure was diagnosed as a product bug;
you get the diagnosis, the failure, and the code it points to. Work in steps. Each reply is
one JSON object choosing one tool:
- read_file: `path` (relative to the repository) and `start_line`; returns 80 numbered lines.
- search: `query`, plain text; returns matching lines as path:line: text.
- edit: `path`, `old` and `new`. `old` = lines copied exactly from the file as read_file
  showed them (without the line numbers), enough to be unique; `new` = what replaces them.
  The edit is made in a copy of the repository and the failing test is run; you are told
  the result.
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
    tool: Literal["read_file", "search", "edit", "give_up"]
    path: str = ""
    start_line: int = 1
    query: str = ""
    old: str = ""
    new: str = ""


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


def _files(repo: Path):
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if not any(part in SKIP for part in rel.parts) and p.suffix in _TEXT and p.is_file():
            yield p, rel


def _is_test(rel: str) -> bool:
    return rel.startswith("test") or "/test" in f"/{rel}" or Path(rel).name.startswith("test_")


def search(repo: Path, query: str, limit: int = 25) -> str:
    """Lines that contain every word of the query, ignoring case; when none do, lines with
    its longest word. Small models search in phrases, so a literal match finds too little."""
    words = [w.lower() for w in query.split() if len(w) > 1]
    if not words:
        return "Search for at least one word of two characters."

    def scan(want):
        hits = []
        for p, rel in _files(repo):
            try:
                for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    low = line.lower()
                    if all(w in low for w in want):
                        hits.append(f"{rel}:{n}: {line.strip()[:160]}")
                        if len(hits) >= limit:
                            return hits
            except OSError:
                continue
        return hits

    hits = scan(words)
    note = ""
    if not hits and len(words) > 1:
        longest = max(words, key=len)
        hits, note = scan([longest]), f"No line has all of {query!r}; lines with {longest!r}:\n"
    if not hits:
        return f"No line contains {query!r}."
    return note + "\n".join(hits) + ("\n(more matches not shown)" if len(hits) >= limit else "")


def file_map(repo: Path, limit: int = 60) -> str:
    """The product's source files, so the model knows what exists before it reads."""
    paths = [str(rel) for p, rel in _files(repo) if p.suffix not in {".json", ".txt", ".ini", ".cfg"}
             and not _is_test(str(rel))]
    more = f" (and {len(paths) - limit} more)" if len(paths) > limit else ""
    return "Product source files: " + ", ".join(paths[:limit]) + more


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
    return sorted(seen.items(), key=lambda kv: _is_test(kv[0]))


def _server_log(case: Case, settings: Settings):
    """The app log around the failure, read now when the case carries none: a server-side
    traceback is what points at the product code."""
    if any(e.kind == "log" and e.excerpt for e in case.evidence):
        return None
    if not (settings.logs.path or settings.logs.url):
        return None
    from tessella.investigate.logs import log_evidence

    try:
        return log_evidence(case, settings.logs, settings.network.allow, settings.redact)
    except Exception:  # noqa: BLE001 - a log that cannot be read costs only this hint
        return None


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
    parts.append(file_map(repo))
    pointers = code_pointers(case, repo)
    product = [pl for pl in pointers if not _is_test(pl[0])]
    for path, line in list(reversed(product or pointers))[:2]:  # the deepest frame first
        parts.append(read_file(repo, path, max(1, line - 30)))
    return "\n\n".join(parts)


def replace_once(text: str, old: str, new: str) -> str:
    """`text` with `old` replaced by `new`. Small models copy indentation loosely, so a
    second pass matches line by line ignoring surrounding spaces and keeps the file's
    indentation. An error comes back as a string starting with NUL."""
    old = re.sub(r"^\s*\d+  ", "", old, flags=re.M) if re.match(r"^\s*\d+  ", old) else old
    if not old.strip():
        return "\0`old` is empty: copy the lines to change from read_file."
    count = text.count(old)
    if count == 1:
        return text.replace(old, new)
    if count > 1:
        return "\0`old` occurs more than once: include a line or two more around it."
    lines, want = text.splitlines(keepends=True), [ln.strip() for ln in old.strip("\n").splitlines()]
    spots = [i for i in range(len(lines) - len(want) + 1)
             if [ln.strip() for ln in lines[i:i + len(want)]] == want]
    if len(spots) != 1:
        return ("\0`old` was not found in the file. Copy it exactly from read_file, without "
                "the line numbers." if not spots else
                "\0`old` occurs more than once: include a line or two more around it.")
    at = spots[0]
    indent = re.match(r"\s*", lines[at]).group(0)
    base = re.match(r"\s*", old.strip("\n").splitlines()[0]).group(0)
    body = [indent + ln[len(base):] if ln.startswith(base) else ln
            for ln in new.strip("\n").splitlines()]
    tail = "\n" if lines[at + len(want) - 1].endswith("\n") else ""
    return "".join(lines[:at]) + "\n".join(body) + tail + "".join(lines[at + len(want):])


def unified(path: str, before: str, after: str) -> str:
    import difflib

    return "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                        after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))


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
    errors = tempfile.TemporaryFile(mode="w+")  # why it died, if it does
    proc = subprocess.Popen(f.app_command.format(port=port), shell=True, cwd=copy,
                            stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                httpx.get(url + f.app_ready_path, timeout=1)
                break
            except httpx.HTTPError:
                if proc.poll() is not None:
                    errors.seek(0)
                    raise RuntimeError("the app exited while starting from the copy: "
                                       + errors.read()[-900:].strip())
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
        errors.close()


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

    def edit(self, path: str, old: str, new: str) -> str | None:
        """Replace `old` with `new` in the copy's file; returns the error, or None when done.
        `old` must occur once, exactly or ignoring each line's surrounding spaces."""
        target = _inside(self.repo, path)
        if target is None:
            return f"{path!r} is not a file in the repository."
        rel = str(target.relative_to(self.repo))
        if _is_test(rel):
            return "Fix the product code, not the tests."
        patched = replace_once(target.read_text(), old, new)
        if isinstance(patched, str) and patched.startswith("\0"):
            return patched[1:]
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
    try:
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
            if failing is not None and failing - baseline:
                again = _suite(settings, box.copy, env)  # a flaky test must fail twice to count
                failing = baseline | ((failing - baseline) & (again if again is not None else failing))
    except RuntimeError as exc:  # the edit broke the app: tell the model, let it try again
        return False, str(exc)
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
        log = _server_log(case, settings)
        if log is not None:
            case = case.model_copy(update={"evidence": [*case.evidence, log]})
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
                box.reset()
                error = box.edit(step.path, step.old, step.new)
                if error:
                    result = error
                else:
                    ok, why = check(case, settings, box, baseline)
                    if ok:
                        rel = box.touched[0]
                        return unified(rel, (repo / rel).read_text(), (box.copy / rel).read_text()), why
                    result, last = f"Edit tried: {why}", why
            transcript.append(f"Step: {step.tool} {step.path or step.query}"
                              + (f"\nold:\n{step.old}\nnew:\n{step.new}" if step.tool == "edit" else "")
                              + "\n"
                              f"Why: {step.thought}\nResult:\n{result[-4000:]}")
        return "", "no fix: " + (f"the last edit was rejected, {last[:200]}" if last
                                 else "the fixer ran out of steps")
    except Exception as exc:  # noqa: BLE001 - a fixer that breaks costs only the fix
        return "", f"no fix: {type(exc).__name__}: {str(exc)[:160]}"
    finally:
        box.close()
