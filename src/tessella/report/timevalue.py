"""Net time saved, counted from what Tessella recorded instead of assumed.

    net = manual work avoided - time people spent reviewing

Manual work avoided is counted only for work that is finished and that Tessella really did:
approved clusters it had a verdict for, and repeats of a failure that an earlier decision
already covered. A rejected draft earns nothing: the person did that work themselves. An
open draft earns nothing until someone has reviewed it.

Each input has a source, shown next to it on the page:
  measured    taken from timestamps Tessella recorded
  calibrated  the median of manual investigations your team timed (`tessella calibrate`)
  assumed     a default, used only until one of the above exists

Machine time and avoided duplicate tickets are reported but never turned into hours: nobody
waits on a CI run, and a duplicate has no defensible price.
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import median

# A person who leaves a card open for an hour was not reviewing for an hour. Gaps longer than
# this are cut to it, so idle time cannot inflate the review cost (or hide it).
REVIEW_CAP_S = 600.0
# Used only while nothing better exists. Minutes.
ASSUMED = {"ticket_min": 30.0, "other_min": 15.0, "review_min": 3.0}
SMALL_SAMPLE = 5


def review_intervals(timeline: list[dict], cap_s: float = REVIEW_CAP_S) -> list[float | None]:
    """Seconds a person spent on each decision, in timeline order; None if it was not timed.

    The clock for a decision starts when its card was first shown, or at that reviewer's
    previous decision if that came later: cards are shown together, so the time between two
    decisions is the real time on the second one. Negative gaps count as zero."""
    last_by_reviewer: dict[str, datetime] = {}
    out: list[float | None] = []
    for row in timeline:
        decided, shown = row["decided_at"], row["shown_at"]
        previous = last_by_reviewer.get(row["by"])
        last_by_reviewer[row["by"]] = decided
        if shown is None:
            out.append(None)
            continue
        start = max(shown, previous) if previous else shown
        out.append(min(max((decided - start).total_seconds(), 0.0), cap_s))
    return out


def baseline(samples: list[dict]) -> dict:
    """Minutes a manual investigation takes, from timed samples where there are any.
    Two kinds: ones that ended in a ticket (investigate + write up) and ones that did not."""
    out: dict = {}
    for key, filed in (("ticket", True), ("other", False)):
        seconds = [s["seconds"] for s in samples if s["filed"] == filed]
        out[f"n_{key}"] = len(seconds)
        if seconds:
            out[f"{key}_min"] = round(median(seconds) / 60, 1)
            out[f"{key}_src"] = "calibrated"
        else:
            out[f"{key}_min"] = ASSUMED[f"{key}_min"]
            out[f"{key}_src"] = "assumed"
    return out


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


def compute(clusters: list[dict], recalled: int, timeline: list[dict], samples: list[dict],
            runs: list[dict], duplicates: int, cap_s: float = REVIEW_CAP_S) -> dict:
    """`clusters` carry state ('open'|'approved'|'rejected'), has_draft and category."""
    base = baseline(samples)
    judged = [c for c in clusters if c["category"] != "none"]
    approved_ticket = sum(1 for c in judged if c["state"] == "approved" and c["has_draft"])
    approved_other = sum(1 for c in judged if c["state"] == "approved" and not c["has_draft"])
    rejected = sum(1 for c in judged if c["state"] == "rejected")
    still_open = sum(1 for c in clusters if c["state"] == "open")

    # reviews
    intervals = review_intervals(timeline, cap_s)
    timed = [i for i in intervals if i is not None]
    untimed = len(intervals) - len(timed)
    if timed:
        each_min, review_src = median(timed) / 60, ("measured" if not untimed else "partly measured")
    else:
        each_min, review_src = ASSUMED["review_min"], ("assumed" if intervals else "none")
    spent_min = sum(timed) / 60 + untimed * each_min
    rejected_min = sum((i / 60 if i is not None else each_min)
                       for i, row in zip(intervals, timeline) if row["action"] == "reject")

    avoided_ticket = approved_ticket * base["ticket_min"]
    avoided_other = approved_other * base["other_min"]
    avoided_repeat = recalled * base["other_min"]
    avoided = avoided_ticket + avoided_other + avoided_repeat

    assumed = []
    if approved_ticket and base["ticket_src"] == "assumed":
        assumed.append("time to investigate and write up a ticket")
    if (approved_other or recalled) and base["other_src"] == "assumed":
        assumed.append("time to investigate a failure")
    if review_src in ("assumed", "partly measured"):
        assumed.append("review time" if review_src == "assumed"
                       else f"review time for {untimed} untimed review{'s' if untimed != 1 else ''}")

    timed_runs = [r for r in runs if r.get("duration_s") is not None]
    has_data = bool(approved_ticket or approved_other or recalled or intervals)
    return {
        "has_data": has_data,
        "baseline": base,
        "counts": {"approved_ticket": approved_ticket, "approved_other": approved_other,
                   "rejected": rejected, "recalled": recalled, "open": still_open},
        "avoided": {"ticket_min": avoided_ticket, "other_min": avoided_other,
                    "repeat_min": avoided_repeat, "total_min": avoided},
        "review": {"n": len(intervals), "timed": len(timed), "untimed": untimed,
                   "each_min": each_min, "src": review_src,
                   "timed_total_min": sum(timed) / 60, "total_min": spent_min,
                   "median_s": median(timed) if timed else None,
                   "p90_s": _p90(timed) if timed else None,
                   "rejected_min": rejected_min, "cap_min": cap_s / 60},
        "net_min": avoided - spent_min,
        "assumed": assumed,
        "compute": {"runs": len(runs), "timed_runs": len(timed_runs),
                    "seconds": sum(r["duration_s"] for r in timed_runs),
                    "model_seconds": sum(r.get("model_s") or 0 for r in timed_runs)},
        "duplicates_avoided": duplicates,
    }
