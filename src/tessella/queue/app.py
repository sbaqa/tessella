"""The dashboard, served on this machine. Install with `pip install tessella[queue]`.

Every decision is recorded in memory; approve files to Jira (and nothing else does).
"""

from __future__ import annotations

import base64
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from tessella.config import Settings
from tessella.memory.store import Store
from tessella.models import Action, Decision, RejectReason

# The board could not be reached at all, as opposed to answering with a refusal.
_UNREACHABLE = (httpx.TransportError, OSError)
_REVIEW_PAGE = """<!doctype html><meta charset="utf-8"><title>Load this CI run</title>
<body style="font:16px/1.5 system-ui,sans-serif;max-width:34em;margin:4rem auto;padding:0 1rem">
<h1 style="font-size:1.4rem">Load this CI run?</h1>
<p>Tessella will download its results from <strong>{where}</strong> and add them to your dashboard.</p>
<form method="post" action="/review"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="run" value="{run}"><input type="hidden" name="case" value="{case}">
<button style="font:inherit;padding:.5rem 1rem">Download and open</button> <a href="/">Cancel</a></form>"""


def reviewer(settings: Settings, authorization: str | None) -> str | None:
    """The reviewer named by an HTTP Basic `Authorization` header, if the password matches
    the one in their environment variable. None for anything else."""
    if not authorization or not authorization.lower().startswith("basic "):
        return None
    try:
        name, _, password = base64.b64decode(authorization[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    expected = settings.team.password(name)
    if not expected or not secrets.compare_digest(password.encode(), expected.encode()):
        return None
    return name


def create_app(settings: Settings):
    app = FastAPI(title="Tessella review queue")
    store = Store(settings.storage.path)

    if settings.team.reviewers:
        from fastapi.responses import PlainTextResponse

        @app.middleware("http")
        async def sign_in(request: Request, call_next):
            """With reviewers configured, every page needs one of them signed in."""
            name = reviewer(settings, request.headers.get("authorization"))
            if name is None:
                return PlainTextResponse("Sign in with your reviewer name and password.",
                                         status_code=401,
                                         headers={"WWW-Authenticate": 'Basic realm="Tessella"'})
            request.state.reviewer = name
            return await call_next(request)
    # The HTML test reports Tessella reads (Allure, Playwright, pytest-html), served from
    # their own folders so a reviewer can open the original report next to the draft.
    report_dirs = _report_dirs(settings)
    for i, folder in enumerate(report_dirs):
        from starlette.staticfiles import StaticFiles

        app.mount(f"/reports/{i}", StaticFiles(directory=folder, html=True), name=f"report{i}")

    # Every decision form carries this token. A page on another site can make the browser
    # POST to 127.0.0.1:8484 (no CORS check applies to a plain form), but it cannot read the
    # queue page, so it cannot know the token, and so it cannot approve and file a ticket.
    app.state.csrf = secrets.token_urlsafe(32)

    def _reconcile_tickets():
        """Drop decisions whose Jira ticket was deleted, so the queue reflects reality:
        they leave 'Recently decided' and their cases reappear as open to re-file. Reuses
        pipeline._ticket_gone, which fails open (no-op when Jira is unconfigured/offline)."""
        from tessella.memory import decisions_file
        from tessella.pipeline import _ticket_gone

        for case, decision in store.recent_decisions():
            if _ticket_gone(settings, decision):
                store.forget(case.signature)
                decisions_file.remove(settings, case.signature)

    def _file(case):
        """File a case's draft on the board. Screenshots leave the machine here."""
        from tessella.integrations import github, jira
        from tessella.redact_image import for_upload

        draft = for_upload(case.draft, settings)
        if settings.board == "jira":
            return jira.create_or_link(settings.jira, draft, settings.network.allow)
        return (*github.create_issue(settings.github, draft, settings.network.allow), False)

    def _file_pending():
        """File what was approved while the board could not be reached. Stops at the first
        sign that it still cannot be; anything else stays pending and is tried on the next load."""
        from tessella import egress

        if not settings.board or egress.active():
            return
        for case, decision in store.pending_filings():
            try:
                decision.ticket_key, decision.ticket_url, decision.linked_existing = _file(case)
            except _UNREACHABLE:
                return
            except Exception:  # noqa: BLE001, S112 - the card keeps saying it is not filed yet
                continue
            decision.board, decision.pending = settings.board, False
            store.mark_filed(case, decision)
            from tessella.memory import decisions_file

            decisions_file.save(settings, case, decision)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/report", response_class=HTMLResponse)
    def dashboard(request: Request, view: str = "full"):
        """The one screen: every failure, and in each failure's panel its ticket and the
        decision. Built from the live store on every load. `?view=management` is the short page."""
        from tessella import report

        if view == "full":
            _reconcile_tickets()
            _file_pending()
            store.mark_shown([c.id for c in store.open_cases()])  # starts the review clock for "time saved"
        data = report.build(settings, store)
        for cluster in data["clusters"]:
            for case in cluster["members"]:
                if case.id in data["details"]:
                    data["details"][case.id]["allure"] = _allure_link(cluster["lead"], report_dirs)
        return HTMLResponse(report.render(data, live=True, view=view, csrf=app.state.csrf,
                                          links=[_report_link(i, d) for i, d in enumerate(report_dirs)],
                                          reviewer=getattr(request.state, "reviewer", None)),
                            headers={"Cache-Control": "no-store"})

    @app.get("/cases/{case_id}/image/{n}")
    def image(case_id: str, n: int):
        """Serve the n-th image of a case: its screenshots, then evidence images (diffs).
        Only paths recorded on the case can be served; nothing else on disk is reachable."""
        from fastapi import HTTPException
        from fastapi.responses import FileResponse

        case = store.get_case(case_id)
        images = (case.screenshots + [e.image for e in case.evidence if e.image]) if case else []
        if n < 0 or n >= len(images) or not Path(images[n]).is_file():
            raise HTTPException(status_code=404)
        return FileResponse(images[n])

    @app.post("/cases/{case_id}/decide")
    def decide(request: Request, case_id: str, action: str = Form(...),
               reason: str | None = Form(None),
               by: str = Form("sdet"), csrf: str = Form("")):
        from fastapi import HTTPException

        if not secrets.compare_digest(csrf, app.state.csrf):
            raise HTTPException(status_code=403,
                                detail="Stale or missing form token; reload the queue.")
        case = store.get_case(case_id)
        if case is None:
            return RedirectResponse("/", status_code=303)
        if store.decided(case_id):
            # Already approved/rejected: a double-submit, or a retry after a slow/failed
            # board call. Don't file to the board again (store.record would reject the row
            # anyway, but this also avoids the redundant network round-trip).
            return RedirectResponse("/", status_code=303)
        by = getattr(request.state, "reviewer", None) or by  # signed in: the name is theirs
        decision = Decision(action=Action(action), by=by, at=datetime.now(),
                            reason=RejectReason(reason) if reason else None)
        if decision.action == Action.approve and case.draft and settings.board:
            from tessella import egress

            try:
                if egress.active():
                    raise httpx.ConnectError("offline mode")
                decision.ticket_key, decision.ticket_url, decision.linked_existing = _file(case)
            except _UNREACHABLE:  # no internet: the decision stands and is filed later
                decision.pending = True
            except Exception as exc:  # noqa: BLE001 - say why, and record nothing
                from fastapi.responses import PlainTextResponse

                return PlainTextResponse(f"Not filed: {exc}", status_code=502)
            decision.board = settings.board
        store.record(case, decision)
        from tessella.memory import decisions_file

        decisions_file.save(settings, case, decision)
        return RedirectResponse("/", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    def review_link(run: str = "", case: str = ""):
        """Where the email's Review button lands: straight to the card if this run is already
        here, or a confirmation first. A link must never download or merge anything by itself."""
        from html import escape

        from tessella import review

        if case and store.get_case(case) is not None:
            return RedirectResponse(f"/#case-{quote(case)}", status_code=303)
        try:
            where = review.describe(run)
        except ValueError as exc:
            return HTMLResponse(f"<p>{escape(str(exc))}</p>", status_code=400)
        return HTMLResponse(_REVIEW_PAGE.format(
            where=escape(where), run=escape(run, quote=True), case=escape(case, quote=True),
            csrf=app.state.csrf))

    @app.post("/review")
    async def review_load(request: Request):
        """Download the run the reviewer confirmed, and open the card they came for."""
        from fastapi import HTTPException
        from fastapi.responses import PlainTextResponse

        from tessella import review

        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), app.state.csrf):
            raise HTTPException(status_code=403, detail="Stale or missing form token; reload the queue.")
        try:
            await run_in_threadpool(review.load, settings, store, str(form.get("run", "")))
        except Exception as exc:  # noqa: BLE001 - say why in the page
            return PlainTextResponse(f"Could not load that run: {exc}", status_code=502)
        case = str(form.get("case", ""))
        return RedirectResponse(f"/#case-{quote(case)}" if case else "/", status_code=303)

    def _editable(case_id: str, form) -> object | None:
        """The case a reviewer may still change, after checking the form token."""
        from fastapi import HTTPException

        if not secrets.compare_digest(str(form.get("csrf", "")), app.state.csrf):
            raise HTTPException(status_code=403,
                                detail="Stale or missing form token; reload the queue.")
        case = store.get_case(case_id)
        return case if case and case.draft and not store.decided(case_id) else None

    @app.post("/cases/{case_id}/draft")
    async def write_draft(request: Request, case_id: str):
        """A person says this failure is a bug though Tessella wrote no ticket: an empty draft
        to fill in, with every required field saying what is not known."""
        from fastapi import HTTPException

        from tessella.draft import ticket as tickets
        from tessella.models import Category, Verdict

        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), app.state.csrf):
            raise HTTPException(status_code=403, detail="Stale or missing form token; reload the page.")
        case = store.get_case(case_id)
        if case is None or case.draft or store.decided(case_id):
            return RedirectResponse(f"/#case-{quote(case_id)}", status_code=303)
        known = case.verdict or Verdict(category=Category.product_bug, confidence=0.0,
                                        root_cause="Not determined by the model.")
        case.verdict = known.model_copy(update={"category": Category.product_bug})
        case.draft = tickets.ticket(case)
        store.save_case(case)
        return RedirectResponse(f"/#edit-{quote(case_id)}", status_code=303)

    @app.post("/cases/{case_id}/edit")
    async def edit(request: Request, case_id: str):
        """Save a reviewer's changes to a draft. Filing still needs Approve."""
        from tessella.draft import ticket

        form = await request.form()
        case = _editable(case_id, form)
        if case is None:
            return RedirectResponse("/", status_code=303)
        by = getattr(request.state, "reviewer", None) or str(form.get("by", "sdet"))
        ticket.edit(case, {n: str(form[n]) for n in ticket.EDITABLE if n in form}, by)
        store.save_case(case)
        return RedirectResponse(f"/#case-{case_id}", status_code=303)

    @app.post("/cases/{case_id}/undo-edits")
    async def undo_edits(request: Request, case_id: str):
        """Put back the text Tessella wrote."""
        from tessella.draft import ticket

        case = _editable(case_id, await request.form())
        if case is None:
            return RedirectResponse("/", status_code=303)
        ticket.undo_edits(case)
        store.save_case(case)
        return RedirectResponse(f"/#case-{case_id}", status_code=303)

    return app


def _report_dirs(settings: Settings) -> list[Path]:
    """Report folders from inputs.reports that hold an HTML report (an index.html)."""
    out = []
    for entry in settings.inputs.reports:
        p = Path(entry)
        if p.is_dir() and (p / "index.html").is_file() and p.resolve() not in out:
            out.append(p.resolve())
    return out


def _report_link(i: int, folder: Path) -> dict:
    """The navbar link: Allure opens on Categories, which lists every failed and broken
    test of the run, decided or not; other reports open on their front page."""
    name = _report_name(folder)
    if name == "Allure report":
        return {"url": f"/reports/{i}/#categories", "name": "Allure: all failures"}
    return {"url": f"/reports/{i}/", "name": name}


def _report_name(folder: Path) -> str:
    name = folder.name.lower()
    if "allure" in name:
        return "Allure report"
    if "playwright" in name:
        return "Playwright report"
    return f"Test report ({folder.name})"


def _allure_link(case, report_dirs: list[Path]) -> str | None:
    """This failure in the Allure report, from the report evidence's test-case file."""
    for ev in case.evidence:
        ref = Path(ev.ref or "")
        if ev.kind == "report" and ref.parent.name == "test-cases" and ref.suffix == ".json":
            folder = ref.parent.parent.parent.resolve()  # <report>/data/test-cases/<uid>.json
            if folder in report_dirs:
                return f"/reports/{report_dirs.index(folder)}/#testresult/{ref.stem}"
    return None


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def serve(settings: Settings, port: int = 8484, host: str = "127.0.0.1",
          tls_cert: str | None = None, tls_key: str | None = None,
          no_sign_in: bool = False) -> None:
    """Serve on this machine only, or, for a team, on `host` with every reviewer signed in.
    Refuses to listen beyond this machine without reviewers: anyone who can reach the port
    could otherwise approve and file tickets. `no_sign_in` is for a container whose port is
    published to the host's loopback only (compose.yaml), where the host is the boundary."""
    import uvicorn

    if host not in LOOPBACK_HOSTS and not no_sign_in:
        if not settings.team.reviewers:
            raise SystemExit(f"Refusing to serve the queue on {host} without reviewers: add "
                             "team.reviewers to the config (name: PASSWORD_ENV_VAR).")
        missing = [n for n in settings.team.reviewers if not settings.team.password(n)]
        if missing:
            raise SystemExit("These reviewers have no password in their environment "
                             f"variable: {', '.join(missing)}")
    uvicorn.run(create_app(settings), host=host, port=port,
                ssl_certfile=tls_cert, ssl_keyfile=tls_key)
