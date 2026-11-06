"""One short PR/MR comment per run: clusters, verdicts, link to the review queue.

Posted to the pull request (GitHub Actions) or merge request (GitLab CI) the job runs for,
through net.client, with a CI token from the environment. Each run edits Tessella's earlier
comment instead of adding another. Posting is best-effort: it never fails the build."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from tessella import net
from tessella.models import Case, Category

# Hidden in the rendered comment; finds Tessella's own comment on the next run.
MARKER = "<!-- tessella-ci-summary -->"
_ACTIONABLE = (Category.product_bug, Category.test_bug)


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join((text or "").split())
    line = line.replace("|", "\\|")  # inside a Markdown table cell
    return line if len(line) <= limit else line[:limit - 1] + "…"


def summary(cases: list[Case], queue_url: str | None = None, result=None) -> str:
    """The comment. With the triage `result`, it opens with what the run means for the board
    (already filed, probably filed, new drafts) and gives each cluster its board status."""
    counts = Counter(c.verdict.category if c.verdict else "pending" for c in cases)
    lines = [MARKER, "### Tessella run summary", ""]
    status = {}
    if result is not None:
        from tessella import board

        rows = board.classify(result)
        status = {r.signature: r.note for r in rows}
        lines += [f"**{board.headline(result, rows)}**", ""]
    if not cases:
        lines += ["No failures in the reports.", ""]
    lines += [f"- **{cat}**: {n}" for cat, n in counts.most_common()]
    clusters: dict[str, list[Case]] = {}
    for c in cases:
        clusters.setdefault(c.signature or c.id, []).append(c)
    if clusters:
        lines += ["", "| Failure | Tests | Verdict | Suspected cause | Board |",
                  "| --- | --- | --- | --- | --- |"]
        for members in clusters.values():
            lead = members[0]
            v = lead.verdict
            verdict = f"{v.category} ({v.confidence:.0%})" if v else "pending"
            cause = v.root_cause if v else lead.message
            d = lead.draft
            if d and d.possible_duplicates:
                cause += f" Possible duplicate of {', '.join(d.possible_duplicates)}."
            board_note = status.get(lead.signature, "new draft" if d else "not filed")
            lines.append(f"| {_one_line(lead.title, 80)} | {len(members)} | {verdict} "
                         f"| {_one_line(cause)} | {_one_line(board_note, 100)} |")
    drafted = sum(1 for members in clusters.values()
                  if members[0].draft and members[0].verdict
                  and members[0].verdict.category in _ACTIONABLE)
    if queue_url:
        lines += ["", f"Drafts are waiting for review: {queue_url}"]
    elif drafted:
        lines += ["", f"{drafted} ticket draft(s) are waiting in the review queue (`tessella queue`)."]
    lines += ["", "_Nothing is filed to Jira without SDET approval._"]
    return "\n".join(lines)


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
