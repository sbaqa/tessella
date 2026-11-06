"""`tessella eval`: how well a model judges your failures, measured against a golden file.

A golden file names failures and the category a human gave them:

    cases:
      - match: test_cancel_paid_discounted_order   # part of the test name or id
        category: product_bug
        facts: [refund]                            # optional: words a good diagnosis mentions

Eval collects and clusters the reports, gathers evidence once per cluster, then judges each
cluster `runs` times with every model asked for. It never writes to the store, never uses
recall or few-shot memory (they would grade the team's history, not the model), and skips
the reply cache unless asked, so repeated runs really ask the model again.

Results go to the terminal and are appended to an eval history file with a hash of the
prompts, so a change to a prompt or a model shows up as a change in the numbers. Every model
call the run makes is kept exactly as sent and answered (CallLog) and written to
`.tessella/eval_runs/<UTC time>-<model>.jsonl`, one file per model.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from tessella.config import Settings
from tessella.models import Case, Category, Verdict

TICKET_FIELDS = ("actual_result", "expected_result", "steps_to_reproduce", "suggested_fix")
HISTORY = ".tessella/eval_history.jsonl"
EVAL_RUNS = ".tessella/eval_runs"


@dataclass
class Golden:
    match: str
    category: Category
    facts: list[str] = field(default_factory=list)

    def matches(self, case: Case) -> bool:
        needle = self.match.lower()
        return needle in case.title.lower() or needle in case.test_id.lower()


def load_golden(path: str | Path) -> list[Golden]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    entries = data.get("cases") if isinstance(data, dict) else data
    if not entries:
        raise ValueError(f"{path} has no `cases:` entries")
    out = []
    for e in entries:
        if not e.get("match") or not e.get("category"):
            raise ValueError(f"every golden entry needs `match` and `category`: {e}")
        out.append(Golden(str(e["match"]), Category(e["category"]),
                          [str(f) for f in e.get("facts") or []]))
    return out


def prompt_version() -> str:
    """A short hash of every prompt the reader sees, so history shows when prompts changed."""
    from tessella.verdict import prompts

    text = f"{prompts.CLASSIFY_SYSTEM}\n{prompts.TICKET_SYSTEM}\n{prompts.SYSTEM}"
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@dataclass
class Graded:
    """One golden entry, judged once."""

    match: str
    expected: str
    got: str
    confidence: float
    seconds: float
    fields: float | None = None  # share of ticket fields filled, for correct bug verdicts
    facts: float | None = None  # share of golden facts the diagnosis mentions
    root_cause: str = ""
    # What was judged, for the per-failure table: never part of metrics or history.
    title: str = ""
    test_id: str = ""
    error: str = ""  # the case's failure message, first 200 characters
    evidence_used: list[int] = field(default_factory=list)
    model: str = ""
    run: int = 1

    @property
    def correct(self) -> bool:
        return self.got == self.expected


def grade(g: Golden, v: Verdict | None, seconds: float, case: Case | None = None,
          model: str = "", run: int = 1) -> Graded:
    v = v or Verdict(category=Category.unknown, confidence=0.0, root_cause="no verdict")
    r = Graded(g.match, g.category.value, v.category.value, v.confidence, seconds,
               root_cause=v.root_cause[:200], evidence_used=list(v.evidence_used),
               model=model, run=run)
    if case is not None:
        r.title, r.test_id, r.error = case.title, case.test_id, case.message[:200]
    if r.correct and v.category in (Category.product_bug, Category.test_bug):
        r.fields = sum(1 for f in TICKET_FIELDS if getattr(v, f).strip()) / len(TICKET_FIELDS)
    if g.facts:
        text = " ".join([v.root_cause, *(getattr(v, f) for f in TICKET_FIELDS)]).lower()
        r.facts = sum(1 for f in g.facts if f.lower() in text) / len(g.facts)
    return r


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def metrics(results: list[Graded], runs: int) -> dict:
    """Accuracy (unknown counts as wrong), unknown rate, Brier score of the stated confidence
    (0 is perfect; lower is better calibrated), ticket field completeness, facts hit rate,
    seconds per judgement, and how often all runs of one failure agreed."""
    n = len(results)
    if not n:
        return {"graded": 0}
    by_case: dict[str, list[str]] = {}
    for r in results:
        by_case.setdefault(r.match, []).append(r.got)
    out = {
        "graded": n,
        "accuracy": round(sum(r.correct for r in results) / n, 3),
        "unknown_rate": round(sum(r.got == Category.unknown for r in results) / n, 3),
        "brier": round(sum((r.confidence - r.correct) ** 2 for r in results) / n, 3),
        "field_completeness": _mean([r.fields for r in results if r.fields is not None]),
        "facts_hit_rate": _mean([r.facts for r in results if r.facts is not None]),
        "seconds_per_cluster": _mean([r.seconds for r in results]),
    }
    if runs > 1:
        out["consistency"] = round(sum(len(set(v)) == 1 for v in by_case.values())
                                   / len(by_case), 3)
    return out


@dataclass
class EvalResult:
    model: str
    metrics: dict
    results: list[Graded]
    missing: list[str]  # golden entries no failure in the reports matched
    calls: list[dict] = field(default_factory=list)  # every model call, see CallLog
    calls_path: str = ""  # where write_calls put them


class CallLog:
    """Every model call made while one golden failure is judged, tagged with that failure
    and run. Installed as llm.recorder for the eval run only."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.tag: dict = {}

    def add(self, call: dict) -> None:
        self.calls.append({"at": datetime.now(UTC).isoformat(timespec="seconds"), **self.tag,
                           **call})


def _with_reader(settings: Settings, name: str | None) -> Settings:
    s = settings.model_copy(deep=True)
    if name:
        s.models.reader.name = name
    return s


def run(settings: Settings, golden: list[Golden], models: list[str] | None = None,
        runs: int = 1, rerun: bool = False, use_cache: bool = False,
        clock=time.monotonic) -> list[EvalResult]:
    """Judge every golden failure `runs` times with each model (default: the configured
    reader). Evidence is gathered once and shared, so models are compared on the same input."""
    from tessella import pipeline
    from tessella.cluster import cluster
    from tessella.collect import collect_cases

    base = settings.model_copy(deep=True)
    if not use_cache:
        base.cache.enabled = False
    clusters = cluster(collect_cases(base.inputs.reports, base.redact))
    targets: list[tuple[Golden, Case]] = []
    missing = []
    for g in golden:
        lead = next((members[0] for members in clusters.values()
                     if any(g.matches(c) for c in members)), None)
        if lead is None:
            missing.append(g.match)
        else:
            targets.append((g, lead))
    for lead in {id(lead): lead for _, lead in targets}.values():
        try:
            pipeline._gather(lead, base, rerun)
        except Exception:  # noqa: BLE001, S110 - judge on whatever evidence the cluster has
            pass

    from tessella.integrations import llm

    out = []
    for name in models or [None]:
        s = _with_reader(base, name)
        ms = s.models.reader
        label = f"{ms.provider}/{ms.name}"
        if targets:
            pipeline._warm_models(s)
        results = []
        log = CallLog()
        token = llm.recorder.set(log.add)
        try:
            for n in range(1, runs + 1):
                for g, lead in targets:
                    case = lead.model_copy(deep=True)
                    log.tag = {"golden": g.match, "test": lead.title, "run": n}
                    started = clock()
                    try:
                        v = pipeline._judge(case, s, use_model=True, rerun=rerun)
                    except Exception as exc:  # noqa: BLE001
                        v = pipeline._failed(exc)
                    results.append(grade(g, v, clock() - started, lead, label, n))
        finally:
            llm.recorder.reset(token)
        out.append(EvalResult(label, metrics(results, runs), results, missing, log.calls))
    return out


def calls_for(result: EvalResult, r: Graded) -> list[dict]:
    """The model calls made while judging `r`, in order."""
    return [c for c in result.calls if (c.get("golden"), c.get("run")) == (r.match, r.run)]


def write_calls(results: list[EvalResult], folder: str | Path = EVAL_RUNS) -> list[Path]:
    """One jsonl file per model with every call it was sent, exactly. Sets calls_path."""
    d = Path(folder)
    d.mkdir(parents=True, exist_ok=True)
    at = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    paths = []
    for r in results:
        p = (d / f"{at}-{re.sub(r'[^A-Za-z0-9._-]+', '_', r.model)}.jsonl").resolve()
        with p.open("w") as f:
            for c in r.calls:
                f.write(json.dumps(c) + "\n")
        r.calls_path = str(p)
        paths.append(p)
    return paths


def append_history(results: list[EvalResult], golden_path: str | Path, runs: int,
                   path: str | Path = HISTORY) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    at = datetime.now(UTC).isoformat(timespec="seconds")
    version = prompt_version()
    with p.open("a") as f:
        for r in results:
            f.write(json.dumps({"at": at, "model": r.model, "golden": str(golden_path),
                                "runs": runs, "prompt_version": version, **r.metrics}) + "\n")


def as_json(results: list[EvalResult]) -> str:
    return json.dumps([{"model": r.model, "metrics": r.metrics, "missing": r.missing,
                        "prompt_version": prompt_version(), "calls_file": r.calls_path,
                        "results": [asdict(g) | {"correct": g.correct} for g in r.results]}
                       for r in results], indent=2)


def confusion(results: list[Graded]) -> Counter:
    """(expected, got) pairs that were wrong, most common first."""
    return Counter((r.expected, r.got) for r in results if not r.correct)
