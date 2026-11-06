"""The investigator's tool menu: what the reader may ask for when its first answer is unsure.

A small model does badly when it may do anything, and well when it picks one item from a
short list. So the menu is fixed, each tool is plain code with no model inside (except the
screenshot close-up, which asks the vision role), and every result is ordinary Evidence
with a number the verdict must cite. Only tools that can work for this failure are offered:
no log tool without logs, no diff outside git, no close-up without a screenshot.
"""

from __future__ import annotations

import threading

from tessella.config import Settings
from tessella.models import Case, Evidence, EvidenceKind

# Re-runs share the test environment: never two at once, even when clusters are judged
# in parallel.
_RERUN_LOCK = threading.Lock()

DESCRIPTIONS = {
    "log_window": "backend log lines over a wider time window that contain the text in "
                  "`argument` (an endpoint, an id, an error word)",
    "git_diff": "what changed since the last green run in the file named in `argument` "
                "(a path relative to the repository)",
    "test_source": "more of the failing test's source: 60 lines around the line number in "
                   "`argument`, or around the failing line if `argument` is empty",
    "screenshot_region": "a close-up of the failure screenshot: `argument` is top, bottom, "
                         "left, right, center or x,y,w,h as fractions",
    "rerun_detail": "run the test once more with detailed output (`argument` is ignored)",
}


def _green(settings: Settings) -> str | None:
    if settings.inputs.green_ref:
        return settings.inputs.green_ref
    from tessella.memory.store import Store

    return Store(settings.storage.path).last_green_commit()


def available(case: Case, settings: Settings, rerun: bool = True) -> dict[str, str]:
    """The tools that can work for this case, with what each one returns. `rerun` False
    (triage --no-rerun) keeps the re-run tool off the menu."""
    from tessella.investigate import git_diff, test_source
    from tessella.investigate.rerun import test_id_for

    repo = settings.inputs.repo
    out = {}
    if settings.logs.path or settings.logs.url:
        out["log_window"] = DESCRIPTIONS["log_window"]
    if _green(settings) and git_diff.is_repo(repo):
        out["git_diff"] = DESCRIPTIONS["git_diff"]
    if test_source.test_source_evidence(case, repo) is not None:
        out["test_source"] = DESCRIPTIONS["test_source"]
    if case.screenshots and settings.models.vision is not None:
        out["screenshot_region"] = DESCRIPTIONS["screenshot_region"]
    if rerun and settings.rerun.command and test_id_for(case, repo):
        out["rerun_detail"] = DESCRIPTIONS["rerun_detail"]
    return out


def run(tool: str, argument: str, case: Case, settings: Settings,
        approver=None) -> Evidence | None:
    """Run one tool. None when it found nothing; the caller tells the model so."""
    argument = (argument or "").strip()
    repo = settings.inputs.repo
    if tool == "log_window":
        from tessella.investigate.logs import log_evidence

        return log_evidence(case, settings.logs, settings.network.allow, settings.redact,
                            needle=argument or None, widen=2.0)
    if tool == "git_diff":
        from tessella.investigate import git_diff

        return git_diff.for_case(case, repo, _green(settings), file=argument or None)
    if tool == "test_source":
        from tessella.investigate import test_source

        ev = test_source.test_source_evidence(case, repo, context=60,
                                              redact_keys=settings.redact)
        if ev and argument.isdigit():
            ev = test_source.around(case, repo, int(argument), 30, settings.redact) or ev
        return ev
    if tool == "screenshot_region":
        from tessella.verdict.vision import describe_region

        return describe_region(case, settings, argument or "center", approver)
    if tool == "rerun_detail":
        from tessella.investigate.rerun import run_once

        with _RERUN_LOCK:
            passed, output = run_once(case, settings.rerun.command, settings.rerun.detail_args,
                                      repo=repo)
        from tessella.redact import redact_text

        state = {True: "passed", False: "failed", None: "could not run"}[passed]
        return Evidence(kind=EvidenceKind.rerun,
                        summary=f"Re-run with detailed output: {state}",
                        excerpt=redact_text(output, settings.redact),
                        ref=settings.rerun.detail_args)
    return None
