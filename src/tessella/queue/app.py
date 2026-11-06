"""FastAPI + HTMX review queue. Install with `pip install tessella[queue]`.

Every decision is recorded in memory; approve files to Jira (and nothing else does).
"""

from __future__ import annotations

import base64
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from tessella.config import Settings
from tessella.memory.store import Store
from tessella.models import Action, Decision, RejectReason


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
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
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
        from tessella.pipeline import _ticket_gone

        for case, decision in store.recent_decisions():
            if _ticket_gone(settings, decision):
                store.forget(case.signature)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        _reconcile_tickets()
        cases = store.open_cases()
        store.mark_shown([c.id for c in cases])  # starts the review clock for "time saved"
        decisions = store.recent_decisions(current=True)
        return templates.TemplateResponse(
            request, "queue.html",
            {
                "cases": cases,
                "reports": [{"url": f"/reports/{i}/", "name": _report_name(d)}
                            for i, d in enumerate(report_dirs)],
                "allure": {c.id: _allure_link(c, report_dirs) for c in cases},
                "todo": {c.id: _todo(c) for c in cases},
                "suggest": {c.id: _suggested_reason(c) for c in cases},
                "reasons": list(RejectReason),
                "decisions": decisions,
                "latest": _latest_run_matches(store, decisions) if not cases else None,
                "csrf": app.state.csrf,
                "reviewer": getattr(request.state, "reviewer", None),
            },
        )

    @app.get("/report", response_class=HTMLResponse)
    def report_page(view: str = "full"):
        """The `tessella report` dashboard, built from the live store on every load, so a
        decision made in the queue shows up on the next refresh. It makes no Jira calls
        (unlike the queue page), so it never waits on the board. `?view=management` is the
        short page."""
        from tessella import report

        return HTMLResponse(report.render(report.build(settings, store), live=True, view=view,
                                          csrf=app.state.csrf),
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
            from tessella.integrations import github, jira
            from tessella.redact_image import for_upload

            draft = for_upload(case.draft, settings)  # screenshots leave the machine here
            try:
                if settings.board == "jira":
                    (decision.ticket_key, decision.ticket_url,
                     decision.linked_existing) = jira.create_or_link(
                        settings.jira, draft, settings.network.allow)
                else:
                    decision.ticket_key, decision.ticket_url = github.create_issue(
                        settings.github, draft, settings.network.allow)
            except Exception as exc:  # noqa: BLE001 - say why, and record nothing
                from fastapi.responses import PlainTextResponse

                return PlainTextResponse(f"Not filed: {exc}", status_code=502)
            decision.board = settings.board
        store.record(case, decision)
        return RedirectResponse("/", status_code=303)

    return app


def _report_dirs(settings: Settings) -> list[Path]:
    """Report folders from inputs.reports that hold an HTML report (an index.html)."""
    out = []
    for entry in settings.inputs.reports:
        p = Path(entry)
        if p.is_dir() and (p / "index.html").is_file() and p.resolve() not in out:
            out.append(p.resolve())
    return out


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


def _todo(case) -> str:
    """One plain line on what this card asks of the reviewer."""
    from tessella.board import _call
    from tessella.models import Category

    v = case.verdict
    if case.draft:
        if case.draft.possible_duplicates:
            return ("Possible duplicate of " + ", ".join(case.draft.possible_duplicates)
                    + ": check it, then reject as duplicate or approve to file a new ticket.")
        return "A new bug, drafted with every field: approve to file it, or reject with a reason."
    if v is None:
        return "Not analysed yet: run tessella triage with a model to get a verdict."
    if v.category in (Category.product_bug, Category.test_bug):
        return "Judged a bug but no draft was written: re-run tessella triage."
    return _call(v)[0].upper() + _call(v)[1:] + "."


def _suggested_reason(case) -> str:
    v = case.verdict
    cat = str(v.category) if v else ""
    return cat if cat in {r.value for r in RejectReason} else ""


def _latest_run_matches(store: Store, decisions) -> dict | None:
    """When the queue is empty, how many of the latest run's failures match earlier
    decisions, so an empty queue after a red run is not a mystery."""
    runs = store.runs()
    if not runs or not decisions:
        return None
    run = runs[-1]
    detail = run["detail"] if isinstance(run["detail"], dict) else {}
    seen: dict[str, str] = {}  # signature -> what `tessella forget` takes for it
    for case, decision in decisions:  # newest first: the latest decision per signature
        if case.signature in detail and case.signature not in seen:
            seen[case.signature] = decision.ticket_key or case.signature[:8]
    if not seen:
        return None
    at = datetime.fromisoformat(run["at"])
    return {"failures": sum(detail[sig] for sig in seen), "total": run["failures"],
            "today": at.date() == datetime.now().date(),
            # a ticket key reads better than a signature prefix in the hint
            "forget": next((k for s, k in seen.items() if k != s[:8]), next(iter(seen.values())))}


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
