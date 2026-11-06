"""Replay: run steps 1..N-1 for real, then step N with a mutation, and collect a Result.

TODO(v2) — the core of probe:
1. resolve {{variables}} from live responses of earlier steps (flow.variables paths)
2. apply the Mutation to step N (body field, identity, order, concurrency)
3. read the resource back before/after when a matching GET exists (state check)
4. count created resources for repeat/parallel sends (duplicate check)
5. return oracles.Result for oracles.judge()
All HTTP goes through net.client(allow) so the allowlist is enforced.
"""

from __future__ import annotations

from tessella.models import Flow
from tessella.probe.mutators import Mutation
from tessella.probe.oracles import Result


def baseline(flow: Flow, base_url: str, allow: list[str]) -> list[Result]:
    raise NotImplementedError("v2: replay the flow unchanged to get baseline responses")


def run_mutation(flow: Flow, mutation: Mutation, base_url: str, allow: list[str]) -> Result:
    raise NotImplementedError("v2: replay prefix, apply mutation, read back state")


def still_fails(flow: Flow, steps: list[int], base_url: str, allow: list[str]) -> bool:
    raise NotImplementedError("v2: used by shrink.ddmin to confirm a smaller repro")
