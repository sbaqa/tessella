"""One plain page with every ticket draft of a CI run, published next to the run's reports.

The email's Open draft links jump to a failure on this page (`#case-<id>`). It uses no scripts
and loads nothing, so a CI provider can show it as it is, and it stays read-only: deciding
happens with `tessella review`, whose command is printed under each draft."""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

from tessella.memory.decisions_file import FOLDER
from tessella.memory.store import Store
from tessella.models import Case

_FIELDS = (("Description", "description"), ("Steps to reproduce", "steps_to_reproduce"),
           ("Actual result", "actual_result"), ("Expected result", "expected_result"),
           ("Environment", "environment"), ("Root cause (suspected)", "root_cause"),
           ("Suggested fix", "suggested_fix"))
_LABEL = {"product_bug": "Product bug", "test_bug": "Test bug", "flaky": "Flaky", "environment": "Environment",
          "test_data": "Test data", "unknown": "Unclear"}
_MAX_IMAGES, _MAX_WIDTH = 2, 1000
_STYLE = """
body{margin:0;background:#eef1f4;color:#121920;font:15px/1.55 system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 14px 60px}
h1{font-size:22px;margin:0 0 4px} p.where{color:#6b7680;font-size:13px;margin:0 0 12px;word-break:break-word}
nav a{display:inline-block;margin:0 8px 8px 0;padding:7px 12px;border:1px solid #121920;border-radius:6px;color:inherit;text-decoration:none;font-weight:600;font-size:14px}
ol.toc{background:#fff;border:1px solid #dde2e7;border-radius:8px;padding:12px 12px 12px 34px;margin:12px 0 18px;word-break:break-word}
section{background:#fff;border:1px solid #dde2e7;border-left:4px solid #7d8791;border-radius:8px;padding:14px 18px;margin:0 0 14px;scroll-margin-top:12px}
section:target{outline:3px solid #2a78d6}
body:has(section:target) section:not(:target),body:has(section:target) .all{display:none}
.only{display:none} body:has(section:target) .only{display:block;margin:0 0 12px}
.act{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 4px} .act a{padding:7px 11px;border:1px solid #121920;border-radius:6px;color:inherit;text-decoration:none;font-size:13.5px;font-weight:600}
.act a.go{background:#121920;color:#fff}
p.dup{margin:8px 0 2px;font-weight:600;font-size:13.5px} ul.dup{margin:0 0 6px;padding-left:18px;font-size:13.5px} p.hint{color:#6b7680;font-size:12.5px;margin:6px 0 0}
section.product_bug{border-left-color:#2a78d6} section.test_bug{border-left-color:#eb6834} section.flaky{border-left-color:#1baf7a}
.kind{font-size:12px;font-weight:700;color:#46525d} h2{font-size:17px;margin:2px 0 2px;word-break:break-word} p.test{color:#6b7680;font-size:13px;margin:0 0 10px;word-break:break-word}
dl{display:grid;grid-template-columns:170px minmax(0,1fr);gap:6px 14px;margin:0} dt{color:#6b7680;font-size:13px;font-weight:600} dd{margin:0;white-space:pre-wrap;word-break:break-word}
img{max-width:100%;border:1px solid #dde2e7;border-radius:6px;display:block;margin:8px 0}
pre,code.cmd{display:block;background:#eef1f4;border-radius:6px;padding:9px 11px;font-size:12.5px;white-space:pre-wrap;word-break:break-all;margin:6px 0 0}
h3{font-size:13px;margin:14px 0 2px}
@media(max-width:560px){dl{grid-template-columns:1fr}}
"""


def _image(path: str) -> str | None:
    """A screenshot as a data URI, made small enough to keep the page light."""
    source = Path(path)
    if not source.is_file():
        return None
    try:
        from PIL import Image

        with Image.open(source) as im:
            im = im.convert("RGB")
            if im.width > _MAX_WIDTH:
                im = im.resize((_MAX_WIDTH, round(im.height * _MAX_WIDTH / im.width)))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    except (ImportError, OSError, ValueError):
        return None  # the draft still reads without its picture


@dataclass
class Online:
    """Where the buttons act without Tessella running: Jira's own create page for Approve, and
    GitHub's new-file page to commit any other decision. Each part is optional."""

    jira_url: str = ""
    project_id: str = ""
    issue_type_id: str = ""
    github: str = ""  # https://github.com/owner/repo
    branch: str = ""
    folder: str = ""  # where decision files go, from the repository root
    at: str = ""  # when the page was made: the decision's time


def _jira_create(case: Case, online: Online, test_task: bool = False) -> str | None:
    """Jira's "Create issue" page with this draft filled in and Tessella's label, so the ticket
    is known as this failure's. The person still checks it and clicks Create. `test_task` makes
    it a task to fix the test, its data or its environment, labelled as not a product bug."""
    from urllib.parse import urlencode

    from tessella.integrations.jira import _label

    d = case.draft
    if not (d and online.jira_url and online.project_id and online.issue_type_id):
        return None
    # Plain text: Jira's create page shows wiki or Markdown marks literally.
    fields = "\n\n".join(f"{label}:\n{getattr(d, name) or 'Not available'}" for label, name in _FIELDS)
    summary = f"[test fix] {case.title}" if test_task else d.title
    if test_task:
        fields = ("The code looks fine: the test, its data or its environment needs fixing.\n\n" + fields)
    params = [("pid", online.project_id), ("issuetype", online.issue_type_id), ("summary", summary[:250]),
              ("description", fields[:6000]), ("labels", "tessella")]
    if test_task:
        params.append(("labels", "tessella-test-fix"))
    if case.signature:
        params.append(("labels", _label(case.signature)))
    return f"{online.jira_url.rstrip('/')}/secure/CreateIssueDetails!init.jspa?{urlencode(params)}"


def _commit_decision(case: Case, action: str, reason: str, online: Online) -> str | None:
    """GitHub's page for a new file holding this one decision. Committing it records the
    decision where CI and every teammate read it. An approval made this way waits as pending
    and is filed the next time a dashboard with the board's token opens."""
    from urllib.parse import quote, urlencode

    if not (online.github and online.branch):
        return None
    entry = {"case_id": case.id, "signature": case.signature, "action": action, "at": online.at}
    if action == "approve":
        entry["pending"] = True
    else:
        entry["reason"] = reason
    name = f"{online.folder}{FOLDER}/{case.id}-{(case.signature or 'x')[:8]}-{reason or action}.json"
    return (f"{online.github}/new/{quote(online.branch, safe='')}?"
            + urlencode({"filename": name, "value": json.dumps(entry, indent=2) + "\n"}))


def _matches(case: Case, online: Online) -> list[tuple[str, str, str]]:
    """Tickets already in Jira that read like this failure: (key, summary, link)."""
    d, base = case.draft, online.jira_url.rstrip("/")
    if not d:
        return []
    out = [(m["key"], m.get("summary", ""), m.get("url") or (f"{base}/browse/{m['key']}" if base else ""))
           for m in d.jira_matches]
    seen = {k for k, _, _ in out}
    out += [(k, "", f"{base}/browse/{k}" if base else "") for k in d.possible_duplicates if k not in seen]
    return out


def _search_jira(case: Case, online: Online) -> str | None:
    """Jira's issue search for this failure's words, to look for a duplicate by eye."""
    from urllib.parse import urlencode

    if not (online.jira_url and online.project_id):
        return None
    words = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", case.message or case.title)[:6])
    jql = f'project = {online.project_id} AND text ~ "{words}" ORDER BY created DESC'
    return f"{online.jira_url.rstrip('/')}/issues/?{urlencode({'jql': jql})}"


def _button(url: str, label: str, hint: str, primary: bool = False) -> str:
    return (f'<a{" class=go" if primary else ""} href="{escape(url, quote=True)}" title="{escape(hint)}" '
            f'target="_blank" rel="noopener">{escape(label)}</a>')


def _actions(case: Case, run: str | None, base: str, online: Online | None = None) -> str:
    """Five choices. They open Jira or GitHub, never this machine: the page is read from an
    email, where nothing local can be assumed to be running. A choice that can go to neither
    goes to a team's dashboard server if one is set, or is left out."""
    from urllib.parse import quote

    online = online or Online()

    def local(key: str) -> str | None:
        if not base:
            return None
        return (f"{base}/review?run={quote(run or '', safe='')}&case={quote(case.id, safe='')}"
                f"&decide={quote(key, safe=':')}")

    def record(action: str, reason: str = "") -> str | None:
        return _commit_decision(case, action, reason, online) or local(f"{action}:{reason}" if reason else action)

    buttons, found = [], ""
    if case.draft:
        url = _jira_create(case, online)
        if url:
            buttons.append(_button(url, "Review and file in Jira", "Opens Jira's Create page filled in: "
                                   "edit anything, then click Create", True))
        elif record("approve"):
            buttons.append(_button(record("approve"), "Approve and file", "File this ticket to the board", True))
    matches = _matches(case, online)
    if matches:
        rows = "".join(f'<li><a href="{escape(link, quote=True)}" target="_blank" rel="noopener">View '
                       f'{escape(key)}</a> {escape(summary)}</li>' if link else
                       f"<li>{escape(key)} {escape(summary)}</li>" for key, summary, link in matches)
        found = f'<p class="dup">Possibly already on the board:</p><ul class="dup">{rows}</ul>'
        url = record("reject", "duplicate")
        if url:
            buttons.append(_button(url, f"Duplicate of {matches[0][0]}", "Record it as a duplicate of that ticket"))
    elif _search_jira(case, online):
        buttons.append(_button(_search_jira(case, online), "Search Jira for a duplicate",
                               "Opens Jira's search for this failure's words"))
    url = record("reject", "flaky")
    if url:
        buttons.append(_button(url, "Flaky test", "It passes on a re-run: nothing wrong with the product"))
    url = _jira_create(case, online, test_task=True) or record("reject", "test_bug")
    if url:
        buttons.append(_button(url, "Test or setup problem", "The code is fine: the test, its data or its "
                               "environment needs fixing. Opens Jira to file a task for it"))
    url = record("reject", "no_action")
    if url:
        buttons.append(_button(url, "No action needed", "Real, but nothing to file: fixed since, or not worth fixing"))
    return found + (f'<div class="act">{"".join(buttons)}</div>' if buttons else "")


def _section(case: Case, row: dict, run: str | None, base: str = "",
             online: Online | None = None) -> str:
    v, d = case.verdict, case.draft
    kind = _LABEL.get(str(v.category), "Not analysed") if v else "Not analysed"
    sure = f", {v.confidence:.0%} sure" if v and v.confidence else ""
    title = d.title if d else case.title
    if d:
        fields = "".join(f"<dt>{label}</dt><dd>{escape(getattr(d, name) or 'Not available')}</dd>"
                         for label, name in _FIELDS)
        body = f"<dl>{fields}</dl>"
    else:
        body = (f"<dl><dt>The failure</dt><dd>{escape(case.message or 'Not available')}</dd>"
                + (f"<dt>Why</dt><dd>{escape(v.root_cause)}</dd>" if v else "") + "</dl>"
                "<p>Tessella wrote no ticket for this failure.</p>")
    images = [src for src in map(_image, (d.images if d else case.screenshots)[:_MAX_IMAGES]) if src]
    shots = "<h3>Screenshots</h3>" + "".join(f'<img alt="Screenshot" src="{s}">' for s in images) if images else ""
    patch = (f"<h3>Proposed fix ({escape(d.patch_status)})</h3><pre>{escape(d.patch)}</pre>"
             if d and d.patch else "")
    command = f"tessella review {run} --case {case.id}" if run else f"tessella review --case {case.id}"
    return (f'<section id="case-{escape(case.id, quote=True)}" class="{escape(str(v.category) if v else "none", quote=True)}">'
            f'<div class="kind">{escape(kind)}{sure}</div><h2>{escape(title)}</h2>'
            f'<p class="test">{escape(case.title)}</p>{body}{shots}{patch}'
            f'<h3>Decide</h3>{_actions(case, run, base, online)}'
            f'<p class="hint">{_how(case, online)}</p>'
            f'<code class="cmd">{escape(command)}</code></section>')


def _how(case: Case, online: Online | None) -> str:
    """What the buttons do, in one line, since some go to Jira, some to GitHub, some here."""
    online = online or Online()
    parts = []
    if _jira_create(case, online):
        parts.append("Review and file opens Jira's Create page with this ticket filled in: change "
                     "anything there, then click Create.")
        parts.append("Test or setup problem opens it as a task to fix the test.")
    elif online.github and case.draft:
        parts.append("Approve commits your approval through GitHub; the ticket is filed the next time "
                     "your Tessella dashboard opens with Jira connected.")
    if online.github:
        parts.append("Duplicate, Flaky test and No action needed commit your decision to the repository "
                     "through GitHub, so CI and your team see it.")
    parts.append("To edit or decide in your own Tessella dashboard instead, run:")
    return " ".join(parts)


def render(digest: dict, store: Store, ctx: dict, run: str | None, links: list[tuple[str, str]],
           base: str = "", online: Online | None = None) -> str:
    """The page, for the failures of this run that still need a person."""
    sections, toc = [], []
    for row in digest["rows"]:
        case = store.get_case(row["case_id"]) if row.get("open") else None
        if case is None:
            continue
        sections.append(_section(case, row, run, base.rstrip("/"), online))
        toc.append(f'<li><a href="#case-{escape(case.id, quote=True)}">{escape(case.title)}</a></li>')
    where = " · ".join(escape(v) for v in (ctx.get("repo"), ctx.get("change"), ctx.get("branch"), ctx.get("commit")) if v)
    nav = "".join(f'<a href="{escape(url, quote=True)}">{escape(name)}</a>' for name, url in links)
    if not sections:
        sections = ["<p>Nothing in this run needs a decision.</p>"]
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Tessella ticket drafts</title><style>{_STYLE}</style></head>'
            f'<body><main><h1>Ticket drafts</h1><p class="where">{where}</p>'
            '<p class="only"><a href="#">All drafts of this run</a></p>'
            f'<div class="all"><p>{escape(digest["headline"] or "")}</p><nav>{nav}</nav>'
            + (f'<ol class="toc">{"".join(toc)}</ol>' if toc else "") + "</div>"
            + "".join(sections)
            + "<p>Nothing is filed to the board until a person approves it.</p></main></body></html>")
