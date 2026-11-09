"""`tessella report`: the numbers on the page must match the store, and the page must be safe
to open (test titles are untrusted text) and self-contained (nothing fetched)."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tessella import report
from tessella.config import Settings
from tessella.memory.store import Store
from tessella.models import (Action, Case, Category, Decision, Draft, DraftKind, Evidence,
                             EvidenceKind, Origin, RejectReason, Verdict)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 24, 12, 0)


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    return s


def _case(sig, n, category=Category.product_bug, title=None, draft=True, evidence_used=(0,)):
    cid = Case.make_id(sig, str(n))
    return Case(
        id=cid, origin=Origin.test_failure, title=title or f"{sig}#t{n}", message="boom",
        signature=sig,
        evidence=[Evidence(kind=EvidenceKind.rerun, summary="rerun")],
        verdict=Verdict(category=category, confidence=0.9, root_cause="rc",
                        evidence_used=list(evidence_used)),
        draft=Draft(kind=DraftKind.ticket, title="t", body="b") if draft else None,
    )


def _seed(store):
    """Cluster a: 3 failures, approved and filed. b: 1 failure, rejected as flaky.
    c: 2 failures, still open and no draft."""
    for n in range(3):
        store.save_case(_case("a", n))
    store.save_case(_case("b", 0, Category.flaky, draft=False))
    for n in range(2):
        store.save_case(_case("c", n, Category.environment, draft=False))
    store.record(_case("a", 0), Decision(
        action=Action.approve, by="sdet", at=NOW, board="jira", ticket_key="KAN-1",
        ticket_url="https://example.atlassian.net/browse/KAN-1"))
    store.record(_case("b", 0), Decision(
        action=Action.reject, reason=RejectReason.flaky, by="sdet", at=NOW))


def test_build_counts_match_the_store(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    _seed(store)
    data = report.build(s, store, now=NOW, project="shop")
    t = data["totals"]
    assert (t["failures"], t["clusters"]) == (6, 3)
    assert (t["approved"], t["rejected"], t["open"], t["filed"]) == (1, 1, 1, 1)
    assert t["drafted"] == 1 and t["drafted_filed"] == 1
    assert data["categories"] == {"product_bug": 1, "flaky": 1, "environment": 1}
    assert data["trust"]["per_category"]["flaky"] == {"approved": 0, "rejected": 1}
    assert data["trust"]["reasons"] == {"flaky": 1}
    assert data["trust"]["enough"] is False  # 2 reviews is not a trend
    # largest cluster first
    assert [c["signature"] for c in data["clusters"]][0] == "a"


def test_empty_store_renders_with_empty_states(tmp_path):
    s = _settings(tmp_path)
    page = report.render(report.build(s, Store(s.storage.path), now=NOW, project="shop"))
    assert "No failures are stored yet" in page
    assert "No model calls are logged" in page
    assert "Run history starts with your next" in page
    assert "{{" not in page  # no placeholder left behind


def test_run_history_round_trip_and_recurring(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    _seed(store)
    store.record_run(6, 3, 0, 1, {"a": 3, "b": 1, "c": 2}, at=NOW - timedelta(days=1))
    store.record_run(4, 2, 2, 1, {"a": 3, "c": 1}, at=NOW)
    runs = store.runs()
    assert [r["failures"] for r in runs] == [6, 4] and runs[1]["detail"] == {"a": 3, "c": 1}
    data = report.build(s, store, now=NOW)
    assert {r["signature"]: r["runs"] for r in data["recurring"]} == {"a": 2, "c": 2}
    assert "Came back" in report.render(data)


def test_damaged_run_detail_does_not_break_the_report(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    store.db.execute("INSERT INTO runs (at, failures, clusters, recalled, drafted, detail) "
                     "VALUES ('2026-10-01T00:00:00', 1, 1, 0, 0, 'not json')")
    store.db.commit()
    assert store.runs()[0]["detail"] == {}
    report.render(report.build(s, store, now=NOW))


def test_triage_records_a_run(tmp_path):
    from tessella.pipeline import triage

    s = _settings(tmp_path)
    s.inputs.reports = [str(FIXTURES / "junit_sample.xml")]
    result = triage(s, rerun=False, use_model=False)
    runs = Store(s.storage.path).runs()
    assert len(runs) == 1
    assert runs[0]["failures"] == len(result.cases) == 3
    assert runs[0]["clusters"] == len(result.clusters)
    assert sum(runs[0]["detail"].values()) == 3


def test_page_escapes_untrusted_titles_and_never_expands_placeholders(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    store.save_case(_case("x", 0, title='<img src=x onerror=alert(1)> {{cases}} "quoted"'))
    page = report.render(report.build(s, store, now=NOW))
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt; {{cases}}" in page


def test_ticket_link_must_be_http(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    store.save_case(_case("x", 0))
    store.record(_case("x", 0), Decision(
        action=Action.approve, by="sdet", at=NOW, board="jira", ticket_key="KAN-9",
        ticket_url="javascript:alert(1)"))
    page = report.render(report.build(s, store, now=NOW))
    assert "javascript:" not in page and "KAN-9" in page


def test_page_is_self_contained(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    _seed(store)
    page = report.render(report.build(s, store, now=NOW))
    assert not re.search(r'<script[^>]+src=|<link[^>]+href=|@import|url\(', page)
    # the only absolute URL is the ticket the reviewer filed
    assert set(re.findall(r"https?://[^\s\"'<>]+", page)) == {
        "https://example.atlassian.net/browse/KAN-1"}


def test_models_panel_reads_the_audit_log(tmp_path):
    s = _settings(tmp_path)
    rec = {"at": "2026-10-20T10:00:00+00:00", "role": "reader", "location": "local",
           "provider": "ollama", "model": "qwen3.5:4b", "host": "localhost:11434",
           "data_classes": ["evidence_excerpts"], "chars": 100, "images": 0,
           "sha256": "x", "approved_by_human": False}
    Path(s.data_policy.audit_log).write_text(
        json.dumps(rec) + "\nnot json\n" + json.dumps({**rec, "role": "vision", "images": 1}) + "\n")
    store = Store(s.storage.path)
    m = report.build(s, store, now=NOW)["models"]
    assert (m["calls"], m["cloud_calls"], m["images"]) == (2, 0, 1)  # the damaged line is skipped
    assert "0</p>" in report.render(report.build(s, store, now=NOW))

    cloud = {**rec, "role": "fixer", "location": "cloud", "host": "api.example.com",
             "approved_by_human": True}
    with open(s.data_policy.audit_log, "a") as f:
        f.write(json.dumps(cloud) + "\n")
    m = report.build(s, store, now=NOW)["models"]
    assert (m["cloud_calls"], m["human_approved"]) == (1, 1)
    assert "went to a cloud model" in report.render(report.build(s, store, now=NOW))


def test_report_command_writes_the_page(tmp_path):
    typer_testing = pytest.importorskip("typer.testing")
    from tessella.cli import app

    cfg = tmp_path / "t.yaml"
    cfg.write_text(f"storage:\n  path: {tmp_path}/db.sqlite\n"
                   f"data_policy:\n  audit_log: {tmp_path}/audit.jsonl\n")
    out = tmp_path / "out" / "r.html"
    result = typer_testing.CliRunner().invoke(app, ["report", "-c", str(cfg), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "Tessella" in out.read_text()


def _page(tmp_path, live=False):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    _seed(store)
    return report.render(report.build(s, store, now=NOW, project="shop"), live=live)


def test_live_flag_links_back_to_the_queue(tmp_path):
    assert 'data-live="false"' in _page(tmp_path) and 'href="/"' not in _page(tmp_path)
    live = _page(tmp_path, live=True)
    assert 'data-live="true"' in live and "Review queue" not in live


def test_every_panel_can_be_hidden_and_reordered(tmp_path):
    page = _page(tmp_path)
    for pid, title in report.PANELS:
        assert f'data-panel="{pid}"' in page, pid  # the panel itself
        assert f'data-panel-row="{pid}"' in page, pid  # its row in the Customize drawer
        assert f'data-move="up" data-for="{pid}"' in page and f'data-show="{pid}"' in page


def test_tiles_carry_what_the_filters_and_grouping_need(tmp_path):
    page = _page(tmp_path)
    assert page.count('class="tile ') == 6  # one tile per failure, not per cluster
    for attr in ('data-cat="product_bug"', 'data-state="approved"', 'data-state="rejected"',
                 'data-state="open"', 'data-cluster="a"', 'data-glyph="P"'):
        assert attr in page, attr
    assert 'data-filter="cat" data-value="flaky"' in page


def test_funnel_tiles_stand_for_several_when_there_are_many():
    t = {"failures": 200, "clusters": 10, "drafted": 5, "drafted_approved": 3,
         "drafted_filed": 2, "decided": 0, "drafted_reviewed": 0}
    body, take = report._funnel(t)
    assert "Each tile stands for 5." in body
    assert body.count("<i></i>") == 40 + 2 + 1 + 1 + 1  # 200/5, 10/5, then 5, 3, 2 round up
    assert take == "A person reads 10 clusters instead of 200 failures."


def test_funnel_says_so_when_grouping_saved_nothing():
    t = {"failures": 3, "clusters": 3, "drafted": 1, "drafted_approved": 1,
         "drafted_filed": 1, "decided": 1, "drafted_reviewed": 1}
    body, take = report._funnel(t)
    assert "Each tile stands for" not in body
    assert take == "Every failure was its own cluster, so grouping saved nothing here."


def test_queue_serves_the_live_report(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("jinja2")
    from starlette.testclient import TestClient

    from tessella.queue.app import create_app

    s = _settings(tmp_path)
    store = Store(s.storage.path)
    _seed(store)
    app = create_app(s)
    client = TestClient(app)
    first = client.get("/report")
    assert first.status_code == 200 and 'data-live="true"' in first.text
    assert "1 approved, 1 rejected." in first.text and "1 still waiting" in first.text
    # a decision made in the queue shows up on the next load, without rebuilding anything
    client.post(f"/cases/{_case('c', 0).id}/decide", data={"action": "approve", "csrf": app.state.csrf})
    assert "2 approved, 1 rejected." in client.get("/report").text
    assert client.get("/").text.count('data-live="true"') == 1  # the dashboard is the home page


def test_failures_table_says_what_is_already_filed_and_what_is_new(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    _seed(store)
    near = _case("d", 0, title="test_cancel <b>x</b>")
    near.draft.jira_matches = [{"key": "KAN-8", "summary": "Cancel returns 500", "score": 0.75,
                                "url": "https://example.atlassian.net/browse/KAN-8"}]
    store.save_case(near)
    store.save_case(_case("e", 0))
    store.save_case(_case("old", 0))  # stored, but not in the latest run
    store.record_run(failures=8, clusters=5, recalled=2, drafted=2,
                     detail={"a": 3, "b": 1, "c": 2, "d": 1, "e": 1})
    data = report.build(s, store, now=NOW, project="shop")
    st = data["board"]["status"]
    assert st["a"]["status"] == "on_board" and st["a"]["links"][0]["key"] == "KAN-1"
    assert st["d"]["status"] == "likely_on_board" and st["d"]["links"][0]["key"] == "KAN-8"
    assert st["e"]["status"] == "new" and st["c"]["status"] == "your_call"
    assert st["old"]["latest"] is False and data["board"]["failures"] == 8
    assert data["board"]["headline"].startswith("5 failure clusters: 1 already on the board (KAN-1)")
    page = report.render(data)
    assert len(re.findall(r'<section[^>]* data-panel="', page)) == len(report.PANELS)  # once each
    assert "Probably on the board" in page and "Not in the latest run" in page
    assert page.index('data-case="' + _case("e", 0).id) < page.index("Not in the latest run")
    assert ('<h1>Review 2 drafts (1 may already be on the board) and decide 1 failure that is '
            'not a bug ticket.</h1>') in page
    assert 'class="kpis"' in page and 'role="tablist"' in page
    assert "<b>x</b>" not in page  # titles are escaped
    short = report.render(data, view="management")
    assert 'class="kpis"' not in short and 'role="tablist"' not in short


def test_every_panel_sits_in_one_tab_and_has_a_menu(tmp_path):
    assert sorted(p for _, _, pids in report.TABS for p in pids) == sorted(p for p, _ in report.PANELS)
    s = _settings(tmp_path)
    page = report.render(report.build(s, Store(s.storage.path), now=NOW))
    for pid, _ in report.PANELS:
        assert f'data-menu="{pid}"' in page


def test_board_view_is_read_only_in_a_saved_file_and_decides_on_the_live_page(tmp_path):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from tessella.queue.app import create_app

    s = _settings(tmp_path)
    store = Store(s.storage.path)
    _seed(store)
    store.record_run(failures=6, clusters=3, recalled=0, drafted=1, detail={"a": 3, "b": 1, "c": 2})
    saved = report.render(report.build(s, store, now=NOW))
    assert 'data-fview-pane="board"' in saved and 'class="kanban"' in saved
    assert 'draggable="true"' not in saved.split('class="kanban"')[1].split("</section></div>")[0]
    assert 'data-csrf=""' in saved and 'class="drops"' not in saved and "Read-only" in saved

    app = create_app(s)
    client = TestClient(app)
    live = client.get("/report").text
    assert f'data-csrf="{app.state.csrf}"' in live and 'class="drops"' in live
    assert 'data-decide="' in live  # a keyboard way to decide each waiting card
    short = client.get("/report?view=management").text
    assert app.state.csrf not in short  # the short page has no board, so no token
    # What a drop sends: the queue's own decide endpoint, with the token.
    waiting = _case("c", 0, Category.environment, draft=False)
    r = client.post(f"/cases/{waiting.id}/decide", data={"action": "reject", "reason": "environment",
                                                         "csrf": app.state.csrf},
                    follow_redirects=False)
    assert r.status_code == 303 and Store(s.storage.path).decided(waiting.id)


def test_the_drawer_carries_the_draft_a_card_would_file(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    case = _case("n", 0)
    case.draft.steps_to_reproduce = "1. open cart"
    store.save_case(case)
    d = report.build(s, store, now=NOW)["details"][case.id]["draft"]
    assert d["title"] == "t" and d["steps"] == "1. open cart"


def test_a_board_that_refuses_the_ticket_says_why_and_records_nothing(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient

    from tessella.integrations import jira
    from tessella.queue.app import create_app

    s = _settings(tmp_path)
    s.board = "jira"
    store = Store(s.storage.path)
    case = _case("n", 0)
    store.save_case(case)

    def refuse(*a, **kw):
        raise RuntimeError("Jira rejected the issue (400): issuetype: invalid")

    monkeypatch.setattr(jira, "create_or_link", refuse)
    app = create_app(s)
    r = TestClient(app).post(f"/cases/{case.id}/decide",
                             data={"action": "approve", "csrf": app.state.csrf}, follow_redirects=False)
    assert r.status_code == 502 and "issuetype: invalid" in r.text
    assert not Store(s.storage.path).decided(case.id)
