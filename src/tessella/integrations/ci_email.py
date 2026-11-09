"""The CI run as an email: the verdict at the top, direct links to the reports, one block per
failure. Sent by `tessella notify` through the SMTP server in the config, which must be in
network.allow. Like the CI comment it carries test names and one-line causes, never logs,
stacks or screenshots, and a failure to send never fails the build."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from html import escape

from tessella.config import Email
from tessella.net import is_allowed

_LABEL = {"product_bug": "Product bug", "test_bug": "Test bug", "flaky": "Flaky", "environment": "Environment",
          "test_data": "Test data", "unknown": "Unclear", "not judged": "Not judged"}
_HUE = {"product_bug": "#2a78d6", "test_bug": "#eb6834", "flaky": "#1baf7a", "environment": "#c98500",
        "test_data": "#d55181", "unknown": "#7d8791", "filed": "#46525d", "none": "#7d8791"}


def _plural(n: int, one: str) -> str:
    return f"{n} {one}{'' if n == 1 else 's'}"


def subject(d: dict, ctx: dict) -> str:
    """One line a person can triage from the inbox. Built from counts and CI names only."""
    where = " ".join(p for p in (ctx.get("repo"), ctx.get("change")) if p)
    total = sum(d["counts"].values())
    parts = [_plural(n, _LABEL[c].lower()) if c in ("product_bug", "test_bug")
             else f"{n} {_LABEL.get(c, c).lower()}" for c, n in d["counts"].items()]
    found = f"{_plural(total, 'failed test')}: {', '.join(parts)}" if total else "no failures"
    return " ".join(f"[Tessella] {where + ': ' if where else ''}{found}".split())


def _short(text: str, limit: int = 400) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def text_body(d: dict, ctx: dict, links: list[tuple[str, str]]) -> str:
    """The plain-text part, for mail clients that do not show HTML."""
    lines = [d["headline"] or "Tessella run summary", ""]
    where = [v for v in (ctx.get("repo"), ctx.get("change"), ctx.get("branch"), ctx.get("commit")) if v]
    if where:
        lines += [" / ".join(where), ""]
    if d["note"]:
        lines += [d["note"], ""]
    if links:
        lines += ["REPORTS", *[f"  {name}: {url}" for name, url in links], ""]
    if d["rows"]:
        lines.append("FAILURES")
    for r in d["rows"]:
        lines += [f"* {r['title']}", f"  Verdict: {r['verdict']}", f"  Cause: {_short(r['cause'])}"]
        if r["fix"]:
            lines.append(f"  Suggested fix: {_short(r['fix'])}")
        lines += [f"  Board: {_short(r['board'])}", ""]
    if d["more"]:
        lines += [f"And {d['more']} more failures: see the full report.", ""]
    lines.append("Nothing is filed to the board without a person approving it.")
    return "\n".join(lines)


def _field(name: str, value: str) -> str:
    if not value:
        return ""
    return ('<tr><td style="padding:3px 12px 3px 0;color:#6b7680;font-size:13px;vertical-align:top;'
            f'white-space:nowrap">{escape(name)}</td><td style="padding:3px 0;font-size:14px;'
            f'color:#121920">{escape(_short(value))}</td></tr>')


def _failure(r: dict) -> str:
    hue = _HUE.get(r["category"], "#7d8791")
    label = _LABEL.get(r["category"], r["verdict"]) if r["category"] not in ("none", "filed") else r["verdict"]
    tests = len(r["tests"])
    count = f" &middot; {tests} tests" if tests > 1 else ""
    fields = "".join([
        _field("Cause", r["cause"]), _field("Actual", r["actual"]), _field("Expected", r["expected"]),
        _field("Suggested fix", r["fix"]), _field("Checked fix", r["checked_fix"]), _field("Board", r["board"]),
    ])
    return f"""<tr><td style="padding:0 0 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border:1px solid #dde2e7;border-left:4px solid {hue};border-radius:6px">
<tr><td style="padding:12px 14px">
<div style="font-size:12px;font-weight:700;color:{hue}">{escape(label)}{count}</div>
<div style="font-size:15px;font-weight:600;color:#121920;padding:2px 0 8px;word-break:break-word">{escape(_short(r['title'], 140))}</div>
<table role="presentation" cellpadding="0" cellspacing="0">{fields}</table>
</td></tr></table></td></tr>"""


def _button(name: str, url: str, first: bool) -> str:
    bg, fg = ("#121920", "#ffffff") if first else ("#ffffff", "#121920")
    return (f'<a href="{escape(url, quote=True)}" style="display:inline-block;margin:0 8px 8px 0;padding:9px 14px;'
            f'background:{bg};color:{fg};border:1px solid #121920;border-radius:6px;font-size:14px;'
            f'font-weight:600;text-decoration:none">{escape(name)}</a>')


def html_body(d: dict, ctx: dict, links: list[tuple[str, str]]) -> str:
    where = " &middot; ".join(escape(v) for v in (ctx.get("repo"), ctx.get("change"), ctx.get("branch"), ctx.get("commit")) if v)
    tiles = "".join(
        f'<td style="padding:0 16px 0 0;font-size:13px;color:#46525d"><b style="font-size:20px;'
        f'color:{_HUE.get(c, "#7d8791")}">{n}</b> {escape(_LABEL.get(c, c))}</td>'
        for c, n in d["counts"].items())
    note = f'<p style="margin:0 0 14px;font-size:13px;color:#6b7680">{escape(d["note"])}</p>' if d["note"] else ""
    buttons = "".join(_button(n, u, i == 0) for i, (n, u) in enumerate(links))
    reports = (f'<h2 style="margin:18px 0 8px;font-size:12px;letter-spacing:.05em;text-transform:uppercase;'
               f'color:#6b7680">Reports</h2><div>{buttons}</div>') if links else ""
    failures = "".join(_failure(r) for r in d["rows"])
    title = ('<h2 style="margin:12px 0 8px;font-size:12px;letter-spacing:.05em;text-transform:uppercase;'
             'color:#6b7680">Failures</h2>') if failures else ""
    more = (f'<p style="margin:0 0 12px;font-size:13px;color:#6b7680">And {d["more"]} more failures: '
            'see the full report.</p>') if d["more"] else ""
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#eef1f4;font-family:system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px">
<tr><td style="background:#fff;border:1px solid #dde2e7;border-radius:8px;padding:18px 20px">
<div style="font-size:13px;font-weight:700;color:#121920">Tessella run summary</div>
<div style="font-size:12px;color:#6b7680;padding:2px 0 12px;word-break:break-word">{where}</div>
<h1 style="margin:0 0 14px;font-size:18px;line-height:1.35;font-weight:650;color:#121920">{escape(d['headline'] or 'Run finished')}</h1>
{note}<table role="presentation" cellpadding="0" cellspacing="0"><tr>{tiles}</tr></table>
{reports}
</td></tr>
<tr><td style="padding:6px 0 0"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td>{title}</td></tr>{failures}<tr><td>{more}</td></tr></table></td></tr>
<tr><td style="padding:4px 2px;font-size:12px;color:#6b7680">Nothing is filed to the board without a person approving it.</td></tr>
</table></td></tr></table></body></html>"""


def send(cfg: Email, subject_line: str, text: str, html: str, allow: list[str]) -> None:
    """Send one message. Raises on any problem; the caller reports it and carries on."""
    if not cfg.to or not cfg.host:
        raise ValueError("no recipients or SMTP host")
    if not is_allowed(f"smtp://{cfg.host}:{cfg.port}", allow):
        raise PermissionError(f"add {cfg.host} to network.allow")
    user, password = cfg.username, cfg.password
    message = EmailMessage()
    message["Subject"] = subject_line
    message["From"] = cfg.sender or user or "tessella@localhost"
    message["To"] = ", ".join(cfg.to)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    if cfg.tls == "ssl":
        client = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=context)
    else:
        client = smtplib.SMTP(cfg.host, cfg.port, timeout=30)
    with client:
        if cfg.tls == "starttls":
            client.starttls(context=context)
        if user and password:
            client.login(user, password)
        client.send_message(message)
