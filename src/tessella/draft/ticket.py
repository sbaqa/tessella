"""Bug ticket drafts, with every field CLAUDE.md requires: description, steps to
reproduce, actual/expected result, environment, root cause, suggested fix, screenshots."""

from __future__ import annotations

import re

from tessella.models import Case, Category, Draft, DraftKind

_BROWSER = re.compile(r"\[(chromium|firefox|webkit)\]")
# What a reviewer can change in the queue before filing.
EDITABLE = ("title", "description", "steps_to_reproduce", "actual_result", "expected_result",
            "environment", "root_cause", "suggested_fix")
_TITLE_LIMIT = 255  # Jira's limit for a summary


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

    flaky = v.category == Category.flaky
    description = f"{case.title} failed: {v.actual_result or case.message}"
    root_cause = v.root_cause

    steps_to_reproduce = ""
    if case.flow and case.flow.steps:
        steps_to_reproduce = "\n".join(
            f"{s.index + 1}. {s.method} {s.url} -> {s.status}" for s in case.flow.steps
        )
    if not steps_to_reproduce and case.reproduce:
        steps_to_reproduce = f"Send this request:\n```\n{case.reproduce}\n```"
    if flaky and not v.steps_to_reproduce:
        steps_to_reproduce = steps_to_reproduce or (
            f"1. Run this test several times: {case.test_id or case.title}\n"
            "2. Compare a passing run with a failing run.")
    steps_to_reproduce = steps_to_reproduce or v.steps_to_reproduce or "Not available — see evidence below."

    actual_result = v.actual_result or (f"{case.message} ({v.root_cause})" if flaky else case.message)
    expected_result = v.expected_result or ("The test passes on every run." if flaky
                                            else "Not determined by the model.")
    environment = v.environment or _environment(case)
    suggested_fix = v.suggested_fix or (
        "Find what differs between passing and failing runs (timing, shared data, test order), "
        "then make the test wait for it or isolate it." if flaky else "Not determined by the model.")

    # Screenshots stay local paths in the draft; the review queue shows them. Attaching them
    # to Jira is a separate, policy-controlled step (TODO v1: redaction before upload).
    images = [case.screenshots[-1]] if case.screenshots else []
    images += [e.image for e in cited if e.image and e.image not in images]
    draft = Draft(
        kind=DraftKind.ticket,
        title=f"[{'flaky' if flaky else case.origin}] {case.message[:90] or case.title}",
        body="",
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
    draft.body = render(case, draft)
    return draft


def duplicates_block(draft: Draft) -> str:
    """The issues already in Jira that read like this draft, as the filed body lists them."""
    if not draft.jira_matches:
        return ""
    lines = "\n".join(f"- {i['key']} ({i.get('status') or 'unknown status'}): {i['summary']}"
                      for i in draft.jira_matches)
    return f"\n\n**Possible duplicates already in Jira:**\n{lines}"


def patch_block(draft: Draft, category: Category | str) -> str:
    """The checked fix, as the filed body shows it."""
    if not draft.patch:
        return ""
    what = "Proposed fix" if category == Category.product_bug else "Proposed test patch"
    return f"\n\n**{what}** ({draft.patch_status}):\n```diff\n{draft.patch}```"


def render(case: Case, draft: Draft) -> str:
    """The body filed to the board, from the draft's fields as they stand now."""
    v = case.verdict
    assert v is not None
    cited = [case.evidence[i] for i in v.evidence_used]
    evidence = "\n\n".join(f"**{e.kind}** — {e.summary}\n```\n{e.excerpt[:1500]}\n```" for e in cited)
    shots = "\n".join(f"- {p}" for p in draft.images)
    who = "edited and approved" if draft.edited_by else "approved"
    return (
        f"**Description:** {draft.description}\n\n"
        f"**Steps to reproduce:**\n{draft.steps_to_reproduce}\n\n"
        f"**Actual result:** {draft.actual_result}\n\n"
        f"**Expected result:** {draft.expected_result}\n\n"
        f"**Environment:** {draft.environment}\n\n"
        f"**Root cause (suspected):** {draft.root_cause}\n\n"
        f"**Suggested fix:** {draft.suggested_fix}\n\n"
        + (f"**Screenshots (local):**\n{shots}\n\n" if shots else "")
        + f"**Evidence:**\n\n{evidence}\n\n"
        f"_Confidence {v.confidence:.0%}. Drafted by Tessella, {who} by an SDET._"
        + duplicates_block(draft) + patch_block(draft, v.category)
    )


def _clean(name: str, text: str, current: str) -> str:
    """A reviewer's text as it is stored. A required field left empty says so, never blank."""
    text = text.replace("\r\n", "\n").strip()
    if name == "title":
        return " ".join(text.split())[:_TITLE_LIMIT] or current
    return text or "Not available"


def edit(case: Case, fields: dict[str, str], by: str) -> None:
    """Apply a reviewer's changes to the draft and rebuild the body that approving files.
    Remembers Tessella's text for each changed field; typing it back counts as unchanged."""
    draft = case.draft
    assert draft is not None
    for name in EDITABLE:
        if name not in fields:
            continue
        current = getattr(draft, name)
        value = _clean(name, fields[name], current)
        if value == current:
            continue
        before = draft.original.setdefault(name, current)
        setattr(draft, name, value)
        if value == before:
            del draft.original[name]
    draft.edited_by = by if draft.original else ""
    draft.body = render(case, draft)


def undo_edits(case: Case) -> None:
    """Back to the text Tessella wrote."""
    draft = case.draft
    assert draft is not None
    for name, text in draft.original.items():
        setattr(draft, name, text)
    draft.original, draft.edited_by = {}, ""
    draft.body = render(case, draft)


def keep_edits(case: Case, earlier: Draft) -> None:
    """A new triage wrote a fresh draft for a failure a reviewer already edited: their
    changes stay, every field they did not touch takes the fresh text."""
    draft = case.draft
    assert draft is not None
    for name in earlier.original:
        draft.original[name] = getattr(draft, name)
        setattr(draft, name, getattr(earlier, name))
    draft.edited_by = earlier.edited_by
    draft.body = render(case, draft)
