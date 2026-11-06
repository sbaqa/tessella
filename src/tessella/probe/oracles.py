"""Oracles: decide whether a mutated replay shows a bug.

Each oracle compares the mutated result with the baseline (the unmutated replay).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tessella.probe.mutators import Mutation


@dataclass
class Result:
    mutation: Mutation
    status: int
    body: Any
    baseline_status: int
    baseline_body: Any
    state_before: Any = None  # read-back of the resource before the step
    state_after: Any = None  # read-back after the step
    created_count: int | None = None  # for duplicate checks


@dataclass
class Finding:
    oracle: str
    severity: str
    title: str
    result: Result


def server_error(r: Result) -> Finding | None:
    if r.status >= 500:
        return Finding("server_error", "high", f"{r.status} on {r.mutation.description}", r)
    return None


def accepted_invalid(r: Result) -> Finding | None:
    invalid = r.mutation.mutator.startswith(("types", "boundaries"))
    if invalid and 200 <= r.status < 300 and r.mutation.value in (None, ..., -1, ""):
        return Finding("accepted_invalid", "medium",
                       f"Invalid input accepted: {r.mutation.description}", r)
    return None


def rejected_but_changed(r: Result) -> Finding | None:
    if 400 <= r.status < 500 and r.state_before is not None and r.state_before != r.state_after:
        return Finding("state_check", "critical",
                       f"Request rejected but state changed: {r.mutation.description}", r)
    return None


def other_user_access(r: Result) -> Finding | None:
    if r.mutation.mutator == "identity" and 200 <= r.status < 300 and r.body == r.baseline_body:
        return Finding("authz", "critical",
                       f"{r.mutation.value} received user A's data: {r.mutation.description}", r)
    return None


def duplicate_effect(r: Result) -> Finding | None:
    repeated = r.mutation.mutator in ("concurrency", "order.repeat")
    if repeated and r.created_count is not None and r.created_count > 1:
        return Finding("duplicate_effect", "critical",
                       f"{r.created_count} effects from {r.mutation.description}", r)
    return None


ORACLES = [server_error, accepted_invalid, rejected_but_changed, other_user_access,
           duplicate_effect]


def judge(r: Result) -> list[Finding]:
    return [f for f in (o(r) for o in ORACLES) if f]
