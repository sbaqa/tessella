"""What became of each failure: the earlier decision and its ticket, in `triage` and in the
queue's "Recently decided" list."""

import json
from datetime import datetime

import pytest

from tessella.memory.store import Store
from tessella.models import Action, Case, Category, Decision, Origin, RejectReason, Verdict

JUNIT = ('<testsuite timestamp="2026-10-30T10:00:00">'
         '<testcase classname="tests.test_orders" name="test_cancel" time="1">'
         '<failure message="AssertionError: status 500">boom</failure></testcase>'
         '<testcase classname="tests.test_login" name="test_login" time="1">'
         '<failure message="TimeoutError: login button">slow</failure></testcase>'
         '</testsuite>')


def _triage(tmp_path, monkeypatch, *args):
    typer_testing = pytest.importorskip("typer.testing")
    from tessella.cli import app

    monkeypatch.setenv("COLUMNS", "220")
    report = tmp_path / "junit.xml"
    report.write_text(JUNIT)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"storage: {{path: {tmp_path / 't.sqlite'}}}\ninputs: {{traces: []}}\n")
    result = typer_testing.CliRunner().invoke(
        app, ["triage", str(report), "-c", str(cfg), "--no-rerun", "--no-model", *args])
    assert result.exit_code == 0, result.output
    return result.output


def _decide_one(tmp_path) -> Case:
    """Approve the cancel-order failure as KAN-7 on 18 Oct, as the queue would."""
    store = Store(tmp_path / "t.sqlite")
    case = next(c for c in store.open_cases() if "cancel" in c.title)
    store.record(case, Decision(action=Action.approve, by="anna", at=datetime(2026, 10, 18, 9, 30),
                                board="jira", ticket_key="KAN-7",
                                ticket_url="https://x.atlassian.net/browse/KAN-7"))
    return case


def test_triage_table_shows_the_recalled_ticket_and_lists_keys_in_the_header(tmp_path, monkeypatch):
    first = _triage(tmp_path, monkeypatch)
    assert "0 already decided)" in first and "→ queue" in first
    _decide_one(tmp_path)
    out = _triage(tmp_path, monkeypatch)
    assert "(1 already decided: KAN-7)" in out
    assert "KAN-7 · approved 18 Oct" in out
    assert "tessella forget KAN-7" in out


def test_triage_json_carries_each_decision_and_ticket(tmp_path, monkeypatch):
    _triage(tmp_path, monkeypatch)
    case = _decide_one(tmp_path)
    data = json.loads(_triage(tmp_path, monkeypatch, "--json"))
    rows = {r["signature"]: r for r in data["clusters"]}
    done = rows.pop(case.signature)
    assert done["ticket_key"] == "KAN-7"
    assert done["ticket_url"] == "https://x.atlassian.net/browse/KAN-7"
    assert done["decision"] == "approve" and done["decided_by"] == "anna"
    assert done["decided_at"] == "2026-10-18T09:30:00"
    (other,) = rows.values()
    assert other["decision"] is None and other["ticket_key"] is None
    assert data["recalled"] == 1


def test_ticket_cell_links_the_key_and_explains_rejections():
    from tessella.cli import _ticket_cell

    lead = Case(id="c", origin=Origin.test_failure, title="t", signature="s")
    approved = _ticket_cell(lead, Decision(action=Action.approve, by="a", at=datetime(2026, 10, 18),
                                           ticket_key="KAN-7", ticket_url="https://j/KAN-7"))
    assert approved.plain == "KAN-7 · approved 18 Oct"
    assert approved.spans[0].style.link == "https://j/KAN-7"
    rejected = _ticket_cell(lead, Decision(action=Action.reject, by="a", at=datetime(2026, 10, 18),
                                           reason=RejectReason.flaky))
    assert rejected.plain == "rejected 18 Oct (flaky)"
    assert _ticket_cell(lead, None).plain == "→ queue"


def _queue(tmp_path):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from tessella.config import Settings
    from tessella.queue.app import create_app

    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    return TestClient(create_app(s))


def test_queue_recently_decided_shows_test_ticket_and_who(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    case = Case(id="c1", origin=Origin.test_failure, title="test_cancel",
                test_id="tests/test_orders.py::test_cancel", message="AssertionError: status 500",
                signature="sigcancel",
                verdict=Verdict(category=Category.product_bug, confidence=0.86, root_cause="x"))
    other = Case(id="c2", origin=Origin.test_failure, title="test_login",
                 message="TimeoutError: login button", signature="siglogin")
    for c in (case, other):
        store.save_case(c)
    store.record(case, Decision(action=Action.approve, by="anna", at=datetime(2026, 10, 18, 9, 30),
                                board="jira", ticket_key="KAN-7",
                                ticket_url="https://x.atlassian.net/browse/KAN-7"))
    store.record(other, Decision(action=Action.reject, by="bob", at=datetime(2026, 10, 18, 10, 0),
                                 reason=RejectReason.flaky))
    store.record_run(failures=3, clusters=2, recalled=2, drafted=0,
                     detail={"sigcancel": 2, "siglogin": 1})

    page = _queue(tmp_path).get("/").text
    assert "tests/test_orders.py::test_cancel" in page
    assert "Product bug · 86%" in page
    assert "<strong>approved</strong>" in page and "by anna on 2026-10-18 09:30" in page
    assert '<a href="https://x.atlassian.net/browse/KAN-7"' in page and ">KAN-7</a>" in page
    assert "AssertionError: status 500" in page
    assert "<strong>rejected</strong> (flaky)" in page and "no ticket" in page
    # The queue is empty, and the latest run's failures all match these decisions.
    text = " ".join(page.split())
    assert ("Today's 3 failures match earlier decisions — see below. "
            "To review one again: <code>tessella forget KAN-7</code>") in text.replace("&#39;", "'")

def test_queue_says_nothing_about_matches_while_cases_are_open(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.save_case(Case(id="c1", origin=Origin.test_failure, title="t", signature="s1"))
    store.record_run(failures=1, clusters=1, recalled=0, drafted=0, detail={"s1": 1})
    assert "match earlier decisions" not in _queue(tmp_path).get("/").text


def test_queue_serves_the_allure_report_and_links_each_failure_to_it(tmp_path, monkeypatch):
    from tessella.config import Settings
    from tessella.models import Evidence, EvidenceKind
    from tessella.queue.app import create_app
    from starlette.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    report = tmp_path / "reports" / "allure-report"
    (report / "data" / "test-cases").mkdir(parents=True)
    (report / "index.html").write_text("<html>allure</html>")
    store = Store(tmp_path / "t.sqlite")
    store.save_case(Case(
        id="c1", origin=Origin.test_failure, title="test_x", signature="s1",
        evidence=[Evidence(kind=EvidenceKind.report, summary="Allure status: failed",
                           ref="reports/allure-report/data/test-cases/abc123.json")]))
    s = Settings(storage={"path": str(tmp_path / "t.sqlite")},
                 inputs={"reports": ["reports/allure-report", "missing/"]})
    client = TestClient(create_app(s))
    page = client.get("/").text
    assert '<a href="/reports/0/#categories" target="_blank" rel="noopener">Allure: all failures' in page
    assert 'href="/reports/0/#testresult/abc123"' in page
    assert client.get("/reports/0/").text == "<html>allure</html>"
    assert client.get("/reports/0/../../t.sqlite").status_code == 404  # only the report folder


def test_a_test_that_failed_another_way_is_listed_once(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    old = Case(id="c1", origin=Origin.test_failure, title="test_slow", signature="old")
    store.save_case(old)
    store.record(old, Decision(action=Action.approve, by="anna", at=datetime(2026, 10, 18)))
    store.save_case(old.model_copy(update={"signature": "new", "message": "took 1.15s"}))
    page = _queue(tmp_path).get("/").text
    assert page.count("test_slow") >= 1 and "Recently decided" not in page


def test_decided_failures_link_to_allure_too(tmp_path, monkeypatch):
    from tessella.config import Settings
    from tessella.models import Evidence, EvidenceKind
    from tessella.queue.app import create_app
    from starlette.testclient import TestClient

    monkeypatch.chdir(tmp_path)
    report = tmp_path / "reports" / "allure-report"
    (report / "data" / "test-cases").mkdir(parents=True)
    (report / "index.html").write_text("<html>allure</html>")
    store = Store(tmp_path / "t.sqlite")
    done = Case(id="c2", origin=Origin.test_failure, title="test_done", signature="s2",
                evidence=[Evidence(kind=EvidenceKind.report, summary="Allure status: failed",
                                   ref="reports/allure-report/data/test-cases/def456.json")])
    store.save_case(done)
    store.record(done, Decision(action=Action.approve, by="anna", at=datetime(2026, 11, 6),
                                ticket_key="KAN-7"))
    s = Settings(storage={"path": str(tmp_path / "t.sqlite")},
                 inputs={"reports": ["reports/allure-report"]})
    page = TestClient(create_app(s)).get("/").text
    assert 'href="/reports/0/#testresult/def456"' in page
