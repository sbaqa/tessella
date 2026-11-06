"""The Case model: one set of data structures shared by every stage.

Readers turn every input (HTML reports, JUnit, traces, HAR) into Cases and Flows.
No later stage touches raw files. Evidence is stored before any model runs,
so every verdict can be checked.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Origin(StrEnum):
    test_failure = "test_failure"  # from a report (triage)
    probe_finding = "probe_finding"  # from replaying a flow (probe)


class Category(StrEnum):
    product_bug = "product_bug"
    test_bug = "test_bug"
    environment = "environment"
    test_data = "test_data"
    flaky = "flaky"
    unknown = "unknown"


class EvidenceKind(StrEnum):
    report = "report"
    rerun = "rerun"
    git_diff = "git_diff"
    log = "log"
    trace = "trace"
    probe_replay = "probe_replay"
    preflight = "preflight"
    artifact = "artifact"
    screenshot = "screenshot"  # what a vision model saw on the failure screenshot
    visual_diff = "visual_diff"  # pixel difference against the last green screenshot
    test_source = "test_source"  # the failing test's own lines, around where it failed
    memory = "memory"  # reads like a failure the team already decided (embedding similarity)


class Evidence(BaseModel):
    kind: EvidenceKind
    summary: str  # one line a human can read
    excerpt: str = ""  # the exact lines (already redacted)
    ref: str | None = None  # file path, commit, log query, trace step
    image: str | None = None  # local path of a screenshot or diff image backing this evidence


class Step(BaseModel):
    """One HTTP call in a flow (from a Playwright trace or a HAR file)."""

    index: int
    method: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any | None = None
    status: int | None = None
    response: Any | None = None


class Flow(BaseModel):
    """The request chain behind a test or a recording."""

    name: str
    steps: list[Step] = Field(default_factory=list)
    # variable name -> where it comes from, e.g. "order_id" -> "step2:$.data.id"
    variables: dict[str, str] = Field(default_factory=dict)


class Verdict(BaseModel):
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause: str
    evidence_used: list[int] = Field(default_factory=list)  # indexes into Case.evidence
    actual_result: str = ""
    expected_result: str = ""
    steps_to_reproduce: str = ""
    environment: str = ""
    suggested_fix: str = ""
    # Share of reader samples that agreed on the category, when the first answer was close
    # to min_confidence and was asked again (see verdict.agent._vote). None: not voted.
    agreement: float | None = None


class DraftKind(StrEnum):
    ticket = "ticket"
    test_patch = "test_patch"
    regression_test = "regression_test"


class Draft(BaseModel):
    kind: DraftKind
    title: str
    body: str
    # The required bug-ticket fields (see CLAUDE.md "Bug ticket drafts").
    description: str = ""
    steps_to_reproduce: str = ""
    actual_result: str = ""
    expected_result: str = ""
    environment: str = ""
    root_cause: str = ""
    suggested_fix: str = ""
    images: list[str] = Field(default_factory=list)  # local screenshot/diff paths, attached on approval
    signature: str = ""  # case signature, filed as a tessella-<signature> label for dedup
    severity: str | None = None
    possible_duplicates: list[str] = Field(default_factory=list)
    # Issues already in Jira that duplicate search matched: {key, summary, status, url, score}
    jira_matches: list[dict] = Field(default_factory=list)
    # Test bugs: a unified diff of the test file from the fixer role, and what checking it
    # showed ("verified: passed 2 of 2 re-runs", "not verified: ...").
    patch: str = ""
    patch_status: str = ""


class Action(StrEnum):
    approve = "approve"
    edit = "edit"
    reject = "reject"
    delete = "delete"


class RejectReason(StrEnum):
    not_a_bug = "not_a_bug"
    test_bug = "test_bug"
    environment = "environment"
    test_data = "test_data"
    flaky = "flaky"
    duplicate = "duplicate"


class Decision(BaseModel):
    action: Action
    reason: RejectReason | None = None
    by: str
    at: datetime
    board: str | None = None
    ticket_key: str | None = None
    ticket_url: str | None = None
    # True when approving linked to a ticket that already carried this signature's label,
    # instead of filing a new one. Each such approval is a duplicate that was not created.
    linked_existing: bool = False


class Case(BaseModel):
    id: str
    origin: Origin
    title: str  # test name or finding name
    # What the rerun command's {test_id} becomes: a pytest node id or a Playwright file:line.
    # Empty when the report doesn't say; rerun then resolves one from the title or skips.
    test_id: str = ""
    message: str = ""  # failure message or finding summary
    stack: str = ""
    source: str = ""  # report file or flow name it came from
    # When the test ran, if the report says (Allure does): log lines are read from this window.
    started_at: datetime | None = None
    failed_at: datetime | None = None
    signature: str = ""  # filled by cluster.signature
    severity: str | None = None  # probe findings: info, low, medium, high or critical
    reproduce: str = ""  # probe findings: the request that shows it (a curl line for Schemathesis)
    screenshots: list[str] = Field(default_factory=list)  # local paths, from the report
    flow: Flow | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    verdict: Verdict | None = None
    draft: Draft | None = None
    decision: Decision | None = None

    @staticmethod
    def make_id(*parts: str) -> str:
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]
