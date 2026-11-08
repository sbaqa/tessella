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
_ACTIONABLE = (Category.product_bug, Category.test_bug)


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


def _detail(members: list[Case]) -> list[str]:
    """A folded block for a drafted bug or a failure that hit several tests."""
    lead, draft = members[0], members[0].draft
    if not draft and len(members) == 1:
        return []
    out = [f"<details><summary>{_one_line(lead.title, 90)}</summary>", ""]
    if draft:
        out += [f"**Actual:** {_one_line(draft.actual_result, 300)}", "",
                f"**Expected:** {_one_line(draft.expected_result, 300)}", "",
                f"**Suggested fix:** {_one_line(draft.suggested_fix, 300)}", ""]
        if draft.patch_status:
            out += [f"**Checked fix:** {_one_line(draft.patch_status, 200)}", ""]
    if len(members) > 1:
        out += [f"**{len(members)} tests fail this way:**", ""]
        out += [f"- {_one_line(c.title, 120)}" for c in members[:20]]
        if len(members) > 20:
            out.append(f"- and {len(members) - 20} more")
        out.append("")
    return [*out, "</details>", ""]


def summary(cases: list[Case], queue_url: str | None = None, result=None,
            run_link: str | None = None) -> str:
    """The comment. With the triage `result`, it opens with what the run means for the board
    (already filed, probably filed, new drafts) and gives each cluster its board status. With
    no model on the runner it still groups the failures, names their kind, says which the
    board already has, and how often each one failed before."""
    counts = Counter(c.verdict.category if c.verdict else "not judged" for c in cases)
    lines = [MARKER, "### Tessella run summary", ""]
    status, on_board = {}, set()
    if result is not None:
        from tessella import board

        rows = board.classify(result)
        status = {r.signature: r.note for r in rows}
        on_board = {r.signature for r in rows if r.status == board.Status.on_board}
        lines += [f"**{board.headline(result, rows)}**", ""]
        if result.out_of_time:
            lines += [f"_Time limit reached: {result.out_of_time} failure(s) were not judged._", ""]
        elif not result.used_model and cases:
            lines += [("_This runner has no model, so failures are grouped but not judged. "
                       f"[Add a model to CI]({_HOW_TO_ADD_A_MODEL})._"), ""]
    if not cases:
        lines += ["No failures in the reports.", ""]
    lines += [f"- **{cat}**: {n}" for cat, n in counts.most_common()]
    clusters: dict[str, list[Case]] = {}
    for c in cases:
        clusters.setdefault(c.signature or c.id, []).append(c)
    history = getattr(result, "history", {}) if result is not None else {}
    checked = getattr(result, "board_checked", False)
    if clusters:
        head = "| Failure | Tests | Verdict | Suspected cause | Board |"
        rule = "| --- | --- | --- | --- | --- |"
        if history:
            head, rule = head + " Seen before |", rule + " --- |"
        lines += ["", head, rule]
        for members in list(clusters.values())[:_ROWS]:
            lead = members[0]
            v = lead.verdict
            if lead.signature in on_board:
                verdict = "already filed"
            elif v:
                verdict = f"{v.category} ({v.confidence:.0%})"
            else:
                verdict = f"{kind(lead.message)}, not judged"
            cause = v.root_cause if v else lead.message
            d = lead.draft
            if d and d.possible_duplicates:
                cause += f" Possible duplicate of {', '.join(d.possible_duplicates)}."
            board_note = status.get(lead.signature, "new draft" if d else "not filed")
            if not v and result is not None and lead.signature not in on_board:
                board_note = "no ticket for this failure yet" if checked else "board not checked"
            row = (f"| {_one_line(lead.title, 80)} | {len(members)} | {verdict} "
                   f"| {_one_line(cause)} | {_one_line(board_note, 100)} |")
            if history:
                row += f" {_seen(history.get(lead.signature))} |"
            lines.append(row)
        if len(clusters) > _ROWS:
            lines.append(f"\n_And {len(clusters) - _ROWS} more failures: see the full report._")
        details = [line for members in list(clusters.values())[:_DETAILS] for line in _detail(members)]
        if details:
            lines += ["", *details]
    drafted = sum(1 for members in clusters.values()
                  if members[0].draft and members[0].verdict
                  and members[0].verdict.category in _ACTIONABLE)
    if queue_url:
        lines += ["", f"Drafts are waiting for review: {queue_url}"]
    elif drafted:
        lines += ["", f"{drafted} ticket draft(s) are waiting in the review queue (`tessella queue`)."]
    if run_link:
        lines += ["", f"Full report and test results: [this run]({run_link})"]
    lines += ["", "_Nothing is filed to Jira without SDET approval._"]
    return "\n".join(lines)


def run_url(env: dict[str, str] | None = None) -> str | None:
    """This CI run's page, where the uploaded reports are."""
    env = dict(os.environ if env is None else env)
    if env.get("GITHUB_ACTIONS") == "true" and env.get("GITHUB_RUN_ID") and env.get("GITHUB_REPOSITORY"):
        server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        return f"{server}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}"
    if env.get("GITLAB_CI") == "true":
        return env.get("CI_PIPELINE_URL")
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
