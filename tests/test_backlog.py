"""Ticket outcomes, run comparison, database merge, the tile drawer's data, the short page and
the probe panel. Numbers are worked out by hand."""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from tessella import outcomes, report
from tessella.config import Settings
from tessella.memory.store import Store
from tessella.models import (Action, Case, Category, Decision, Draft, DraftKind, Origin,
                             RejectReason, Verdict)

T0 = datetime(2026, 10, 24, 9, 0, 0)
NOW = datetime(2026, 10, 24, 12, 0, 0)


def _case(cid="c1", sig="s1", verdict=True, title=None):
    return Case(id=cid, origin=Origin.test_failure, title=title or cid, message="boom", signature=sig,
                verdict=Verdict(category=Category.product_bug, confidence=0.9, root_cause="r")
                if verdict else None,
                draft=Draft(kind=DraftKind.ticket, title="t", body="b") if verdict else None)


def _approve(store, case, key="KAN-1", board="jira", seconds=120):
    store.save_case(case)
    store.record(case, Decision(action=Action.approve, by="sdet", at=T0 + timedelta(seconds=seconds),
                                board=board, ticket_key=key))


def _settings(tmp_path):
    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    return s


# ---- what happened to the tickets

@pytest.mark.parametrize("category, resolution, expected", [
    ("new", None, "open"), ("indeterminate", "Done", "open"), (None, None, "open"),
    ("done", "Done", "fixed"), ("done", "Fixed", "fixed"),
    ("done", "Won't Do", "not_a_bug"), ("done", "WON'T DO", "not_a_bug"),
    ("done", "Cannot Reproduce", "not_a_bug"), ("done", "Duplicate", "duplicate"),
    ("done", "Cannot Do", "other"), ("done", None, "other"),
])
def test_a_resolution_is_never_guessed_into_fixed(category, resolution, expected):
    assert outcomes.classify(category, resolution) == expected


def test_filed_tickets_are_distinct_jira_approvals_only():
    store = Store(":memory:")
    _approve(store, _case("a", "s1"), "KAN-1")
    _approve(store, _case("b", "s1"), "KAN-1")  # a second test linked to the same ticket
    _approve(store, _case("c", "s2"), "KAN-2")
    _approve(store, _case("d", "s3"), "GH-9", board="github")
    _approve(store, _case("e", "s4"), None)
    assert [t["key"] for t in store.filed_tickets()] == ["KAN-1", "KAN-2"]


def test_sync_stores_outcomes_and_a_failed_lookup_keeps_the_last_one():
    store = Store(":memory:")
    _approve(store, _case("a", "s1"), "KAN-1")
    _approve(store, _case("b", "s2"), "KAN-2")
    answers = {"KAN-1": {"status": "Done", "category": "done", "resolution": "Done"},
               "KAN-2": {"status": "To Do", "category": "new", "resolution": None}}
    first = outcomes.sync(Settings(), store, fetch=answers.get)
    assert first == {"checked": 2, "skipped": 0, "changed": 2}
    assert {o["key"]: o["outcome"] for o in store.outcomes()} == {"KAN-1": "fixed", "KAN-2": "open"}
    answers["KAN-2"] = None  # Jira is down for this one
    second = outcomes.sync(Settings(), store, fetch=answers.get)
    assert second == {"checked": 1, "skipped": 1, "changed": 0}
    assert {o["key"]: o["outcome"] for o in store.outcomes()}["KAN-2"] == "open"


def test_forgetting_a_decision_drops_its_ticket_outcome():
    store = Store(":memory:")
    _approve(store, _case("a", "s1"), "KAN-1")
    store.save_outcome("KAN-1", "fixed", "s1")
    store.forget("s1")
    assert store.outcomes() == []


def _filed(store, n):
    for i in range(1, n + 1):
        _approve(store, _case(f"c{i}", f"s{i}"), f"KAN-{i}")


def test_the_outcome_summary_counts_closed_tickets_and_says_when_it_is_too_early():
    store = Store(":memory:")
    _filed(store, 6)  # KAN-6 is never checked
    made, done = "2026-10-01T09:00:00.000+0400", "2026-10-03T09:00:00.000+0400"
    for key in ("KAN-1", "KAN-2", "KAN-3"):
        store.save_outcome(key, "fixed", None, "Done", "Done", made, done)
    store.save_outcome("KAN-4", "not_a_bug", None, "Done", "Won't Do", made, done)
    store.save_outcome("KAN-5", "open", None, "In Progress")
    store.save_outcome("KAN-99", "fixed")  # not a ticket Tessella filed: ignored
    o = report._outcome_summary(store)
    assert (o["filed"], o["checked"], o["closed"]) == (6, 5, 4)
    assert o["counts"] == {"fixed": 3, "not_a_bug": 1, "open": 1}
    assert o["fixed_rate"] == 0.75 and o["enough"] is False
    assert o["median_fix_days"] == 2.0


def _page(tmp_path, build_store, view="full"):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    build_store(store)
    return report.render(report.build(s, store, now=NOW, project="shop"), view=view)


def test_the_outcomes_panel_shows_the_rate_and_what_is_still_unchecked(tmp_path):
    def setup(store):
        _filed(store, 3)
        store.save_outcome("KAN-1", "fixed", None, "Done", "Done")
        store.save_outcome("KAN-2", "not_a_bug", None, "Done", "Won't Do")

    page = _page(tmp_path, setup)
    assert "1 of 2</p>" in page and "closed tickets were fixed" in page
    assert "Fewer than 5 closed tickets so far" in page
    assert "1 ticket not checked yet" in page


def test_the_outcomes_panel_explains_what_to_do_before_any_check(tmp_path):
    assert "No ticket filed yet" in _page(tmp_path, lambda store: None)
    page = _page(tmp_path, lambda store: _filed(store, 2))
    assert "2 tickets filed, none checked yet" in page and "tessella sync" in page


# ---- the latest run against the one before it

def _run(at_day, detail):
    return {"at": f"2026-10-0{at_day}T09:00:00", "failures": sum(detail.values()),
            "detail": detail, "clusters": len(detail)}


def test_new_returned_recurring_and_gone_are_told_apart():
    runs = [_run(1, {"a": 1, "b": 2}), _run(2, {"a": 1}), _run(3, {"a": 1, "b": 2, "c": 1})]
    titles = {"a": "A", "b": "B", "c": "C"}
    c = report._compare(runs, titles, {})
    assert c["has"] and (c["prev_failures"], c["failures"]) == (1, 4)
    assert [r["signature"] for r in c["new"]] == ["c"]
    assert [r["signature"] for r in c["returned"]] == ["b"]  # seen in run 1, missing in 2, back in 3
    assert [r["signature"] for r in c["recurring"]] == ["a"]
    assert c["gone"] == []
    c = report._compare(runs + [_run(4, {"c": 1})], titles, {})
    assert sorted(r["signature"] for r in c["gone"]) == ["a", "b"]
    assert [r["signature"] for r in c["recurring"]] == ["c"]


def test_a_single_run_has_nothing_to_compare():
    assert report._compare([_run(1, {"a": 1})], {}, {}) == {"has": False, "runs": 1}
    assert report._compare([], {}, {})["has"] is False



# ---- merging another database

def _seeded(path, shown_at=T0, with_probe=True):
    store = Store(path)
    case = _case("shared", "s1", verdict=False)
    store.save_case(case)
    store.mark_shown([case.id], at=shown_at)
    return store


def _digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_merging_adds_what_is_missing_and_a_second_merge_adds_nothing(tmp_path):
    mine = _seeded(str(tmp_path / "mine.sqlite"))
    theirs = _seeded(str(tmp_path / "ci.sqlite"))
    new = _case("only-in-ci", "s2")
    _approve(theirs, new, "KAN-5")
    theirs.record_run(3, 2, 0, 1, {"s1": 1, "s2": 2}, at=T0, duration_s=90.0, model_s=60.0)
    theirs.add_manual_sample(600, True, signature="s2", at=T0)
    theirs.save_outcome("KAN-5", "fixed", "s2", at=T0)
    theirs.record_probe("flow", 4, 2, {"x": 3}, at=T0)
    theirs.db.close()
    before = _digest(tmp_path / "ci.sqlite")

    added = mine.merge_from(tmp_path / "ci.sqlite")
    assert added == {"cases": 1, "decisions": 1, "runs": 1, "samples": 1, "outcomes": 1,
                     "probes": 1, "events": 0}
    assert [c.id for c in mine.all_cases()] == ["shared", "only-in-ci"]
    assert mine.runs()[0]["duration_s"] == 90.0 and mine.filed_tickets()[0]["key"] == "KAN-5"
    assert mine.manual_samples()[0]["seconds"] == 600 and mine.probes()[0]["mutations"] == 3
    assert mine.merge_from(tmp_path / "ci.sqlite") == {k: 0 for k in added}  # idempotent
    assert _digest(tmp_path / "ci.sqlite") == before  # the other file is never touched


def test_merge_keeps_the_better_copy_and_the_earlier_first_display(tmp_path):
    mine = _seeded(str(tmp_path / "mine.sqlite"), shown_at=T0 + timedelta(minutes=5))
    theirs = Store(str(tmp_path / "ci.sqlite"))
    theirs.save_case(_case("shared", "s1", verdict=True))  # CI judged it; mine has no verdict
    theirs.mark_shown(["shared"], at=T0)
    theirs.db.close()
    added = mine.merge_from(tmp_path / "ci.sqlite")
    assert added["cases"] == 0
    assert mine.get_case("shared").verdict is not None
    assert mine.review_timeline() == []  # no decision yet, but:
    assert mine.db.execute("SELECT at FROM case_events").fetchone()[0] == T0.isoformat()
    # the reverse never downgrades: merging a verdict-less copy over a judged one changes nothing
    blank = Store(str(tmp_path / "blank.sqlite"))
    blank.save_case(_case("shared", "s1", verdict=False))
    blank.db.close()
    mine.merge_from(tmp_path / "blank.sqlite")
    assert mine.get_case("shared").verdict is not None


def test_merge_refuses_itself_and_a_missing_file(tmp_path):
    store = Store(str(tmp_path / "mine.sqlite"))
    with pytest.raises(ValueError):
        store.merge_from(tmp_path / "mine.sqlite")
    with pytest.raises(FileNotFoundError):
        store.merge_from(tmp_path / "nope.sqlite")


def test_merge_accepts_a_database_from_before_review_timing_existed(tmp_path):
    old = sqlite3.connect(str(tmp_path / "old.sqlite"))
    old.executescript("""
        CREATE TABLE cases (id TEXT PRIMARY KEY, signature TEXT NOT NULL, origin TEXT NOT NULL,
            title TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
            signature TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, by TEXT NOT NULL,
            at TEXT NOT NULL, board TEXT, ticket_key TEXT, ticket_url TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            failures INTEGER NOT NULL, clusters INTEGER NOT NULL, recalled INTEGER NOT NULL,
            drafted INTEGER NOT NULL, detail TEXT NOT NULL);
        INSERT INTO runs (at, failures, clusters, recalled, drafted, detail)
            VALUES ('2026-10-18T09:00:00', 3, 2, 0, 1, '{}');
    """)
    old.commit()
    old.close()
    mine = Store(str(tmp_path / "mine.sqlite"))
    assert mine.merge_from(tmp_path / "old.sqlite")["runs"] == 1


def test_a_store_from_before_generic_boards_opens_and_keeps_its_jira_keys(tmp_path):
    case = _case("c1", "s1")
    old = sqlite3.connect(str(tmp_path / "old.sqlite"))
    old.executescript("""
        CREATE TABLE cases (id TEXT PRIMARY KEY, signature TEXT NOT NULL, origin TEXT NOT NULL,
            title TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
            signature TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, by TEXT NOT NULL,
            at TEXT NOT NULL, jira_key TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            failures INTEGER NOT NULL, clusters INTEGER NOT NULL, recalled INTEGER NOT NULL,
            drafted INTEGER NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE manual_samples (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            seconds REAL NOT NULL, by TEXT NOT NULL);
    """)
    old.execute("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?)",
                (case.id, case.signature, case.origin.value, case.title, case.model_dump_json(),
                 T0.isoformat()))
    old.execute("INSERT INTO decisions (case_id, signature, action, by, at, jira_key) "
                "VALUES (?, ?, 'approve', 'sdet', ?, 'KAN-7')", (case.id, case.signature, T0.isoformat()))
    old.commit()
    old.close()

    store = Store(str(tmp_path / "old.sqlite"))
    [(got, decision)] = store.recent_decisions()
    assert got.id == "c1"
    assert (decision.board, decision.ticket_key) == ("jira", "KAN-7")
    have = {r[1] for r in store.db.execute("PRAGMA table_info(manual_samples)")}
    assert {"filed", "signature", "note", "source"} <= have
    Store(str(tmp_path / "old.sqlite"))  # opening it again changes nothing and does not fail


# ---- what the tile drawer is given

def test_drawer_data_carries_review_time_ticket_and_outcome(tmp_path):
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    case = _case("c1", "s1")
    store.save_case(case)
    store.mark_shown([case.id], at=T0)
    store.record(case, Decision(action=Action.approve, by="anna", at=T0 + timedelta(seconds=150),
                                board="jira", ticket_key="KAN-3", ticket_url="https://x/KAN-3"))
    store.save_outcome("KAN-3", "fixed", "s1", "Done", "Done")
    d = report.build(s, store, now=NOW)["details"]["c1"]
    assert (d["state"], d["by"], d["review_s"], d["ticket"]) == ("approved", "anna", 150.0, "KAN-3")
    assert d["url"] == "https://x/KAN-3" and d["outcome"] == "Fixed (Done)"
    assert d["verdict"] == "Product bug" and d["root_cause"] == "r"


def test_hostile_text_cannot_break_out_of_the_data_block(tmp_path):
    evil = '</script><img src=x onerror=alert(1)> {{cases}} &amp; <!--'
    page = _page(tmp_path, lambda store: store.save_case(_case("c1", "s1", title=evil)))
    block = re.search(r'<script type="application/json" id="case-data">(.*?)</script>', page, re.S)
    assert block, "the data block must end at its own closing tag"
    assert json.loads(block.group(1))["c1"]["title"] == evil  # survives exactly
    assert "<img src=x" not in page and "<!--" not in block.group(1)


def test_the_drawer_data_is_capped_for_large_suites(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "MAX_CASE_ROWS", 2)
    s = _settings(tmp_path)
    store = Store(s.storage.path)
    for i in range(3):
        store.save_case(_case(f"c{i}", f"s{i}"))
    assert len(report.build(s, store, now=NOW)["details"]) == 2



# ---- the short page

def test_the_management_view_keeps_the_summary_panels_and_drops_the_detail(tmp_path):
    def setup(store):
        _approve(store, _case("c1", "s1"), "KAN-1")

    full = _page(tmp_path, setup)
    short = _page(tmp_path, setup, view="management")
    shown = re.findall(r'data-panel="(\w+)"', short)
    assert sorted(set(shown)) == sorted(report.SUMMARY_PANELS)
    assert shown[:2] == ["saved", "outcomes"]  # its own order, not the full page's
    assert "Tessella summary for shop" in short and "Tessella report for shop" in full
    assert 'data-panel="cases"' not in short 
    assert '<script type="application/json" id="case-data">{}</script>' in short
    assert 'data-panel-row="cases"' not in short and 'data-panel-row="saved"' in short
    assert len(short) < len(full)


def test_an_unknown_view_falls_back_to_the_full_page(tmp_path):
    assert 'data-panel="cases"' in _page(tmp_path, lambda store: None, view="nonsense")


def test_every_panel_has_a_row_in_the_customize_drawer_and_a_summary_preset(tmp_path):
    page = _page(tmp_path, lambda store: None)
    assert 'data-summary="saved,outcomes,funnel,trust,runs,models"' in page
    assert 'id="preset-summary"' in page and 'id="preset-all"' in page
    assert set(report.SUMMARY_PANELS) <= {pid for pid, _ in report.PANELS}


# ---- the probe panel


# ---- the commands

def test_sync_and_merge_commands(tmp_path):
    typer_testing = pytest.importorskip("typer.testing")
    from tessella.cli import app

    cfg = tmp_path / "t.yaml"
    cfg.write_text(f"storage:\n  path: {tmp_path}/db.sqlite\n"
                   f"data_policy:\n  audit_log: {tmp_path}/audit.jsonl\n")
    runner = typer_testing.CliRunner()
    assert "nothing to check" in runner.invoke(app, ["sync", "-c", str(cfg)]).output

    other = Store(str(tmp_path / "ci.sqlite"))
    _approve(other, _case("c1", "s1"), "KAN-1")
    other.db.close()
    result = runner.invoke(app, ["merge", str(tmp_path / "ci.sqlite"), "-c", str(cfg)])
    assert result.exit_code == 0 and "1 cases" in result.output and "1 decisions" in result.output
    assert "nothing new" in runner.invoke(app, ["merge", str(tmp_path / "ci.sqlite"), "-c", str(cfg)]).output
    assert runner.invoke(app, ["merge", str(tmp_path / "missing.sqlite"), "-c", str(cfg)]).exit_code == 1
    # tickets exist now, but Jira is not configured: say so instead of pretending to check
    assert runner.invoke(app, ["sync", "-c", str(cfg)]).exit_code == 1


def test_report_command_writes_the_short_page_and_rejects_an_unknown_view(tmp_path):
    typer_testing = pytest.importorskip("typer.testing")
    from tessella.cli import app

    cfg = tmp_path / "t.yaml"
    cfg.write_text(f"storage:\n  path: {tmp_path}/db.sqlite\n"
                   f"data_policy:\n  audit_log: {tmp_path}/audit.jsonl\n")
    out = tmp_path / "short.html"
    runner = typer_testing.CliRunner()
    ok = runner.invoke(app, ["report", "-c", str(cfg), "-o", str(out), "--view", "management"])
    assert ok.exit_code == 0 and "Tessella summary for" in out.read_text()
    assert runner.invoke(app, ["report", "-c", str(cfg), "-o", str(out), "--view", "x"]).exit_code == 1


def test_the_live_report_serves_the_short_page_too(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("jinja2")
    from starlette.testclient import TestClient

    from tessella.queue.app import create_app

    s = _settings(tmp_path)
    Store(s.storage.path).save_case(_case("c1", "s1"))
    client = TestClient(create_app(s))
    assert 'data-panel="cases"' in client.get("/report").text
    assert 'data-panel="cases"' not in client.get("/report?view=management").text
