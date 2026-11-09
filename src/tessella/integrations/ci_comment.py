"""One short PR/MR comment per run: clusters, verdicts, link to the review queue.

Posted to the pull request (GitHub Actions) or merge request (GitLab CI) the job runs for,
through net.client, with a CI token from the environment. Each run edits Tessella's earlier
comment instead of adding another. Posting is best-effort: it never fails the build."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from tessella import net
from tessella.models import Case, Category

# Hidden in the rendered comment; finds Tessella's own comment on the next run.
MARKER = "<!-- tessella-ci-summary -->"
_ACTIONABLE = (Category.product_bug, Category.test_bug, Category.flaky)


# Where this action's docs live, linked when a runner has no model.
_HOW_TO_ADD_A_MODEL = "https://github.com/sbaqa/tessella#running-in-ci"
_ROWS, _DETAILS = 50, 15  # a comment has a size limit, and a broken build has many failures
_KINDS = (  # (pattern in the error, plain name), first match wins
    (r"time(d)?[ -]?out", "Timeout"),
    (r"connection (refused|reset)|econnrefused|connecterror|name or service not known", "Connection"),
    (r"\b5\d\d\b.*(status|error|server)|(status|server error).*\b5\d\d\b", "Server error (5xx)"),
    (r"\b4\d\d\b.*(status|error|client)|(status|client error).*\b4\d\d\b", "Client error (4xx)"),
    (r"locator|selector|strict mode|element", "Page element"),
    (r"assert|expected|!=", "Assertion"),
)


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join((text or "").split())
    line = line.replace("|", "\\|").replace("<", "&lt;")  # inside a Markdown table cell
    return line if len(line) <= limit else line[:limit - 1] + "…"


def kind(message: str) -> str:
    """What sort of failure this is, from the error text alone. No model involved."""
    for pattern, name in _KINDS:
        if re.search(pattern, message or "", re.IGNORECASE):
            return name
    return "Error"


def _seen(history: tuple[int, int] | None) -> str:
    """'new', or how many of the earlier runs had this same failure."""
    if not history or not history[1]:
        return "first run here"
    seen, runs = history
    return f"{seen} of last {runs} runs" if seen else "new"


def digest(cases: list[Case], queue_url: str | None = None, result=None) -> dict:
    """What the run found, as plain data: the comment and the email are both drawn from it,
    and it can be saved so they can be sent after the reports are uploaded."""
    out: dict = {"headline": None, "note": None, "queue_url": queue_url, "rows": [], "more": 0,
                 "counts": dict(Counter(str(c.verdict.category) if c.verdict else "not judged"
                                        for c in cases).most_common())}
    status, on_board, decided = {}, set(), set()
    if result is not None:
        from tessella import board

        rows = board.classify(result)
        decided = {r.signature for r in rows if r.status == board.Status.decided}
        status = {r.signature: r.note for r in rows}
        on_board = {r.signature for r in rows if r.status == board.Status.on_board}
        out["headline"] = board.headline(result, rows)
        if result.out_of_time:
            out["note"] = f"Time limit reached: {result.out_of_time} failure(s) were not judged."
        elif not result.used_model and cases:
            out["note"] = "This runner has no model, so failures are grouped but not judged."
    clusters: dict[str, list[Case]] = {}
    for c in cases:
        clusters.setdefault(c.signature or c.id, []).append(c)
    history = getattr(result, "history", {}) if result is not None else {}
    checked = getattr(result, "board_checked", False)
    out["show_seen"] = any(runs for _, runs in history.values())
    out["more"] = max(0, len(clusters) - _ROWS)
    out["drafted"] = sum(1 for m in clusters.values() if m[0].draft and m[0].verdict
                         and m[0].verdict.category in _ACTIONABLE)
    for members in list(clusters.values())[:_ROWS]:
        lead, v, d = members[0], members[0].verdict, members[0].draft
        if lead.signature in on_board:
            verdict, category = "already filed", "filed"
        elif v:
            verdict, category = f"{v.category} ({v.confidence:.0%})", str(v.category)
        else:
            verdict, category = f"{kind(lead.message)}, not judged", "none"
        cause = v.root_cause if v else lead.message
        if d and d.possible_duplicates:
            cause += f" Possible duplicate of {', '.join(d.possible_duplicates)}."
        board_note = status.get(lead.signature, "new draft" if d else "not filed")
        if not v and result is not None and lead.signature not in on_board:
            board_note = "no ticket for this failure yet" if checked else "board not checked"
        out["rows"].append({
            "case_id": lead.id, "open": lead.signature not in on_board and lead.signature not in decided,
            "title": lead.title, "tests": [c.title for c in members], "verdict": verdict,
            "category": category, "cause": cause, "board": board_note,
            "seen": _seen(history.get(lead.signature)),
            "actual": d.actual_result if d else "", "expected": d.expected_result if d else "",
            "fix": d.suggested_fix if d else "", "checked_fix": d.patch_status if d else ""})
    return out


def _detail(row: dict) -> list[str]:
    """A folded block for a drafted bug or a failure that hit several tests."""
    tests = row["tests"]
    if not row["actual"] and len(tests) == 1:
        return []
    out = [f"<details><summary>{_one_line(row['title'], 90)}</summary>", ""]
    if row["actual"]:
        out += [f"**Actual:** {_one_line(row['actual'], 300)}", "",
                f"**Expected:** {_one_line(row['expected'], 300)}", "",
                f"**Suggested fix:** {_one_line(row['fix'], 300)}", ""]
        if row["checked_fix"]:
            out += [f"**Checked fix:** {_one_line(row['checked_fix'], 200)}", ""]
    if len(tests) > 1:
        out += [f"**{len(tests)} tests fail this way:**", ""]
        out += [f"- {_one_line(t, 120)}" for t in tests[:20]]
        if len(tests) > 20:
            out.append(f"- and {len(tests) - 20} more")
        out.append("")
    return [*out, "</details>", ""]


def markdown(d: dict, run_link: str | None = None, links: list[tuple[str, str]] | None = None) -> str:
    """The digest as the comment GitHub or GitLab shows, and as the CI run's summary page."""
    lines = [MARKER, "### Tessella run summary", ""]
    if d["headline"]:
        lines += [f"**{d['headline']}**", ""]
    if d["note"]:
        more = f" [Add a model to CI]({_HOW_TO_ADD_A_MODEL})." if "no model" in d["note"] else ""
        lines += [f"_{d['note']}{more}_", ""]
    reports = [*(links or []), *([("CI run", run_link)] if run_link else [])]
    if reports:
        lines += ["**Reports:** " + " · ".join(f"[{name}]({url})" for name, url in reports), ""]
    if run_link and any(r.get("open") for r in d["rows"]):
        lines += [f"**Review on your machine:** `tessella review {run_link}`", ""]
    if not d["rows"]:
        lines += ["No failures in the reports.", ""]
    lines += [f"- **{cat}**: {n}" for cat, n in d["counts"].items()]
    if d["rows"]:
        head, rule = "| Failure | Tests | Verdict | Suspected cause | Board |", "| --- | --- | --- | --- | --- |"
        if d["show_seen"]:
            head, rule = head + " Seen before |", rule + " --- |"
        lines += ["", head, rule]
        for r in d["rows"]:
            row = (f"| {_one_line(r['title'], 80)} | {len(r['tests'])} | {r['verdict']} "
                   f"| {_one_line(r['cause'])} | {_one_line(r['board'], 100)} |")
            lines.append(row + (f" {r['seen']} |" if d["show_seen"] else ""))
        if d["more"]:
            lines.append(f"\n_And {d['more']} more failures: see the full report._")
        details = [line for r in d["rows"][:_DETAILS] for line in _detail(r)]
        if details:
            lines += ["", *details]
    if d["queue_url"]:
        lines += ["", f"Drafts are waiting for review: {d['queue_url']}"]
    elif d["drafted"]:
        lines += ["", f"{d['drafted']} ticket draft(s) are waiting in the review queue (`tessella queue`)."]
    lines += ["", "_Nothing is filed to Jira without SDET approval._"]
    return "\n".join(lines)


def summary(cases: list[Case], queue_url: str | None = None, result=None,
            run_link: str | None = None, links: list[tuple[str, str]] | None = None) -> str:
    """The comment. With the triage `result`, it opens with what the run means for the board
    (already filed, probably filed, new drafts) and gives each cluster its board status. With
    no model on the runner it still groups the failures, names their kind, says which the
    board already has, and how often each one failed before."""
    return markdown(digest(cases, queue_url, result), run_link, links)


def context(env: dict[str, str] | None = None) -> dict[str, str]:
    """Which repository, pull request, branch and commit this CI run is for."""
    env = dict(os.environ if env is None else env)
    if env.get("GITHUB_ACTIONS") == "true":
        server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        repo, number = env.get("GITHUB_REPOSITORY", ""), _github_pr_number(env)
        return {"repo": repo, "branch": env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME", ""),
                "commit": env.get("GITHUB_SHA", "")[:7], "change": f"PR #{number}" if number else "",
                "change_url": f"{server}/{repo}/pull/{number}" if number else ""}
    if env.get("GITLAB_CI") == "true":
        iid, url = env.get("CI_MERGE_REQUEST_IID"), env.get("CI_PROJECT_URL", "")
        return {"repo": env.get("CI_PROJECT_PATH", ""),
                "branch": env.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME") or env.get("CI_COMMIT_REF_NAME", ""),
                "commit": env.get("CI_COMMIT_SHORT_SHA", ""), "change": f"MR !{iid}" if iid else "",
                "change_url": f"{url}/-/merge_requests/{iid}" if iid else ""}
    if env.get("TF_BUILD"):  # Azure Pipelines
        return {"repo": env.get("BUILD_REPOSITORY_NAME", ""), "branch": env.get("BUILD_SOURCEBRANCHNAME", ""),
                "commit": env.get("BUILD_SOURCEVERSION", "")[:7], "change": "", "change_url": ""}
    if env.get("JENKINS_URL"):
        return {"repo": env.get("JOB_NAME", ""), "branch": env.get("BRANCH_NAME") or env.get("GIT_BRANCH", ""),
                "commit": env.get("GIT_COMMIT", "")[:7], "change": "", "change_url": ""}
    if env.get("CIRCLECI"):
        return {"repo": f"{env.get('CIRCLE_PROJECT_USERNAME', '')}/{env.get('CIRCLE_PROJECT_REPONAME', '')}",
                "branch": env.get("CIRCLE_BRANCH", ""), "commit": env.get("CIRCLE_SHA1", "")[:7],
                "change": "", "change_url": ""}
    return {"repo": "", "branch": "", "commit": "", "change": "", "change_url": ""}


def run_url(env: dict[str, str] | None = None) -> str | None:
    """This CI run's page, where the uploaded reports are."""
    env = dict(os.environ if env is None else env)
    if env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_RUN_ID") and env.get("GITHUB_REPOSITORY"):
        server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        return f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    if env.get("GITLAB_CI") == "true":
        return env.get("CI_PIPELINE_URL")
    if env.get("TF_BUILD"):
        return (f"{env.get('SYSTEM_COLLECTIONURI', '').rstrip('/')}/{quote(env.get('SYSTEM_TEAMPROJECT', ''))}"
                f"/_build/results?buildId={env.get('BUILD_BUILDID', '')}")
    if env.get("JENKINS_URL"):
        return env.get("BUILD_URL")
    if env.get("CIRCLECI"):
        return env.get("CIRCLE_BUILD_URL")
    return None


@dataclass
class Target:
    """Where the comment goes: one pull/merge request's comment list."""

    host: str  # GitHub or GitLab, for messages
    list_url: str  # GET existing comments, POST a new one
    item_url: str  # format with {id}: PATCH/PUT an existing comment
    headers: dict[str, str]
    edit_method: str  # PATCH (GitHub) or PUT (GitLab)


def _github_pr_number(env: dict[str, str]) -> int | None:
    ref = env.get("GITHUB_REF", "")  # refs/pull/42/merge on pull_request events
    if ref.startswith("refs/pull/"):
        part = ref.split("/")[2]
        if part.isdigit():
            return int(part)
    path = env.get("GITHUB_EVENT_PATH")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                event = json.load(fh)
        except (OSError, ValueError):
            return None
        pr = event.get("pull_request") or {}
        if isinstance(pr.get("number"), int):
            return pr["number"]
    return None


def detect(env: dict[str, str] | None = None, token_env: str | None = None) -> Target | None:
    """The PR/MR this CI job runs for, from the CI's own variables. None: not a PR/MR job,
    or no token (the reason is in `why_not`)."""
    env = dict(os.environ if env is None else env)
    if env.get("GITHUB_ACTIONS") == "true":
        repo, number = env.get("GITHUB_REPOSITORY"), _github_pr_number(env)
        token = env.get(token_env or "GITHUB_TOKEN")
        if not (repo and number and token):
            return None
        api = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        return Target("GitHub", f"{api}/repos/{repo}/issues/{number}/comments",
                      f"{api}/repos/{repo}/issues/comments/{{id}}",
                      {"Authorization": f"Bearer {token}",
                       "Accept": "application/vnd.github+json"}, "PATCH")
    if env.get("GITLAB_CI") == "true":
        project, iid = env.get("CI_PROJECT_ID"), env.get("CI_MERGE_REQUEST_IID")
        token = env.get(token_env or "GITLAB_TOKEN")
        if not (project and iid and token):
            return None
        api = env.get("CI_API_V4_URL", "https://gitlab.com/api/v4").rstrip("/")
        base = f"{api}/projects/{quote(project, safe='')}/merge_requests/{iid}/notes"
        return Target("GitLab", base, f"{base}/{{id}}", {"PRIVATE-TOKEN": token}, "PUT")
    return None


def why_not(env: dict[str, str] | None = None, token_env: str | None = None) -> str:
    """Why `detect` found no PR/MR to comment on, in one sentence."""
    env = dict(os.environ if env is None else env)
    if env.get("GITHUB_ACTIONS") == "true":
        if not _github_pr_number(env):
            return "this GitHub Actions run is not for a pull request"
        return f"the token variable {token_env or 'GITHUB_TOKEN'} is not set"
    if env.get("GITLAB_CI") == "true":
        if not env.get("CI_MERGE_REQUEST_IID"):
            return "this GitLab pipeline is not a merge request pipeline"
        return f"the token variable {token_env or 'GITLAB_TOKEN'} is not set"
    return "not running in GitHub Actions or GitLab CI"


def host_of(target: Target) -> str:
    """The allowlist entry the comment needs, e.g. api.github.com."""
    parts = urlsplit(target.list_url)
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname or ""


def post(target: Target, body: str, allow: list[str]) -> str:
    """Create Tessella's comment, or edit the one an earlier run left. Returns 'created' or
    'updated'. Raises on an HTTP error or a host outside network.allow; the caller reports it."""
    with net.client(allow, timeout=20, headers=target.headers, follow_redirects=True) as c:
        existing = None
        r = c.get(target.list_url, params={"per_page": 100})
        if r.status_code < 400:
            for item in r.json():
                if MARKER in (item.get("body") or ""):
                    existing = item["id"]
                    break
        if existing is not None:
            r = c.request(target.edit_method, target.item_url.format(id=existing), json={"body": body})
            r.raise_for_status()
            return "updated"
        r = c.post(target.list_url, json={"body": body})
        r.raise_for_status()
        return "created"
