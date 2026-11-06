from datetime import datetime

from tessella import board
from tessella.models import Action, Case, Category, Decision, Draft, DraftKind, Origin, Verdict
from tessella.pipeline import RunResult


def _case(sig, category=Category.product_bug, draft=True):
    c = Case(id=sig, origin=Origin.test_failure, title=f"test_{sig}", message="boom", signature=sig)
    c.verdict = Verdict(category=category, confidence=0.9, root_cause="x")
    if draft:
        c.draft = Draft(kind=DraftKind.ticket, title="t", body="b", signature=sig)
    return c


def _filed(key):
    return Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 18), board="jira",
                    ticket_key=key)


def test_five_bugs_three_known_two_new():
    result = RunResult(clusters={s: [_case(s)] for s in ("a", "b", "c", "d", "e")},
                       decisions={"a": _filed("KAN-7"), "b": _filed("KAN-8"), "c": _filed("KAN-9")})
    rows = {r.signature: r for r in board.classify(result)}
    assert rows["a"].status == board.Status.on_board and rows["a"].tickets == ["KAN-7"]
    assert rows["d"].status == board.Status.new
    assert board.headline(result) == (
        "5 failure clusters: 3 already on the board (KAN-7, KAN-8, KAN-9), no new tickets "
        "needed; 2 new, drafted with all details and waiting for review.")


def test_near_duplicate_and_not_filed_and_no_ticket():
    result = RunResult(
        clusters={"n": [_case("n")], "f": [_case("f", Category.flaky, draft=False)],
                  "r": [_case("r", draft=False)], "p": [Case(id="p", origin=Origin.test_failure,
                                                             title="t", signature="p")]},
        decisions={"r": Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 1))},
        jira_matches={"n": [{"key": "KAN-8", "summary": "Cancel returns 500", "score": 0.75}]})
    rows = {r.signature: r for r in board.classify(result)}
    assert rows["n"].status == board.Status.likely_on_board and "KAN-8" in rows["n"].note
    assert rows["f"].status == board.Status.your_call
    assert rows["r"].status == board.Status.decided and "no board was set up then" in rows["r"].note
    assert "by sdet on 01 Oct 2026" in rows["r"].note
    assert "Reject as flaky" in rows["f"].note
    assert rows["p"].status == board.Status.pending
    line = board.headline(result)
    assert "1 probably already on the board (KAN-8)" in line and "1 not analysed yet" in line


def test_an_approval_that_filed_nothing_is_judged_again_once_a_board_exists():
    from tessella.config import Settings
    from tessella.pipeline import _filed_nothing

    nothing = Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 18))
    filed = _filed("KAN-7")
    rejected = Decision(action=Action.reject, by="sdet", at=datetime(2026, 10, 18))
    jira = Settings(board="jira", jira={"url": "https://x.atlassian.net", "project": "KAN"})
    assert _filed_nothing(jira, nothing)
    assert not _filed_nothing(jira, filed) and not _filed_nothing(jira, rejected)
    assert not _filed_nothing(Settings(), nothing)  # still no board: the approval stands

    result = RunResult(clusters={"f": [_case("f", Category.flaky, draft=False)]},
                       reopened={"f": nothing})
    note = board.classify(result)[0].note
    assert note.startswith("approved by sdet on 18 Oct 2026 with no board, so nothing was filed")
    assert "Reject as flaky" in note


def test_a_test_that_fails_differently_is_a_new_question(tmp_path):
    """The case id is the test; a decision belongs to one failure (signature). When the same
    test later fails another way, the old decision must not hide or block the new failure."""
    from tessella.memory.store import Store

    store = Store(str(tmp_path / "t.sqlite"))
    old = _case("c1")
    old.signature = "oldsig"
    store.save_case(old)
    store.record(old, Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 18)))
    assert store.decided("c1") and store.open_cases() == []

    new = old.model_copy(update={"signature": "newsig", "message": "took 1.15s"})
    store.save_case(new)
    assert not store.decided("c1") and [c.id for c in store.open_cases()] == ["c1"]
    assert store.record(new, Decision(action=Action.reject, by="sdet", at=datetime(2026, 11, 6)))
    assert store.decided("c1") and store.last_decision("oldsig").action == Action.approve
    reopened = Store(str(tmp_path / "t.sqlite"))  # reopening runs the dedupe; both survive
    assert reopened.last_decision("newsig").action == Action.reject
    assert reopened.last_decision("oldsig").action == Action.approve
