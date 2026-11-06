import json

import pytest

from tessella.config import ModelSettings, Settings
from tessella.draft import patch
from tessella.integrations import llm
from tessella.integrations.llm import DataClass, PolicyViolation
from tessella.models import Case, Category, Evidence, EvidenceKind, Origin, Verdict
from tessella.verdict import agent

SECRET_LOG = "ERROR order 8812 failed for anna@bank.example"


def _settings(tmp_path, **models):
    s = Settings()
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    for role, ms in models.items():
        setattr(s.models, role, ms)
    return s


def _cloud(**kw):
    return ModelSettings(provider="anthropic", name="some-model", base_url=None, **kw)


def _case():
    return Case(id="c1", origin=Origin.test_failure, title="tests/test_pay.py::test_pay",
                message="locator not found",
                evidence=[Evidence(kind=EvidenceKind.log, summary="1 log line near 10:00",
                                   excerpt=SECRET_LOG)],
                verdict=Verdict(category=Category.test_bug, confidence=0.9,
                                root_cause="Button id renamed from #pay to #pay-now",
                                evidence_used=[0]))


def _fake_call(sent):
    def call(ms, messages, **_):
        sent.append(messages)
        return "--- a/x\n+++ b/x\n@@\n-#pay\n+#pay-now\n", 10
    return call


def test_old_single_model_config_becomes_reader():
    s = Settings.model_validate({"model": {"provider": "ollama", "name": "m1"}})
    assert s.models.reader.name == "m1" and s.model.name == "m1"


def test_cloud_reader_cannot_receive_log_excerpts(tmp_path, monkeypatch):
    s = _settings(tmp_path, reader=_cloud())
    s.network.allow.append("api.anthropic.com")
    sent = []
    monkeypatch.setattr(llm, "_call", _fake_call(sent))
    v = agent.decide(_case(), s)
    assert v.category == Category.unknown and "keeps evidence_excerpts" in v.root_cause
    assert sent == []  # nothing was sent anywhere


def test_cloud_host_must_be_on_the_allowlist(tmp_path):
    s = _settings(tmp_path, fixer=_cloud())
    with pytest.raises(PolicyViolation):
        llm.check("fixer", s, [DataClass.redacted_summary])


def test_approval_is_required_when_configured(tmp_path):
    s = _settings(tmp_path, fixer=_cloud(approve_each_call=True))
    s.network.allow.append("api.anthropic.com")
    with pytest.raises(PolicyViolation):
        llm.check("fixer", s, [DataClass.redacted_summary])
    assert llm.check("fixer", s, [DataClass.redacted_summary], approver=lambda _: True)


def test_local_reads_cloud_writes(tmp_path, monkeypatch):
    """The cloud fixer gets the summary and the test file, never the log line."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pay.py").write_text("page.click('#pay')\n")
    s = _settings(tmp_path, fixer=_cloud())
    s.inputs.repo = str(tmp_path)
    s.network.allow.append("api.anthropic.com")
    sent = []
    monkeypatch.setattr(llm, "_call", _fake_call(sent))

    draft = patch.propose(_case(), s)

    assert draft is not None and "#pay-now" in draft.body
    payload = json.dumps(sent)
    assert "Button id renamed" in payload and "page.click('#pay')" in payload
    assert SECRET_LOG not in payload

    [record] = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert record["location"] == "cloud" and record["host"] == "api.anthropic.com"
    assert record["data_classes"] == ["redacted_summary", "failing_test_file"]
    assert "payload" not in record and len(record["sha256"]) == 64


def test_screenshots_never_go_to_a_cloud_model(tmp_path):
    s = _settings(tmp_path, vision=_cloud())
    s.network.allow.append("api.anthropic.com")
    with pytest.raises(PolicyViolation):
        llm.complete("vision", s, "sys", "what is on screen?", data_classes=[],
                     images=["shot.png"])


def test_local_vision_model_receives_the_image(tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG fake")
    s = _settings(tmp_path, vision=ModelSettings(provider="ollama", name="vis"))
    sent = []
    monkeypatch.setattr(llm, "_call", _fake_call(sent))
    llm.complete("vision", s, "sys", "describe", data_classes=[], images=[str(shot)])
    parts = sent[0][1]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_ollama_options_are_passed_only_to_ollama():
    local = ModelSettings(provider="ollama_chat", name="qwen3.5:4b", think=False, num_ctx=8192)
    opts = llm.call_options(local)
    assert opts["model"] == "ollama_chat/qwen3.5:4b"
    assert opts["think"] is False and opts["num_ctx"] == 8192
    cloud = llm.call_options(_cloud(think=False, num_ctx=8192))
    assert "think" not in cloud and "num_ctx" not in cloud


def test_defaults_fit_a_cpu_laptop():
    ms = ModelSettings()
    assert ms.name == "qwen3.5:4b" and not ms.is_cloud and ms.max_steps_per_cluster == 4
    assert "think" not in llm.call_options(ms)  # unset means the model's own default


def test_thinking_block_is_removed_from_replies():
    assert llm.strip_thinking("<think>long reasoning\nmore</think>\n{\"category\": \"test_bug\"}") \
        == '{"category": "test_bug"}'
    assert llm.strip_thinking("plain answer") == "plain answer"
