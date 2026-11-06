"""Shrink: reduce a failing flow to the fewest steps that still fail (delta debugging).

`still_fails(steps)` must reset state and replay the given steps; it is supplied by
probe.replay so this module stays pure and easy to test.
"""

from __future__ import annotations

from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def ddmin(items: Sequence[T], still_fails: Callable[[list[T]], bool]) -> list[T]:
    """Zeller's ddmin: returns a 1-minimal subsequence for which still_fails is True."""
    current = list(items)
    n = 2
    while len(current) >= 2:
        size = max(1, len(current) // n)
        chunks = [current[i:i + size] for i in range(0, len(current), size)]
        reduced = False
        for i in range(len(chunks)):
            complement = [x for j, c in enumerate(chunks) if j != i for x in c]
            if complement and still_fails(complement):
                current, n, reduced = complement, max(n - 1, 2), True
                break
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current
