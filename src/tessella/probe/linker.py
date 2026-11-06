"""Linker: find values produced by one step and consumed by later steps.

Example: step 2 returns {"id": "ord_8812"}; step 4 calls /orders/ord_8812/cancel.
That becomes variable `order_id` = step2:$.id, so replays use fresh IDs each time.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tessella.models import Flow

_ID_KEY = re.compile(r"(^id$|_id$|Id$|token|ref$|uuid)", re.I)


def _walk(obj: Any, path: str = "$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _is_candidate(path: str, value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    text = str(value)
    key = path.rsplit(".", 1)[-1]
    if _ID_KEY.search(key):
        return len(text) >= 3
    if isinstance(value, int):
        return value >= 1000
    return len(text) >= 8 and " " not in text


def _var_name(path: str, used: set[str]) -> str:
    base = re.sub(r"\[\d+\]", "", path.rsplit(".", 1)[-1]) or "value"
    base = re.sub(r"\W", "_", base).lower()
    name, i = base, 2
    while name in used:
        name, i = f"{base}_{i}", i + 1
    return name


def link(flow: Flow) -> Flow:
    """Fill flow.variables and replace literal values in later steps with {{name}}."""
    used: set[str] = set()
    for producer in flow.steps:
        for path, value in _walk(producer.response):
            if not _is_candidate(path, value):
                continue
            literal = str(value)
            consumers = [
                s for s in flow.steps[producer.index + 1:]
                if literal in s.url or literal in json.dumps(s.body, default=str)
            ]
            if not consumers:
                continue
            name = _var_name(path, used)
            used.add(name)
            flow.variables[name] = f"step{producer.index}:{path}"
            for s in consumers:
                s.url = s.url.replace(literal, "{{" + name + "}}")
                if s.body is not None:
                    s.body = json.loads(json.dumps(s.body, default=str).replace(literal, "{{" + name + "}}"))
    return flow
