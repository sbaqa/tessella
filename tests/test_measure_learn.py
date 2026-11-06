"""Phase 2: embeddings memory, few-shot examples, eval, model advice, dataset export."""

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from tessella import dataset, evaluate, pipeline
from tessella.cluster import recall as recall_mod
from tessella.config import ModelSettings, Settings
from tessella.integrations import llm, local_llm
from tessella.memory import vectors
from tessella.memory.store import Store
from tessella.models import (
    Action,
    Case,
    Category,
    Decision,
    Evidence,
    EvidenceKind,
    Origin,
    RejectReason,
    Verdict,
)
from tessella.setup import recommend as rec
from tessella.verdict import agent

OLLAMA = "http://127.0.0.1:11435"


def _settings(tmp_path, embedder=True):
    data = {"models": {"reader": {"provider": "ollama_chat", "name": "qwen3.5:4b",
                                  "base_url": OLLAMA}},
            "network": {"allow": ["localhost:11435"]},
            "memory": {"path": str(tmp_path / "vectors.sqlite")}}
    if embedder:
        data["models"]["embedder"] = {"provider": "ollama_chat", "name": "nomic-embed-text",
                                      "base_url": OLLAMA}
    s = Settings.model_validate(data)
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    return s


def _case(cid, sig, title="test_cancel", message="KeyError: 'discounted_total'", verdict=None):
    return Case(id=cid, origin=Origin.test_failure, title=title, message=message, signature=sig,
                evidence=[Evidence(kind=EvidenceKind.report, summary="failed in report",
                                   excerpt=message)], verdict=verdict)


PRODUCT = Verdict(category=Category.product_bug, confidence=0.9,
                  root_cause="Refund lookup crashes", evidence_used=[0],
                  actual_result="500", expected_result="200", steps_to_reproduce="cancel",
                  suggested_fix="read total")


def _decide(store, case, action=Action.approve, reason=None, key="KAN-12"):
    store.save_case(case)
    store.record(case, Decision(action=action, reason=reason, by="anna",
                                at=datetime(2026, 10, 20), ticket_key=key))


# --- embeddings ------------------------------------------------------------------------

def test_ollama_embed_request():
    respx = pytest.importorskip("respx")
    ms = ModelSettings(provider="ollama_chat", name="nomic-embed-text", base_url=OLLAMA)
    with respx.mock:
        route = respx.post(f"{OLLAMA}/api/embed").mock(
            return_value=httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]}))
        assert local_llm.embed(ms, ["a", "b"], ["localhost:11435"]) == [[1.0, 0.0], [0.0, 1.0]]
    body = json.loads(route.calls[0].request.content)
    assert body["input"] == ["a", "b"] and body["model"] == "nomic-embed-text"


def test_embed_is_policy_checked_and_audited(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(llm, "_embed", lambda ms, texts, allow: [[1.0]] * len(texts))
    assert llm.embed(s, ["x"], [llm.DataClass.evidence_excerpts]) == [[1.0]]
    entry = json.loads(Path(s.data_policy.audit_log).read_text().splitlines()[-1])
    assert entry["role"] == "embedder"


def test_cloud_embedder_is_refused_by_the_default_policy(tmp_path):
    s = _settings(tmp_path)
    s.models.embedder = ModelSettings(provider="openai", name="text-embedding-3-small")
    with pytest.raises(llm.PolicyViolation):
        llm.embed(s, ["x"], [llm.DataClass.evidence_excerpts])


def test_vector_index_keeps_models_apart(tmp_path):
    index = vectors.VectorIndex(tmp_path / "v.sqlite")
    index.add("s1", "c1", [1.0, 0.0], "m1")
    index.add("s2", "c2", [0.0, 1.0], "m1")
    index.add("s3", "c3", [1.0, 0.0], "m2")
    near = index.nearest([1.0, 0.1], "m1", k=5)
    assert [n[0] for n in near] == ["s1", "s2"] and near[0][2] > 0.99
    assert index.nearest([1.0, 0.0], "m1", exclude="s1")[0][0] == "s2"
    assert index.has("s3", "m2") and not index.has("s3", "m1")


def test_text_for_ignores_volatile_numbers():
    a = _case("a", "s", message="Order 1042 failed after 3 tries")
    b = _case("b", "s", message="Order 77 failed after 5 tries")
    assert vectors.text_for(a) == vectors.text_for(b)


# --- similarity recall and few-shot examples -------------------------------------------

def _memory_with_one_decision(tmp_path, monkeypatch, similar=True):
    s = _settings(tmp_path)
    store = Store(":memory:")
    old = _case("old", "sig-old", verdict=PRODUCT)
    _decide(store, old)
    mem = pipeline._Memory.open(s)
    mem.index.add("sig-old", "old", [1.0, 0.0], mem.model)
    monkeypatch.setattr(llm, "_embed", lambda ms, texts, allow:
                        [[1.0, 0.05] if similar else [0.0, 1.0]] * len(texts))
    return s, store, mem


def test_near_duplicate_becomes_cited_evidence_and_a_possible_duplicate(tmp_path, monkeypatch):
    s, store, mem = _memory_with_one_decision(tmp_path, monkeypatch)
    lead = _case("new", "sig-new")
    mem.recall(lead, s, store)
    ev = lead.evidence[-1]
    assert ev.kind == EvidenceKind.memory and "KAN-12" in ev.summary
    assert mem.duplicates["sig-new"] == ["KAN-12"]
    assert "Similar failures your team already reviewed" in mem.examples["sig-new"]
    assert "team approved as KAN-12" in mem.examples["sig-new"]
    mem.remember(lead)
    assert mem.index.has("sig-new", mem.model)


def test_unrelated_failure_gets_no_memory(tmp_path, monkeypatch):
    s, store, mem = _memory_with_one_decision(tmp_path, monkeypatch, similar=False)
    lead = _case("new", "sig-new")
    mem.recall(lead, s, store)
    assert len(lead.evidence) == 1 and mem.examples["sig-new"] == ""
    assert mem.duplicates["sig-new"] == []


def test_embedder_down_changes_nothing(tmp_path, monkeypatch):
    s, store, mem = _memory_with_one_decision(tmp_path, monkeypatch)

    def down(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(llm, "_embed", down)
    lead = _case("new", "sig-new")
    mem.recall(lead, s, store)
    assert len(lead.evidence) == 1 and "sig-new" not in mem.examples
    mem.remember(lead)  # nothing to remember, and no error


def test_no_embedder_means_no_memory(tmp_path):
    assert pipeline._Memory.open(_settings(tmp_path, embedder=False)) is None


def test_examples_follow_the_pack_and_fit_the_budget(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    sent = []

    def call(ms, messages, schema=None, temperature=0, allow=None):
        sent.append(messages[1]["content"])
        return json.dumps({"category": "flaky", "confidence": 0.95, "root_cause": "r",
                           "evidence_used": [0]}), 1
    monkeypatch.setattr(llm, "_call", call)
    examples = "Similar failures your team already reviewed (for calibration only; do not cite):\n- x"
    agent.decide(_case("c", "s"), s, examples=examples)
    assert sent[0].rstrip().endswith("- x") and sent[0].index("[0]") < sent[0].index("Similar")


def test_reindex_embeds_only_what_is_missing(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    store = Store(":memory:")
    _decide(store, _case("a", "sig-a", verdict=PRODUCT))
    _decide(store, _case("b", "sig-b", verdict=PRODUCT), key="KAN-13")
    calls = []

    def fake(ms, texts, allow):
        calls.append(len(texts))
        return [[1.0, 0.0]] * len(texts)
    monkeypatch.setattr(llm, "_embed", fake)
    assert vectors.reindex(s, store)["embedded"] == 2
    assert vectors.reindex(s, store)["embedded"] == 0
    assert calls == [2]


def test_duplicate_evidence_needs_the_threshold():
    d = Decision(action=Action.approve, by="a", at=datetime(2026, 10, 1), ticket_key="KAN-1")
    m = recall_mod.Similar(_case("o", "s", verdict=PRODUCT), d, 0.85)
    assert recall_mod.duplicate_evidence([m], 0.9) is None
    assert recall_mod.duplicate_evidence([m], 0.8) is not None


# --- eval -------------------------------------------------------------------------------

def _golden(tmp_path):
    p = tmp_path / "golden.yaml"
    p.write_text("cases:\n"
                 "  - {match: test_cancel, category: product_bug, facts: [refund, crash]}\n"
                 "  - {match: test_login, category: test_bug}\n"
                 "  - {match: test_gone, category: flaky}\n")
    return evaluate.load_golden(p)


def _eval_setup(tmp_path, monkeypatch, replies):
    """Two failures from fake reports; the reader answers by test name."""
    from tessella import collect

    cases = [_case("c1", "", title="tests/test_api.py::test_cancel"),
             _case("c2", "", title="tests/test_ui.py::test_login",
                   message="TimeoutError: waiting for locator('#login')")]
    monkeypatch.setattr(collect, "collect_cases", lambda reports, redact: [c.model_copy() for c in cases])
    calls = []

    def call(ms, messages, schema=None, temperature=0, allow=None):
        calls.append(ms.name)
        user = messages[1]["content"]
        if "category" not in (schema or {}).get("properties", {}):
            return json.dumps({"actual_result": "500", "expected_result": "200",
                               "steps_to_reproduce": "cancel", "environment": "",
                               "suggested_fix": ""}), 1
        return replies(ms.name, user), 1
    monkeypatch.setattr(llm, "_call", call)
    return _settings(tmp_path, embedder=False), calls


def _reply(category, confidence=0.95, root="Refund lookup crashes"):
    return json.dumps({"category": category, "confidence": confidence, "root_cause": root,
                       "evidence_used": [0]})


def test_eval_grades_each_model_without_touching_the_store(tmp_path, monkeypatch):
    def replies(model, user):
        if model == "small":
            return _reply("product_bug")  # wrong for the login test
        return _reply("test_bug" if "login" in user else "product_bug")
    s, _calls = _eval_setup(tmp_path, monkeypatch, replies)
    s.storage.path = str(tmp_path / "store.sqlite")
    results = evaluate.run(s, _golden(tmp_path), models=["small", "big"], runs=2)
    small, big = results
    assert small.missing == ["test_gone"]
    assert small.metrics["accuracy"] == 0.5 and big.metrics["accuracy"] == 1.0
    assert small.metrics["graded"] == 4 and small.metrics["consistency"] == 1.0
    assert big.metrics["facts_hit_rate"] == 1.0  # "refund" and "crash" are both in the diagnosis
    assert big.metrics["field_completeness"] == 0.75  # suggested_fix came back empty
    assert not (tmp_path / "store.sqlite").exists()
    assert evaluate.confusion(small.results) == {("test_bug", "product_bug"): 2}


def test_eval_skips_the_reply_cache_unless_asked(tmp_path, monkeypatch):
    s, calls = _eval_setup(tmp_path, monkeypatch, lambda m, u: _reply("product_bug"))
    evaluate.run(s, _golden(tmp_path), runs=2)
    first = len(calls)
    calls.clear()
    evaluate.run(s, _golden(tmp_path), runs=2, use_cache=True)
    evaluate.run(s, _golden(tmp_path), runs=2, use_cache=True)
    assert first == 8  # 2 runs x 2 clusters x (classify + ticket)
    assert len(calls) == 4  # cached: only the first run of the first eval asks


def test_unknown_counts_as_wrong_and_brier_rewards_honest_doubt():
    g = evaluate.Golden("t", Category.product_bug)
    sure_wrong = evaluate.grade(g, Verdict(category=Category.test_bug, confidence=0.95,
                                           root_cause="x"), 1.0)
    doubtful = evaluate.grade(g, Verdict(category=Category.unknown, confidence=0.3,
                                         root_cause="x"), 1.0)
    m = evaluate.metrics([sure_wrong, doubtful], runs=1)
    assert m["accuracy"] == 0 and m["unknown_rate"] == 0.5
    assert (sure_wrong.confidence - 0) ** 2 > (doubtful.confidence - 0) ** 2


def test_history_records_the_prompt_version(tmp_path):
    r = evaluate.EvalResult("ollama_chat/m", {"accuracy": 1.0}, [], [])
    path = tmp_path / "h.jsonl"
    evaluate.append_history([r], "g.yaml", 3, path)
    entry = json.loads(path.read_text())
    assert entry["prompt_version"] == evaluate.prompt_version() and entry["runs"] == 3


def test_eval_keeps_each_failure_and_every_exact_call(tmp_path, monkeypatch):
    s, _calls = _eval_setup(tmp_path, monkeypatch, lambda m, u: _reply("product_bug"))
    (big,) = evaluate.run(s, _golden(tmp_path))
    cancel, login = big.results
    assert (cancel.title, cancel.error, cancel.got, cancel.evidence_used) == (
        "tests/test_api.py::test_cancel", "KeyError: 'discounted_total'", "product_bug", [0])
    assert login.model == "ollama_chat/qwen3.5:4b" and not login.correct
    calls = evaluate.calls_for(big, cancel)
    assert [c["role"] for c in calls] == ["reader", "reader"]  # classify, then ticket fields
    assert calls[0]["schema"] and calls[0]["golden"] == "test_cancel" and calls[0]["run"] == 1
    assert "KeyError: 'discounted_total'" in calls[0]["messages"][1]["content"]
    assert json.loads(calls[0]["reply"])["category"] == "product_bug"
    assert not calls[0]["cached"]
    assert llm.recorder.get() is None  # recording ends with the eval run

    (path,) = evaluate.write_calls([big], tmp_path / "eval_runs")
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert path.is_absolute() and path.name.endswith("-ollama_chat_qwen3.5_4b.jsonl")
    assert lines == big.calls and big.calls_path == str(path)
    out = json.loads(evaluate.as_json([big]))[0]
    assert out["calls_file"] == str(path)
    assert {"title", "test_id", "error", "evidence_used", "model", "run", "root_cause",
            "confidence", "seconds", "expected", "got"} <= set(out["results"][0])
    history = tmp_path / "h.jsonl"
    evaluate.append_history([big], "g.yaml", 1, history)
    assert "results" not in json.loads(history.read_text())  # history stays metrics only


def test_eval_records_cache_hits_and_images_as_placeholders(tmp_path, monkeypatch):
    s, _calls = _eval_setup(tmp_path, monkeypatch, lambda m, u: _reply("product_bug"))
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x" * 1234)
    rec = []
    token = llm.recorder.set(rec.append)
    try:
        for _ in range(2):
            llm.complete("reader", s, "sys", "look", [llm.DataClass.redacted_summary],
                         images=[str(shot)])
    finally:
        llm.recorder.reset(token)
    first, second = rec
    parts = first["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["placeholder"] == "[image shot.png, image/png, 1234 bytes]"
    assert "base64" not in json.dumps(rec)
    assert not first["cached"] and second["cached"] and second["tokens"] == 0


def test_triage_records_no_calls(tmp_path, monkeypatch):
    recorders = []

    def replies(model, user):
        recorders.append(llm.recorder.get())
        return _reply("product_bug")
    s, _calls = _eval_setup(tmp_path, monkeypatch, replies)
    monkeypatch.setattr(pipeline, "collect_cases", lambda reports, redact: [
        _case("c1", "", title="tests/test_api.py::test_cancel")])
    monkeypatch.chdir(tmp_path)
    s.storage.path = str(tmp_path / "store.sqlite")
    pipeline.triage(s, rerun=False)
    assert recorders == [None]
    assert not (tmp_path / ".tessella" / "eval_runs").exists()


def _cli_eval(tmp_path, monkeypatch, *args):
    from typer.testing import CliRunner

    from tessella import cli

    s, _calls = _eval_setup(tmp_path, monkeypatch,
                            lambda m, u: _reply("test_bug" if "login" in u else "product_bug"))
    monkeypatch.setattr(cli, "_settings", lambda *a, **kw: s)
    monkeypatch.chdir(tmp_path)
    golden = tmp_path / "golden.yaml"
    _golden(tmp_path)
    return CliRunner().invoke(cli.app, ["eval", "-g", str(golden), *args],
                              env={"COLUMNS": "200"})


def test_cli_eval_prints_each_failure_and_where_the_calls_are(tmp_path, monkeypatch):
    result = _cli_eval(tmp_path, monkeypatch)
    out = result.output
    assert result.exit_code == 0, out
    for col in ("Test", "Error", "Expected", "Got", "Confidence", "Root cause"):
        assert col in out
    assert "tests/test_api.py::test_cancel" in out and "✓ product_bug" in out
    assert "Refund lookup crashes" in out and "test_gone" in out
    (path,) = (tmp_path / ".tessella" / "eval_runs").glob("*.jsonl")
    assert f"Exact prompts and replies: {path}" in out.replace("\n", "")
    assert "tessella queue" in out
    assert "[system]" not in out  # prompts only with --show-prompts


def test_cli_eval_show_prompts_prints_each_call(tmp_path, monkeypatch):
    out = _cli_eval(tmp_path, monkeypatch, "--show-prompts").output
    assert "call 1: reader ollama_chat/qwen3.5:4b" in out
    assert "[system]" in out and "[user]" in out and "[reply]" in out
    assert "TimeoutError: waiting for locator('#login')" in out
    assert '"category": "test_bug"' in out


def test_cli_eval_json_has_the_new_fields(tmp_path, monkeypatch):
    out = json.loads(_cli_eval(tmp_path, monkeypatch, "--json").output)
    r = out[0]["results"][0]
    assert r["title"] == "tests/test_api.py::test_cancel" and r["error"].startswith("KeyError")
    assert out[0]["calls_file"].endswith(".jsonl")


def test_golden_needs_match_and_category(tmp_path):
    p = tmp_path / "g.yaml"
    p.write_text("cases:\n  - {match: x}\n")
    with pytest.raises(ValueError):
        evaluate.load_golden(p)


# --- model advice ----------------------------------------------------------------------

@pytest.mark.parametrize("hw, reader", [
    (rec.Hardware(ram_gb=16, cpus=4), "qwen3.5:4b"),  # CPU, few cores
    (rec.Hardware(ram_gb=64, cpus=16), "qwen3.5:9b"),  # CPU: capped for speed
    (rec.Hardware(ram_gb=32, cpus=8, gpu="nvidia", vram_gb=24), "qwen3.5:27b"),
    (rec.Hardware(ram_gb=32, cpus=8, gpu="nvidia", vram_gb=8), "qwen3.5:4b"),
    (rec.Hardware(ram_gb=24, cpus=12, gpu="apple"), "qwen3.5:9b"),
    (rec.Hardware(ram_gb=36, cpus=12, gpu="apple"), "qwen3.5:27b"),
    (rec.Hardware(ram_gb=128, cpus=16, gpu="apple"), "qwen3.5:35b"),
    (rec.Hardware(ram_gb=4, cpus=2), "qwen3.5:0.8b"),
])
def test_recommend_by_memory(hw, reader):
    assert rec.recommend(hw).reader == reader


def test_recommend_when_nothing_fits_and_when_memory_is_unknown():
    assert rec.recommend(rec.Hardware(ram_gb=1.5, cpus=2)).reader is None
    assert rec.recommend(rec.Hardware(ram_gb=None, cpus=2)).reader == "qwen3.5:4b"


def test_recommend_mentions_pulled_models_and_the_embedder():
    r = rec.recommend(rec.Hardware(ram_gb=64, cpus=16), pulled=["qwen3.5:4b"])
    assert any("already have" in n and "qwen3.5:4b" in n for n in r.notes)
    assert any("nomic-embed-text" in n for n in r.notes)
    assert "embedder" in r.yaml() and "name: qwen3.5:9b" in r.yaml()


def test_bench_reports_warm_speed(monkeypatch):
    stats = iter([{"load_s": 3.0, "prompt_tps": 50.0, "gen_tps": 5.0},
                  {"load_s": 0.0, "prompt_tps": 200.0, "gen_tps": 20.0}])
    monkeypatch.setattr(local_llm, "generate_stats", lambda ms, p, allow: next(stats))
    st = rec.bench(ModelSettings(provider="ollama_chat", name="m"), [], runs=2)
    assert st["load_s"] == 3.0 and st["prompt_tps"] == 200.0 and st["gen_tps"] == 20.0
    assert rec.estimate_seconds(st) == pytest.approx(3000 / 200 + 250 / 20)


def test_generate_stats_converts_nanoseconds():
    respx = pytest.importorskip("respx")
    ms = ModelSettings(provider="ollama_chat", name="m", base_url=OLLAMA)
    with respx.mock:
        respx.post(f"{OLLAMA}/api/generate").mock(return_value=httpx.Response(200, json={
            "load_duration": 2e9, "prompt_eval_count": 100, "prompt_eval_duration": 5e8,
            "eval_count": 10, "eval_duration": 1e9}))
        st = local_llm.generate_stats(ms, "p", ["localhost:11435"])
    assert st == {"load_s": 2.0, "prompt_tokens": 100, "prompt_tps": 200.0, "gen_tokens": 10,
                  "gen_tps": 10.0}


# --- dataset ---------------------------------------------------------------------------

def test_dataset_labels_come_from_the_team(tmp_path):
    s = _settings(tmp_path, embedder=False)
    store = Store(":memory:")
    _decide(store, _case("a", "sig-a", verdict=PRODUCT))
    wrong = PRODUCT.model_copy()
    _decide(store, _case("b", "sig-b", title="test_login", verdict=wrong), Action.reject,
            RejectReason.test_bug, key=None)
    _decide(store, _case("c", "sig-c", verdict=PRODUCT), Action.reject, RejectReason.not_a_bug,
            key=None)
    out = tmp_path / "d.jsonl"
    assert dataset.export(s, store, out) == {"written": 2, "corrected": 1, "skipped": 1}
    rows = {json.loads(line)["meta"]["signature"]: json.loads(line)
            for line in out.read_text().splitlines()}
    approved = json.loads(rows["sig-a"]["messages"][2]["content"])
    corrected = json.loads(rows["sig-b"]["messages"][2]["content"])
    assert approved["category"] == "product_bug" and approved["root_cause"] == PRODUCT.root_cause
    assert corrected["category"] == "test_bug" and rows["sig-b"]["meta"]["corrected"]
    assert rows["sig-a"]["messages"][0]["content"].startswith("You are a senior SDET")
    assert "[0]" in rows["sig-a"]["messages"][1]["content"]


def test_dataset_drops_memory_evidence(tmp_path):
    case = _case("a", "sig-a", verdict=PRODUCT)
    case.evidence.append(Evidence(kind=EvidenceKind.memory, summary="Reads like KAN-1"))
    d = Decision(action=Action.approve, by="a", at=datetime(2026, 10, 1))
    r = dataset.record(case, d, 10_000)
    assert "Reads like KAN-1" not in r["messages"][1]["content"]
