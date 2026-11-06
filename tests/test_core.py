from datetime import datetime

import pytest

from tessella import net
from tessella.memory.store import Store
from tessella.models import Action, Case, Category, Decision, Evidence, EvidenceKind, Origin, RejectReason
from tessella.redact import redact_text
from tessella.verdict.agent import parse


def _case():
    return Case(id="c1", origin=Origin.test_failure, title="t", message="boom", signature="sig1",
                evidence=[Evidence(kind=EvidenceKind.report, summary="500 in report")])


def test_redaction_masks_tokens_and_keys():
    text = 'Authorization: Bearer eyJabc.def.ghi password=hunter2 {"card_number": "4111"}'
    out = redact_text(text, ["password", "card_number"])
    assert "hunter2" not in out and "eyJabc" not in out and "4111" not in out


def test_allowlist_blocks_unknown_hosts():
    assert net.is_allowed("http://localhost:11434/api", ["localhost:11434"])
    assert not net.is_allowed("https://api.example.com", ["localhost:11434"])
    with net.client(["localhost:11434"]) as c, pytest.raises(net.EgressBlocked):
        c.get("https://example.com")


def test_store_remembers_rejections_by_signature():
    store = Store(":memory:")
    case = _case()
    store.save_case(case)
    store.record(case, Decision(action=Action.reject, reason=RejectReason.environment,
                                by="anna", at=datetime(2026, 10, 12)))
    past = store.last_decision("sig1")
    assert past and past.reason == RejectReason.environment
    assert store.open_cases() == []


def test_store_forget_reopens_signature_for_the_queue():
    store = Store(":memory:")
    case = _case()
    store.save_case(case)
    store.record(case, Decision(action=Action.approve, by="anna", at=datetime(2026, 10, 12),
                                board="jira", ticket_key="KAN-6"))
    assert store.last_decision("sig1") is not None
    assert store.open_cases() == []  # filed, so hidden from the queue
    store.forget("sig1")
    assert store.last_decision("sig1") is None
    assert len(store.open_cases()) == 1  # ticket gone -> reappears for re-approval


def test_record_is_idempotent_per_case():
    # A double-submitted approval (e.g. a retry after a slow Jira call) must not write a
    # second decision row — the board dedups by signature, the local store has to match.
    store = Store(":memory:")
    case = _case()
    store.save_case(case)
    d = Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 18),
                 board="jira", ticket_key="KAN-8")
    assert store.record(case, d) is True
    assert store.decided(case.id) is True
    assert store.record(case, d) is False  # resubmit is ignored
    assert len(store.recent_decisions()) == 1


def test_existing_duplicate_decisions_are_cleaned_on_open(tmp_path):
    # Older builds could leave two rows for one approval; reopening the store collapses them.
    path = tmp_path / "t.db"
    store = Store(path)
    case = _case()
    store.save_case(case)
    d = Decision(action=Action.approve, by="sdet", at=datetime(2026, 10, 18),
                 board="jira", ticket_key="KAN-8")
    # Force two rows past the idempotency guard, as the old unconditional INSERT would have.
    for _ in range(2):
        store.db.execute(
            "INSERT INTO decisions (case_id, signature, action, reason, by, at, "
            "board, ticket_key, ticket_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case.id, case.signature, d.action, None, d.by, d.at.isoformat(),
             d.board, d.ticket_key, None),
        )
    store.db.commit()
    assert len(store.recent_decisions()) == 2
    reopened = Store(path)  # __init__ runs _dedupe_decisions
    assert len(reopened.recent_decisions()) == 1


def test_verdict_without_cited_evidence_becomes_unknown():
    case = _case()
    v = parse('{"category": "product_bug", "confidence": 0.9, "root_cause": "x", '
              '"evidence_used": []}', case, 0.6)
    assert v.category == Category.unknown
    ok = parse('{"category": "product_bug", "confidence": 0.9, "root_cause": "x", '
               '"evidence_used": [0]}', case, 0.6)
    assert ok.category == Category.product_bug
