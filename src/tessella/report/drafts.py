"""One plain page with every ticket draft of a CI run, published next to the run's reports.

The email's Open draft links jump to a failure on this page (`#case-<id>`). It uses no scripts
and loads nothing, so a CI provider can show it as it is, and it stays read-only: deciding
happens with `tessella review`, whose command is printed under each draft."""

from __future__ import annotations

import base64
import io
from html import escape
from pathlib import Path

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


def _section(case: Case, row: dict, run: str | None) -> str:
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
            f'<h3>Decide on your machine</h3><code class="cmd">{escape(command)}</code></section>')


def render(digest: dict, store: Store, ctx: dict, run: str | None, links: list[tuple[str, str]]) -> str:
    """The page, for the failures of this run that still need a person."""
    sections, toc = [], []
    for row in digest["rows"]:
        case = store.get_case(row["case_id"]) if row.get("open") else None
        if case is None:
            continue
        sections.append(_section(case, row, run))
        toc.append(f'<li><a href="#case-{escape(case.id, quote=True)}">{escape(case.title)}</a></li>')
    where = " · ".join(escape(v) for v in (ctx.get("repo"), ctx.get("change"), ctx.get("branch"), ctx.get("commit")) if v)
    nav = "".join(f'<a href="{escape(url, quote=True)}">{escape(name)}</a>' for name, url in links)
    if not sections:
        sections = ["<p>Nothing in this run needs a decision.</p>"]
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Tessella ticket drafts</title><style>{_STYLE}</style></head>'
            f'<body><main><h1>Ticket drafts</h1><p class="where">{where}</p>'
            f'<p>{escape(digest["headline"] or "")}</p><nav>{nav}</nav>'
            + (f'<ol class="toc">{"".join(toc)}</ol>' if toc else "")
            + "".join(sections)
            + "<p>Read-only. Nothing is filed to the board without a person approving it.</p></main></body></html>")
