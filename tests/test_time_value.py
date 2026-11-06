"""Time saved: every number is worked out by hand here, so the code cannot agree with itself."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from tessella import calibrate
from tessella.memory.store import Store
from tessella.models import (Action, Case, Category, Decision, Draft, DraftKind, Origin,
                             Verdict)
from tessella.report import timevalue

T0 = datetime(2026, 10, 24, 9, 0, 0)


def _row(case="c", by="sdet", decided=0, shown=0, action="approve"):
    """A timeline row; `decided` and `shown` are seconds after 09:00 (None: not recorded)."""
    return {"case_id": case, "signature": case, "action": action, "by": by,
            "decided_at": T0 + timedelta(seconds=decided),
            "shown_at": None if shown is None else T0 + timedelta(seconds=shown)}


def _cluster(state, has_draft=True, category="product_bug"):
    return {"state": state, "has_draft": has_draft, "category": category}


# ---- how long a review took

def test_cards_shown_together_are_timed_from_the_previous_decision():
    # three cards appear at 09:00; decisions at 09:02, 09:05 and 09:06:30
    rows = [_row("a", decided=120), _row("b", decided=300), _row("c", decided=390)]
    assert timevalue.review_intervals(rows) == [120, 180, 90]


def test_idle_time_is_capped():
    assert timevalue.review_intervals([_row(decided=3 * 3600)]) == [600.0]
    assert timevalue.review_intervals([_row(decided=3 * 3600)], cap_s=60) == [60.0]


def test_untimed_reviews_stay_untimed_but_still_move_the_clock():
    rows = [_row("a", decided=100, shown=None), _row("b", decided=160, shown=0)]
    # b started at a's decision (100s), not at 0
    assert timevalue.review_intervals(rows) == [None, 60]


def test_two_reviewers_do_not_shorten_each_others_reviews():
    rows = [_row("a", by="anna", decided=120), _row("b", by="bob", decided=180)]
    assert timevalue.review_intervals(rows) == [120, 180]  # bob started at 0, not at anna's 120


def test_a_decision_before_its_display_counts_as_zero_not_negative():
    assert timevalue.review_intervals([_row(decided=10, shown=50)]) == [0.0]


# ---- the manual baseline

def test_baseline_is_the_median_of_timed_samples_and_assumed_when_there_are_none():
    samples = [{"seconds": m * 60, "filed": True} for m in (20, 30, 40)]
    b = timevalue.baseline(samples)
    assert (b["ticket_min"], b["ticket_src"], b["n_ticket"]) == (30.0, "calibrated", 3)
    assert (b["other_min"], b["other_src"], b["n_other"]) == (15.0, "assumed", 0)
    even = timevalue.baseline([{"seconds": 600, "filed": False}, {"seconds": 1200, "filed": False}])
    assert even["other_min"] == 15.0 and even["other_src"] == "calibrated"


# ---- the net figure, worked out by hand

def _scenario():
    clusters = [
        _cluster("approved"),                                   # ticket
        _cluster("approved", category="test_bug"),              # ticket
        _cluster("approved", has_draft=False, category="flaky"),  # investigation only
        _cluster("rejected"),                                   # earns nothing
        _cluster("open"),                                       # earns nothing yet
        _cluster("approved", has_draft=False, category="none"),  # no verdict: Tessella did nothing
    ]
    timeline = [_row("a", decided=60), _row("b", decided=180), _row("c", decided=360),
                _row("d", decided=400, shown=None, action="reject")]
    samples = [{"seconds": 40 * 60, "filed": True}]  # one timed ticket investigation: 40 min
    return clusters, timeline, samples


def test_net_time_saved_matches_the_hand_calculation():
    clusters, timeline, samples = _scenario()
    t = timevalue.compute(clusters, recalled=4, timeline=timeline, samples=samples, runs=[],
                          duplicates=0)
    # avoided: 2 tickets x 40 + 1 other x 15 (assumed) + 4 repeats x 15 = 80 + 15 + 60
    assert t["avoided"] == {"ticket_min": 80, "other_min": 15, "repeat_min": 60, "total_min": 155}
    # reviews: timed 60 + 120 + 180 s = 6 min, median 2 min; the untimed reject is imputed at 2 min
    assert t["review"]["timed_total_min"] == pytest.approx(6.0)
    assert t["review"]["each_min"] == pytest.approx(2.0)
    assert t["review"]["total_min"] == pytest.approx(8.0)
    assert t["review"]["rejected_min"] == pytest.approx(2.0)
    assert t["net_min"] == pytest.approx(147.0)
    assert t["counts"] == {"approved_ticket": 2, "approved_other": 1, "rejected": 1,
                           "recalled": 4, "open": 1}
    # the ticket baseline is calibrated, so only these two are still assumptions
    assert t["assumed"] == ["time to investigate a failure", "review time for 1 untimed review"]


def test_nothing_is_assumed_when_everything_is_measured_or_calibrated():
    clusters = [_cluster("approved")]
    samples = [{"seconds": 1800, "filed": True}]
    t = timevalue.compute(clusters, recalled=0, timeline=[_row(decided=90)], samples=samples,
                          runs=[], duplicates=0)
    assert t["assumed"] == [] and t["review"]["src"] == "measured"
    assert t["net_min"] == pytest.approx(30 - 1.5)


def test_a_net_loss_is_reported_not_hidden():
    # a 5-minute review of a draft whose manual equivalent would have taken 2 minutes
    t = timevalue.compute([_cluster("approved")], 0, [_row(decided=300)],
                          [{"seconds": 120, "filed": True}], [], 0)
    assert t["net_min"] == pytest.approx(2 - 5)


def test_with_no_reviews_nothing_is_counted_and_no_review_time_is_invented():
    t = timevalue.compute([_cluster("open")], 0, [], [], [], 0)
    assert t["has_data"] is False and t["net_min"] == 0
    assert t["review"]["src"] == "none" and t["assumed"] == []


def test_review_time_is_assumed_when_no_review_was_timed():
    t = timevalue.compute([_cluster("approved")], 0, [_row(shown=None, decided=10)], [], [], 0)
    assert t["review"]["src"] == "assumed" and t["review"]["each_min"] == 3.0
    assert "review time" in t["assumed"]


def test_compute_time_and_duplicates_are_reported_but_not_subtracted():
    runs = [{"duration_s": 120.0, "model_s": 90.0}, {"duration_s": None, "model_s": None},
            {"duration_s": 60.0, "model_s": 30.0}]
    with_runs = timevalue.compute([_cluster("approved")], 0, [_row(decided=60)],
                                  [{"seconds": 600, "filed": True}], runs, duplicates=3)
    without = timevalue.compute([_cluster("approved")], 0, [_row(decided=60)],
                                [{"seconds": 600, "filed": True}], [], duplicates=0)
    assert with_runs["net_min"] == without["net_min"]
    assert with_runs["compute"] == {"runs": 3, "timed_runs": 2, "seconds": 180.0,
                                    "model_seconds": 120.0}
    assert with_runs["duplicates_avoided"] == 3


# ---- what the store records

def _old_database(path):
    """A database as the previous release wrote it: no new columns, no new tables."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE cases (id TEXT PRIMARY KEY, signature TEXT NOT NULL, origin TEXT NOT NULL,
            title TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
            signature TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, by TEXT NOT NULL,
            at TEXT NOT NULL, board TEXT, ticket_key TEXT, ticket_url TEXT);
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            failures INTEGER NOT NULL, clusters INTEGER NOT NULL, recalled INTEGER NOT NULL,
            drafted INTEGER NOT NULL, detail TEXT NOT NULL);
        INSERT INTO decisions (case_id, signature, action, by, at) VALUES
            ('c1', 's1', 'approve', 'sdet', '2026-10-18T10:00:00');
        INSERT INTO runs (at, failures, clusters, recalled, drafted, detail)
            VALUES ('2026-10-18T09:00:00', 3, 2, 0, 1, '{}');
    """)
    db.commit()
    db.close()


def test_an_old_database_is_upgraded_in_place_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "old.sqlite")
    _old_database(path)
    store = Store(path)
    (sig, d), = store.all_decisions()
    assert (sig, d.action, d.linked_existing) == ("s1", Action.approve, False)
    run, = store.runs()
    assert run["failures"] == 3 and run["duration_s"] is None and run["model_s"] is None
    store.record_run(1, 1, 0, 0, {}, duration_s=2.5, model_s=1.5)  # the new columns work
    assert store.runs()[-1]["duration_s"] == 2.5
    Store(path)  # opening it again must not try to add the columns twice


def _case(cid="c1", sig="s1"):
    return Case(id=cid, origin=Origin.test_failure, title=cid, message="m", signature=sig,
                verdict=Verdict(category=Category.product_bug, confidence=0.9, root_cause="r"),
                draft=Draft(kind=DraftKind.ticket, title="t", body="b"))


def test_the_first_display_starts_the_clock_and_reloads_do_not_restart_it():
    store = Store(":memory:")
    case = _case()
    store.save_case(case)
    store.mark_shown([case.id], at=T0)
    store.mark_shown([case.id], at=T0 + timedelta(minutes=30))  # page reloaded
    store.record(case, Decision(action=Action.approve, by="sdet", at=T0 + timedelta(minutes=2)))
    (row,) = store.review_timeline()
    assert row["shown_at"] == T0 and row["decided_at"] == T0 + timedelta(minutes=2)
    assert store.shown_ids() == {case.id}


def test_forgetting_a_decision_restarts_its_review_clock():
    store = Store(":memory:")
    case = _case()
    store.save_case(case)
    store.mark_shown([case.id], at=T0)
    store.record(case, Decision(action=Action.approve, by="sdet", at=T0))
    store.forget(case.signature)
    assert store.shown_ids() == set() and store.review_timeline() == []


def test_decisions_remember_whether_they_linked_to_an_existing_ticket():
    store = Store(":memory:")
    a, b = _case("a", "s1"), _case("b", "s2")
    for c in (a, b):
        store.save_case(c)
    store.record(a, Decision(action=Action.approve, by="sdet", at=T0, ticket_key="KAN-1",
                             linked_existing=True))
    store.record(b, Decision(action=Action.approve, by="sdet", at=T0, ticket_key="KAN-2"))
    assert [d.linked_existing for _, d in store.all_decisions()] == [True, False]


def test_jira_reports_a_link_to_an_existing_ticket(monkeypatch):
    from tessella.config import Jira
    from tessella.integrations import jira

    monkeypatch.setenv("JIRA_TOKEN", "fake")
    monkeypatch.setattr(jira, "find_by_signature",
                        lambda cfg, sig, allow: ("KAN-7", "https://x.atlassian.net/browse/KAN-7"))
    cfg = Jira(url="https://x.atlassian.net", project="KAN", email="a@b.com")
    draft = Draft(kind=DraftKind.ticket, title="t", body="b", signature="sig")
    assert jira.create_or_link(cfg, draft, ["x.atlassian.net"]) == (
        "KAN-7", "https://x.atlassian.net/browse/KAN-7", True)
    assert jira.create_issue(cfg, draft, ["x.atlassian.net"])[0] == "KAN-7"  # old API unchanged


def test_triage_records_how_long_it_took(tmp_path):
    from pathlib import Path

    from tessella.config import Settings
    from tessella.pipeline import triage

    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    s.inputs.reports = [str(Path(__file__).parent / "fixtures" / "junit_sample.xml")]
    triage(s, rerun=False, use_model=False)
    run, = Store(s.storage.path).runs()
    assert run["duration_s"] is not None and run["duration_s"] >= 0
    assert run["model_s"] == 0  # no model was called


# ---- calibration

def test_the_stopwatch_times_the_investigation_and_not_the_questions_after_it():
    ticks = iter([100.0, 460.0])  # started at 100 s, stopped at 460 s
    answers = iter(["", "", "n", "stale selector"])  # start, stop, ticket?, note
    said = []
    result = calibrate.time_manual(_case(), ask=lambda prompt: next(answers), say=said.append,
                                   clock=lambda: next(ticks))
    assert result == (360.0, False, "stale selector")
    assert "Tessella's verdict" in " ".join(said)


def test_q_cancels_the_stopwatch_without_a_sample():
    answers = iter(["", "q"])
    ticks = iter([0.0, 5.0])
    assert calibrate.time_manual(_case(), ask=lambda p: next(answers), say=lambda m: None,
                                 clock=lambda: next(ticks)) is None


def test_the_raw_failure_is_shown_without_the_verdict():
    text = calibrate.describe(_case())
    assert "c1" in text and "root cause" not in text.lower() and "product_bug" not in text


def test_candidates_prefer_unseen_untimed_undecided_clusters():
    store = Store(":memory:")
    seen, decided, fresh, timed = _case("a", "sa"), _case("b", "sb"), _case("c", "sc"), _case("d", "sd")
    for c in (seen, decided, fresh, timed):
        store.save_case(c)
    store.mark_shown([seen.id])
    store.record(decided, Decision(action=Action.approve, by="sdet", at=T0))
    store.add_manual_sample(600, True, signature="sd")
    assert [c.signature for c in calibrate.candidates(store)] == ["sc", "sb", "sa"]
    assert calibrate.pick(store).signature == "sc"
    assert calibrate.pick(store, "sa").signature == "sa"
    assert calibrate.pick(store, "zz") is None


def test_importing_a_known_time_validates_it_and_feeds_the_baseline():
    store = Store(":memory:")
    for bad in (0, -5, 100000):
        with pytest.raises(ValueError):
            calibrate.record_minutes(store, bad)
    calibrate.record_minutes(store, 25, filed=True, note="from Tempo")
    calibrate.record_minutes(store, 10, filed=False)
    s = calibrate.summary(store)
    assert s["ticket"] == {"n": 1, "median_min": 25.0} and s["other"] == {"n": 1, "median_min": 10.0}
    assert {x["source"] for x in s["samples"]} == {"imported"}
    assert timevalue.baseline(store.manual_samples())["ticket_min"] == 25.0


# ---- the page

def _render(tmp_path, build_store):
    from tessella import report
    from tessella.config import Settings

    s = Settings()
    s.storage.path = str(tmp_path / "t.sqlite")
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    store = Store(s.storage.path)
    build_store(store)
    return report.render(report.build(s, store, now=T0, project="shop"))


def _approved_after(store, seconds, shown=True, linked=False):
    case = _case()
    store.save_case(case)
    if shown:
        store.mark_shown([case.id], at=T0)
    store.record(case, Decision(action=Action.approve, by="sdet", at=T0 + timedelta(seconds=seconds),
                                board="jira", ticket_key="KAN-1", linked_existing=linked))


def test_a_fully_evidenced_figure_says_so_and_offers_nothing_to_edit(tmp_path):
    def setup(store):
        _approved_after(store, 120)
        store.add_manual_sample(30 * 60, True)

    page = _render(tmp_path, setup)
    assert "Every input is measured or timed by your team." in page
    assert 'data-assume=' not in page  # nothing is assumed, so there is nothing to adjust
    assert 'Net time saved: <span id="h-net">28 min</span>.' in page  # 30 avoided - 2 reviewing
    assert "Still assumed" not in page


def test_an_assumed_figure_is_flagged_everywhere_it_is_shown(tmp_path):
    page = _render(tmp_path, lambda store: _approved_after(store, 120, shown=False))
    assert "Still assumed:" in page and "(estimate)" in page
    assert "tessella calibrate" in page
    assert 'data-assume="ticket"' in page and 'data-assume="review"' in page  # editable


def test_a_net_loss_shows_a_minus_sign(tmp_path):
    def setup(store):
        _approved_after(store, 300)  # 5 minutes of review...
        store.add_manual_sample(2 * 60, True)  # ...for work that takes 2 minutes by hand

    assert '<span id="h-net">−3 min</span>' in _render(tmp_path, setup)


def test_with_nothing_reviewed_the_page_claims_no_time_saved(tmp_path):
    def setup(store):
        store.save_case(_case())  # judged and drafted, but nobody has reviewed it

    page = _render(tmp_path, setup)
    assert "Nothing to count yet" in page and "Net time saved" not in page


def test_compute_time_and_avoided_duplicates_are_shown_apart_from_the_hours(tmp_path):
    def setup(store):
        _approved_after(store, 120, linked=True)
        store.record_run(3, 1, 0, 1, {"s1": 3}, duration_s=300.0, model_s=225.0)

    page = _render(tmp_path, setup)
    assert "Tessella ran for 5 min across 1 run, 75% of it waiting on models." in page
    assert "1 duplicate ticket avoided" in page and "not in the hours above" in page
