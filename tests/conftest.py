import pytest

from tessella.integrations import reply_cache


@pytest.fixture(autouse=True)
def _private_reply_cache(tmp_path, monkeypatch):
    """Each test gets its own model reply cache, so a reply cached by one test can never
    answer another, and nothing is written under the working directory."""
    real_db = reply_cache._db
    monkeypatch.setattr(reply_cache, "_db", lambda path: real_db(str(tmp_path / "replies.sqlite")))


@pytest.fixture(autouse=True)
def _no_model_server(monkeypatch):
    """Tests never start a real Ollama server, even on a machine that has one installed."""
    from tessella.integrations import ollama_runtime

    def refuse(*_, **__):
        raise RuntimeError("tests do not start Ollama")

    monkeypatch.setattr(ollama_runtime, "ensure_running", refuse)


@pytest.fixture(autouse=True)
def _no_model_warm_up(monkeypatch):
    """Tests never warm a real model: triage and eval call local_llm.warm, which would post
    to an Ollama already running on this machine and wait for it to load the model."""
    from tessella.integrations import local_llm

    monkeypatch.setattr(local_llm, "warm", lambda *_, **__: None)
