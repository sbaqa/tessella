import httpx
import pytest
import respx

from tessella.config import Jira
from tessella.integrations import jira
from tessella.models import Draft, DraftKind

ALLOW = ["x.atlassian.net"]
CFG = Jira(url="https://x.atlassian.net", project="KAN", email="a@b.com")


def _draft(signature="sig123"):
    return Draft(kind=DraftKind.ticket, title="t", body="b", signature=signature)


@respx.mock
def test_issue_exists_false_only_on_404():
    respx.get("https://x.atlassian.net/rest/api/2/issue/KAN-9").mock(return_value=httpx.Response(404))
    respx.get("https://x.atlassian.net/rest/api/2/issue/KAN-1").mock(
        return_value=httpx.Response(200, json={"key": "KAN-1"}))
    respx.get("https://x.atlassian.net/rest/api/2/issue/KAN-5").mock(return_value=httpx.Response(500))
    assert jira.issue_exists(CFG, "KAN-9", ALLOW) is False
    assert jira.issue_exists(CFG, "KAN-1", ALLOW) is True
    assert jira.issue_exists(CFG, "KAN-5", ALLOW) is True  # 5xx is inconclusive -> keep


@respx.mock
def test_find_by_signature_hit_and_miss():
    route = respx.get("https://x.atlassian.net/rest/api/3/search/jql")
    route.mock(return_value=httpx.Response(200, json={"issues": [{"key": "KAN-7"}]}))
    assert jira.find_by_signature(CFG, "sig123", ALLOW) == ("KAN-7", "https://x.atlassian.net/browse/KAN-7")
    route.mock(return_value=httpx.Response(200, json={"issues": []}))
    assert jira.find_by_signature(CFG, "sig123", ALLOW) is None
    assert jira.find_by_signature(CFG, "", ALLOW) is None  # no signature -> no search


@respx.mock
def test_create_issue_links_to_existing_duplicate_instead_of_creating(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    respx.get("https://x.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": [{"key": "KAN-7"}]}))
    post = respx.post("https://x.atlassian.net/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"key": "SHOULD-NOT-HAPPEN"}))
    key, url = jira.create_issue(CFG, _draft(), ALLOW)
    assert key == "KAN-7"
    assert not post.called  # linked to the existing labelled issue; no new issue created


@respx.mock
def test_queue_reconciles_deleted_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    from datetime import datetime
    from starlette.testclient import TestClient
    from tessella.config import Settings, Storage
    from tessella.memory.store import Store
    from tessella.models import Action, Case, Decision, Origin
    from tessella.queue.app import create_app

    db = str(tmp_path / "t.sqlite")
    settings = Settings(board="jira", jira=CFG, storage=Storage(path=db),
                        network={"allow": ALLOW})
    seed = Store(db)
    case = Case(id="c1", origin=Origin.test_failure, title="t", message="boom", signature="sig80a1")
    seed.save_case(case)
    seed.record(case, Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 17),
                               board="jira", ticket_key="KAN-6"))
    assert seed.open_cases() == []  # filed -> hidden

    respx.get("https://x.atlassian.net/rest/api/2/issue/KAN-6").mock(return_value=httpx.Response(404))
    TestClient(create_app(settings)).get("/")

    reloaded = Store(db)
    assert reloaded.recent_decisions() == []          # dropped from "Recently decided"
    assert len(reloaded.open_cases()) == 1            # re-surfaced for re-approval


@respx.mock
def test_create_issue_adds_signature_label_when_creating(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    respx.get("https://x.atlassian.net/rest/api/3/search/jql").mock(
        return_value=httpx.Response(200, json={"issues": []}))
    post = respx.post("https://x.atlassian.net/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"key": "KAN-10"}))
    key, url = jira.create_issue(CFG, _draft("abc"), ALLOW)
    assert key == "KAN-10"
    sent = post.calls.last.request
    import json as _json
    labels = _json.loads(sent.content)["fields"]["labels"]
    assert "tessella-abc" in labels and "tessella" in labels


@respx.mock
def test_create_or_link_says_whether_a_duplicate_was_avoided(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    search = respx.get("https://x.atlassian.net/rest/api/3/search/jql")
    post = respx.post("https://x.atlassian.net/rest/api/2/issue").mock(
        return_value=httpx.Response(201, json={"key": "KAN-11"}))
    search.mock(return_value=httpx.Response(200, json={"issues": [{"key": "KAN-7"}]}))
    assert jira.create_or_link(CFG, _draft("abc"), ALLOW)[::2] == ("KAN-7", True)
    assert not post.called
    search.mock(return_value=httpx.Response(200, json={"issues": []}))
    assert jira.create_or_link(CFG, _draft("abc"), ALLOW)[::2] == ("KAN-11", False)
    assert post.called


@respx.mock
def test_ticket_status_reads_status_and_resolution_and_fails_open(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    route = respx.get("https://x.atlassian.net/rest/api/2/issue/KAN-7")
    route.mock(return_value=httpx.Response(200, json={"fields": {
        "status": {"name": "Done", "statusCategory": {"key": "done"}},
        "resolution": {"name": "Won't Do"},
        "created": "2026-10-01T09:00:00.000+0400", "resolutiondate": "2026-10-03T09:00:00.000+0400"}}))
    got = jira.ticket_status(CFG, "KAN-7", ALLOW)
    assert got == {"status": "Done", "category": "done", "resolution": "Won't Do",
                   "created": "2026-10-01T09:00:00.000+0400", "resolved": "2026-10-03T09:00:00.000+0400"}
    assert route.calls.last.request.method == "GET"  # read-only
    route.mock(return_value=httpx.Response(404))
    assert jira.ticket_status(CFG, "KAN-7", ALLOW) is None
    route.mock(return_value=httpx.Response(200, text="<html>login</html>"))
    assert jira.ticket_status(CFG, "KAN-7", ALLOW) is None
    monkeypatch.delenv("JIRA_TOKEN")
    assert jira.ticket_status(CFG, "KAN-7", ALLOW) is None  # no token: no call at all


def _case_with_draft(title="test_checkout_total[chromium]",
                     message="AssertionError: checkout total ignores discount code"):
    from tessella.models import Case, Category, Origin, Verdict
    from tessella.draft.ticket import ticket

    case = Case(id="c9", origin=Origin.test_failure, title=title, message=message, signature="s9")
    case.verdict = Verdict(category=Category.product_bug, confidence=0.9,
                           root_cause="discount is not applied to the checkout total")
    case.draft = ticket(case)
    return case


def _search_reply(*issues):
    return httpx.Response(200, json={"issues": [
        {"key": k, "fields": {"summary": s, "status": {"name": "To Do"}}} for k, s in issues]})


@respx.mock
def test_search_text_queries_plain_words_and_fails_open(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    route = respx.get("https://x.atlassian.net/rest/api/3/search/jql")
    route.mock(return_value=_search_reply(("KAN-3", "Discount code ignored at checkout")))
    found = jira.search_text(CFG, ["discount", "checkout", 'bad"word'], ALLOW)
    assert found == [{"key": "KAN-3", "summary": "Discount code ignored at checkout",
                      "status": "To Do", "url": "https://x.atlassian.net/browse/KAN-3"}]
    jql = route.calls.last.request.url.params["jql"]
    assert 'text ~ "discount OR checkout"' in jql and "bad" not in jql  # never a broken quote
    route.mock(return_value=httpx.Response(500))
    assert jira.search_text(CFG, ["discount"], ALLOW) == []
    assert jira.search_text(CFG, [], ALLOW) == []  # nothing to search for -> no call
    monkeypatch.delenv("JIRA_TOKEN")
    assert jira.search_text(CFG, ["discount"], ALLOW) == []


@respx.mock
def test_jira_duplicates_lists_near_matches_only(monkeypatch):
    from tessella.config import Settings
    from tessella.draft.duplicates import jira_duplicates

    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    respx.get("https://x.atlassian.net/rest/api/3/search/jql").mock(return_value=_search_reply(
        ("KAN-3", "Discount code ignored at checkout"),
        ("KAN-4", "Checkout page slow"),          # one shared word: not a duplicate
        ("KAN-5", "Login button misaligned")))
    settings = Settings(board="jira", jira=CFG, network={"allow": ALLOW})
    found = jira_duplicates(_case_with_draft(), settings, use_model=False)
    assert [i["key"] for i in found] == ["KAN-3"]
    # Not the board, or switched off: no search at all.
    assert jira_duplicates(_case_with_draft(), Settings(jira=CFG, network={"allow": ALLOW})) == []
    off = Settings(board="jira", jira=CFG.model_copy(update={"search_duplicates": False}),
                   network={"allow": ALLOW})
    assert jira_duplicates(_case_with_draft(), off) == []


@respx.mock
def test_jira_duplicates_scores_with_the_embedder_when_configured(monkeypatch):
    from tessella.config import Settings
    from tessella.draft.duplicates import jira_duplicates
    from tessella.integrations import llm

    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    respx.get("https://x.atlassian.net/rest/api/3/search/jql").mock(return_value=_search_reply(
        ("KAN-3", "Promo codes do nothing"), ("KAN-5", "Login button misaligned")))
    # The failure and KAN-3 point the same way; KAN-5 does not.
    monkeypatch.setattr(llm, "_embed", lambda ms, texts, allow: [[1, 0], [0.95, 0.05], [0, 1]])
    settings = Settings(board="jira", jira=CFG, network={"allow": [*ALLOW, "localhost:11435"]},
                        models={"embedder": {"name": "nomic-embed-text"}})
    found = jira_duplicates(_case_with_draft(), settings)
    assert [i["key"] for i in found] == ["KAN-3"]


def test_note_duplicates_adds_keys_and_body_lines():
    from tessella.pipeline import _note_duplicates

    draft = _case_with_draft().draft
    _note_duplicates(draft, [{"key": "KAN-3", "summary": "Discount ignored", "status": "To Do"}])
    assert draft.possible_duplicates == ["KAN-3"]
    assert "Possible duplicates already in Jira:**\n- KAN-3 (To Do): Discount ignored" in draft.body


@respx.mock
def test_board_check_reads_only_and_names_the_problem(monkeypatch, tmp_path):
    from tessella.config import Settings
    from tessella.integrations import board_check

    settings = Settings(board="jira", jira=CFG, network={"allow": ALLOW},
                        storage={"path": str(tmp_path / "t.sqlite")})
    assert board_check.check(settings)[0].message == "the env var JIRA_TOKEN is not set"
    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    respx.get("https://x.atlassian.net/rest/api/2/myself").mock(
        return_value=httpx.Response(200, json={"displayName": "Anna"}))
    respx.get("https://x.atlassian.net/rest/api/2/project/KAN").mock(
        return_value=httpx.Response(200, json={"issueTypes": [{"name": "Task"}]}))
    respx.get("https://x.atlassian.net/rest/api/3/search/jql").mock(return_value=_search_reply())
    post = respx.post(url__regex=r".*").mock(return_value=httpx.Response(201))
    checks = {c.name: c for c in board_check.check(settings)}
    assert checks["sign-in"].ok and "Anna" in checks["sign-in"].message
    assert checks["issue type"].ok is False and "Task" in checks["issue type"].message
    assert not post.called  # files nothing


@respx.mock
def test_board_check_verifies_link_back_and_duplicate_search_per_filed_ticket(monkeypatch, tmp_path):
    from datetime import datetime

    from tessella.config import Settings
    from tessella.integrations import board_check
    from tessella.memory.store import Store
    from tessella.models import Action, Decision

    monkeypatch.setenv("JIRA_TOKEN", "faketoken")
    db = str(tmp_path / "t.sqlite")
    store = Store(db)
    case = _case_with_draft()
    store.save_case(case)
    store.record(case, Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 18),
                                board="jira", ticket_key="KAN-9"))
    filed = {"key": "KAN-9", "fields": {"summary": case.draft.title, "status": {"name": "To Do"}}}

    def reply(request):
        jql = request.url.params["jql"]
        if f"tessella-{case.signature}" in jql or 'labels = "tessella"' in jql:
            return httpx.Response(200, json={"issues": [filed]})
        return httpx.Response(200, json={"issues": []})  # Jira's text index found nothing

    respx.get("https://x.atlassian.net/rest/api/3/search/jql").mock(side_effect=reply)
    settings = Settings(board="jira", jira=CFG, network={"allow": ALLOW}, storage={"path": db})
    checks = {c.name: c for c in board_check.filed(settings)}
    assert checks["KAN-9 link-back"].ok
    assert checks["KAN-9 duplicate search"].ok  # found through the tessella label
