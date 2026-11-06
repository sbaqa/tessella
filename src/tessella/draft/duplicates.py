"""Jira duplicate search: before a reviewer files a draft, look for issues already on the board
that describe the same failure in other words (filed by hand, or before Tessella was used).

The exact case, an issue carrying this signature's label, is handled on approval by
`jira.create_or_link`. This finds the near ones and only lists them on the draft as possible
duplicates: a reviewer decides, nothing is skipped or linked automatically."""

from __future__ import annotations

import re

from tessella.cluster.signature import normalize
from tessella.config import Settings
from tessella.models import Case

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
# Words every failure has; searching or scoring on them only adds noise.
_COMMON = frozenset("""
the and for with from that this was were not but are has have had into when then than
test tests failed failure fails error errors assert assertion assertionerror expected
actual got none true false null should would could product bug origin
""".split())


def words(text: str) -> list[str]:
    """Distinctive lowercase words, in order of first appearance."""
    seen: dict[str, None] = {}
    for w in _WORD.findall(text or ""):
        w = w.lower()
        if w not in _COMMON and w not in seen:
            seen[w] = None
    return list(seen)


def _failure_text(case: Case) -> str:
    d = case.draft
    parts = [case.title, normalize(case.message or case.stack)]
    if d:
        parts += [d.description, d.actual_result]
    return "\n".join(p for p in parts if p)


def _query_words(case: Case, limit: int = 6) -> list[str]:
    """The longest distinctive words of the test name and error: few enough for one JQL
    text query, specific enough not to match the whole board."""
    ws = words(f"{case.title}\n{normalize(case.message or case.stack)}")
    return sorted(ws, key=len, reverse=True)[:limit]


def text_score(failure: str, summary: str) -> float:
    """Share of the issue summary's words that the failure also uses (0..1). At least two
    shared words, so a one-word summary never matches everything."""
    s, f = set(words(summary)), set(words(failure))
    shared = s & f
    return len(shared) / len(s) if s and len(shared) >= 2 else 0.0


def _embedding_scores(settings: Settings, failure: str, summaries: list[str]) -> list[float] | None:
    """Cosine similarity with the embedder role, when one is configured. None: score by text."""
    if settings.models.embedder is None:
        return None
    from tessella.integrations import llm
    from tessella.memory.vectors import cosine

    try:
        vectors = llm.embed(settings, [failure, *summaries], [llm.DataClass.evidence_excerpts])
    except Exception:  # noqa: BLE001 - embedder down or refused by policy: fall back to text
        return None
    return [cosine(vectors[0], v) for v in vectors[1:]]


def jira_duplicates(case: Case, settings: Settings, use_model: bool = True) -> list[dict]:
    """Open or closed Jira issues that read like this failure, best first:
    [{key, summary, status, url, score}]. [] when Jira is not the board, not configured,
    offline, or unreachable (fails open, like every Jira call during triage)."""
    from tessella import egress

    j = settings.jira
    if (settings.board != "jira" or not j.search_duplicates
            or not (j.url and j.project and j.token) or egress.active()):
        return []
    from tessella.integrations import jira

    allow = settings.network.allow
    found = jira.search_text(j, _query_words(case), allow) + jira.search_tessella(j, allow)
    issues = list({i["key"]: i for i in found}.values())  # one entry per issue
    if not issues:
        return []
    failure = _failure_text(case)
    scores = _embedding_scores(settings, failure, [i["summary"] for i in issues]) if use_model else None
    threshold = j.duplicate_threshold
    if scores is None:
        scores = [text_score(failure, i["summary"]) for i in issues]
        threshold = j.duplicate_text_threshold
    found = [{**i, "score": round(s, 2)} for i, s in zip(issues, scores) if s >= threshold]
    return sorted(found, key=lambda i: i["score"], reverse=True)[:j.duplicate_limit]
