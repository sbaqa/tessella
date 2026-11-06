"""Failure signatures: 40 red tests usually collapse into 3-5 clusters.

A signature = normalized error message + the top stack frame from project code.
Numbers, IDs, timestamps and quoted values become placeholders, so the same
problem with different data lands in the same cluster.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from tessella.models import Case

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?"), "<ts>"),
    (re.compile(r"0x[0-9a-f]+", re.I), "<hex>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]

# Python frames: File "x.py", line 42, in fn  |  JS/Java frames: at fn (x.ts:42:7)
_FRAME = re.compile(r'(?:File "([^"]+)", line (\d+))|(?:at .*?\(?([\w./\\-]+\.(?:ts|js|java|py)):(\d+))')
_LIBRARY = ("site-packages", "node_modules", "/usr/lib", "<frozen")


def normalize(message: str) -> str:
    out = message.strip().splitlines()[0] if message.strip() else ""
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out.strip()[:300]


def top_project_frame(stack: str) -> str:
    """File and line of the first project frame. The function name is left out on purpose:
    parametrized or copied tests have different names but fail on the same line."""
    for m in _FRAME.finditer(stack or ""):
        path = m.group(1) or m.group(3) or ""
        line = m.group(2) or m.group(4) or ""
        if path and not any(lib in path for lib in _LIBRARY):
            return f"{path.split('/')[-1]}:{line}"
    return ""


def signature(case: Case) -> str:
    base = f"{normalize(case.message or case.stack)}|{top_project_frame(case.stack)}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def cluster(cases: list[Case]) -> dict[str, list[Case]]:
    groups: dict[str, list[Case]] = defaultdict(list)
    for case in cases:
        case.signature = case.signature or signature(case)
        groups[case.signature].append(case)
    return dict(groups)
