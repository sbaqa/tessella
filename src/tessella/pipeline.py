"""The loop: collect -> cluster/recall -> investigate -> probe -> verdict -> draft.

Each stage reads Cases and returns Cases. Evidence first, model last.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from tessella.cluster import cluster
from tessella.cluster.recall import recall
from tessella.collect import collect_cases, collect_flows
from tessella.collect.findings import at_least
from tessella.config import Settings
from tessella.draft.ticket import duplicates_block, keep_edits, patch_block, ticket
from tessella.integrations import llm
from tessella.memory import decisions_file
from tessella.memory.store import Store
from tessella.models import (
    Action,
    Case,
    Category,
    Decision,
    Evidence,
    EvidenceKind,
    Flow,
    Origin,
    RejectReason,
    Verdict,
)
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
    # signature -> (earlier runs this failure appeared in, earlier runs looked at)
    history: dict[str, tuple[int, int]] = field(default_factory=dict)
    used_model: bool = True
    board_checked: bool = False  # the board itself was asked which failures it already has
    out_of_time: int = 0  # clusters the model did not reach within the time limit


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


def _filed_on_board(settings: Settings, signature: str) -> Decision | None:
    """The Jira ticket already labelled with this failure, for a machine with no memory of
    filing it (a CI runner). Only `tessella ci` asks: on a laptop the store knows, and
    `tessella forget` must be able to bring a failure back. Fails open."""
    j = settings.jira
    if not _can_ask_board(settings):
        return None
    from tessella.integrations import jira

    try:
        found = jira.find_by_signature(j, signature, settings.network.allow)
    except Exception:  # noqa: BLE001 - never re-file or stop a run on doubt
        return None
    if not found:
        return None
    return Decision(action=Action.approve, by="", at=datetime.now(), board="jira",
                    ticket_key=found[0], ticket_url=found[1], linked_existing=True)


def _worth_rechecking(settings: Settings, past: Decision, use_model: bool) -> bool:
    """A rejected failure that came back gets another look, unless it duplicates a ticket."""
    return (use_model and settings.memory.recheck_rejected and past.action == Action.reject
            and past.reason != RejectReason.duplicate)


def _fingerprint(case: Case) -> str:
    """What could be new about a failure: its re-runs, its test and the code diff."""
    import hashlib

    parts = sorted((str(e.kind), e.summary if e.kind == EvidenceKind.rerun else e.excerpt)
                   for e in case.evidence
                   if e.kind in (EvidenceKind.rerun, EvidenceKind.test_source, EvidenceKind.git_diff))
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def _said(past: Decision) -> str:
    reason = f" as {str(past.reason).replace('_', ' ')}" if past.reason else ""
    return f"rejected{reason} on {past.at:%d %b %Y}"


def _rejection(past: Decision, signature: str) -> Evidence:
    """The earlier rejection, for the model to weigh against what is known now."""
    return Evidence(kind=EvidenceKind.memory, summary=f"A reviewer {_said(past)}",
                    excerpt="The evidence has changed since that decision. Judge the failure on "
                            "the evidence as it is now.", ref=signature)


def _stands(result: RunResult, members: list[Case], past: Decision, prior: Case | None) -> None:
    """A recalled decision: the failure keeps its earlier verdict and draft."""
    result.recalled += 1
    result.decisions[members[0].signature] = past
    note = (f"Already filed as {past.ticket_key}." if past.ticket_key and not past.by else
            f"Already {past.action}"
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


def _can_ask_board(settings: Settings) -> bool:
    from tessella import egress

    j = settings.jira
    return settings.board == "jira" and bool(j.url and j.project and j.token) and not egress.active()


def _history(store: Store, signatures, last: int = 20) -> dict[str, tuple[int, int]]:
    """How often each failure showed up in the runs before this one. Nice to have: a store
    that cannot answer costs only this column."""
    try:
        runs = [r["detail"] if isinstance(r["detail"], dict) else {} for r in store.runs()[-last:]]
    except Exception:  # noqa: BLE001
        return {}
    return {s: (sum(s in detail for detail in runs), len(runs)) for s in signatures}


def _filed_nothing(settings: Settings, past) -> bool:
    """An approval that filed no ticket because no board was set up then, now that one is.
    That decision never reached anyone, so the failure is judged again and goes back to the
    queue. Approvals with a ticket and rejections stand."""
    if past.action != Action.approve or past.ticket_key or past.pending:
        return False
    if settings.board == "jira":
        return bool(settings.jira.url and settings.jira.project)
    if settings.board == "github":
        return bool(settings.github.repo)
    return False


def _keep_screenshots(case: Case, settings: Settings) -> None:
    """Copy a case's images next to the store and point the case at the copies. Reports are
    rebuilt on every run (Allure's --clean deletes the old attachments), so a path into a
    report goes stale, and with it the screenshots of a ticket not yet filed."""
    import shutil
    from pathlib import Path

    folder = Path(settings.storage.path).parent / "screenshots" / case.id

    def kept(path: str | None) -> str | None:
        source = Path(path) if path else None
        if source is None or not source.is_file() or folder in source.parents:
            return path
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return str(shutil.copy2(source, folder / source.name))
        except OSError:  # a copy we cannot make costs only the copy
            return path

    case.screenshots = [kept(p) for p in case.screenshots]
    for ev in case.evidence:
        ev.image = kept(ev.image)


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
    draft.body += duplicates_block(draft)


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


def triage(settings: Settings, rerun: bool = True, use_model: bool = True,
           ask_board: bool = False, minutes: float | None = None) -> RunResult:
    """`ask_board` also checks the board for each failure (see _filed_on_board); `minutes`
    stops asking the model after that long, leaving the remaining failures not judged."""
    started, model_before = time.monotonic(), llm.model_seconds()
    store = Store(settings.storage.path)
    decisions_file.load(settings, store)
    cases = [c for c in collect_cases(settings.inputs.reports, settings.redact)
             if c.origin != Origin.probe_finding
             or at_least(c.severity, settings.probe.min_severity)]
    flows = collect_flows(settings.inputs.traces, settings.redact)
    clusters = cluster(cases)
    result = RunResult(cases=cases, clusters=clusters, flows=flows, used_model=use_model,
                       history=_history(store, clusters),
                       board_checked=ask_board and _can_ask_board(settings))
    todo: list[list[Case]] = []
    verdicts: dict[str, Verdict | None] = {}
    rechecks: dict[str, Decision] = {}

    for signature, members in clusters.items():
        lead = members[0]
        past = recall(lead, store)
        if past is not None and _ticket_gone(settings, past):
            store.forget(lead.signature)  # the filed ticket was deleted; re-surface for re-approval
            decisions_file.remove(settings, lead.signature)
            past = None
        if past is not None and _filed_nothing(settings, past):
            store.forget(lead.signature)  # approved with no board: judge it afresh, file on approval
            decisions_file.remove(settings, lead.signature)
            result.reopened[signature] = past
            past = None
        if past is None and ask_board:
            past = _filed_on_board(settings, lead.signature)
        if past is not None and _worth_rechecking(settings, past, use_model):
            rechecks[signature] = past  # judged again below if there is anything new
        elif past is not None:
            # By signature as a fallback: a test that moved or was renamed has a new id but
            # the same failure. Recall never re-saves.
            _stands(result, members, past,
                    store.get_case(lead.id) or store.case_with_signature(lead.signature))
            continue

        todo.append(members)

    # Evidence first, one cluster at a time (re-runs share the environment) ...
    memory = _Memory.open(settings) if use_model else None
    green = settings.inputs.green_ref or store.last_green_commit()
    problems = _preflight(settings) if todo else []
    gathered: list[list[Case]] = []
    settled: set[str] = set()
    for members in todo:
        try:
            _gather(members[0], settings, rerun, green, problems)
            past = rechecks.get(members[0].signature)
            if past is not None:
                prior = store.get_case(members[0].id) or store.case_with_signature(members[0].signature)
                seen = prior and (prior.recheck_fingerprint or _fingerprint(prior))
                if seen == _fingerprint(members[0]):  # nothing new: the rejection stands
                    settled.add(members[0].signature)
                    _stands(result, members, past, prior)
                    continue
                members[0].evidence.append(_rejection(past, members[0].signature))
            if memory:
                memory.recall(members[0], settings, store)
            gathered.append(members)
        except Exception as exc:  # noqa: BLE001 - one broken cluster must never cost the rest
            verdicts[members[0].signature] = _failed(exc)
    todo = [m for m in todo if m[0].signature not in settled]
    # ... then the model, up to `parallel` clusters at once.
    if use_model and gathered:
        _warm_models(settings)
    workers = max(1, settings.models.reader.parallel)
    deadline = None if minutes is None else started + minutes * 60

    def in_time() -> bool:
        return deadline is None or time.monotonic() < deadline

    def judge(members: list[Case]) -> tuple[str, Verdict | None]:
        try:
            examples = memory.examples.get(members[0].signature, "") if memory else ""
            ask = use_model and in_time()
            verdict = _judge(members[0], settings, ask, examples, rerun)
            if use_model and not ask and verdict is None:
                result.out_of_time += 1
            return members[0].signature, verdict
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
        past = rechecks.get(signature)
        if past is not None:
            if verdict and verdict.category in (Category.product_bug, Category.test_bug):
                store.reopen(signature, past)  # new evidence: a person decides again
                decisions_file.remove(settings, signature)
                result.reopened[signature] = past
                for c in members:
                    c.reopened_from = _said(past)
            else:
                prior = store.get_case(members[0].id) or store.case_with_signature(signature)
                _stands(result, members, past, prior)
                if prior and verdict:  # not asked again until the evidence changes again
                    prior.recheck_fingerprint = _fingerprint(members[0])
                    store.save_case(prior)
                continue
        if (use_model and in_time() and settings.models.fixer is not None and verdict
                and verdict.category == Category.test_bug):
            from tessella.draft.patch import fix

            patch = fix(members[0], settings, rerun)  # re-runs: one cluster at a time
        elif (use_model and in_time() and rerun and settings.models.fixer is not None and verdict
              and verdict.category == Category.product_bug and settings.fix.product):
            from tessella.draft import fixer_agent

            patch = fixer_agent.run(members[0], settings)  # sequential: it re-runs tests
        for c in members:
            _keep_screenshots(c, settings)
            c.verdict = verdict
            # Bugs and flaky tests always get a draft for a person to review; nothing is filed
            # without approval. environment/test_data/unknown get none (see CLAUDE.md).
            if verdict and verdict.category in (Category.product_bug, Category.test_bug, Category.flaky):
                c.draft = ticket(c, duplicates)
                if c is members[0]:
                    in_jira = _jira_duplicates(c, settings, use_model)  # one search per cluster
                    if in_jira:
                        result.jira_matches[signature] = in_jira
                _note_duplicates(c.draft, in_jira)
                if patch:
                    c.draft.patch, c.draft.patch_status = patch
                    c.draft.body += patch_block(c.draft, verdict.category)
                earlier = store.get_case(c.id)
                if (earlier and earlier.signature == c.signature and earlier.draft
                        and earlier.draft.original):
                    keep_edits(c, earlier.draft)  # a re-run never overwrites a reviewer's words
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
