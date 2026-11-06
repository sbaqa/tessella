"""Bug ticket drafts, with every field CLAUDE.md requires: description, steps to
reproduce, actual/expected result, environment, root cause, suggested fix, screenshots."""

from __future__ import annotations

import re

from tessella.models import Case, Draft, DraftKind

_BROWSER = re.compile(r"\[(chromium|firefox|webkit)\]")


def _environment(case: Case) -> str:
    """A best-effort environment line from the case itself, used when the model
    didn't supply one. Framework/browser/API-vs-UI, plus the report it came from."""
    bits: list[str] = []
    browser = _BROWSER.search(case.title)
    is_ui = bool(browser) or bool(case.screenshots)
    bits.append("UI test" if is_ui else "API/service test")
    if browser:
        bits.append(f"browser: {browser.group(1)}")
    if any(e.kind == "trace" for e in case.evidence):
        bits.append("Playwright trace captured")
    if case.source:
        bits.append(f"report: {case.source}")
    return "; ".join(bits)


def ticket(case: Case, duplicates: list[str] | None = None) -> Draft:
    assert case.verdict is not None
    v = case.verdict
    cited = [case.evidence[i] for i in v.evidence_used]

    description = f"{case.title} failed: {v.actual_result or case.message}"
    root_cause = v.root_cause

    steps_to_reproduce = ""
    if case.flow and case.flow.steps:
        steps_to_reproduce = "\n".join(
            f"{s.index + 1}. {s.method} {s.url} -> {s.status}" for s in case.flow.steps
        )
    if not steps_to_reproduce and case.reproduce:
        steps_to_reproduce = f"Send this request:\n```\n{case.reproduce}\n```"
    steps_to_reproduce = steps_to_reproduce or v.steps_to_reproduce or "Not available — see evidence below."

    actual_result = v.actual_result or case.message
    expected_result = v.expected_result or "Not determined by the model."
    environment = v.environment or _environment(case)
    suggested_fix = v.suggested_fix or "Not determined by the model."

    evidence = "\n\n".join(f"**{e.kind}** — {e.summary}\n```\n{e.excerpt[:1500]}\n```" for e in cited)
    # Screenshots stay local paths in the draft; the review queue shows them. Attaching them
    # to Jira is a separate, policy-controlled step (TODO v1: redaction before upload).
    images = [case.screenshots[-1]] if case.screenshots else []
    images += [e.image for e in cited if e.image and e.image not in images]
    shots = "\n".join(f"- {p}" for p in images)

    body = (
        f"**Description:** {description}\n\n"
        f"**Steps to reproduce:**\n{steps_to_reproduce}\n\n"
        f"**Actual result:** {actual_result}\n\n"
        f"**Expected result:** {expected_result}\n\n"
        f"**Environment:** {environment}\n\n"
        f"**Root cause (suspected):** {root_cause}\n\n"
        f"**Suggested fix:** {suggested_fix}\n\n"
        + (f"**Screenshots (local):**\n{shots}\n\n" if shots else "")
        + f"**Evidence:**\n\n{evidence}\n\n"
        f"_Confidence {v.confidence:.0%}. Drafted by Tessella, approved by an SDET._"
    )
    return Draft(
        kind=DraftKind.ticket,
        title=f"[{case.origin}] {case.message[:90] or case.title}",
        body=body,
        description=description,
        steps_to_reproduce=steps_to_reproduce,
        actual_result=actual_result,
        expected_result=expected_result,
        environment=environment,
        root_cause=root_cause,
        suggested_fix=suggested_fix,
        images=images,
        signature=case.signature,
        severity=case.severity,
        possible_duplicates=duplicates or [],
    )
