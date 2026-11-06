"""Mutators: what to change in step N. Each yields Mutation objects; replay applies them.

Focus is functional correctness: boundaries, wrong types, identity, step order, parallel sends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from tessella.models import Flow, Step


@dataclass
class Mutation:
    mutator: str  # e.g. "boundaries.number"
    step: int
    location: str  # "body:quantity", "identity", "order", "concurrency"
    value: Any = None
    description: str = ""


BOUNDARY_NUMBERS = [0, -1, 2**31, 0.0001]
BOUNDARY_STRINGS = ["", " ", "a" * 10_000, "  padded  ", "emoji 🚀", "مرحبا"]


def boundaries(flow: Flow, step: Step, limit: int = 8) -> Iterator[Mutation]:
    if not isinstance(step.body, dict):
        return
    count = 0
    for key, value in step.body.items():
        pool = BOUNDARY_NUMBERS if isinstance(value, (int, float)) else BOUNDARY_STRINGS
        for v in pool:
            if count >= limit:
                return
            count += 1
            yield Mutation("boundaries", step.index, f"body:{key}", v, f"{key} = {str(v)[:20]!r}")


def types(flow: Flow, step: Step) -> Iterator[Mutation]:
    if not isinstance(step.body, dict):
        return
    for key in step.body:
        yield Mutation("types.null", step.index, f"body:{key}", None, f"{key} = null")
        yield Mutation("types.missing", step.index, f"body:{key}", ..., f"{key} removed")


def identity(flow: Flow, step: Step, others: list[str]) -> Iterator[Mutation]:
    for who in others:
        yield Mutation("identity", step.index, "identity", who, f"step {step.index} as {who}")


def order(flow: Flow, step: Step) -> Iterator[Mutation]:
    if step.index > 0:
        yield Mutation("order.skip_previous", step.index, "order", step.index - 1,
                       f"skip step {step.index - 1}")
    yield Mutation("order.repeat", step.index, "order", 2, f"send step {step.index} twice")


def concurrency(flow: Flow, step: Step, parallel: int = 10) -> Iterator[Mutation]:
    if step.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        yield Mutation("concurrency", step.index, "concurrency", parallel,
                       f"step {step.index} sent {parallel} times in parallel")


def all_mutations(flow: Flow, enabled: list[str], identities: list[str]) -> Iterator[Mutation]:
    for step in flow.steps:
        if "boundaries" in enabled:
            yield from boundaries(flow, step)
        if "types" in enabled:
            yield from types(flow, step)
        if "identity" in enabled:
            yield from identity(flow, step, identities)
        if "order" in enabled:
            yield from order(flow, step)
        if "concurrency" in enabled:
            yield from concurrency(flow, step)
