"""A reviewer changes a draft before it is filed, and a CI run asks the board what it has."""

from datetime import datetime

import pytest

from tessella.config import Settings
from tessella.draft.ticket import edit, keep_edits, render, ticket, undo_edits
from tessella.memory.store import Store
from tessella.models import Action, Case, Category, Decision, Origin, Verdict


def _case(cid="c1", signature="sig"):
    case = Case(id=cid, origin=Origin.test_failure, title="test_total", message="1 != 2", signature=signature)
    case.verdict = Verdict(category=Category.product_bug, confidence=0.9, root_cause="rounding",
                           expected_result="the total matches", suggested_fix="round half up")
    case.draft = ticket(case)
    return case


def test_edit_changes_the_field_and_the_body_that_gets_filed():
    case = _case()
    edit(case, {"root_cause": "promo applied twice\r\nin cart.py"}, "anna")
    assert case.draft.root_cause == "promo applied twice\nin cart.py"
    assert "**Root cause (suspected):** promo applied twice\nin cart.py" in case.draft.body
    assert "rounding" not in case.draft.body
    assert "edited and approved by an SDET" in case.draft.body
    assert case.draft.edited_by == "anna" and case.draft.original == {"root_cause": "rounding"}
    assert case.verdict.root_cause == "rounding"  # the model's verdict stays what it was


def test_an_emptied_field_says_so_and_an_empty_title_is_refused():
    case = _case()
    title = case.draft.title
    edit(case, {"suggested_fix": "   ", "title": "  "}, "anna")
    assert case.draft.suggested_fix == "Not available" and case.draft.title == title
    assert "**Suggested fix:** Not available" in case.draft.body


def test_title_is_one_line_and_fits_jira():
    case = _case()
    edit(case, {"title": "a\nb " + "x" * 400}, "anna")
    assert "\n" not in case.draft.title and len(case.draft.title) == 255


def test_typing_the_original_back_is_no_edit_and_undo_restores():
    case = _case()
    before = case.draft.body
    edit(case, {"root_cause": "other"}, "anna")
    edit(case, {"root_cause": "rounding"}, "anna")
    assert not case.draft.original and not case.draft.edited_by and case.draft.body == before
    edit(case, {"root_cause": "other", "environment": "staging"}, "anna")
    undo_edits(case)
    assert case.draft.root_cause == "rounding" and not case.draft.edited_by and case.draft.body == before


def test_body_keeps_duplicates_and_the_checked_fix_after_an_edit():
    case = _case()
    case.draft.jira_matches = [{"key": "KAN-3", "summary": "Discount", "status": "To Do"}]
    case.draft.patch, case.draft.patch_status = "--- a\n+++ b\n", "verified"
    edit(case, {"description": "new words"}, "anna")
    assert "- KAN-3 (To Do): Discount" in case.draft.body
    assert "**Proposed fix** (verified)" in case.draft.body and "new words" in case.draft.body


def test_a_fresh_draft_keeps_the_reviewers_fields_only():
    earlier = _case()
    edit(earlier, {"root_cause": "my words"}, "anna")
    fresh = _case()
    fresh.draft.suggested_fix = "fresh fix"
    keep_edits(fresh, earlier.draft)
    assert fresh.draft.root_cause == "my words" and fresh.draft.suggested_fix == "fresh fix"
    assert fresh.draft.original == {"root_cause": "rounding"} and fresh.draft.edited_by == "anna"
    assert render(fresh, fresh.draft) == fresh.draft.body


@pytest.fixture
def queue(tmp_path):
    from starlette.testclient import TestClient

    from tessella.queue.app import create_app

    settings = Settings()
    settings.storage.path = str(tmp_path / "s.sqlite")
    store = Store(settings.storage.path)
    store.save_case(_case())
    app = create_app(settings)
    return TestClient(app), app, store


def test_queue_saves_an_edit_without_filing(queue):
    client, app, store = queue
    r = client.post("/cases/c1/edit", data={"csrf": app.state.csrf, "root_cause": "my words"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#case-c1"
    case = store.get_case("c1")
    assert case.draft.root_cause == "my words" and case.draft.expected_result == "the total matches"
    assert not store.decided("c1")
    page = client.get("/").text
    assert "my words" in page and '"edited_by":"sdet"' in page and '"changed":["root_cause"]' in page


def test_queue_edit_needs_the_form_token(queue):
    client, _app, store = queue
    assert client.post("/cases/c1/edit", data={"root_cause": "forged"}).status_code == 403
    assert client.post("/cases/c1/undo-edits", data={}).status_code == 403
    assert store.get_case("c1").draft.root_cause == "rounding"


def test_a_decided_draft_cannot_be_edited(queue):
    client, app, store = queue
    store.record(store.get_case("c1"), Decision(action=Action.approve, by="a", at=datetime(2026, 11, 8), ticket_key="KAN-1"))
    client.post("/cases/c1/edit", data={"csrf": app.state.csrf, "root_cause": "late"})
    assert store.get_case("c1").draft.root_cause == "rounding"


def test_approving_files_the_edited_text(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from tessella.integrations import jira
    from tessella.queue.app import create_app

    filed = {}

    def create(cfg, draft, allow):
        filed["draft"] = draft
        return "KAN-5", "https://j/KAN-5", False

    monkeypatch.setattr(jira, "create_or_link", create)
    settings = Settings(board="jira")
    settings.storage.path = str(tmp_path / "s.sqlite")
    store = Store(settings.storage.path)
    store.save_case(_case())
    app = create_app(settings)
    client = TestClient(app)
    client.post("/cases/c1/edit", data={"csrf": app.state.csrf, "title": "Better title", "root_cause": "my words"})
    client.post("/cases/c1/decide", data={"action": "approve", "csrf": app.state.csrf})
    assert filed["draft"].title == "Better title" and "my words" in filed["draft"].body
    assert store.last_decision("sig").action == Action.approve  # still counts as on the board
    assert store.last_decision("sig").ticket_key == "KAN-5"


def _triage_settings(tmp_path, report):
    settings = Settings()
    settings.storage.path = str(tmp_path / "s.sqlite")
    settings.inputs.reports = [str(report)]
    return settings


JUNIT = """<testsuite tests="1" failures="1"><testcase classname="t" name="test_total">
<failure message="AssertionError: 1 != 2">AssertionError: 1 != 2</failure></testcase></testsuite>"""


@pytest.fixture
def report(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT)
    return path


def _bug(monkeypatch):
    from tessella import pipeline

    monkeypatch.setattr(pipeline, "_judge", lambda lead, *a, **k: Verdict(
        category=Category.product_bug, confidence=0.9, root_cause="model words"))


def test_a_new_triage_keeps_a_reviewers_edits(tmp_path, report, monkeypatch):
    from tessella.pipeline import triage

    _bug(monkeypatch)
    settings = _triage_settings(tmp_path, report)
    case = triage(settings, rerun=False).cases[0]
    store = Store(settings.storage.path)
    saved = store.get_case(case.id)
    edit(saved, {"root_cause": "reviewer words"}, "anna")
    store.save_case(saved)
    again = triage(settings, rerun=False).cases[0]
    assert again.draft.root_cause == "reviewer words" and again.draft.edited_by == "anna"
    assert store.get_case(case.id).draft.root_cause == "reviewer words"


def test_ci_asks_the_board_and_skips_what_it_already_has(tmp_path, report, monkeypatch):
    from tessella import board
    from tessella.integrations import jira
    from tessella.pipeline import triage

    monkeypatch.setenv("JIRA_TOKEN", "t")
    settings = _triage_settings(tmp_path, report)
    settings.board = "jira"
    settings.jira.url, settings.jira.project, settings.jira.email = "https://j", "KAN", "a@b.c"
    settings.jira.token_env = "JIRA_TOKEN"
    asked = []

    def found(cfg, signature, allow):
        asked.append(signature)
        return "KAN-7", "https://j/browse/KAN-7"

    monkeypatch.setattr(jira, "find_by_signature", found)
    result = triage(settings, rerun=False, use_model=False, ask_board=True)
    rows = board.classify(result)
    assert asked and result.board_checked and rows[0].status == board.Status.on_board
    assert rows[0].tickets == ["KAN-7"] and result.cases[0].verdict.root_cause == "Already filed as KAN-7."
    # A laptop triage trusts its own store, so tessella forget can bring a failure back.
    asked.clear()
    assert board.classify(triage(settings, rerun=False, use_model=False))[0].status == board.Status.pending
    assert not asked


def test_ci_board_check_fails_open(tmp_path, report, monkeypatch):
    from tessella import board
    from tessella.integrations import jira
    from tessella.pipeline import triage

    monkeypatch.setenv("JIRA_TOKEN", "t")
    settings = _triage_settings(tmp_path, report)
    settings.board = "jira"
    settings.jira.url, settings.jira.project, settings.jira.email = "https://j", "KAN", "a@b.c"
    settings.jira.token_env = "JIRA_TOKEN"

    def broken(*a):
        raise RuntimeError("down")

    monkeypatch.setattr(jira, "find_by_signature", broken)
    result = triage(settings, rerun=False, use_model=False, ask_board=True)
    assert board.classify(result)[0].status == board.Status.pending


def test_time_limit_leaves_failures_not_judged(tmp_path, report, monkeypatch):
    from tessella import pipeline

    called = []
    monkeypatch.setattr(pipeline.agent, "decide", lambda *a, **k: called.append(1))
    monkeypatch.setattr(pipeline, "_warm_models", lambda s: None)
    result = pipeline.triage(_triage_settings(tmp_path, report), rerun=False, minutes=0)
    assert not called and result.out_of_time == 1 and result.cases[0].verdict is None


def test_history_counts_earlier_runs_only(tmp_path, report, monkeypatch):
    from tessella.pipeline import triage

    settings = _triage_settings(tmp_path, report)
    first = triage(settings, rerun=False, use_model=False)
    second = triage(settings, rerun=False, use_model=False)
    sig = next(iter(first.clusters))
    assert first.history[sig] == (0, 0) and second.history[sig] == (1, 1)


def test_a_failure_with_no_draft_can_still_become_a_ticket(queue):
    client, app, store = queue
    bare = Case(id="c2", origin=Origin.test_failure, title="test_b", message="boom", signature="s2")
    store.save_case(bare)
    r = client.post("/cases/c2/draft", data={"csrf": app.state.csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#edit-c2"
    case = store.get_case("c2")
    assert case.verdict.category == Category.product_bug and case.verdict.confidence == 0
    assert case.draft.root_cause == "Not determined by the model."
    assert case.draft.expected_result == "Not determined by the model." and "boom" in case.draft.description
    assert not store.decided("c2")  # writing it files nothing
    assert client.post("/cases/c1/draft", data={}).status_code == 403


def test_a_person_can_overrule_a_flaky_verdict(queue):
    client, app, store = queue
    flaky = Case(id="c3", origin=Origin.test_failure, title="t", signature="s3",
                 verdict=Verdict(category=Category.flaky, confidence=0.9, root_cause="timing"))
    store.save_case(flaky)
    client.post("/cases/c3/draft", data={"csrf": app.state.csrf})
    assert store.get_case("c3").draft.root_cause == "timing"
    assert store.get_case("c3").verdict.category == Category.product_bug


def test_the_page_says_when_no_triage_ran_in_this_folder(queue):
    client, _app, _store = queue
    assert "No triage has run in this folder" in client.get("/").text


def test_dashboard_starts_the_review_clock(queue):
    client, _app, store = queue
    client.get("/")
    assert "c1" in store.shown_ids()


JUNIT_TWO = """<testsuite tests="2" failures="2">
<testcase classname="t" name="test_one"><failure message="boom 1">boom 1</failure></testcase>
<testcase classname="t" name="test_two"><failure message="boom 1">boom 1</failure></testcase></testsuite>"""


def test_flaky_tests_always_get_a_draft_to_review(tmp_path, report, monkeypatch):
    from tessella import board, pipeline
    from tessella.draft.ticket import ticket

    monkeypatch.setattr(pipeline, "_judge", lambda lead, *a, **k: Verdict(
        category=Category.flaky, confidence=0.9, root_cause="Passed 1 of 3 re-runs: flaky"))
    result = pipeline.triage(_triage_settings(tmp_path, report), rerun=False)
    draft = result.cases[0].draft
    assert draft.title.startswith("[flaky]") and draft.expected_result == "The test passes on every run."
    assert "Passed 1 of 3" in draft.root_cause and "Not determined" not in draft.suggested_fix
    assert "several times" in draft.steps_to_reproduce
    row = board.classify(result)[0]
    assert row.status == board.Status.new and "Passed 1 of 3" in row.note and "mark it flaky" in row.note
    assert "1 flaky, drafted for review" in board.headline(result)


def test_the_dashboard_drafts_flaky_failures_stored_before(queue):
    client, _app, store = queue
    store.save_case(Case(id="old", origin=Origin.test_failure, title="t", signature="f",
                         verdict=Verdict(category=Category.flaky, confidence=0.9, root_cause="timing")))
    client.get("/")
    assert store.get_case("old").draft is not None


def test_every_row_of_a_filed_failure_shows_its_status_and_ticket(tmp_path):
    from tessella import report as page

    settings = _triage_settings(tmp_path, tmp_path / "none")
    store = Store(settings.storage.path)
    for cid in ("a", "b"):
        store.save_case(Case(id=cid, origin=Origin.test_failure, title=f"test_{cid}", signature="same",
                             verdict=Verdict(category=Category.product_bug, confidence=0.9, root_cause="x")))
    store.record(store.get_case("a"), Decision(action=Action.approve, by="anna", at=datetime(2026, 11, 8),
                                               ticket_key="KAN-7", ticket_url="https://j/KAN-7"))
    store.record_run(failures=2, clusters=1, recalled=1, drafted=0, detail={"same": 2})
    html = page.render(page.build(settings, store))
    table = html[html.index('<table class="cases">'):html.index("</table>", html.index('<table class="cases">'))]
    assert table.count("Already on the board") == 2 and table.count(">KAN-7</a>") == 2
    assert "same failure" not in table


def test_comparison_is_plain_numbers_and_never_says_fixed(tmp_path):
    from tessella import report as page

    settings = _triage_settings(tmp_path, tmp_path / "none")
    store = Store(settings.storage.path)
    store.save_case(Case(id="a", origin=Origin.test_failure, title="a", signature="bug",
                         verdict=Verdict(category=Category.product_bug, confidence=0.9, root_cause="x")))
    store.save_case(Case(id="b", origin=Origin.test_failure, title="b", signature="fl",
                         verdict=Verdict(category=Category.flaky, confidence=0.9, root_cause="x")))
    store.record_run(failures=1, clusters=1, recalled=0, drafted=0, detail={"bug": 1}, at=datetime(2026, 11, 1))
    store.record_run(failures=3, clusters=2, recalled=0, drafted=0, detail={"bug": 2, "fl": 1}, at=datetime(2026, 11, 2))
    data = page.build(settings, store)
    assert [(r["failed"], r["bugs"], r["flaky"]) for r in data["trend"]] == [(1, 1, 0), (3, 2, 1)]
    body, take = page._trend_html(data)
    assert take == "2 failed tests more than the previous run."
    assert '<td class="chg bad">up 2</td>' in body and "fixed" not in body.lower()
    assert page._trend_html({"trend": []})[1] == ""
    assert "First run" in page._trend_html({"trend": data["trend"][:1]})[1]


def test_probe_and_tiles_are_gone_from_the_dashboard(tmp_path):
    from tessella import report as page

    settings = _triage_settings(tmp_path, tmp_path / "none")
    html = page.render(page.build(settings, Store(settings.storage.path)))
    assert 'data-panel="probe"' not in html and 'data-panel="mosaic"' not in html
