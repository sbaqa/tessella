"""The evidence pack: what the reader model sees, fitted to its context window.

Every evidence item keeps its number and summary line, because verdicts cite evidence by
number. Only excerpts are cut, and the cut is shared out by weight: re-runs, the report's
own error and the test source keep more than logs or traces. A long excerpt keeps its head
and its tail, where a traceback's last frame and error line are.

Before the share-out, log-like excerpts are made leaner: repeated lines (identical, or
identical once timestamps, ids and numbers are masked) fold into one line with a count, and
stack traces drop runs of library frames, keeping the user's own frames, the first frame,
the last two and every error line. The size is capped by `max_evidence_tokens`.
"""

from __future__ import annotations

import re

from tessella.config import ModelSettings
from tessella.models import Case, EvidenceKind

CHARS_PER_TOKEN = 3.5  # rough, for code and logs; errs on the side of fitting
RESERVED_TOKENS = 1200  # the system prompt and the reply
DEFAULT_CTX = 4096  # what Ollama uses when num_ctx is not set

WEIGHT = {
    EvidenceKind.rerun: 3, EvidenceKind.preflight: 3, EvidenceKind.report: 3,
    EvidenceKind.test_source: 3, EvidenceKind.probe_replay: 2, EvidenceKind.visual_diff: 2,
    EvidenceKind.screenshot: 2, EvidenceKind.git_diff: 2, EvidenceKind.log: 2,
    EvidenceKind.trace: 1, EvidenceKind.artifact: 1, EvidenceKind.memory: 2,
}


# Excerpts of these kinds are folded and have their stack traces shortened. Test source and
# diffs are left exactly as they are: their line numbers and context matter.
LEAN_KINDS = {EvidenceKind.log, EvidenceKind.trace, EvidenceKind.artifact,
              EvidenceKind.report, EvidenceKind.rerun}


def budget_chars(ms: ModelSettings | None) -> int:
    ctx = (ms.num_ctx if ms and ms.num_ctx else None) or DEFAULT_CTX
    budget = max(2000, int((ctx - RESERVED_TOKENS) * CHARS_PER_TOKEN))
    if ms and ms.max_evidence_tokens:
        budget = min(budget, max(2000, int(ms.max_evidence_tokens * CHARS_PER_TOKEN)))
    return budget


_VOLATILE = [
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}([.,]\d+)?)?(Z|[+-]\d{2}:?\d{2})?"),
    re.compile(r"\b\d{1,2}:\d{2}:\d{2}([.,]\d+)?\b"),
    re.compile(r"\b(0x)?(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{6,}\b"),
    re.compile(r"\b\d+(\.\d+)?\s?(ns|us|µs|ms|s|m|h)\b"),
    re.compile(r"\d+(\.\d+)?"),
]


def _masked(line: str) -> str:
    for pattern in _VOLATILE:
        line = pattern.sub("#", line)
    return line


def fold(text: str) -> str:
    """Consecutive lines that match once volatile parts are masked become the first line
    plus ` (×N)`. Blank lines are left alone."""
    out: list[str] = []
    key, count = None, 0
    for line in text.split("\n"):
        k = _masked(line) if line.strip() else None
        if k is not None and k == key:
            count += 1
            continue
        if count > 1:
            out[-1] += f" (×{count})"
        out.append(line)
        key, count = k, 1
    if count > 1:
        out[-1] += f" (×{count})"
    return "\n".join(out)


_PY_FRAME = re.compile(r'^\s*File "(?P<loc>[^"]+)", line \d+')
_PYTEST_FRAME = re.compile(r"^\s*(?P<loc>\S+\.py):\d+: in \S+")
# JS/Node `at fn (file.js:1:2)` / `at file.js:1:2`, Java `at pkg.Cls.fn(Cls.java:3)`
_AT_FRAME = re.compile(r"^\s*at (?P<loc>\S.*?(\([^()]*\)|:\d+(:\d+)?))\s*$")
_LIBRARY = re.compile(
    r"site-packages|dist-packages|node_modules|(^|[/\\])\.?venv[/\\]|[/\\]lib[/\\]python\d"
    r"|<frozen|<anonymous>|node:internal|\binternal[/\\]|[/\\]jdk|\bjdk\.|\b(java|javax|sun|com\.sun|kotlin"
    r"|scala|org\.junit|org\.testng|org\.gradle|worker\.org\.gradle|org\.apache\.maven"
    r"|org\.springframework|io\.netty)\.")


def _frame(line: str) -> str | None:
    """The location of a stack frame's first line, or None for any other line."""
    m = _PY_FRAME.match(line) or _PYTEST_FRAME.match(line) or _AT_FRAME.match(line)
    return m.group("loc") if m else None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def shorten_traces(text: str) -> str:
    """Drop runs of library frames from stack traces. The first frame, the last two, the
    user's own frames and every line that is not a frame (error lines) are kept."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _frame(lines[i]) is None:
            out.append(lines[i])
            i += 1
            continue
        frames: list[tuple[list[str], bool]] = []  # (lines, is library) for one stack
        while i < len(lines) and (loc := _frame(lines[i])) is not None:
            j = i + 1
            while (j < len(lines) and lines[j].strip() and _frame(lines[j]) is None
                   and _indent(lines[j]) > _indent(lines[i])):
                j += 1  # the frame's code lines (Python, pytest)
            frames.append((lines[i:j], bool(_LIBRARY.search(loc))))
            i = j
        last = len(frames) - 1
        dropped: list[list[str]] = []
        for n, (body, library) in enumerate(frames):
            if library and 0 < n < last - 1:
                dropped.append(body)
                continue
            out.extend(_dropped(dropped))
            dropped = []
            out.extend(body)
    return "\n".join(out)


def _dropped(run: list[list[str]]) -> list[str]:
    if sum(len(body) for body in run) <= 1:
        return [line for body in run for line in body]  # a marker would save nothing
    pad = run[0][0][:_indent(run[0][0])]
    return [f"{pad}… {len(run)} library frames …"]


def lean(text: str) -> str:
    return fold(shorten_traces(text))


def trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit < 80:
        return text[:limit]
    head = int(limit * 0.4)
    tail = limit - head - 40
    return f"{text[:head]}\n… [{len(text) - head - tail} characters cut] …\n{text[-tail:]}"


def _shares(case: Case, excerpts: list[str], room: int) -> list[int]:
    """Water-filling by weight: short excerpts take what they need, the rest is split."""
    sizes = [len(x) for x in excerpts]
    weights = [WEIGHT.get(ev.kind, 1) for ev in case.evidence]
    shares = [0] * len(sizes)
    left = {i for i, n in enumerate(sizes) if n}
    while left and room > 0:
        total_w = sum(weights[i] for i in left)
        done = [i for i in left if sizes[i] <= room * weights[i] / total_w]
        if not done:
            for i in left:
                shares[i] = int(room * weights[i] / total_w)
            break
        for i in done:
            shares[i] = sizes[i]
            room -= sizes[i]
            left.discard(i)
    return shares


def evidence_pack(case: Case, budget: int | None = None) -> str:
    budget = budget or budget_chars(None)
    head = [f"Case: {case.title}", f"Origin: {case.origin}", f"Message: {case.message}", ""]
    labels = [f"[{i}] {ev.kind}: {ev.summary}" for i, ev in enumerate(case.evidence)]
    room = budget - sum(len(x) + 1 for x in head + labels) - 2 * len(labels)
    excerpts = [lean(ev.excerpt) if ev.excerpt and ev.kind in LEAN_KINDS else ev.excerpt
                for ev in case.evidence]
    shares = _shares(case, excerpts, max(0, room))
    lines = head
    for label, excerpt, share in zip(labels, excerpts, shares):
        lines.append(label)
        if excerpt and share:
            lines.append(trim(excerpt, share))
        lines.append("")
    return "\n".join(lines)
