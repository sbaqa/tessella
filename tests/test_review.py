"""CI's results reach a laptop, decisions travel back, and rejected failures get a second look."""

import base64
import io
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from tessella import review
from tessella.config import Settings
from tessella.integrations import ci_artifacts
from tessella.memory import decisions_file
from tessella.memory.store import Store
from tessella.models import Action, Case, Category, Decision, Evidence, EvidenceKind, Origin, RejectReason, Verdict

NOW = datetime(2026, 11, 9, 10, 0)


def _case(cid="c1", sig="sig", verdict=True):
    c = Case(id=cid, origin=Origin.test_failure, title="test_total", message="1 != 2", signature=sig)
    if verdict:
        c.verdict = Verdict(category=Category.product_bug, confidence=0.9, root_cause="rounding")
    return c


def _settings(tmp_path):
    s = Settings()
    s.storage.path = str(tmp_path / "local" / "tessella.sqlite")
    s.inputs.repo = str(tmp_path)
    return s


def _rejected(reason=RejectReason.not_a_bug, at=NOW):
    return Decision(action=Action.reject, reason=reason, by="", at=at)


def _database(tmp_path, *cases):
    path = tmp_path / "ci.sqlite"
    store = Store(path)
    for c in cases:
        store.save_case(c)
    target = tmp_path / "exported.sqlite"
    target.unlink(missing_ok=True)
    store.snapshot(target)
    store.db.close()
    return target


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# -- the snapshot ---------------------------------------------------------------------------


def test_snapshot_is_one_complete_file(tmp_path):
    db = _database(tmp_path, _case())
    assert sqlite3.connect(db).execute("select count(*) from cases").fetchone() == (1,)


# -- downloading from each CI ---------------------------------------------------------------


@pytest.fixture
def artifact(tmp_path):
    return _zip({"tessella.sqlite": _database(tmp_path, _case()).read_bytes(),
                 "screenshots/c1/shot.png": b"png", "notes/other.txt": b"ignored"})


@respx.mock
def test_github_follows_the_redirect_without_the_token(tmp_path, artifact, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    respx.get("https://api.github.com/repos/o/r/actions/runs/9/artifacts").respond(
        json={"artifacts": [{"expired": False, "archive_download_url": "https://api.github.com/zip/1"}]})
    respx.get("https://api.github.com/zip/1").respond(302, headers={"location": "https://x.blob.core.windows.net/a"})
    blob = respx.get("https://x.blob.core.windows.net/a").respond(content=artifact)
    out = tmp_path / "got"
    out.mkdir()
    ci_artifacts.fetch("https://github.com/o/r/actions/runs/9", out)
    assert (out / "tessella.sqlite").is_file() and (out / "screenshots/c1/shot.png").read_bytes() == b"png"
    assert not (out / "notes").exists()
    assert "authorization" not in blob.calls[0].request.headers


@respx.mock
def test_a_redirect_to_an_unknown_host_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    respx.get("https://api.github.com/repos/o/r/actions/runs/9/artifacts").respond(
        json={"artifacts": [{"expired": False, "archive_download_url": "https://api.github.com/zip/1"}]})
    respx.get("https://api.github.com/zip/1").respond(302, headers={"location": "https://evil.example/a"})
    with pytest.raises(RuntimeError, match="refused a redirect"):
        ci_artifacts.fetch("https://github.com/o/r/actions/runs/9", tmp_path)


@respx.mock
def test_gitlab_finds_the_job_with_artifacts(tmp_path, artifact, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    base = "https://gl.example/api/v4/projects/g%2Fp"
    respx.get(f"{base}/pipelines/5/jobs").respond(json=[{"id": 1, "artifacts": []},
                                                        {"id": 2, "artifacts": [{"file_type": "archive"}]}])
    route = respx.get(f"{base}/jobs/2/artifacts").respond(content=artifact)
    ci_artifacts.fetch("https://gl.example/g/p/-/pipelines/5", tmp_path)
    assert route.calls[0].request.headers["private-token"] == "t" and (tmp_path / "tessella.sqlite").is_file()


@respx.mock
def test_azure_devops(tmp_path, artifact, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "pat")
    respx.get("https://dev.azure.com/org/proj/_apis/build/builds/7/artifacts").respond(
        json={"resource": {"downloadUrl": "https://a1.artifacts.visualstudio.com/x?format=zip"}})
    route = respx.get("https://a1.artifacts.visualstudio.com/x").respond(
        content=_zip({"tessella-db/tessella.sqlite": _database(tmp_path, _case()).read_bytes()}))
    ci_artifacts.fetch("https://dev.azure.com/org/proj/_build/results?buildId=7&view=results", tmp_path)
    expected = "Basic " + base64.b64encode(b":pat").decode()
    assert route.calls[0].request.headers["authorization"] == expected and (tmp_path / "tessella.sqlite").is_file()


@respx.mock
def test_jenkins_including_folders(tmp_path, artifact, monkeypatch):
    monkeypatch.setenv("JENKINS_USER", "u")
    monkeypatch.setenv("JENKINS_TOKEN", "t")
    route = respx.get("https://ci.example/job/team/job/app/12/artifact/*zip*/archive.zip").respond(content=artifact)
    ci_artifacts.fetch("https://ci.example/job/team/job/app/12/console", tmp_path)
    assert route.called and (tmp_path / "tessella.sqlite").is_file()


@respx.mock
def test_circleci(tmp_path, monkeypatch):
    monkeypatch.setenv("CIRCLE_TOKEN", "t")
    db = _database(tmp_path, _case()).read_bytes()
    respx.get("https://circleci.com/api/v2/project/gh/o/r/44/artifacts").respond(json={"items": [
        {"path": "tessella-db/tessella.sqlite", "url": "https://output.circle-artifacts.com/a"},
        {"path": "other.log", "url": "https://output.circle-artifacts.com/skip"}]})
    respx.get("https://output.circle-artifacts.com/a").respond(content=db)
    ci_artifacts.fetch("https://app.circleci.com/pipelines/github/o/r/3/workflows/abc/jobs/44", tmp_path)
    assert (tmp_path / "tessella.sqlite").is_file()


def test_any_other_ci_by_file(tmp_path):
    db = _database(tmp_path, _case())
    out = tmp_path / "out"
    out.mkdir()
    ci_artifacts.fetch(str(db), out)
    assert (out / "tessella.sqlite").is_file()


def test_missing_token_says_which_one(tmp_path, monkeypatch):
    for name in ("AZURE_DEVOPS_PAT", "AZURE_DEVOPS_EXT_PAT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ci_artifacts.NoAccess, match="AZURE_DEVOPS_PAT"):
        ci_artifacts.fetch("https://dev.azure.com/o/p/_build/results?buildId=1", tmp_path)


def test_unknown_link_and_a_file_that_is_not_a_database(tmp_path):
    with pytest.raises(ValueError, match="Download the"):
        ci_artifacts.fetch("https://example.com/whatever", tmp_path)
    bad = tmp_path / "bad.sqlite"
    bad.write_text("not sqlite")
    out = tmp_path / "o"
    out.mkdir()
    with pytest.raises(ValueError, match="not a database"):
        ci_artifacts.fetch(str(bad), out)


def test_archive_cannot_write_outside_the_folder(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    db = _database(tmp_path, _case()).read_bytes()
    blob = tmp_path / "a.zip"
    blob.write_bytes(_zip({"tessella.sqlite": db, "screenshots/../../evil.png": b"x", "screenshots/../evil/x.png": b"x"}))
    ci_artifacts.fetch(str(blob), out)
    assert not (tmp_path / "evil.png").exists() and not list(out.rglob("evil*"))


# -- loading into the laptop's store ------------------------------------------------------------


def test_load_merges_and_moves_screenshots_here(tmp_path):
    shot = "/home/runner/work/.tessella/screenshots/c1/shot.png"
    case = _case()
    case.screenshots = [shot]
    folder = tmp_path / "artifact"
    (folder / "screenshots/c1").mkdir(parents=True)
    (folder / "screenshots/c1/shot.png").write_bytes(b"png")
    (folder / "tessella.sqlite").write_bytes(_database(tmp_path, case).read_bytes())
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    assert review.load(s, store, str(folder))["cases"] == 1
    path = Path(store.get_case("c1").screenshots[0])
    assert path.read_bytes() == b"png" and path.is_relative_to(Path(s.storage.path).parent)


def test_a_newer_run_updates_an_undecided_case_but_not_an_edited_one(tmp_path):
    from tessella.draft.ticket import edit, ticket

    s = _settings(tmp_path)
    store = Store(s.storage.path)
    old = _case()
    old.verdict.root_cause = "old"
    store.save_case(old)
    new = _case()
    new.verdict.root_cause = "new"
    store.db.execute("UPDATE cases SET created_at = '2000-01-01'")
    store.db.commit()
    db = _database(tmp_path, new)
    store.merge_from(db)
    assert store.get_case("c1").verdict.root_cause == "new"
    assert store.merge_from(db) == {k: 0 for k in store.merge_from(db)}  # still idempotent
    mine = store.get_case("c1")
    mine.draft = ticket(mine)
    edit(mine, {"root_cause": "my words"}, "anna")
    store.save_case(mine)
    store.db.execute("UPDATE cases SET created_at = '2000-01-01'")
    store.db.commit()
    store.merge_from(db)
    assert store.get_case("c1").draft.root_cause == "my words"


# -- the decisions file -------------------------------------------------------------------------


@pytest.fixture
def real_file(monkeypatch):
    monkeypatch.setattr(decisions_file, "ENABLED", True)


def test_file_holds_ids_and_decisions_only(tmp_path, real_file):
    s = _settings(tmp_path)
    case = _case()
    case.message = "secret log line"
    decisions_file.save(s, case, Decision(action=Action.approve, by="anna", at=NOW, board="jira", ticket_key="KAN-1"))
    text = (tmp_path / "tessella-decisions.jsonl").read_text()
    assert json.loads(text) == {"case_id": "c1", "signature": "sig", "action": "approve", "board": "jira",
                                "ticket_key": "KAN-1", "at": NOW.isoformat()}
    assert "secret" not in text and "anna" not in text


def test_a_new_decision_replaces_the_line_and_forget_removes_it(tmp_path, real_file):
    s = _settings(tmp_path)
    decisions_file.save(s, _case(), _rejected())
    decisions_file.save(s, _case(), Decision(action=Action.approve, by="a", at=NOW, ticket_key="KAN-1"))
    decisions_file.save(s, _case("c2", "other"), _rejected())
    lines = (tmp_path / "tessella-decisions.jsonl").read_text().splitlines()
    assert len(lines) == 2 and "KAN-1" in "".join(lines)
    decisions_file.remove(s, "sig")
    assert len((tmp_path / "tessella-decisions.jsonl").read_text().splitlines()) == 1


def test_ci_reads_the_file_and_local_decisions_stand(tmp_path, real_file):
    s = _settings(tmp_path)
    decisions_file.save(s, _case(), Decision(action=Action.approve, by="a", at=NOW, board="jira", ticket_key="KAN-1"))
    (tmp_path / "tessella-decisions.jsonl").open("a").write("<<<<<<< a merge conflict\n")
    store = Store(s.storage.path)
    assert decisions_file.load(s, store) == 1 and decisions_file.load(s, store) == 0
    assert store.last_decision("sig").ticket_key == "KAN-1"


def test_a_rejection_the_recheck_undid_is_not_imported_again(tmp_path, real_file):
    s = _settings(tmp_path)
    decisions_file.save(s, _case(), _rejected())
    store = Store(s.storage.path)
    decisions_file.load(s, store)
    store.reopen("sig", store.last_decision("sig"))
    assert store.last_decision("sig") is None
    assert decisions_file.load(s, store) == 0 and store.last_decision("sig") is None


def test_merge_carries_a_reopened_rejection_to_the_laptop(tmp_path):
    mine = Store(tmp_path / "mine.sqlite")
    mine.save_case(_case())
    mine.record(_case(), _rejected())
    other = Store(tmp_path / "ci.sqlite")
    other.reopen("sig", _rejected())
    other.db.close()
    mine.merge_from(tmp_path / "ci.sqlite")
    assert mine.last_decision("sig") is None and [c.id for c in mine.open_cases()] == ["c1"]


# -- re-checking rejected failures ---------------------------------------------------------------


JUNIT = """<testsuite tests="1" failures="1"><testcase classname="t" name="test_total">
<failure message="AssertionError: 1 != 2">AssertionError: 1 != 2</failure></testcase></testsuite>"""


@pytest.fixture
def rejected(tmp_path, monkeypatch):
    """A failure rejected as not a bug, and a triage whose evidence and model the test controls."""
    from tessella import pipeline

    report = tmp_path / "junit.xml"
    report.write_text(JUNIT)
    s = _settings(tmp_path)
    s.inputs.reports = [str(report)]
    monkeypatch.setattr(pipeline, "_warm_models", lambda _: None)
    state = {"verdict": Verdict(category=Category.flaky, confidence=0.9, root_cause="timing"), "calls": 0, "source": "v1"}

    def gather(lead, *a, **k):
        lead.evidence.append(Evidence(kind=EvidenceKind.test_source, summary="test", excerpt=state["source"]))

    def judge(lead, *a, **k):
        state["calls"] += 1
        state["seen"] = [e.summary for e in lead.evidence]
        return state["verdict"]

    monkeypatch.setattr(pipeline, "_gather", gather)
    monkeypatch.setattr(pipeline, "_judge", judge)
    first = pipeline.triage(s, rerun=False, use_model=False).cases[0]
    store = Store(s.storage.path)
    first.verdict = Verdict(category=Category.product_bug, confidence=0.8, root_cause="first look")
    first.evidence = [Evidence(kind=EvidenceKind.test_source, summary="test", excerpt="v1")]
    store.save_case(first)
    store.record(first, Decision(action=Action.reject, reason=RejectReason.not_a_bug, by="anna", at=NOW))
    state["calls"] = 0
    return s, store, state, first


def test_nothing_new_means_no_model_call(rejected):
    from tessella import pipeline

    s, store, state, _ = rejected
    result = pipeline.triage(s, rerun=False)
    assert state["calls"] == 0 and result.recalled == 1 and store.last_decision(result.cases[0].signature)


def test_new_evidence_that_changes_the_verdict_reopens_it_with_the_history(rejected):
    from tessella import board, pipeline

    s, store, state, first = rejected
    state["source"], state["verdict"] = "v2", Verdict(category=Category.product_bug, confidence=0.9, root_cause="now clear")
    result = pipeline.triage(s, rerun=False)
    assert state["calls"] == 1 and any("rejected as not_a_bug" in x or "rejected as not a bug" in x for x in state["seen"])
    assert store.last_decision(first.signature) is None
    saved = store.open_cases()[0]
    assert saved.draft and "rejected as not a bug" in saved.reopened_from
    row = board.classify(result)[0]
    assert row.status == board.Status.new and "new evidence changed the verdict" in row.note


def test_new_evidence_that_changes_nothing_stands_and_is_not_asked_twice(rejected):
    from tessella import pipeline

    s, store, state, first = rejected
    state["source"] = "v2"
    pipeline.triage(s, rerun=False)
    assert state["calls"] == 1 and store.last_decision(first.signature).action == Action.reject
    pipeline.triage(s, rerun=False)
    assert state["calls"] == 1  # the same new evidence is not judged again


def test_a_duplicate_rejection_is_not_rechecked(rejected):
    from tessella import pipeline

    s, store, state, first = rejected
    store.forget(first.signature)
    store.record(first, Decision(action=Action.reject, reason=RejectReason.duplicate, by="a", at=NOW))
    state["source"] = "v2"
    pipeline.triage(s, rerun=False)
    assert state["calls"] == 0


def test_without_a_model_a_rejection_simply_stands(rejected):
    from tessella import pipeline

    s, _, state, _ = rejected
    state["source"] = "v2"
    assert pipeline.triage(s, rerun=False, use_model=False).recalled == 1 and state["calls"] == 0


# -- approving offline, and the link from the email ------------------------------------------------


@pytest.fixture
def queue(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from tessella.draft.ticket import ticket
    from tessella.integrations import jira
    from tessella.queue.app import create_app

    s = Settings(board="jira")
    s.storage.path = str(tmp_path / "s.sqlite")
    store = Store(s.storage.path)
    case = _case()
    case.draft = ticket(case)
    store.save_case(case)
    app = create_app(s)
    calls = []
    return TestClient(app), app, store, jira, calls, monkeypatch


def test_approving_offline_records_it_and_files_when_back(queue):
    client, app, store, jira, calls, patch = queue

    def down(*a):
        raise httpx.ConnectError("no network")

    patch.setattr(jira, "create_or_link", down)
    r = client.post("/cases/c1/decide", data={"action": "approve", "csrf": app.state.csrf}, follow_redirects=False)
    assert r.status_code == 303
    d = store.last_decision("sig")
    assert d.action == Action.approve and d.pending and d.ticket_key is None
    assert "filed when the board can be reached" in client.get("/").text  # still down: stays pending
    patch.setattr(jira, "create_or_link", lambda *a: ("KAN-3", "https://j/KAN-3", False))
    client.get("/")
    d = store.last_decision("sig")
    assert d.ticket_key == "KAN-3" and not d.pending


def test_a_refusal_from_the_board_is_still_an_error(queue):
    client, app, store, jira, _, patch = queue

    def refuse(*a):
        raise RuntimeError("bad project")

    patch.setattr(jira, "create_or_link", refuse)
    r = client.post("/cases/c1/decide", data={"action": "approve", "csrf": app.state.csrf})
    assert r.status_code == 502 and store.last_decision("sig") is None


def test_a_pending_approval_is_not_reopened_by_triage():
    from tessella.pipeline import _filed_nothing

    s = Settings(board="jira")
    s.jira.url, s.jira.project = "https://j", "KAN"
    assert not _filed_nothing(s, Decision(action=Action.approve, by="a", at=NOW, pending=True))
    assert _filed_nothing(s, Decision(action=Action.approve, by="a", at=NOW))


def test_review_link_opens_a_known_card_and_never_downloads_by_itself(queue, monkeypatch):
    client, app, _store, *_ = queue
    r = client.get("/review?case=c1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#case-c1"

    def boom(*a):
        raise AssertionError("downloaded on a GET")

    monkeypatch.setattr(review, "load", boom)
    page = client.get("/review?run=https://github.com/o/r/actions/runs/9&case=other")
    assert page.status_code == 200 and "github.com" in page.text and "Download and open" in page.text
    assert client.get("/review?run=https://example.com/x").status_code == 400


def test_review_post_needs_the_token_then_loads(queue, monkeypatch):
    client, app, *_ = queue
    loaded = []
    monkeypatch.setattr(review, "load", lambda s, st, run: loaded.append(run))
    data = {"run": "https://github.com/o/r/actions/runs/9", "case": "other"}
    assert client.post("/review", data=data).status_code == 403 and not loaded
    r = client.post("/review", data={**data, "csrf": app.state.csrf}, follow_redirects=False)
    assert r.headers["location"] == "/#case-other" and loaded == [data["run"]]


def _email_rows():
    row = {"title": "t", "tests": ["t"], "verdict": "v", "category": "product_bug", "cause": "c", "board": "b",
           "actual": "", "expected": "", "fix": "", "checked_fix": "", "case_id": "c1", "open": True,
           "ticket_key": None, "ticket_url": None}
    filed = {**row, "open": False, "case_id": "c2", "title": "filed", "ticket_key": "KAN-7", "ticket_url": "https://j/KAN-7"}
    return {"headline": "h", "note": None, "counts": {"product_bug": 2}, "rows": [row, filed], "more": 0}, row


def test_email_links_each_failure_to_its_draft_and_never_to_localhost():
    from tessella.integrations import ci_email

    d, row = _email_rows()
    run, drafts = "https://github.com/o/r/actions/runs/9", "https://github.com/o/r/actions/runs/9/artifacts/5"
    html = ci_email.html_body(d, {}, [], run, drafts)
    assert f'href="{drafts}#case-c1"' in html and html.count(">Open draft</a>") == 1
    assert 'href="https://j/KAN-7"' in html and ">Open KAN-7</a>" in html
    assert "127.0.0.1" not in html and "localhost" not in html and f"tessella review {run}" in html
    text = ci_email.text_body(d, {}, [], run, drafts)
    assert f"Open draft: {drafts}#case-c1" in text and "localhost" not in text and "127.0.0.1" not in text
    assert ci_email.review_url(row, run, None) is None
    assert ci_email.review_url(row, run, "https://tessella.lan:8484/").startswith("https://tessella.lan:8484/review?")


def test_drafts_page_is_complete_escaped_and_has_no_scripts(tmp_path):
    from tessella.draft.ticket import ticket
    from tessella.report import drafts

    store = Store(tmp_path / "s.sqlite")
    case = _case()
    case.verdict.root_cause = "<script>alert(1)</script>"
    case.draft = ticket(case)
    store.save_case(case)
    bare = _case("c9", "s9", verdict=False)
    bare.message = "boom"
    store.save_case(bare)
    filed = _case("c8", "s8")
    store.save_case(filed)
    d = {"headline": "h", "rows": [{"case_id": "c1", "open": True}, {"case_id": "c9", "open": True}, {"case_id": "c8", "open": False}]}
    run = "https://github.com/o/r/actions/runs/9"
    page = drafts.render(d, store, {"repo": "o/r"}, run, [("Allure report", "https://x/a")])
    assert 'id="case-c1"' in page and 'id="case-c9"' in page and 'id="case-c8"' not in page
    for label in ("Description", "Steps to reproduce", "Actual result", "Expected result", "Environment",
                  "Root cause (suspected)", "Suggested fix"):
        assert label in page
    assert "<script" not in page and "&lt;script&gt;" in page and "boom" in page
    assert f"tessella review {run} --case c1" in page and 'href="https://x/a"' in page


def test_drafts_page_embeds_screenshots(tmp_path):
    from PIL import Image

    from tessella.draft.ticket import ticket
    from tessella.report import drafts

    shot = tmp_path / "s.png"
    Image.new("RGB", (2000, 1000), "white").save(shot)
    store = Store(tmp_path / "s.sqlite")
    case = _case()
    case.screenshots = [str(tmp_path / "missing.png"), str(shot)]
    case.draft = ticket(case)
    store.save_case(case)
    page = drafts.render({"headline": "", "rows": [{"case_id": "c1", "open": True}]}, store, {}, None, [])
    assert page.count('src="data:image/jpeg;base64,') == 1 and "tessella review --case c1" in page


def test_each_ci_gives_its_run_link():
    from tessella.integrations import ci_comment

    assert ci_comment.run_url({"TF_BUILD": "True", "SYSTEM_COLLECTIONURI": "https://dev.azure.com/org/",
                               "SYSTEM_TEAMPROJECT": "my proj", "BUILD_BUILDID": "7"}) == \
        "https://dev.azure.com/org/my%20proj/_build/results?buildId=7"
    assert ci_comment.run_url({"JENKINS_URL": "x", "BUILD_URL": "https://j/job/a/1/"}) == "https://j/job/a/1/"
    assert ci_comment.run_url({"CIRCLECI": "true", "CIRCLE_BUILD_URL": "https://c/1"}) == "https://c/1"
    for link in ("https://dev.azure.com/org/my%20proj/_build/results?buildId=7", "https://j.example/job/a/1/",
                 "https://app.circleci.com/pipelines/github/o/r/3/workflows/a/jobs/4",
                 "https://gl.example/g/p/-/pipelines/5", "https://github.com/o/r/actions/runs/9"):
        assert ci_artifacts.host_of(link)


def test_a_test_failing_a_new_way_on_ci_reaches_the_laptop_despite_an_old_decision(tmp_path):
    mine = Store(tmp_path / "mine.sqlite")
    old = _case("slow", "old-failure")
    mine.save_case(old)
    mine.record(old, Decision(action=Action.approve, by="anna", at=NOW))
    mine.db.execute("UPDATE cases SET created_at = '2000-01-01'")
    mine.db.commit()
    db = _database(tmp_path, _case("slow", "new-failure"))
    mine.merge_from(db)
    assert mine.get_case("slow").signature == "new-failure" and [c.id for c in mine.open_cases()] == ["slow"]
    assert mine.merge_from(db) == {k: 0 for k in mine.merge_from(db)}
