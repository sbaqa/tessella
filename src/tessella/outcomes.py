"""What happened to the tickets Tessella filed: fixed, dismissed, a duplicate, or still open.

An approval says a person believed the draft. The ticket's fate says whether the bug was real:
a developer who fixes it confirms it, one who closes it as "won't do" or "not a bug" does not.
`tessella sync` reads each filed ticket's status from Jira (read-only, fail open) and keeps the
result, so the report can show how many tickets were worth filing. The report itself never
calls Jira.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from tessella.config import Settings
from tessella.memory.store import Store

OUTCOMES = ("fixed", "not_a_bug", "duplicate", "other", "open")
LABELS = {"fixed": "Fixed", "not_a_bug": "Closed as not a bug", "duplicate": "Closed as duplicate",
          "other": "Closed, reason unclear", "open": "Still open"}
# Jira resolution names, lower-cased. Teams rename these, so anything unknown is "other"
# instead of being guessed into fixed or dismissed.
_FIXED = {"done", "fixed", "resolved", "complete", "completed", "closed"}
_NOT_A_BUG = {"won't do", "wont do", "won't fix", "wont fix", "invalid", "not a bug",
              "cannot reproduce", "can't reproduce", "works as designed", "rejected",
              "incomplete", "declined", "not an issue"}
_DUPLICATE = {"duplicate"}


def classify(category: str | None, resolution: str | None) -> str:
    """One of OUTCOMES from Jira's status category ('new', 'indeterminate', 'done') and the
    resolution name. A ticket that is not in the 'done' category is open whatever else is set."""
    if category != "done":
        return "open"
    name = (resolution or "").strip().lower()
    if name in _DUPLICATE:
        return "duplicate"
    if name in _NOT_A_BUG:
        return "not_a_bug"
    if name in _FIXED:
        return "fixed"
    return "other"


def sync(settings: Settings, store: Store,
         fetch: Callable[[str], dict | None] | None = None) -> dict:
    """Check every filed Jira ticket and store its outcome. Returns counts.

    `fetch(key)` is injectable for tests; by default it asks Jira. A ticket Jira cannot
    answer for keeps its last recorded outcome and is counted as skipped."""
    from tessella.integrations import jira  # lazy: the report imports this module for its labels

    allow = settings.network.allow
    fetch = fetch or (lambda key: jira.ticket_status(settings.jira, key, allow))
    out = {"checked": 0, "skipped": 0, "changed": 0}
    known = {o["key"]: o["outcome"] for o in store.outcomes()}
    for ticket in store.filed_tickets():
        info = fetch(ticket["key"])
        if not info:
            out["skipped"] += 1
            continue
        outcome = classify(info.get("category"), info.get("resolution"))
        store.save_outcome(ticket["key"], outcome, ticket["signature"], info.get("status"),
                           info.get("resolution"), info.get("created"), info.get("resolved"),
                           at=datetime.now())
        out["checked"] += 1
        if known.get(ticket["key"]) != outcome:
            out["changed"] += 1
    return out
