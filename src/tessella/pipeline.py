"""The loop: collect -> cluster/recall -> investigate -> probe -> verdict -> draft.

Each stage reads Cases and returns Cases. Evidence first, model last.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from tessella.cluster import cluster
from tessella.cluster.recall import recall
from tessella.collect import collect_cases, collect_flows
from tessella.collect.findings import at_least
from tessella.config import Settings
from tessella.draft.ticket import ticket
from tessella.integrations import llm
from tessella.memory.store import Store
from tessella.models import Action, Case, Category, Decision, Flow, Origin, Verdict
from tessella.verdict import agent


@dataclass
class RunResult:
    cases: list[Case] = field(default_factory=list)
    clusters: dict[str, list[Case]] = field(default_factory=dict)
    flows: list[Flow] = field(default_factory=list)
    recalled: int = 0
    # signature -> the earlier decision it was recalled from (recalled clusters only)
    decisions: dict[str, Decision] = field(default_factory=dict)
    # signature -> issues already in Jira that read like its new draft (see draft/duplicates)
    jira_matches: dict[str, list[dict]] = field(default_factory=dict)
    # signature -> an approval that filed nothing (no board then), now judged again
    reopened: dict[str, Decision] = field(default_factory=dict)


def _ticket_gone(settings: Settings, past) -> bool:
    """True only when a previously filed Jira ticket is confirmed deleted (404). Fails open:
    if Jira isn't the board, isn't configured, or can't be reached, returns False so triage
    keeps the existing recall and never re-files on doubt (offline/CI stay unaffected)."""
    if past.board != "jira" or not past.ticket_key:
        return False
    j = settings.jira
    if settings.board != "jira" or not (j.url and j.project and j.token):
        return False
    from tessella.integrations import jira

    try:
        return not jira.issue_exists(j, past.ticket_key, settings.network.allow)
    except Exception:
        return False


def _filed_nothing(settings: Settings, past) -> bool:
    """An approval that filed no ticket because no board was set up then, now that one is.
    That decision never reached anyone, so the failure is judged again and goes back to the
    queue. Approvals with a ticket and rejections stand."""
    if past.action != Action.approve or past.ticket_key:
        return False
    if settings.board == "jira":
        return bool(settings.jira.url and settings.jira.project)
    if settings.board == "github":
        return bool(settings.github.repo)
    return False


def _visual_diff(case: Case) -> None:
    """A pixel diff against the last green screenshot. No model needed."""
    if not case.screenshots:
        return
    try:
        from tessella.investigate.visual import visual_evidence

        ev = visual_evidence(case)
        if ev:
            case.evidence.append(ev)
    except ImportError:  # Pillow not installed: pip install tessella[vision]
        pass


def _vision_reading(case: Case, settings: Settings) -> None:
    """What the vision role sees on the failure screenshot, as cited evidence."""
    if not case.screenshots or settings.models.vision is None:
        return
    from tessella.integrations.llm import PolicyViolation
    from tessella.verdict.vision import describe

    try:
        ev = describe(case, settings)
        if ev:
            case.evidence.append(ev)
    except PolicyViolation:
        pass  # screenshots may not leave the network; the visual diff still counts
    except Exception:  # noqa: BLE001
        pass  # vision server down or timed out: the reader still judges the other evidence


def _visual_evidence(case: Case, settings: Settings, use_model: bool) -> None:
    """Screenshot evidence: a pixel diff against the last green run (no model needed),
    then a description from the vision role when one is configured."""
    _visual_diff(case)
    if use_model:
        _vision_reading(case, settings)


def _record_run(store: Store, result: RunResult, duration_s: float | None = None,
                model_s: float | None = None, git_commit: str | None = None) -> None:
    """Remember this run for `tessella report`. History is a nice-to-have: a failure to
    write it must never fail a triage (or a CI build)."""
    try:
        drafted = sum(1 for members in result.clusters.values() if members[0].draft)
        store.record_run(
            failures=len(result.cases), clusters=len(result.clusters), recalled=result.recalled,
            drafted=drafted, detail={sig: len(m) for sig, m in result.clusters.items()},
            duration_s=duration_s, model_s=model_s, git_commit=git_commit)
    except Exception:
        pass


def _gather(lead: Case, settings: Settings, rerun: bool, green: str | None = None,
            preflight: list | None = None) -> None:
    """Deterministic evidence, one cluster at a time: re-runs share the test environment,
    so they never run in parallel. `green` is the commit to diff against; `preflight` the
    problems found before the run, which every cluster carries."""
    if preflight:
        lead.evidence.extend(e.model_copy() for e in preflight)
    if lead.origin == Origin.probe_finding:
        return  # imported from Schemathesis/ZAP: nothing to re-run, no test source or stack
    if rerun:
        from tessella.investigate.rerun import rerun as do_rerun

        lead.evidence.append(do_rerun(lead, settings.rerun.command, settings.rerun.attempts,
                                       repo=settings.inputs.repo))
    _visual_diff(lead)
    from tessella.investigate.test_source import test_source_evidence

    source = test_source_evidence(lead, settings.inputs.repo, redact_keys=settings.redact)
    if source:
        lead.evidence.append(source)
    if settings.logs.path or settings.logs.url:
        from tessella.investigate.logs import log_evidence

        try:
            ev = log_evidence(lead, settings.logs, settings.network.allow, settings.redact)
        except Exception:  # noqa: BLE001 - a log server that is down costs only this evidence
            ev = None
        if ev:
            lead.evidence.append(ev)
    if green:
        from tessella.investigate import git_diff

        ev = git_diff.for_case(lead, settings.inputs.repo, green)
        if ev:
            lead.evidence.append(ev)


def _judge(lead: Case, settings: Settings, use_model: bool, examples: str = "",
           rerun: bool = True) -> Verdict | None:
    """Model evidence and the verdict: rules first, model last. Safe to run in parallel."""
    if use_model:
        _vision_reading(lead, settings)
    verdict = agent.heuristic(lead)
    if verdict is None and use_model:
        verdict = agent.decide(lead, settings, examples=examples, rerun=rerun)
    return verdict


@dataclass
class _Memory:
    """Near-duplicate recall and few-shot examples, when an embedder role is configured."""

    index: object
    model: str
    vectors: dict[str, list[float]] = field(default_factory=dict)
    examples: dict[str, str] = field(default_factory=dict)
    duplicates: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def open(cls, settings: Settings) -> _Memory | None:
        ms = settings.models.embedder
        if ms is None:
            return None
        from tessella.memory.vectors import VectorIndex

        return cls(VectorIndex(settings.memory.path), f"{ms.provider}/{ms.name}")

    def recall(self, lead: Case, settings: Settings, store: Store) -> None:
        """Embed the lead, cite a near-duplicate decision, pick examples. Fails open: memory
        only ever adds evidence, so an embedder that is down changes nothing else."""
        from tessella.cluster.recall import (
            duplicate_evidence,
            duplicate_keys,
            examples_text,
            similar_decided,
        )
        from tessella.integrations.llm import DataClass
        from tessella.memory.vectors import text_for

        try:
            vector = llm.embed(settings, [text_for(lead)], [DataClass.evidence_excerpts])[0]
        except Exception:  # noqa: BLE001
            return
        self.vectors[lead.signature] = vector
        m = settings.memory
        matches = similar_decided(vector, lead, store, self.index, self.model)
        ev = duplicate_evidence(matches, m.similar_threshold)
        if ev:
            lead.evidence.append(ev)
        self.examples[lead.signature] = examples_text(matches, m.examples, m.example_threshold)
        self.duplicates[lead.signature] = duplicate_keys(matches, m.similar_threshold)

    def remember(self, lead: Case) -> None:
        vector = self.vectors.get(lead.signature)
        if vector:
            self.index.add(lead.signature, lead.id, vector, self.model)


def _jira_duplicates(lead: Case, settings: Settings, use_model: bool) -> list[dict]:
    """Issues already in Jira that read like this drafted failure. Fails open."""
    from tessella.draft.duplicates import jira_duplicates

    try:
        return jira_duplicates(lead, settings, use_model)
    except Exception:  # noqa: BLE001 - a missed duplicate must never cost the run
        return []


def _note_duplicates(draft, issues: list[dict]) -> None:
    """List near-duplicate Jira issues on the draft, for the reviewer and in the filed body."""
    if not issues:
        return
    draft.jira_matches = issues
    draft.possible_duplicates += [i["key"] for i in issues if i["key"] not in draft.possible_duplicates]
    lines = "\n".join(f"- {i['key']} ({i['status'] or 'unknown status'}): {i['summary']}"
                      for i in issues)
    draft.body += f"\n\n**Possible duplicates already in Jira:**\n{lines}"


def _preflight(settings: Settings) -> list:
    """Problems with the environment, checked once per run before any cluster."""
    if not settings.preflight.urls:
        return []
    from tessella.investigate import preflight

    try:
        return preflight.check(settings.preflight.urls, settings.network.allow)
    except Exception:  # noqa: BLE001 - a broken check must not cost the run
        return []


def _failed(exc: Exception) -> Verdict:
    return Verdict(category=Category.unknown, confidence=0.0,
                   root_cause=f"Not analysed: investigation failed "
                              f"({type(exc).__name__}: {str(exc)[:200]})")


def _warm_models(settings: Settings) -> None:
    """Load local models before the first cluster needs them (Ollama only, no data sent)."""
    from tessella.integrations import local_llm

    for role in ("reader", "vision"):
        ms = settings.models.get(role)
        if ms is not None and not ms.is_cloud:
            local_llm.warm(ms, settings.network.allow)


def triage(settings: Settings, rerun: bool = True, use_model: bool = True) -> RunResult:
    started, model_before = time.monotonic(), llm.model_seconds()
    store = Store(settings.storage.path)
    cases = [c for c in collect_cases(settings.inputs.reports, settings.redact)
             if c.origin != Origin.probe_finding
             or at_least(c.severity, settings.probe.min_severity)]
    flows = collect_flows(settings.inputs.traces, settings.redact)
    clusters = cluster(cases)
    result = RunResult(cases=cases, clusters=clusters, flows=flows)
    todo: list[list[Case]] = []
    verdicts: dict[str, Verdict | None] = {}

    for signature, members in clusters.items():
        lead = members[0]
        past = recall(lead, store)
        if past is not None and _ticket_gone(settings, past):
            store.forget(lead.signature)  # the filed ticket was deleted; re-surface for re-approval
            past = None
        if past is not None and _filed_nothing(settings, past):
            store.forget(lead.signature)  # approved with no board: judge it afresh, file on approval
            result.reopened[signature] = past
            past = None
        if past is not None:
            result.recalled += 1
            result.decisions[signature] = past
            # keeps its original verdict/draft; recall never re-saves. By signature as a
            # fallback: a test that moved or was renamed has a new id but the same failure.
            prior = store.get_case(lead.id) or store.case_with_signature(lead.signature)
            note = (f"Already {past.action}"
                    + (f" as {past.reason}" if past.reason else "")
                    + (f" → {past.ticket_key}" if past.ticket_key else "")
                    + f" on {past.at:%Y-%m-%d}.")
            for c in members:
                if prior and prior.verdict:
                    # Keep the real classification (e.g. product_bug) and draft; just note it was judged.
                    c.verdict = prior.verdict.model_copy(
                        update={"root_cause": f"{note} {prior.verdict.root_cause}"})
                    c.draft = prior.draft
                else:
                    c.verdict = Verdict(category=Category.unknown, confidence=1.0, root_cause=note)
            continue

        todo.append(members)

    # Evidence first, one cluster at a time (re-runs share the environment) ...
    memory = _Memory.open(settings) if use_model else None
    green = settings.inputs.green_ref or store.last_green_commit()
    problems = _preflight(settings) if todo else []
    gathered: list[list[Case]] = []
    for members in todo:
        try:
            _gather(members[0], settings, rerun, green, problems)
            if memory:
                memory.recall(members[0], settings, store)
            gathered.append(members)
        except Exception as exc:  # noqa: BLE001 - one broken cluster must never cost the rest
            verdicts[members[0].signature] = _failed(exc)
    # ... then the model, up to `parallel` clusters at once.
    if use_model and gathered:
        _warm_models(settings)
    workers = max(1, settings.models.reader.parallel)

    def judge(members: list[Case]) -> tuple[str, Verdict | None]:
        try:
            examples = memory.examples.get(members[0].signature, "") if memory else ""
            return members[0].signature, _judge(members[0], settings, use_model, examples, rerun)
        except Exception as exc:  # noqa: BLE001
            return members[0].signature, _failed(exc)

    if workers == 1:
        verdicts.update(judge(m) for m in gathered)
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            verdicts.update(pool.map(judge, gathered))

    for members in todo:
        signature = members[0].signature
        verdict = verdicts.get(signature)
        duplicates = memory.duplicates.get(signature) if memory else None
        patch, in_jira = None, []
        if (use_model and settings.models.fixer is not None and verdict
                and verdict.category == Category.test_bug):
            from tessella.draft.patch import fix

            patch = fix(members[0], settings, rerun)  # re-runs: one cluster at a time
        elif (use_model and rerun and settings.models.fixer is not None and verdict
              and verdict.category == Category.product_bug and settings.fix.product):
            from tessella.draft import fixer_agent

            patch = fixer_agent.run(members[0], settings)  # sequential: it re-runs tests
        for c in members:
            c.verdict = verdict
            # Both product bugs and test bugs are actionable tickets someone must fix;
            # flaky/environment/test_data/unknown are not filed (see CLAUDE.md).
            if verdict and verdict.category in (Category.product_bug, Category.test_bug):
                c.draft = ticket(c, duplicates)
                if c is members[0]:
                    in_jira = _jira_duplicates(c, settings, use_model)  # one search per cluster
                    if in_jira:
                        result.jira_matches[signature] = in_jira
                _note_duplicates(c.draft, in_jira)
                if patch:
                    c.draft.patch, c.draft.patch_status = patch
                    if c.draft.patch:
                        what = ("Proposed fix" if verdict.category == Category.product_bug
                                else "Proposed test patch")
                        c.draft.body += (f"\n\n**{what}** ({c.draft.patch_status}):"
                                         f"\n```diff\n{c.draft.patch}```")
            store.save_case(c)
        if memory:
            try:
                memory.remember(members[0])
            except Exception:  # noqa: BLE001, S110 - the index is derived data, never worth a failed run
                pass
    from tessella.investigate.git_diff import head

    _record_run(store, result, duration_s=time.monotonic() - started,
                model_s=llm.model_seconds() - model_before, git_commit=head(settings.inputs.repo))
    return result
