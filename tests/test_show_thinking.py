"""--show-thinking: Ollama's thinking streamed live to stderr, <think> blocks in content,
the thinking.jsonl line, the cached-reply note, the one-time hint, and think: true forced
for the run whatever the config says."""

import io
import json

import httpx
import pytest

from tessella import config
from tessella.config import ModelSettings, Settings
from tessella.integrations import llm, thinking

respx = pytest.importorskip("respx")

OLLAMA = "http://127.0.0.1:11435"
ALLOW = ["localhost:11435"]


@pytest.fixture
def shown(tmp_path, monkeypatch):
    """Turn --show-thinking on for one test and capture what it prints."""
    from rich.console import Console

    monkeypatch.setattr(config, "loaded_path", None)
    out = Console(record=True, width=200, highlight=False, file=io.StringIO())
    monkeypatch.setattr(thinking, "console", out)
    monkeypatch.setattr(thinking, "log_path", str(tmp_path / "thinking.jsonl"))
    thinking.enable()
    yield out
    thinking.enable(False)


def _ms(**kw):
    return ModelSettings(provider="ollama_chat", name="qwen3.5:4b", base_url=OLLAMA, **kw)


def _ndjson(*chunks):
    return httpx.Response(200, content="\n".join(json.dumps(c) for c in chunks).encode())


STREAM = [
    {"message": {"role": "assistant", "content": "", "thinking": "The 500 comes "}},
    {"message": {"role": "assistant", "content": "", "thinking": "from refund().\nSo a "}},
    {"message": {"role": "assistant", "content": "", "thinking": "product bug."}},
    {"message": {"role": "assistant", "content": '{"category": '}},
    {"message": {"role": "assistant", "content": '"product_bug"}'}},
    {"message": {"role": "assistant", "content": ""}, "done": True,
     "prompt_eval_count": 120, "eval_count": 30},
]


def test_flag_off_sends_no_stream_and_prints_nothing(tmp_path, capsys):
    assert not thinking.enabled
    with respx.mock:
        route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"content": "ok", "thinking": "hmm"},
                       "prompt_eval_count": 2, "eval_count": 1}))
        assert llm._call(_ms(), [], allow=ALLOW) == ("ok", 3)
    assert json.loads(route.calls[0].request.content)["stream"] is False
    assert capsys.readouterr() == ("", "")
    assert not (tmp_path / "thinking.jsonl").exists()


def test_flag_on_streams_thinking_in_order_and_assembles_the_answer(shown, tmp_path):
    with respx.mock:
        route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=_ndjson(*STREAM))
        text, tokens = llm._call(_ms(), [{"role": "user", "content": "why?"}], allow=ALLOW)
    assert json.loads(route.calls[0].request.content)["stream"] is True
    assert (text, tokens) == ('{"category": "product_bug"}', 150)
    lines = [x for x in shown.export_text().splitlines()
             if not x.startswith("--show-thinking:")]
    assert lines[0].startswith("── qwen3.5:4b thinking [call ")
    assert lines[1:4] == ["  The 500 comes from refund().", "  So a product bug.",
                          lines[3]] and lines[3].startswith("── answering [call ")
    assert "returned no thinking" not in shown.export_text()
    record = json.loads((tmp_path / "thinking.jsonl").read_text())
    assert record["thinking"] == "The 500 comes from refund().\nSo a product bug."
    assert record["answer"] == '{"category": "product_bug"}'
    assert (record["prompt_tokens"], record["eval_tokens"]) == (120, 30)
    assert record["model"] == "qwen3.5:4b" and record["host"] == "127.0.0.1"
    assert record["call"].startswith("call ") and record["seconds"] >= 0


def test_think_block_in_content_is_shown_and_stripped(shown, tmp_path):
    reply = [{"message": {"content": "<think>Check the logs first.</think>\n{}"}},
             {"message": {"content": ""}, "done": True, "prompt_eval_count": 1,
              "eval_count": 1}]
    with respx.mock:
        respx.post(f"{OLLAMA}/api/chat").mock(return_value=_ndjson(*reply))
        assert llm._call(_ms(), [], allow=ALLOW) == ("{}", 2)
    assert "  Check the logs first." in shown.export_text()
    record = json.loads((tmp_path / "thinking.jsonl").read_text())
    assert (record["thinking"], record["answer"]) == ("Check the logs first.", "{}")


def test_empty_thinking_hints_once_per_run(shown):
    reply = [{"message": {"content": "{}"}},
             {"message": {"content": ""}, "done": True}]
    with respx.mock:
        respx.post(f"{OLLAMA}/api/chat").mock(side_effect=lambda req: _ndjson(*reply))
        llm._call(_ms(), [], allow=ALLOW)
        llm._call(_ms(), [], allow=ALLOW)
    hint = ("qwen3.5:4b returned no thinking even with think on; this model or server may "
            "not support it (config (none loaded))")
    assert shown.export_text().count(hint) == 1
    assert "set think" not in shown.export_text()


def _reader_settings(tmp_path, think):
    """A loaded tessella.yaml with the given think value, as `config.load` sees it."""
    cfg = tmp_path / "tessella.yaml"
    cfg.write_text(f"models:\n  reader:\n    provider: ollama_chat\n    name: qwen3.5:4b\n"
                   f"    base_url: {OLLAMA}\n    think: {think}\n"
                   f"network:\n  allow: [{ALLOW[0]}]\n"
                   f"cache:\n  path: {tmp_path / 'cache.sqlite'}\n"
                   f"data_policy:\n  audit_log: {tmp_path / 'calls.jsonl'}\n")
    return config.load(cfg), cfg.resolve()


def test_flag_on_forces_think_true_over_config_false_and_says_so_once(shown, tmp_path):
    s, cfg = _reader_settings(tmp_path, "false")
    assert s.models.reader.think is False
    with respx.mock:
        route = respx.post(f"{OLLAMA}/api/chat").mock(side_effect=lambda req: _ndjson(*STREAM))
        llm.complete("reader", s, "s", "u1", [])
        llm.complete("reader", s, "s", "u2", [])
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert [b["think"] for b in bodies] == [True, True]
    assert all(b["stream"] is True for b in bodies)
    notice = (f"--show-thinking: thinking turned on for this run (config {cfg} has think: "
              "false for reader)")
    assert shown.export_text().count(notice) == 1
    assert s.models.reader.think is False  # the loaded settings are not changed


def test_forced_think_has_its_own_cache_key(shown, tmp_path):
    s, _ = _reader_settings(tmp_path, "false")
    from tessella.integrations import reply_cache

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    off = reply_cache.key(s.models.reader, messages, None)
    on = reply_cache.key(thinking.effective(s.models.reader), messages, None)
    assert off != on
    reply_cache.put(s.cache.path, off, "answer with think off", 1)
    with respx.mock:
        route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=_ndjson(*STREAM))
        text, _ = llm.complete("reader", s, "s", "u", [])
    assert route.called and text == '{"category": "product_bug"}'


def test_no_thinking_hint_names_role_and_config(shown, tmp_path):
    s, cfg = _reader_settings(tmp_path, "false")
    reply = [{"message": {"content": "{}"}}, {"message": {"content": ""}, "done": True}]
    with respx.mock:
        respx.post(f"{OLLAMA}/api/chat").mock(return_value=_ndjson(*reply))
        llm.complete("reader", s, "s", "u", [])
    assert (f"qwen3.5:4b (reader) returned no thinking even with think on; this model or "
            f"server may not support it (config {cfg})") in shown.export_text()


def test_flag_off_keeps_config_think_and_does_not_stream(tmp_path, capsys, monkeypatch):
    assert not thinking.enabled
    monkeypatch.setattr(config, "loaded_path", None)
    s, _ = _reader_settings(tmp_path, "false")
    with respx.mock:
        route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}))
        llm.complete("reader", s, "s", "u", [])
    body = json.loads(route.calls[0].request.content)
    assert body["think"] is False and body["stream"] is False
    assert thinking.effective(s.models.reader) is s.models.reader
    assert capsys.readouterr().err == ""


def test_cached_reply_says_there_is_no_thinking(shown, tmp_path, monkeypatch):
    s = Settings.model_validate({"models": {"reader": {"provider": "ollama_chat",
                                                       "name": "qwen3.5:4b",
                                                       "base_url": OLLAMA}},
                                 "network": {"allow": ALLOW}})
    s.data_policy.audit_log = str(tmp_path / "calls.jsonl")
    monkeypatch.setattr(llm, "_call", lambda *a, **k: ("hi", 1))
    llm.complete("reader", s, "s", "u", [])
    llm.complete("reader", s, "s", "u", [])
    assert "(cached reply: no thinking)" in shown.export_text()


def test_parallel_calls_prefix_lines_with_their_tag(shown):
    a, b = thinking.watch(_ms()), thinking.watch(_ms())
    a.think("first line\n")
    b.think("other line\n")
    a.finish("{}", 0)
    b.finish("{}", 0)
    text = shown.export_text()
    assert f"[{a.tag}] first line" in text and f"[{b.tag}] other line" in text
