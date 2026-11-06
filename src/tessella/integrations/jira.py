"""Jira: duplicate search before drafting, issue creation only on SDET approval."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from tessella import net
from tessella.config import Jira
from tessella.models import Draft


def _error_detail(r) -> str:
    """Jira's 400/403 body names the offending field(s). Surface it instead of a bare status."""
    try:
        data = r.json()
        parts = list(data.get("errorMessages", []))
        parts += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
        return "; ".join(parts) or r.text[:500]
    except ValueError:
        return r.text[:500]


def _auth_header(cfg: Jira) -> str:
    """Atlassian Cloud: Basic auth with the account email + API token.
    Self-hosted Server/Data Center (no email set): a Bearer personal access token."""
    if cfg.email:
        raw = base64.b64encode(f"{cfg.email}:{cfg.token}".encode()).decode()
        return f"Basic {raw}"
    return f"Bearer {cfg.token}"


def _label(signature: str) -> str:
    return f"tessella-{signature}"


def create_issue(cfg: Jira, draft: Draft, allow: list[str]) -> tuple[str, str]:
    """Create a Jira bug from an approved draft. Returns (issue key, issue url).

    If an issue already carries this signature's dedup label (filed earlier, or by a
    teammate on another machine), link to it instead of creating a duplicate."""
    key, url, _linked = create_or_link(cfg, draft, allow)
    return key, url


def create_or_link(cfg: Jira, draft: Draft, allow: list[str]) -> tuple[str, str, bool]:
    """Like create_issue, but also says whether it linked to an existing ticket
    (True: a duplicate was avoided) or filed a new one (False)."""
    missing = [n for n, v in (("jira.url", cfg.url), ("jira.project", cfg.project)) if not v]
    if missing:
        raise RuntimeError(f"Set {' and '.join(missing)} in tessella.yaml (project must be the "
                           "project KEY, e.g. KAN — not its display name).")
    if not cfg.token:
        hint = ""
        if len(cfg.token_env) > 40 or cfg.token_env.startswith("ATATT"):
            hint = (" It looks like the API token itself was pasted into jira.token_env; "
                    "token_env must be the NAME of an env var (e.g. JIRA_TOKEN).")
        raise RuntimeError(
            f"No Jira token: the environment variable named by jira.token_env "
            f"('{cfg.token_env[:12]}…') is not set.{hint} "
            f"Set token_env to a name like JIRA_TOKEN, then `export JIRA_TOKEN=<your-api-token>`."
        )
    existing = find_by_signature(cfg, draft.signature, allow)
    if existing:
        return existing[0], existing[1], True
    labels = ["tessella"] + ([_label(draft.signature)] if draft.signature else [])
    payload = {"fields": {"project": {"key": cfg.project}, "summary": draft.title,
                          "description": draft.body, "issuetype": {"name": cfg.issue_type},
                          "labels": labels}}
    base = cfg.url.rstrip("/")
    headers = {"Authorization": _auth_header(cfg), "Content-Type": "application/json",
               "Accept": "application/json"}
    with net.client(allow, timeout=20, headers=headers, follow_redirects=True) as c:
        r = c.post(f"{base}/rest/api/2/issue", json=payload)
        if r.status_code >= 400:
            detail = _error_detail(r)
            if "issuetype" in detail.lower():
                types = _issue_types(cfg, base, c)
                if types:
                    detail += (f". Project {cfg.project}'s issue types are: {', '.join(types)}"
                               f" — set jira.issue_type to one of these (currently '{cfg.issue_type}').")
            raise RuntimeError(f"Jira rejected the issue ({r.status_code}): {detail}")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(
                f"Jira returned {r.status_code} but not JSON — jira.url is probably wrong. "
                f"It must be the site base only (https://your-site.atlassian.net), with no "
                f"/jira/... path. Body starts: {r.text[:200]!r}"
            )
        key = data["key"]
    _attach(cfg, base, key, draft.images, allow)
    return key, f"{base}/browse/{key}", False


def _attach(cfg: Jira, base: str, key: str, images: list[str], allow: list[str]) -> None:
    """Upload the draft's screenshots to the created issue. Best-effort: a failed or
    missing file is skipped, never undoing an issue that was already created."""
    files = [Path(p) for p in images]
    files = [p for p in files if p.is_file()]
    if not files:
        return
    # Multipart upload needs its own client: no application/json Content-Type, and Jira
    # requires the X-Atlassian-Token: no-check header for attachment endpoints.
    headers = {"Authorization": _auth_header(cfg), "X-Atlassian-Token": "no-check"}
    with net.client(allow, timeout=30, headers=headers) as c:
        for p in files:
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            with p.open("rb") as fh:
                c.post(f"{base}/rest/api/2/issue/{key}/attachments",
                       files={"file": (p.name, fh, ctype)})


def _issue_types(cfg: Jira, base: str, c) -> list[str]:
    """The issue type names available in the project, for a helpful error message."""
    try:
        r = c.get(f"{base}/rest/api/2/project/{cfg.project}")
        if r.status_code >= 400:
            return []
        return [t["name"] for t in r.json().get("issueTypes", [])]
    except (ValueError, KeyError, OSError):
        return []


def issue_exists(cfg: Jira, key: str, allow: list[str]) -> bool:
    """Does this issue still exist in Jira? False only on a hard 404 (deleted). Any other
    outcome — 2xx, auth error, 5xx, network — returns True so we never re-file on doubt."""
    base = cfg.url.rstrip("/")
    headers = {"Authorization": _auth_header(cfg), "Accept": "application/json"}
    with net.client(allow, timeout=15, headers=headers, follow_redirects=True) as c:
        r = c.get(f"{base}/rest/api/2/issue/{key}", params={"fields": "key"})
        return r.status_code != 404


def find_by_signature(cfg: Jira, signature: str, allow: list[str]) -> tuple[str, str] | None:
    """Find an issue already labelled for this signature. Returns (key, url) or None.
    Any error returns None (fail open: the caller proceeds to create)."""
    if not signature:
        return None
    base = cfg.url.rstrip("/")
    headers = {"Authorization": _auth_header(cfg), "Accept": "application/json"}
    jql = f'project = "{cfg.project}" AND labels = "{_label(signature)}"'
    try:
        with net.client(allow, timeout=15, headers=headers, follow_redirects=True) as c:
            # /rest/api/3/search/jql is the current endpoint; /rest/api/2/search is deprecated.
            r = c.get(f"{base}/rest/api/3/search/jql",
                      params={"jql": jql, "fields": "key", "maxResults": 1})
            if r.status_code >= 400:
                return None
            issues = r.json().get("issues", [])
    except (OSError, ValueError):
        return None
    if not issues:
        return None
    key = issues[0]["key"]
    return key, f"{base}/browse/{key}"


def ticket_status(cfg: Jira, key: str, allow: list[str]) -> dict | None:
    """What Jira says about one ticket: status, resolution and dates. Read-only.

    Returns None on any problem (not configured, no token, network, 404, bad JSON): the caller
    leaves what it already knew, so a flaky board never erases a recorded outcome."""
    if not (cfg.url and cfg.token):
        return None
    base = cfg.url.rstrip("/")
    headers = {"Authorization": _auth_header(cfg), "Accept": "application/json"}
    try:
        with net.client(allow, timeout=15, headers=headers, follow_redirects=True) as c:
            r = c.get(f"{base}/rest/api/2/issue/{key}",
                      params={"fields": "status,resolution,created,resolutiondate"})
            if r.status_code >= 400:
                return None
            fields = r.json().get("fields", {})
    except (OSError, ValueError):
        return None
    status = fields.get("status") or {}
    resolution = fields.get("resolution") or {}
    return {"status": status.get("name"),
            "category": (status.get("statusCategory") or {}).get("key"),
            "resolution": resolution.get("name"),
            "created": fields.get("created"), "resolved": fields.get("resolutiondate")}


def search_text(cfg: Jira, words: list[str], allow: list[str], limit: int = 20) -> list[dict]:
    """Issues in the project whose text mentions any of these words, newest first. Read-only.

    Returns [{key, summary, status, url}]. Any problem (not configured, no token, network,
    4xx/5xx, bad JSON) returns [] — a missed duplicate costs a reviewer a look, never a run."""
    words = [w for w in words if w.isalnum()]  # JQL text search: plain words only, no quoting
    if not words:
        return []
    return search(cfg, f'text ~ "{" OR ".join(words)}"', allow, limit)


def search_tessella(cfg: Jira, allow: list[str], limit: int = 100) -> list[dict]:
    """Every issue Tessella filed in the project (they all carry the `tessella` label), so
    duplicate search scores them all, whatever Jira's text index makes of their wording."""
    return search(cfg, 'labels = "tessella"', allow, limit)


def search(cfg: Jira, clause: str, allow: list[str], limit: int = 20) -> list[dict]:
    """Issues in the project matching a JQL clause, newest first. Read-only, fails open ([])."""
    if not (cfg.url and cfg.project and cfg.token):
        return []
    base = cfg.url.rstrip("/")
    headers = {"Authorization": _auth_header(cfg), "Accept": "application/json"}
    jql = f'project = "{cfg.project}" AND {clause} ORDER BY updated DESC'
    try:
        with net.client(allow, timeout=15, headers=headers, follow_redirects=True) as c:
            r = c.get(f"{base}/rest/api/3/search/jql",
                      params={"jql": jql, "fields": "summary,status", "maxResults": limit})
            if r.status_code >= 400:
                return []
            issues = r.json().get("issues", [])
    except (OSError, ValueError, RuntimeError):
        return []
    out = []
    for i in issues:
        fields = i.get("fields") or {}
        out.append({"key": i["key"], "summary": fields.get("summary") or "",
                    "status": (fields.get("status") or {}).get("name"),
                    "url": f"{base}/browse/{i['key']}"})
    return out
