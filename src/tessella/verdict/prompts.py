"""Prompt templates and reply schemas. The model sees a compact evidence pack (verdict/pack),
never raw logs or files.

Two-step (the default, best for small local models): CLASSIFY_SYSTEM asks only for the
category, confidence, root cause and cited evidence; TICKET_SYSTEM then writes the ticket
fields for product and test bugs. One-step: SYSTEM asks for everything at once. The reply
schemas use plain strings and literals (no $defs), so every local server can enforce them.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from tessella.models import Case
from tessella.verdict.pack import evidence_pack  # noqa: F401  (re-exported for callers)

CategoryName = Literal["product_bug", "test_bug", "environment", "test_data", "flaky", "unknown"]


class Classification(BaseModel):
    category: CategoryName
    confidence: float = Field(ge=0.0, le=1.0)
    root_cause: str
    evidence_used: list[int]


class TicketFields(BaseModel):
    actual_result: str
    expected_result: str
    steps_to_reproduce: str
    environment: str
    suggested_fix: str


class FullVerdict(Classification, TicketFields):
    pass


CLASSIFY_SYSTEM = """You are a senior SDET investigating a failed test or a probe finding.
Classify it as exactly one of:
- product_bug: the application behaves wrongly; the test is right
- test_bug: the test itself is wrong (stale locator, wrong assertion, too-tight timeout)
- environment: infrastructure was down or misconfigured
- test_data: the data the test relied on was missing or wrong
- flaky: it passes and fails without a code change
- unknown: the evidence does not support a confident answer
A probe finding comes from Schemathesis or OWASP ZAP: product_bug when the evidence shows the
application misbehaving (a response that breaks its own API schema counts); unknown when it
reads like a scanner false positive.
Use ONLY the numbered evidence. Cite every evidence number you relied on in evidence_used.
root_cause: one or two plain sentences naming the most likely cause.
confidence: 0 to 1; below 0.6 means you are guessing.
Reply with JSON only: {"category": ..., "confidence": ..., "root_cause": "...",
"evidence_used": [numbers]}"""

TICKET_SYSTEM = """You write the fields of a bug ticket from a diagnosed test failure, in plain
language a non-engineer could read. Use ONLY the numbered evidence and the diagnosis.
Use an empty string for any field the evidence does not support; never guess.
- actual_result: what actually happened
- expected_result: what should have happened instead
- steps_to_reproduce: numbered steps to trigger it, as one string with line breaks
- environment: where it reproduces (test framework, browser, API vs UI, service/host)
- suggested_fix: a short, concrete recommendation for where/how to fix it
Reply with JSON only: {"actual_result": "...", "expected_result": "...",
"steps_to_reproduce": "...", "environment": "...", "suggested_fix": "..."}"""

SYSTEM = """You are a senior SDET investigating a failed test or a probe finding.
Classify it as one of: product_bug, test_bug, environment, test_data, flaky, unknown.
Use ONLY the numbered evidence below. Cite the evidence numbers you relied on.
If the evidence does not support a confident answer, return "unknown".
A probe finding comes from Schemathesis or OWASP ZAP: product_bug when the evidence shows the
application misbehaving (a response that breaks its own API schema counts); unknown when it
reads like a scanner false positive.
Also fill in these bug-ticket fields, in plain language a non-engineer could read.
Use an empty string for any the evidence does not support — never guess:
- actual_result: what actually happened
- expected_result: what should have happened instead
- steps_to_reproduce: numbered steps to trigger it, inferred from the test and evidence
- environment: where it reproduces (test framework, browser, API vs UI, service/host) as the evidence shows
- suggested_fix: a short, concrete recommendation for where/how to fix it
Reply with JSON only: {"category": ..., "confidence": 0-1, "root_cause": "...",
"evidence_used": [numbers], "actual_result": "...", "expected_result": "...",
"steps_to_reproduce": "...", "environment": "...", "suggested_fix": "..."}"""


INVESTIGATE_SYSTEM = """You are a senior SDET investigating a failed test. Your first reading of
the evidence was unsure. Pick ONE tool that would best settle the category, with its
argument, or "done" if no tool would help. Tools:
{menu}
Reply with JSON only: {{"tool": "...", "argument": "...", "reason": "one short sentence"}}"""


def investigate_prompt(pack: str, category: str, confidence: float, root_cause: str,
                       tried: list[str]) -> str:
    lines = [pack, f"Your first reading: {category} ({confidence:.2f}). {root_cause}"]
    if tried:
        lines.append("Already tried: " + "; ".join(tried))
    return "\n".join(lines)


def redacted_summary(case: Case) -> str:
    """What may leave the network for a cloud fixer: no log lines, no screenshots,
    only the verdict and one-line evidence summaries written by the local reader."""
    lines = [f"Test: {case.title}"]
    if case.verdict:
        lines += [f"Category: {case.verdict.category}", f"Root cause: {case.verdict.root_cause}"]
    lines += [f"- {ev.kind}: {ev.summary}" for ev in case.evidence]
    return "\n".join(lines)


def ticket_prompt(pack: str, category: str, root_cause: str) -> str:
    return f"{pack}\nDiagnosis: {category}. {root_cause}"


_NUMBER = re.compile(r"\d+")


def coerce(data: dict) -> dict:
    """Repair the near-misses small models make before validating: a list where a string
    belongs (steps as an array), a percentage for a confidence, evidence numbers as
    strings like "[2]" or "evidence 2". Anything else is left for validation to reject."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for field in TicketFields.model_fields:
        value = out.get(field)
        if isinstance(value, list):
            items = [str(x).strip() for x in value if str(x).strip()]
            numbered = field == "steps_to_reproduce" and not any(_NUMBER.match(x) for x in items)
            out[field] = "\n".join(f"{n}. {x}" if numbered else x for n, x in enumerate(items, 1))
        elif value is None:
            out[field] = ""
    conf = out.get("confidence")
    if isinstance(conf, str):
        conf = conf.strip().rstrip("%")
        try:
            conf = float(conf)
        except ValueError:
            pass
    if isinstance(conf, (int, float)) and 1 < conf <= 100:
        conf = conf / 100
    if conf is not None:
        out["confidence"] = conf
    used = out.get("evidence_used")
    if isinstance(used, (str, int)):
        used = [used]
    if isinstance(used, list):
        out["evidence_used"] = [int(n) for x in used for n in _NUMBER.findall(str(x))]
    if isinstance(out.get("category"), str):
        out["category"] = out["category"].strip().lower().replace(" ", "_").replace("-", "_")
    return out
