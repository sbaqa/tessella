"""`tessella export-dataset`: your team's reviewed decisions as a fine-tuning dataset.

One JSON line per decided failure, in the chat format that local fine-tuning tools read
(Unsloth, Axolotl, LLaMA-Factory, MLX-LM): the classification system prompt, the evidence
pack exactly as the reader saw it, and the answer the team stands behind.

The label comes from the decision, not from the model:
- approved (or edited and approved): the verdict's category was right;
- rejected as test_bug, environment, test_data or flaky: the reviewer named the right one;
- rejected as not_a_bug or duplicate, and deletes: left out, since they name no category.

The evidence was redacted when it was collected, but it still holds log lines and test
source, so the file is for training on your own machine. Nothing here sends it anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

from tessella.config import Settings
from tessella.memory.store import Store
from tessella.models import Action, Case, Category, Decision, EvidenceKind
from tessella.verdict import prompts
from tessella.verdict.pack import budget_chars

CORRECTABLE = {"test_bug", "environment", "test_data", "flaky"}


def label(case: Case, d: Decision) -> tuple[Category, bool] | None:
    """(category the team stands behind, whether it corrects the model), or None to skip."""
    if d.action in (Action.approve, Action.edit):
        if case.verdict is None or case.verdict.category == Category.unknown:
            return None
        return case.verdict.category, False
    if d.action == Action.reject and d.reason and d.reason.value in CORRECTABLE:
        corrected = case.verdict is None or case.verdict.category != d.reason.value
        return Category(d.reason.value), corrected
    return None


def record(case: Case, d: Decision, budget: int) -> dict | None:
    picked = label(case, d)
    if picked is None:
        return None
    category, corrected = picked
    case = case.model_copy(deep=True)
    # Memory evidence describes the team's past, which a trained model should not lean on.
    case.evidence = [e for e in case.evidence if e.kind != EvidenceKind.memory]
    v = case.verdict
    cited = [i for i in (v.evidence_used if v else []) if 0 <= i < len(case.evidence)]
    answer = {"category": category.value, "confidence": 0.9,
              "root_cause": (v.root_cause if v and not corrected
                             else f"Reviewed by the team as {category.value}."),
              "evidence_used": cited}
    return {"messages": [{"role": "system", "content": prompts.CLASSIFY_SYSTEM},
                         {"role": "user", "content": prompts.evidence_pack(case, budget)},
                         {"role": "assistant", "content": json.dumps(answer)}],
            "meta": {"signature": case.signature, "test": case.title, "action": d.action.value,
                     "reason": d.reason.value if d.reason else None, "corrected": corrected,
                     "at": d.at.isoformat()}}


def export(settings: Settings, store: Store, out: str | Path, limit: int = 100_000) -> dict:
    """Write the dataset; the latest decision per signature wins. Returns counts."""
    budget = budget_chars(settings.models.reader)
    seen: set[str] = set()
    written = corrected = skipped = 0
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        for case, d in store.recent_decisions(limit=limit):
            if case.signature in seen:
                continue
            seen.add(case.signature)
            r = record(case, d, budget)
            if r is None:
                skipped += 1
                continue
            f.write(json.dumps(r) + "\n")
            written += 1
            corrected += r["meta"]["corrected"]
    return {"written": written, "corrected": corrected, "skipped": skipped}
