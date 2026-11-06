"""
Recall: has the team already judged this signature, or one like it?
Exact signature match first (cheap, certain, and it skips the investigation). Then, with an
`embedder` model configured, embedding similarity (memory.vectors) for near-duplicates: a
failure whose signature changed (a refactor moved the line) but that reads like one the team
already decided. A similar match never skips anything: it becomes cited evidence and a
possible duplicate on the draft, and the most similar reviewed failures are shown to the
reader as calibration examples.
"""

from __future__ import annotations

from dataclasses import dataclass

from tessella.memory.store import Store
from tessella.models import Case, Decision, Evidence, EvidenceKind


def recall(case: Case, store: Store) -> Decision | None:
    """Return the most recent decision for this signature, if any."""
    return store.last_decision(case.signature)


@dataclass
class Similar:
    case: Case  # the earlier failure, as it was stored (with its verdict)
    decision: Decision
    similarity: float


def similar_decided(vector: list[float], case: Case, store: Store, index, model: str,
                    k: int = 10) -> list[Similar]:
    """Earlier failures with a recorded decision, most similar first."""
    out = []
    for signature, case_id, sim in index.nearest(vector, model, k=k, exclude=case.signature):
        decision = store.last_decision(signature)
        prior = store.get_case(case_id) if decision else None
        if decision and prior:
            out.append(Similar(prior, decision, sim))
    return out


def _outcome(d: Decision) -> str:
    if d.action == "approve":
        return "approved" + (f" as {d.ticket_key}" if d.ticket_key else "")
    return "rejected" + (f" as {d.reason}" if d.reason else "")


def duplicate_evidence(matches: list[Similar], threshold: float) -> Evidence | None:
    """Evidence that this failure reads like one already decided, above `threshold`."""
    if not matches or matches[0].similarity < threshold:
        return None
    m = matches[0]
    v = m.case.verdict
    lines = [f"Earlier failure: {m.case.title}", f"Message: {m.case.message}"]
    if v:
        lines.append(f"Its verdict: {v.category}. {v.root_cause}")
    excerpt = "\n".join(lines)
    return Evidence(kind=EvidenceKind.memory,
                    summary=f"Reads like a failure the team {_outcome(m.decision)} on "
                            f"{m.decision.at:%Y-%m-%d} (similarity {m.similarity:.2f})",
                    excerpt=excerpt, ref=m.case.signature)


def examples_text(matches: list[Similar], k: int, threshold: float) -> str:
    """The reviewed failures most like this one, for the reader's prompt. They are how the
    team judged similar cases, not evidence about this one, so they carry no numbers."""
    picked = [m for m in matches if m.similarity >= threshold][:k]
    if not picked:
        return ""
    lines = ["Similar failures your team already reviewed (for calibration only; do not cite):"]
    for m in picked:
        v = m.case.verdict
        lines.append(f"- {m.case.title}: {m.case.message[:200]}")
        if v:
            lines.append(f"  model said {v.category}: {v.root_cause[:200]}")
        lines.append(f"  team {_outcome(m.decision)}")
    return "\n".join(lines)


def duplicate_keys(matches: list[Similar], threshold: float) -> list[str]:
    return [m.decision.ticket_key for m in matches
            if m.similarity >= threshold and m.decision.ticket_key]
