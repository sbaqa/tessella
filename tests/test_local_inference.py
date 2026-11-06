"""Phase 1: the direct local client, schema-enforced replies, two-step verdicts, the
evidence pack, agreement voting, the reply cache, and vision image preparation."""

import json

import httpx
import pytest

from tessella.config import ModelSettings, Settings
from tessella.integrations import llm, local_llm
from tessella.investigate import test_source
from tessella.models import Case, Category, Evidence, EvidenceKind, Origin
from tessella.verdict import agent, pack, prompts

OLLAMA = "http://127.0.0.1:11435"


def _settings(tmp_path, **reader):
    s = Settings.model_validate({
        "models": {"reader": {"provider": "ollama_chat", "name": "qwen3.5:4b",
                              "base_url": OLLAMA, **reader}},
        "network": {"allow": ["localhost:11435"]},
    })
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    return s


def _case(**kw):
    return Case(id="c1", origin=Origin.test_failure, title="tests/test_pay.py::test_pay",
                message="AssertionError: 500 != 200",
                evidence=[Evidence(kind=EvidenceKind.report, summary="failed in report",
                                   excerpt="E   AssertionError: 500 != 200"),
                          Evidence(kind=EvidenceKind.rerun,
                                   summary="Failed on all 2 re-runs: consistent failure")], **kw)


def _replies(*texts):
    """A fake _call that returns the given replies in order and records what was sent."""
    sent, queue = [], list(texts)

    def call(ms, messages, schema=None, temperature=0, allow=None):
        sent.append({"messages": messages, "schema": schema, "temperature": temperature})
        return queue.pop(0), 10
    return call, sent


CLASSIFIED = json.dumps({"category": "product_bug", "confidence": 0.9,
                         "root_cause": "Cancel returns 500 for discounted orders",
                         "evidence_used": [0, 1]})
FIELDS = json.dumps({"actual_result": "500", "expected_result": "200 and a refund",
                     "steps_to_reproduce": ["Pay an order", "Cancel it"],
                     "environment": "API", "suggested_fix": "Handle the discount in refund()"})


# --- direct local client --------------------------------------------------------------

def test_ollama_request_carries_schema_options_and_images(tmp_path):
    respx = pytest.importorskip("respx")
    ms = ModelSettings(provider="ollama_chat", name="m", base_url=OLLAMA, num_ctx=8192,
                       think=False)
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": [{"type": "text", "text": "look"},
                                             {"type": "image_url",
                                              "image_url": {"url": "data:image/png;base64,QUJD"}}]}]
    with respx.mock:
        route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"content": "{}"}, "prompt_eval_count": 7, "eval_count": 3}))
        text, tokens = local_llm.chat(ms, messages, ["localhost:11435"], schema={"type": "object"})
    body = json.loads(route.calls[0].request.content)
    assert (text, tokens) == ("{}", 10)
    assert body["format"] == {"type": "object"} and body["stream"] is False
    assert body["options"] == {"temperature": 0, "num_ctx": 8192} and body["think"] is False
    assert body["keep_alive"] == "30m"
    assert body["messages"][1] == {"role": "user", "content": "look", "images": ["QUJD"]}


def test_other_local_servers_get_the_schema_as_response_format():
    respx = pytest.importorskip("respx")
    ms = ModelSettings(provider="lm_studio", name="m", base_url="http://localhost:1234/v1")
    with respx.mock:
        route = respx.post("http://localhost:1234/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                                   "usage": {"total_tokens": 5}}))
        assert local_llm.chat(ms, [], ["localhost:1234"], schema={"type": "object"}) == ("ok", 5)
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}


def test_local_calls_go_through_the_allowlist():
    ms = ModelSettings(provider="ollama_chat", name="m", base_url="http://10.1.2.3:11434")
    with pytest.raises(Exception, match="not in network.allow"):
        local_llm.chat(ms, [], ["localhost:11434"])


def test_local_roles_never_import_litellm(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "litellm", None)  # importing it would now fail
    monkeypatch.setattr(local_llm, "chat", lambda *a, **k: ("hi", 1))
    assert llm.complete("reader", _settings(tmp_path), "s", "u", []) == ("hi", 1)


# --- schema, repair and coercion ------------------------------------------------------

def test_invalid_reply_is_retried_once_with_the_error(tmp_path, monkeypatch):
    call, sent = _replies("not json at all", CLASSIFIED)
    monkeypatch.setattr(llm, "_call", call)
    reply, _raw, tokens = llm.complete_json("reader", _settings(tmp_path), "s", "u", [],
                                            prompts.Classification)
    assert reply.category == "product_bug" and tokens == 20
    assert sent[0]["schema"]["properties"]["category"]["enum"][0] == "product_bug"
    retry = sent[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "not json at all"}
    assert "not valid" in retry[-1]["content"]


def test_two_bad_replies_give_none(tmp_path, monkeypatch):
    call, _ = _replies("nope", "still nope")
    monkeypatch.setattr(llm, "_call", call)
    assert llm.complete_json("reader", _settings(tmp_path), "s", "u", [],
                             prompts.Classification)[0] is None


def test_coerce_repairs_small_model_habits():
    fixed = prompts.coerce({"category": "Product Bug", "confidence": "85%",
                            "evidence_used": ["[0]", "evidence 2"],
                            "steps_to_reproduce": ["Open the cart", "Pay"]})
    assert fixed["category"] == "product_bug" and fixed["confidence"] == 0.85
    assert fixed["evidence_used"] == [0, 2]
    assert fixed["steps_to_reproduce"] == "1. Open the cart\n2. Pay"


def test_schemas_have_no_refs_so_every_server_can_enforce_them():
    for model in (prompts.Classification, prompts.TicketFields, prompts.FullVerdict):
        assert "$defs" not in json.dumps(model.model_json_schema())


# --- two-step verdicts ----------------------------------------------------------------

def test_two_step_classifies_then_writes_the_ticket(tmp_path, monkeypatch):
    call, sent = _replies(CLASSIFIED, FIELDS)
    monkeypatch.setattr(llm, "_call", call)
    v = agent.decide(_case(), _settings(tmp_path))
    assert v.category == Category.product_bug and v.evidence_used == [0, 1]
    assert v.steps_to_reproduce == "1. Pay an order\n2. Cancel it"
    assert "Diagnosis: product_bug" in sent[1]["messages"][1]["content"]
    assert set(sent[0]["schema"]["properties"]) == {"category", "confidence", "root_cause",
                                                     "evidence_used"}


def test_no_ticket_call_for_flaky_or_unknown(tmp_path, monkeypatch):
    call, sent = _replies(json.dumps({"category": "environment", "confidence": 0.95,
                                      "root_cause": "DNS down", "evidence_used": [0]}))
    monkeypatch.setattr(llm, "_call", call)
    assert agent.decide(_case(), _settings(tmp_path)).category == Category.environment
    assert len(sent) == 1


def test_one_step_asks_for_everything_at_once(tmp_path, monkeypatch):
    combined = {**json.loads(CLASSIFIED), **json.loads(FIELDS)}
    call, sent = _replies(json.dumps(combined))
    monkeypatch.setattr(llm, "_call", call)
    v = agent.decide(_case(), _settings(tmp_path, two_step=False))
    assert v.expected_result == "200 and a refund" and len(sent) == 1


# --- agreement voting -----------------------------------------------------------------

def _verdict(category, confidence):
    return json.dumps({"category": category, "confidence": confidence, "root_cause": "x",
                       "evidence_used": [0]})


def test_borderline_verdict_is_voted_and_agreement_lifts_it(tmp_path, monkeypatch):
    call, sent = _replies(_verdict("test_bug", 0.55), _verdict("test_bug", 0.6),
                          _verdict("test_bug", 0.5), FIELDS)
    monkeypatch.setattr(llm, "_call", call)
    v = agent.decide(_case(), _settings(tmp_path, investigate_steps=0))
    assert [s["temperature"] for s in sent[:3]] == [0, agent.VOTE_TEMPERATURE,
                                                    agent.VOTE_TEMPERATURE]
    assert v.category == Category.test_bug and v.agreement == 1.0
    assert v.confidence == pytest.approx((0.55 + 1) / 2, abs=0.01)  # mean of 0.55, 0.6, 0.5


def test_split_vote_stays_below_the_threshold(tmp_path, monkeypatch):
    call, _ = _replies(_verdict("test_bug", 0.6), _verdict("product_bug", 0.6),
                       _verdict("flaky", 0.6))
    monkeypatch.setattr(llm, "_call", call)
    v = agent.decide(_case(), _settings(tmp_path, investigate_steps=0))
    assert v.category == Category.unknown and v.agreement == pytest.approx(0.33, abs=0.01)


def test_confident_verdict_is_not_voted(tmp_path, monkeypatch):
    call, sent = _replies(CLASSIFIED, FIELDS)
    monkeypatch.setattr(llm, "_call", call)
    agent.decide(_case(), _settings(tmp_path))
    assert [s["temperature"] for s in sent] == [0, 0]


# --- reply cache ----------------------------------------------------------------------

def test_same_prompt_is_answered_from_the_cache(tmp_path, monkeypatch):
    call, sent = _replies("first")
    monkeypatch.setattr(llm, "_call", call)
    s = _settings(tmp_path)
    assert llm.complete("reader", s, "sys", "u", []) == ("first", 10)
    assert llm.complete("reader", s, "sys", "u", []) == ("first", 0)  # no call, no tokens
    assert len(sent) == 1
    audit = (tmp_path / "calls.jsonl").read_text().splitlines()
    assert len(audit) == 1  # the audit log lists calls that were made


def test_sampled_calls_and_disabled_cache_always_call(tmp_path, monkeypatch):
    call, sent = _replies("a", "b", "c", "d")
    monkeypatch.setattr(llm, "_call", call)
    s = _settings(tmp_path)
    llm.complete("reader", s, "sys", "u", [], temperature=0.7)
    llm.complete("reader", s, "sys", "u", [], temperature=0.7)
    s.cache.enabled = False
    llm.complete("reader", s, "sys", "u", [])
    llm.complete("reader", s, "sys", "u", [])
    assert len(sent) == 4


# --- evidence pack and test source ----------------------------------------------------

def test_pack_keeps_every_number_and_fits_the_budget():
    long_log = "\n".join(f"{'abcdefg'[i % 7]} log line {i}" for i in range(3000))  # no folding
    case = _case()
    case.evidence.append(Evidence(kind=EvidenceKind.log, summary="many lines", excerpt=long_log))
    text = pack.evidence_pack(case, budget=3000)
    assert len(text) <= 3100
    for i in range(3):
        assert f"[{i}] " in text
    assert "log line 0" in text and "log line 2999" in text and "characters cut" in text
    assert "E   AssertionError: 500 != 200" in text  # short, high-weight excerpts stay whole


def test_budget_follows_num_ctx():
    big = ModelSettings(num_ctx=8192, max_evidence_tokens=None)
    assert pack.budget_chars(big) > pack.budget_chars(ModelSettings(max_evidence_tokens=None))


def test_max_evidence_tokens_caps_the_budget():
    assert pack.budget_chars(ModelSettings(num_ctx=32768)) == int(3000 * pack.CHARS_PER_TOKEN)
    assert pack.budget_chars(ModelSettings()) == int((4096 - 1200) * 3.5)  # context is smaller
    old = max(2000, int((32768 - pack.RESERVED_TOKENS) * pack.CHARS_PER_TOKEN))
    assert pack.budget_chars(ModelSettings(num_ctx=32768, max_evidence_tokens=None)) == old
    case = _case()
    case.evidence.append(Evidence(kind=EvidenceKind.trace, summary="big",
                                  excerpt="\n".join(f"{chr(97 + i % 26)} {i}" for i in range(20000))))
    budget = pack.budget_chars(ModelSettings(num_ctx=32768))
    assert len(pack.evidence_pack(case, budget)) <= budget + 100


def test_fold_identical_and_volatile_lines():
    text = (
        "ERROR db down\n"
        "ERROR db down\n"
        "ERROR db down\n"
        "2026-10-30T10:00:01.123Z retry 1 after 200ms id=3fa85f64-5717-4562-b3fc-2c963f66afa6\n"
        "2026-10-30T10:00:02.523Z retry 2 after 400ms id=4fa85f64-5717-4562-b3fc-2c963f66afa6\n"
        "INFO ok\n"
        "ERROR db down\n"
        "INFO ok")
    assert pack.fold(text).split("\n") == [
        "ERROR db down (×3)",
        "2026-10-30T10:00:01.123Z retry 1 after 200ms id=3fa85f64-5717-4562-b3fc-2c963f66afa6 (×2)",
        "INFO ok", "ERROR db down", "INFO ok"]  # non-consecutive repeats are untouched


PY_TB = """Traceback (most recent call last):
  File "/home/u/proj/tests/test_pay.py", line 12, in test_pay
    r = client.post("/pay")
  File "/home/u/proj/venv/lib/python3.12/site-packages/requests/api.py", line 115, in post
    return request("post", url)
  File "/home/u/proj/venv/lib/python3.12/site-packages/requests/api.py", line 59, in request
    return session.request(method=method, url=url)
  File "/home/u/proj/shop/pay.py", line 40, in charge
    total = cart.total()
  File "/usr/lib/python3.12/site-packages/urllib3/pool.py", line 7, in urlopen
    conn.send()
  File "<frozen importlib._bootstrap>", line 1, in _call
  File "/usr/lib/python3.12/json/decoder.py", line 337, in decode
    obj, end = self.raw_decode(s)
  File "/usr/lib/python3.12/json/decoder.py", line 355, in raw_decode
    raise JSONDecodeError("Expecting value", s, err.value)
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)"""


def test_python_traceback_keeps_user_frames_and_error():
    out = pack.shorten_traces(PY_TB).split("\n")
    assert out == [
        "Traceback (most recent call last):",
        '  File "/home/u/proj/tests/test_pay.py", line 12, in test_pay',
        '    r = client.post("/pay")',
        "  … 2 library frames …",
        '  File "/home/u/proj/shop/pay.py", line 40, in charge',
        "    total = cart.total()",
        "  … 2 library frames …",
        '  File "/usr/lib/python3.12/json/decoder.py", line 337, in decode',
        "    obj, end = self.raw_decode(s)",
        '  File "/usr/lib/python3.12/json/decoder.py", line 355, in raw_decode',
        '    raise JSONDecodeError("Expecting value", s, err.value)',
        "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)"]


def test_js_stack_keeps_user_frames_and_error():
    stack = (
        "TypeError: Cannot read properties of undefined (reading 'id')\n"
        "    at renderCart (/app/src/cart.js:22:15)\n"
        "    at Array.map (<anonymous>)\n"
        "    at processTicksAndRejections (node:internal/process/task_queues:95:5)\n"
        "    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)\n"
        "    at handler (/app/src/server.js:10:3)\n"
        "    at next (/app/node_modules/express/lib/router/route.js:144:13)\n"
        "    at Route.dispatch (/app/node_modules/express/lib/router/route.js:114:3)\n"
        "    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)\n"
        "    at /app/node_modules/express/lib/router/index.js:284:15")
    assert pack.shorten_traces(stack).split("\n") == [
        "TypeError: Cannot read properties of undefined (reading 'id')",
        "    at renderCart (/app/src/cart.js:22:15)",
        "    … 3 library frames …",
        "    at handler (/app/src/server.js:10:3)",
        "    … 2 library frames …",
        "    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)",
        "    at /app/node_modules/express/lib/router/index.js:284:15"]


def test_java_stack_drops_library_frames():
    stack = (
        "java.lang.AssertionError: expected 200\n"
        "\tat org.junit.Assert.fail(Assert.java:89)\n"
        "\tat org.junit.Assert.assertEquals(Assert.java:12)\n"
        "\tat org.junit.Assert.assertEquals(Assert.java:15)\n"
        "\tat com.shop.PayTest.pays(PayTest.java:31)\n"
        "\tat java.base/jdk.internal.reflect.Method.invoke(Method.java:566)\n"
        "\tat org.junit.runners.Runner.run(Runner.java:1)")
    out = pack.shorten_traces(stack)
    assert "\t… 2 library frames …" in out and "com.shop.PayTest.pays" in out
    assert out.startswith("java.lang.AssertionError") and out.endswith("Runner.java:1)")


def test_lean_pack_keeps_numbering_and_leaves_test_source_alone():
    case = _case()
    source = "  1 | x = 1\n  2 | x = 1\n> 3 | assert x == 2"
    case.evidence.append(Evidence(kind=EvidenceKind.test_source, summary="src", excerpt=source))
    case.evidence.append(Evidence(kind=EvidenceKind.log, summary="log",
                                  excerpt="\n".join(["WARN slow 12ms"] * 50)))
    case.evidence.append(Evidence(kind=EvidenceKind.trace, summary="tb", excerpt=PY_TB))
    text = pack.evidence_pack(case, 10000)
    labels = [ln for ln in text.split("\n") if ln.startswith("[")]
    assert labels == [f"[{i}] {ev.kind}: {ev.summary}" for i, ev in enumerate(case.evidence)]
    assert source in text and "WARN slow 12ms (×50)" in text and "library frames" in text
    assert "json.decoder.JSONDecodeError" in text


def test_test_source_shows_the_failing_line(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pay.py").write_text(
        "def test_pay(page):\n    page.goto('/cart')\n    page.click('#pay')\n    assert ok\n")
    case = _case(stack='  File "/ci/tests/test_pay.py", line 3, in test_pay\n    page.click')
    ev = test_source.test_source_evidence(case, tmp_path, context=1)
    assert ev.kind == EvidenceKind.test_source and ev.ref == "tests/test_pay.py:3"
    assert ev.excerpt.splitlines() == ["  2 |     page.goto('/cart')",
                                       "> 3 |     page.click('#pay')",
                                       "  4 |     assert ok"]


def test_test_source_never_reads_outside_the_repo(tmp_path):
    (tmp_path / "secret.py").write_text("x = 1\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    case = _case(test_id="../secret.py::x")
    assert test_source.test_source_evidence(case, repo) is None


def test_reader_declares_the_test_file_when_it_sends_it(tmp_path, monkeypatch):
    call, _ = _replies(CLASSIFIED, FIELDS)
    monkeypatch.setattr(llm, "_call", call)
    case = _case()
    case.evidence.append(Evidence(kind=EvidenceKind.test_source, summary="src", excerpt="x"))
    agent.decide(case, _settings(tmp_path))
    record = json.loads((tmp_path / "calls.jsonl").read_text().splitlines()[0])
    assert record["data_classes"] == ["evidence_excerpts", "failing_test_file"]


# --- vision ---------------------------------------------------------------------------

def test_vision_images_are_scaled_and_the_changed_region_cropped(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from tessella.verdict import vision

    shot = tmp_path / "shot.png"
    Image.new("RGB", (3000, 2000), "white").save(shot)
    paths = vision.prepare(str(shot), 1024, box=(100, 100, 400, 300), out_dir=tmp_path / "v")
    sizes = [Image.open(p).size for p in paths]
    assert sizes[0][0] == 1024 and sizes[0][1] in (682, 683)
    assert len(paths) == 2 and max(sizes[1]) <= 1024
