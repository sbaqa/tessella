"""
Ask the configured model for a verdict, then enforce the evidence rule.
A verdict that cites no evidence, cites evidence that does not exist, or is below
min_confidence becomes `unknown` and is shown in the queue, never drafted.

The reader replies against a JSON schema (enforced by local servers), with one repair
retry. By default it classifies first and writes ticket fields in a second call, only for
product and test bugs. A verdict near min_confidence is sampled again and the majority wins.
"""

from __future__ import annotations
import json
from collections import Counter
from typing import Literal
from pydantic import BaseModel, create_model
from tessella.config import ModelSettings, Settings
from tessella.models import Case, Category, EvidenceKind, Verdict
from tessella.verdict import prompts
from tessella.verdict.budget import Budget, BudgetExceeded
from tessella.verdict.pack import budget_chars

VOTE_TEMPERATURE = 0.7  # enough spread to show whether the first answer was a coin toss


def enforce(v: Verdict, case: Case, min_confidence: float) -> Verdict:
    """The evidence rule: cite real evidence, with enough confidence, or be `unknown`."""
    valid = [i for i in v.evidence_used if 0 <= i < len(case.evidence)]
    if not valid or v.confidence < min_confidence:
        return Verdict(category=Category.unknown, confidence=v.confidence,
                       root_cause=f"Not enough evidence: {v.root_cause}", evidence_used=valid,
                       agreement=v.agreement)
    return v.model_copy(update={"evidence_used": valid})


def parse(raw: str, case: Case, min_confidence: float) -> Verdict:
    try:
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        v = Verdict.model_validate(prompts.coerce(data))
    except (ValueError, TypeError):
        return Verdict(category=Category.unknown, confidence=0.0, root_cause="Unparseable model reply")
    return enforce(v, case, min_confidence)


def _data_classes(case: Case):
    from tessella.integrations.llm import DataClass

    classes = [DataClass.evidence_excerpts]
    if any(e.kind == EvidenceKind.test_source for e in case.evidence):
        classes.append(DataClass.failing_test_file)
    if any(e.kind == EvidenceKind.git_diff for e in case.evidence):
        classes.append(DataClass.diff_hunk)
    return classes


def _ask(case: Case, settings: Settings, reader: ModelSettings, approver,
         temperature: float = 0, examples: str = "") -> tuple[Verdict | None, int, str]:
    """One verdict from the reader: classification only (two-step) or everything (one-step).
    `examples` (similar reviewed failures, cluster.recall.examples_text) follow the pack."""
    from tessella.integrations import llm

    pack = prompts.evidence_pack(case, budget_chars(reader) - len(examples))
    if examples:
        pack = f"{pack}\n{examples}\n"
    system, schema = ((prompts.CLASSIFY_SYSTEM, prompts.Classification) if reader.two_step
                      else (prompts.SYSTEM, prompts.FullVerdict))
    reply, raw, tokens = llm.complete_json("reader", settings, system, pack, _data_classes(case),
                                           schema, coerce=prompts.coerce, approver=approver,
                                           temperature=temperature)
    return (Verdict(**reply.model_dump()) if reply else None), tokens, raw


def _vote(case: Case, settings: Settings, reader: ModelSettings, first: Verdict,
          budget: Budget, approver, examples: str = "") -> Verdict:
    """Ask again when the first answer sits near the threshold. The majority category wins;
    its confidence becomes the mean of (the agreeing replies' confidence, the agreement)."""
    if reader.votes <= 1 or abs(first.confidence - reader.min_confidence) > reader.vote_margin:
        return first
    ballots = [first]
    try:
        for _ in range(reader.votes - 1):
            v, tokens, _raw = _ask(case, settings, reader, approver, VOTE_TEMPERATURE, examples)
            budget.spend(tokens)
            if v is not None:
                ballots.append(v)
    except BudgetExceeded:
        pass  # vote with the ballots we have
    if len(ballots) == 1:
        return first
    counts = Counter(b.category for b in ballots)
    top = max(counts.values())
    winner = first.category if counts[first.category] == top else counts.most_common(1)[0][0]
    agreeing = [b for b in ballots if b.category == winner]
    agreement = len(agreeing) / len(ballots)
    mean_conf = sum(b.confidence for b in agreeing) / len(agreeing)
    return agreeing[0].model_copy(update={"confidence": round((mean_conf + agreement) / 2, 2),
                                          "agreement": round(agreement, 2)})


def unsure(v: Verdict, reader: ModelSettings) -> bool:
    return v.category == Category.unknown or v.confidence < reader.min_confidence + reader.vote_margin


def _choice_model(tools: list[str]) -> type[BaseModel]:
    """A reply schema whose `tool` may only be one of the offered tools, or done."""
    names = Literal.__getitem__((*tools, "done"))
    return create_model("ToolChoice", tool=(names, ...), argument=(str, ""), reason=(str, ""))


def _investigate(case: Case, settings: Settings, reader: ModelSettings, v: Verdict,
                 budget: Budget, approver, examples: str = "", rerun: bool = True) -> Verdict:
    """While the reader is unsure, let it ask for one more piece of evidence at a time from
    a fixed menu (investigate/tools.py), then read everything again. Each round is two calls
    within the cluster's budget; every tool result is numbered evidence like the rest."""
    from tessella.integrations import llm
    from tessella.investigate import tools

    tried: list[str] = []
    used: set[str] = set()
    for _ in range(reader.investigate_steps):
        if not unsure(v, reader):
            break
        menu = {k: d for k, d in tools.available(case, settings, rerun).items() if k not in used}
        if not menu:
            break
        system = prompts.INVESTIGATE_SYSTEM.format(
            menu="\n".join(f"- {k}: {d}" for k, d in menu.items()))
        prompt = prompts.investigate_prompt(
            prompts.evidence_pack(case, budget_chars(reader)), v.category, v.confidence,
            v.root_cause, tried)
        try:
            choice, _raw, tokens = llm.complete_json(
                "reader", settings, system, prompt, _data_classes(case), _choice_model(list(menu)),
                approver=approver)
            budget.spend(tokens)
            if choice is None or choice.tool == "done":
                break
            used.add(choice.tool)
            ev = tools.run(choice.tool, choice.argument, case, settings, approver)
            if ev is None:
                tried.append(f"{choice.tool}({choice.argument!r}) found nothing")
                continue
            case.evidence.append(ev)
            tried.append(f"{choice.tool}({choice.argument!r}) added evidence "
                         f"[{len(case.evidence) - 1}]")
            again, tokens, _raw = _ask(case, settings, reader, approver, examples=examples)
            budget.spend(tokens)
            if again is not None:
                v = again
        except BudgetExceeded:
            break
    return v


def _ticket_fields(case: Case, settings: Settings, v: Verdict, budget: Budget,
                   approver) -> Verdict:
    """Second step: the ticket fields, written from the cited evidence and the diagnosis.
    A failed or unparseable reply leaves them empty; draft.ticket fills explicit fallbacks."""
    from tessella.integrations import llm

    reader = settings.models.reader
    prompt = prompts.ticket_prompt(prompts.evidence_pack(case, budget_chars(reader)),
                                   v.category, v.root_cause)
    try:
        fields, _raw, tokens = llm.complete_json(
            "reader", settings, prompts.TICKET_SYSTEM, prompt, _data_classes(case),
            prompts.TicketFields, coerce=prompts.coerce, approver=approver)
        budget.spend(tokens)
    except BudgetExceeded:
        return v
    except Exception:  # noqa: BLE001 - the verdict stands without ticket wording
        return v
    return v.model_copy(update=fields.model_dump()) if fields else v


def decide(case: Case, settings: Settings, budget: Budget | None = None,
           approver=None, examples: str = "", rerun: bool = True) -> Verdict:
    """Ask the reader role. The evidence pack holds redacted log and stack excerpts (and the
    failing test's source), so a cloud reader is refused unless the data policy lets them leave."""
    from tessella.integrations.llm import PolicyViolation

    reader = settings.models.reader
    budget = budget or Budget(max_steps=reader.max_steps_per_cluster
                              + 2 * max(0, reader.investigate_steps))
    try:
        first, tokens, raw = _ask(case, settings, reader, approver, examples=examples)
        budget.spend(tokens)
        if first is None:
            return Verdict(category=Category.unknown, confidence=0.0,
                           root_cause=f"Unparseable model reply: {raw[:200]}")
        first = _investigate(case, settings, reader, first, budget, approver, examples, rerun)
        v = enforce(_vote(case, settings, reader, first, budget, approver, examples), case,
                    reader.min_confidence)
    except PolicyViolation as exc:
        return Verdict(category=Category.unknown, confidence=0.0,
                       root_cause=f"Not analysed: {exc}")
    except Exception as exc:  # noqa: BLE001 - server down, timeout, no model: this cluster only
        return Verdict(category=Category.unknown, confidence=0.0,
                       root_cause=f"Not analysed: the reader model call failed "
                                  f"({type(exc).__name__}: {str(exc)[:200]})")
    if reader.two_step and v.category in (Category.product_bug, Category.test_bug):
        v = _ticket_fields(case, settings, v, budget, approver)
    return v


def heuristic(case: Case) -> Verdict | None:
    """Cheap rules that need no model, applied before decide()."""
    reruns = [e for e in case.evidence if e.kind == "rerun"]
    if reruns and "flaky" in reruns[-1].summary:
        return Verdict(category=Category.flaky, confidence=0.9,
                       root_cause=reruns[-1].summary, evidence_used=[case.evidence.index(reruns[-1])])
    if any(e.kind == "preflight" for e in case.evidence):
        idx = next(i for i, e in enumerate(case.evidence) if e.kind == "preflight")
        return Verdict(category=Category.environment, confidence=0.9,
                       root_cause=case.evidence[idx].summary, evidence_used=[idx])
    return _certain_finding(case)


_EXPECTED = {
    "server_error": "The API answers with a 4xx validation error (or success), never a 5xx.",
    "state_check": "A resource the API created or deleted is found, or gone, as the API said.",
    "authz": "The request is refused (401/403) and no other user's data is returned.",
}


def _certain_finding(case: Case) -> Verdict | None:
    """An imported Schemathesis finding whose check leaves no doubt (a 5xx, a broken
    create/delete, another user's data) or a ZAP alert a person already confirmed in ZAP:
    a product bug without asking a model. Everything else imported goes to the reader."""
    from tessella.collect.findings import CERTAIN

    if case.origin != "probe_finding":
        return None
    for i, e in enumerate(case.evidence):
        if e.kind != "probe_replay" or not e.ref:
            continue
        oracles = set(e.ref.removeprefix("schemathesis:").split(",")) \
            if e.ref.startswith("schemathesis:") else set()
        confirmed = e.ref.startswith("zap:") and e.ref.endswith("confidence=4")
        sure = sorted(oracles & CERTAIN)
        if sure or confirmed:
            return Verdict(category=Category.product_bug, confidence=0.9,
                           root_cause=e.summary, evidence_used=[i],
                           actual_result=case.message,
                           expected_result=_EXPECTED.get(sure[0], "") if sure else "",
                           steps_to_reproduce=case.reproduce)
    return None
