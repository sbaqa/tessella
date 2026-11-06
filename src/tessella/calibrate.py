"""`tessella calibrate`: time real manual investigations, so "time saved" rests on how long
your own team takes without Tessella instead of on a guess.

A sample is one investigation done the old way (report, logs, code) from the raw failure to
either a ticket you could file or a decision that it is not a bug. Five or more samples
give a stable median. A time you already know (from a time tracker, say) can be imported.
"""

from __future__ import annotations

import time
from collections import defaultdict
from statistics import median
from typing import Callable

from tessella.memory.store import Store
from tessella.models import Case

MAX_MINUTES = 1440.0  # one day: anything longer is a typo, not an investigation


def candidates(store: Store) -> list[Case]:
    """Clusters worth timing, best first: not yet timed, then ones the reviewer has not seen
    in the queue (a verdict you have read makes the manual attempt less honest), then ones
    still undecided, then the biggest."""
    groups: dict[str, list[Case]] = defaultdict(list)
    for case in store.all_cases():
        groups[case.signature].append(case)
    done = {s["signature"] for s in store.manual_samples() if s["signature"]}
    decided = {sig for sig, _ in store.all_decisions()}
    seen = store.shown_ids()

    def rank(members: list[Case]) -> tuple:
        sig = members[0].signature
        return (any(m.id in seen for m in members), sig in decided, -len(members), sig)

    ordered = sorted((m for sig, m in groups.items() if sig not in done), key=rank)
    return [members[0] for members in ordered]


def pick(store: Store, signature: str | None = None) -> Case | None:
    """The cluster to time: the one named (full signature or prefix), else the best candidate."""
    options = candidates(store)
    if signature:
        options = [c for c in options if c.signature.startswith(signature)]
    return options[0] if options else None


def describe(case: Case, lines: int = 12) -> str:
    """The raw failure and nothing else: the verdict stays hidden so the timing is honest."""
    stack = "\n".join(case.stack.splitlines()[:lines])
    return f"{case.title}\n\n{case.message[:600]}\n\n{stack}".strip()


def time_manual(case: Case, ask: Callable[[str], str], say: Callable[[str], None],
                clock: Callable[[], float] = time.monotonic) -> tuple[float, bool, str] | None:
    """Run the stopwatch. Returns (seconds, ended_in_ticket, note), or None if cancelled."""
    say(describe(case))
    say("\nInvestigate this the way you would without Tessella: the report, the logs, the code. "
        "Do not open Tessella's verdict for it.")
    ask("Press Enter to start the clock ")
    started = clock()
    answer = ask("Press Enter when you could write the ticket, or know it is not a bug "
                 "(q to cancel) ")
    seconds = clock() - started
    if answer.strip().lower() == "q":
        return None
    filed = ask("Did it end in a ticket? [Y/n] ").strip().lower() not in ("n", "no")
    note = ask("Note (optional) ").strip()
    return seconds, filed, note


def record_minutes(store: Store, minutes: float, filed: bool = True, note: str = "",
                   signature: str | None = None, by: str = "sdet") -> None:
    """Import a time you already know, for example from a time tracker."""
    if not 0 < minutes <= MAX_MINUTES:
        raise ValueError(f"minutes must be more than 0 and at most {MAX_MINUTES:.0f}")
    store.add_manual_sample(minutes * 60, filed, signature=signature, note=note, by=by,
                            source="imported")


def summary(store: Store) -> dict:
    """What has been recorded: per kind, how many samples and their median in minutes."""
    out: dict = {"samples": store.manual_samples()}
    for key, filed in (("ticket", True), ("other", False)):
        secs = [s["seconds"] for s in out["samples"] if s["filed"] == filed]
        out[key] = {"n": len(secs), "median_min": round(median(secs) / 60, 1) if secs else None}
    return out
