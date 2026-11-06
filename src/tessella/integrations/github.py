"""GitHub Issues: issue creation only on SDET approval."""

from __future__ import annotations

from tessella import net
from tessella.config import GitHub
from tessella.models import Draft


def create_issue(cfg: GitHub, draft: Draft, allow: list[str]) -> tuple[str, str]:
    """Create a GitHub issue from an approved draft. Returns (issue key, issue url)."""
    if not (cfg.repo and cfg.token):
        raise RuntimeError("github.repo and the token env var must be set")
    payload = {"title": draft.title, "body": draft.body}
    if cfg.labels:
        payload["labels"] = cfg.labels
    headers = {"Authorization": f"Bearer {cfg.token}", "Accept": "application/vnd.github+json"}
    with net.client(allow, timeout=20, headers=headers) as c:
        r = c.post(f"https://api.github.com/repos/{cfg.repo}/issues", json=payload)
        r.raise_for_status()
        data = r.json()
        return f"#{data['number']}", data["html_url"]
