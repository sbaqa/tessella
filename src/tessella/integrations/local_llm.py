"""Talks to local model servers directly, through net.py, so local traffic obeys the
allowlist like everything else and needs no LiteLLM. Two dialects:

- Ollama's own /api/chat, which takes a JSON schema in `format` and keeps the model loaded
  for `keep_alive`;
- the OpenAI-style /v1/chat/completions that llama.cpp server, vLLM and LM Studio speak
  on your own machine, with the schema in `response_format`.

Called only by integrations.llm, after the policy check and the audit record.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from tessella import net
from tessella.config import ModelSettings
from tessella.integrations import thinking

if TYPE_CHECKING:
    from tessella.integrations.thinking import Call

OLLAMA_PROVIDERS = {"ollama", "ollama_chat"}


def headers(ms: ModelSettings) -> dict[str, str]:
    """The API key header for a shared model server (vLLM `--api-key`, or Ollama behind a
    proxy that checks one). The key is read from the environment, never from the config."""
    if not ms.api_key_env:
        return {}
    key = os.environ.get(ms.api_key_env)
    if not key:
        raise RuntimeError(f"model server key missing: set the {ms.api_key_env} "
                           "environment variable")
    return {"Authorization": f"Bearer {key}"}


def _base(ms: ModelSettings) -> str:
    return (ms.base_url or "http://127.0.0.1:11435").rstrip("/")


def _ollama_messages(messages: list[dict]) -> list[dict]:
    """OpenAI-style parts (text + data-URL images) to Ollama's {content, images: [base64]}."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        text = "\n".join(p["text"] for p in content if p.get("type") == "text")
        images = [p["image_url"]["url"].split(",", 1)[1] for p in content
                  if p.get("type") == "image_url"]
        out.append({"role": m["role"], "content": text, **({"images": images} if images else {})})
    return out


def ollama_chat(ms: ModelSettings, messages: list[dict], allow: list[str],
                schema: dict | None = None, temperature: float = 0,
                watch: Call | None = None) -> tuple[str, int]:
    """`watch` (--show-thinking) streams the reply so its thinking is shown as it arrives.
    --show-thinking also sends think: true, whatever the config says."""
    ms = thinking.effective(ms)
    options: dict = {"temperature": temperature}
    if ms.num_ctx:
        options["num_ctx"] = ms.num_ctx
    body: dict = {"model": ms.name, "messages": _ollama_messages(messages), "stream": False,
                  "options": options}
    if ms.think is not None:
        body["think"] = ms.think
    if ms.keep_alive:
        body["keep_alive"] = ms.keep_alive
    if schema:
        body["format"] = schema
    if watch is not None and watch.live:
        return _ollama_stream(ms, body, allow, watch)
    with net.client(allow, timeout=ms.timeout_s, headers=headers(ms)) as c:
        r = c.post(f"{_base(ms)}/api/chat", json=body)
        r.raise_for_status()
        data = r.json()
    tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
    if watch is not None:
        watch.think((data.get("message") or {}).get("thinking") or "")
    return (data.get("message") or {}).get("content") or "", tokens


def _ollama_stream(ms: ModelSettings, body: dict, allow: list[str],
                   watch: Call) -> tuple[str, int]:
    """Ollama's NDJSON stream: thinking chunks go to `watch`, content is collected, and the
    token counts come from the final `done` chunk."""
    body = {**body, "stream": True}
    content: list[str] = []
    last: dict = {}
    with (net.client(allow, timeout=ms.timeout_s, headers=headers(ms)) as c,
          c.stream("POST", f"{_base(ms)}/api/chat", json=body) as r):
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(f"ollama: {chunk['error']}")
            message = chunk.get("message") or {}
            watch.think(message.get("thinking") or "")
            if message.get("content"):
                watch.answer()
                content.append(message["content"])
            if chunk.get("done"):
                last = chunk
    watch.prompt_tokens = last.get("prompt_eval_count") or 0
    watch.eval_tokens = last.get("eval_count") or 0
    return "".join(content), watch.prompt_tokens + watch.eval_tokens


def openai_chat(ms: ModelSettings, messages: list[dict], allow: list[str],
                schema: dict | None = None, temperature: float = 0,
                watch: Call | None = None) -> tuple[str, int]:
    base = _base(ms)
    url = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
    body: dict = {"model": ms.name, "messages": messages, "temperature": temperature}
    if schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "reply", "schema": schema}}
    with net.client(allow, timeout=ms.timeout_s, headers=headers(ms)) as c:
        r = c.post(url, json=body)
        r.raise_for_status()
        data = r.json()
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return text, (data.get("usage") or {}).get("total_tokens") or 0


def chat(ms: ModelSettings, messages: list[dict], allow: list[str],
         schema: dict | None = None, temperature: float = 0,
         watch: Call | None = None) -> tuple[str, int]:
    send = ollama_chat if ms.provider in OLLAMA_PROVIDERS else openai_chat
    return send(ms, messages, allow, schema=schema, temperature=temperature, watch=watch)


def embed(ms: ModelSettings, texts: list[str], allow: list[str]) -> list[list[float]]:
    """One vector per text: Ollama /api/embed, or /v1/embeddings on other local servers."""
    base = _base(ms)
    with net.client(allow, timeout=ms.timeout_s, headers=headers(ms)) as c:
        if ms.provider in OLLAMA_PROVIDERS:
            r = c.post(f"{base}/api/embed", json={"model": ms.name, "input": texts,
                                                  **({"keep_alive": ms.keep_alive}
                                                     if ms.keep_alive else {})})
            r.raise_for_status()
            return r.json()["embeddings"]
        url = f"{base}/embeddings" if base.endswith("/v1") else f"{base}/v1/embeddings"
        r = c.post(url, json={"model": ms.name, "input": texts})
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def generate_stats(ms: ModelSettings, prompt: str, allow: list[str],
                   max_tokens: int = 128) -> dict:
    """One timed Ollama generation for `tessella models bench`: load, prompt and generation
    speed as Ollama reports them (durations are in nanoseconds)."""
    body = {"model": ms.name, "prompt": prompt, "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens,
                        **({"num_ctx": ms.num_ctx} if ms.num_ctx else {})}}
    if ms.think is not None:
        body["think"] = ms.think
    with net.client(allow, timeout=ms.timeout_s, headers=headers(ms)) as c:
        r = c.post(f"{_base(ms)}/api/generate", json=body)
        r.raise_for_status()
        d = r.json()
    return {"load_s": (d.get("load_duration") or 0) / 1e9,
            "prompt_tokens": d.get("prompt_eval_count") or 0,
            "prompt_tps": _per_second(d.get("prompt_eval_count"), d.get("prompt_eval_duration")),
            "gen_tokens": d.get("eval_count") or 0,
            "gen_tps": _per_second(d.get("eval_count"), d.get("eval_duration"))}


def _per_second(count: int | None, nanoseconds: int | None) -> float | None:
    return count / (nanoseconds / 1e9) if count and nanoseconds else None


def warm(ms: ModelSettings, allow: list[str]) -> None:
    """Load an Ollama model before the first real call, so cluster one does not pay the
    load time inside its timeout. Sends no data. Best effort: any failure is ignored and
    the real call reports it."""
    if ms.provider not in OLLAMA_PROVIDERS:
        return
    try:
        with net.client(allow, timeout=ms.timeout_s, headers=headers(ms)) as c:
            c.post(f"{_base(ms)}/api/generate",
                   json={"model": ms.name, "keep_alive": ms.keep_alive or "5m"})
    except Exception:  # noqa: BLE001, S110 - warm-up is an optimisation only
        pass
