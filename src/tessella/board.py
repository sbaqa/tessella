"""What a triage means for the board, in one sentence and one line per cluster: which
failures are already filed (no new ticket needed), which probably are (a near-duplicate in
Jira), which are new drafts waiting for review, and which need a person's call.

Read from a finished RunResult; it calls nothing, so triage, CI and JSON all say the same."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from tessella.models import Action, Case, Category


class Status(StrEnum):
    on_board = "on_board"  # filed earlier: this failure's ticket exists, no new ticket
    likely_on_board = "likely_on_board"  # new draft, but an issue in Jira reads like it
    new = "new"  # a new bug, drafted and waiting for review
    decided = "decided"  # decided earlier without a ticket (rejected, or approved before a board)
    your_call = "your_call"  # flaky/environment/test data/unknown: shown, never filed
    pending = "pending"  # no verdict (--no-model) or not analysed


_PAST = {Action.approve: "approved", Action.reject: "rejected", Action.edit: "edited",
         Action.delete: "deleted"}


@dataclass
class Row:
    signature: str
    status: Status
    tickets: list[str] = field(default_factory=list)  # existing ticket keys, best first
    note: str = ""
    category: str = ""  # your_call rows: flaky, environment, test_data or unknown


def classify(result) -> list[Row]:
    """One Row per cluster. `result` needs `clusters`, `decisions` (signature -> the decision
    that stands) and `jira_matches`: a RunResult, or the report's view of the store."""
    rows = []
    for sig, members in result.clusters.items():
        lead: Case = members[0]
        past = result.decisions.get(sig)
        if past is not None:
            if past.ticket_key and past.action == Action.approve:
                rows.append(Row(sig, Status.on_board, [past.ticket_key],
                                f"already filed as {past.ticket_key}: no new ticket needed"))
            else:
                what = _PAST.get(past.action, str(past.action))
                if past.reason:
                    what += f" as {str(past.reason).replace('_', ' ')}"
                when = f" by {past.by} on {past.at:%d %b %Y}" if past.by else f" on {past.at:%d %b %Y}"
                tail = ("; it stays closed (tessella forget " + sig[:8] + " to review it again)"
                        if past.action == Action.reject else
                        "; it will be filed when the board can be reached" if past.pending else
                        "; no ticket was filed because no board was set up then. If it fails this "
                        "way again with a board set up, triage judges it afresh")
                rows.append(Row(sig, Status.decided, note=f"{what}{when}{tail}"))
            continue
        reopened = getattr(result, "reopened", {}).get(sig)
        before = ""
        if reopened and reopened.action == Action.reject:
            before = (f"rejected as {str(reopened.reason).replace('_', ' ')} on {reopened.at:%d %b %Y}, "
                      "but new evidence changed the verdict: ")
        elif reopened:
            before = (f"approved by {reopened.by} on {reopened.at:%d %b %Y} with no board, so nothing "
                      "was filed; judged again now: ")
        v = lead.verdict
        matches = result.jira_matches.get(sig) or (lead.draft.jira_matches if lead.draft else [])
        if lead.draft and matches:
            best = matches[0]
            rows.append(Row(sig, Status.likely_on_board, [m["key"] for m in matches],
                            before + f"probably already {best['key']} ({best['summary'][:60]}, "
                            f"score {best['score']}): mark it duplicate unless it differs"))
        elif lead.draft:
            rows.append(Row(sig, Status.new, note=before + "new bug: draft ready, approve to file it"))
        elif v and v.category not in (Category.product_bug, Category.test_bug):
            rows.append(Row(sig, Status.your_call, note=before + _call(v), category=str(v.category)))
        else:
            rows.append(Row(sig, Status.pending, note="not analysed yet"))
    return rows


_CALL = {
    Category.flaky: "flaky: re-runs disagree, so nothing to file. Mark it flaky to close it, "
                    "or fix the test's timing",
    Category.environment: "environment problem, not a code bug: nothing to file. Mark it "
                          "as environment once the environment is fixed",
    Category.test_data: "test data problem: nothing to file. Mark it as test data, or fix the data",
    Category.unknown: "not enough evidence to call it: open it, read the evidence, and decide",
}


def _call(v) -> str:
    """What a person should do with a failure that is not a bug ticket, in plain words."""
    return f"{_CALL.get(v.category, str(v.category))} ({v.confidence:.0%} sure)"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def headline(result, rows: list[Row] | None = None) -> str:
    """One plain sentence a reviewer can act on, e.g. '5 bugs: 3 already on the board
    (KAN-7, KAN-8, KAN-9), no new tickets needed; 2 new, drafted and waiting for review.'"""
    rows = classify(result) if rows is None else rows
    by = {s: [r for r in rows if r.status == s] for s in Status}
    parts = []
    known = by[Status.on_board]
    if known:
        keys = ", ".join(r.tickets[0] for r in known)
        parts.append(f"{len(known)} already on the board ({keys}), no new "
                     f"{'ticket' if len(known) == 1 else 'tickets'} needed")
    likely = by[Status.likely_on_board]
    if likely:
        keys = ", ".join(r.tickets[0] for r in likely)
        parts.append(f"{len(likely)} probably already on the board ({keys}): "
                     "check the draft before filing")
    if by[Status.new]:
        parts.append(f"{len(by[Status.new])} new, drafted with all details and waiting for review")
    if by[Status.your_call]:
        kinds = Counter(r.category.replace("_", " ") or "unknown" for r in by[Status.your_call])
        parts.append(", ".join(f"{n} {k}" for k, n in kinds.most_common())
                     + " (not a bug ticket, nothing filed): your call")
    if by[Status.decided]:
        parts.append(f"{len(by[Status.decided])} decided earlier without a ticket")
    if by[Status.pending]:
        parts.append(f"{len(by[Status.pending])} not analysed yet")
    total = _plural(len(rows), "failure cluster")
    if not parts:
        return f"{total}."
    return f"{total}: " + "; ".join(parts) + "."
