"""`tessella doctor --board`: can approving a draft actually file it? Read-only: signs in,
finds the project or repo, checks the issue type and the duplicate search. Files nothing."""

from __future__ import annotations

from dataclasses import dataclass

from tessella import net
from tessella.config import Settings


@dataclass
class Check:
    name: str
    ok: bool | None  # None: a note, not a failure
    message: str


def _get(url: str, headers: dict, allow: list[str], **params):
    with net.client(allow, timeout=15, headers=headers, follow_redirects=True) as c:
        return c.get(url, params=params or None)


def jira(s: Settings) -> list[Check]:
    from tessella.integrations.jira import _auth_header, search_text

    j, out = s.jira, []
    if not (j.url and j.project):
        return [Check("config", False, "set jira.url and jira.project")]
    if not j.token:
        return [Check("token", False, f"the env var {j.token_env} is not set")]
    if not net.is_allowed(j.url, s.network.allow):
        return [Check("network", False, f"add {j.url.split('//')[-1].rstrip('/')} to network.allow")]
    base = j.url.rstrip("/")
    headers = {"Authorization": _auth_header(j), "Accept": "application/json"}
    try:
        r = _get(f"{base}/rest/api/2/myself", headers, s.network.allow)
        if r.status_code >= 400:
            hint = " (Cloud needs jira.email plus an API token)" if r.status_code == 401 else ""
            return [Check("sign-in", False, f"Jira answered {r.status_code}{hint}")]
        who = r.json()
        out.append(Check("sign-in", True, f"as {who.get('displayName') or who.get('name')}"))
        r = _get(f"{base}/rest/api/2/project/{j.project}", headers, s.network.allow)
        if r.status_code >= 400:
            out.append(Check("project", False, f"project {j.project} not found or not visible "
                                               f"({r.status_code}); use the project KEY"))
            return out
        types = [t["name"] for t in r.json().get("issueTypes", [])]
        out.append(Check("project", True, j.project))
        if j.issue_type in types:
            out.append(Check("issue type", True, j.issue_type))
        else:
            out.append(Check("issue type", False, f"'{j.issue_type}' is not one of: {', '.join(types)}"))
    except (OSError, ValueError, RuntimeError) as exc:
        out.append(Check("connection", False, str(exc)[:200]))
        return out
    found = search_text(j, ["tessella"], s.network.allow, limit=5)
    out.append(Check("duplicate search", None, "search works; finds "
                     + ", ".join(i["key"] for i in found) if found else
                     "search returned nothing (fine on a new board; a failed search also "
                     "returns nothing)"))
    return out


def github(s: Settings) -> list[Check]:
    g = s.github
    if not g.repo:
        return [Check("config", False, "set github.repo (owner/repo)")]
    if not g.token:
        return [Check("token", False, f"the env var {g.token_env} is not set")]
    api = "https://api.github.com"
    if not net.is_allowed(api, s.network.allow):
        return [Check("network", False, "add api.github.com to network.allow")]
    headers = {"Authorization": f"Bearer {g.token}", "Accept": "application/vnd.github+json"}
    try:
        r = _get(f"{api}/repos/{g.repo}", headers, s.network.allow)
        if r.status_code >= 400:
            return [Check("repo", False, f"GitHub answered {r.status_code} for {g.repo}")]
        repo = r.json()
    except (OSError, ValueError, RuntimeError) as exc:
        return [Check("connection", False, str(exc)[:200])]
    out = [Check("repo", True, repo.get("full_name", g.repo))]
    if not repo.get("has_issues", True):
        out.append(Check("issues", False, "issues are turned off for this repo"))
    out.append(Check("permission", None, "creating an issue needs a token with Issues: write "
                                         "(fine-grained) or the repo scope (classic)"))
    return out


def filed(s: Settings) -> list[Check]:
    """For every ticket the store says was filed: would approving that failure again link
    back to it (its signature label), and does duplicate search list it for the failure?"""
    from tessella.draft.duplicates import jira_duplicates
    from tessella.integrations.jira import find_by_signature
    from tessella.memory.store import Store

    out = []
    seen: set[str] = set()
    for case, d in Store(s.storage.path).recent_decisions(limit=200):
        if d.board != "jira" or not d.ticket_key or d.ticket_key in seen:
            continue
        seen.add(d.ticket_key)
        hit = find_by_signature(s.jira, case.signature, s.network.allow)
        if hit and hit[0] == d.ticket_key:
            out.append(Check(f"{d.ticket_key} link-back", True,
                             f"approving {case.signature[:8]} again links to it"))
        else:
            got = hit[0] if hit else "nothing"
            out.append(Check(f"{d.ticket_key} link-back", False,
                             f"the label tessella-{case.signature} finds {got}: approving "
                             "again would file a new ticket"))
        if case.draft is None:
            continue
        keys = [i["key"] for i in jira_duplicates(case, s, use_model=False)]
        out.append(Check(f"{d.ticket_key} duplicate search", d.ticket_key in keys,
                         f"lists {', '.join(keys) or 'nothing'}"))
    return out


def check(s: Settings) -> list[Check]:
    if s.board == "jira":
        checks = jira(s)
        if all(c.ok is not False for c in checks):
            checks += filed(s)
        return checks
    if s.board == "github":
        return github(s)
    return [Check("board", False, "set board: jira or board: github")]
