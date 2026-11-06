"""The only door to language models. Every call, local or cloud, goes through complete().

Before a call it checks, in order:
1. the role is configured (reader, vision, fixer);
2. for a cloud model, every data class in the payload is allowed by the data policy;
3. the model's host is on the network allowlist;
4. if the role says approve_each_call, a human approved this call.
Every call is written to the audit log (where it went, which data classes, size, hash).

Local models (Ollama, llama.cpp server, vLLM, LM Studio) are called directly through net.py
(integrations.local_llm). LiteLLM is only needed for cloud roles: `pip install tessella[llm]`.
complete_json() adds schema-enforced replies with one repair retry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from pydantic import BaseModel

from tessella import net
from tessella.config import ModelSettings, Settings
from tessella.integrations import local_llm, reply_cache, thinking


class DataClass(StrEnum):
    raw_logs = "raw_logs"
    stack_traces = "stack_traces"
    screenshots = "screenshots"
    test_data = "test_data"
    evidence_excerpts = "evidence_excerpts"  # redacted log/stack lines gathered as evidence
    redacted_summary = "redacted_summary"  # titles, categories, one-line evidence summaries
    failing_test_file = "failing_test_file"
    diff_hunk = "diff_hunk"
    source_code = "source_code"


class PolicyViolation(RuntimeError):
    pass


# Hosts used when a cloud provider has no explicit base_url.
PROVIDER_HOSTS = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "gemini": "generativelanguage.googleapis.com",
    "mistral": "api.mistral.ai",
}

Approver = Callable[[str], bool]


def model_host(ms: ModelSettings) -> str | None:
    if ms.base_url:
        parts = urlsplit(ms.base_url)
        return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    return PROVIDER_HOSTS.get(ms.provider)


def check(role: str, settings: Settings, data_classes: list[DataClass],
          approver: Approver | None = None) -> ModelSettings:
    """Raise PolicyViolation unless this call is allowed. Returns the role's settings."""
    ms = settings.models.get(role)
    if ms is None:
        raise PolicyViolation(f"model role '{role}' is not configured")

    if ms.is_cloud:
        blocked = [d for d in data_classes if not settings.data_policy.allows(d)]
        if blocked:
            raise PolicyViolation(
                f"'{role}' is a cloud model; the data policy keeps {', '.join(blocked)} inside the network"
            )

    host = model_host(ms)
    if host is None or not net.is_allowed(f"https://{host}", settings.network.allow):
        raise PolicyViolation(f"model host {host or '(unknown)'} is not in network.allow")

    if ms.approve_each_call:
        summary = f"{role} -> {ms.provider}/{ms.name} with {', '.join(data_classes)}"
        if approver is None or not approver(summary):
            raise PolicyViolation(f"call to '{role}' needs approval and was not approved")
    return ms


def audit(settings: Settings, role: str, ms: ModelSettings, data_classes: list[DataClass],
          payload: str, images: list[str], approved: bool) -> None:
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "location": "cloud" if ms.is_cloud else "local",
        "provider": ms.provider,
        "model": ms.name,
        "host": model_host(ms),
        "data_classes": [str(d) for d in data_classes],
        "chars": len(payload),
        "images": len(images),
        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "approved_by_human": approved,
    }
    if settings.data_policy.audit_payloads:
        record["payload"] = payload
    path = Path(settings.data_policy.audit_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _image_part(path: str) -> dict:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    data = base64.b64encode(Path(path).read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


OLLAMA_PROVIDERS = {"ollama", "ollama_chat"}
_THINKING = re.compile(r"<think>.*?</think>\s*", re.S)


def call_options(ms: ModelSettings, temperature: float = 0) -> dict:
    """Keyword arguments for litellm.completion (cloud roles), including Ollama-only options."""
    opts: dict = {"model": f"{ms.provider}/{ms.name}", "api_base": ms.base_url,
                  "temperature": temperature, "timeout": ms.timeout_s}
    if ms.provider in OLLAMA_PROVIDERS:
        if ms.think is not None:
            opts["think"] = ms.think
        if ms.num_ctx:
            opts["num_ctx"] = ms.num_ctx
    return opts


def strip_thinking(text: str) -> str:
    """Drop a reasoning block some local models put before their answer."""
    return _THINKING.sub("", text).strip()


_model_seconds = 0.0
_seconds_lock = threading.Lock()


def model_seconds() -> float:
    """Total seconds this process has spent waiting on model calls. Triage reads it before
    and after a run to record how much of the run was model time."""
    return _model_seconds


# `tessella eval` sets this to a function that keeps every model call it makes, exactly as
# sent and answered (evaluate.CallLog). None everywhere else, so triage records nothing.
recorder: ContextVar[Callable[[dict], None] | None] = ContextVar("llm_recorder", default=None)
_raw: ContextVar[tuple[str, str] | None] = ContextVar("llm_raw", default=None)


class _Capture:
    """Collects a call's thinking without printing it, for the eval recorder. It stands in
    for a thinking.Call when --show-thinking is off."""

    live = False

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.prompt_tokens: int | None = None
        self.eval_tokens: int | None = None

    def think(self, text: str) -> None:
        if text:
            self.parts.append(text)

    def answer(self) -> None:
        pass


def _call(ms: ModelSettings, messages: list[dict], schema: dict | None = None,
          temperature: float = 0, allow: list[str] | None = None) -> tuple[str, int]:
    """The actual model call. Kept separate so tests can replace it. Local servers are
    called directly through net.py; only cloud roles go through LiteLLM. With
    --show-thinking the model's thinking is shown as it arrives (integrations.thinking)."""
    watch = thinking.watch(ms)
    capture = _Capture() if watch is None and recorder.get() is not None else None
    try:
        text, tokens = _send(ms, messages, schema, temperature, allow, watch or capture)
    except Exception as exc:
        if watch is not None:
            watch.fail(exc)
        raise
    if watch is not None:
        watch.finish(text, tokens)
    if recorder.get() is not None:
        parts = watch._parts if watch is not None else capture.parts
        blocks = thinking._THINK_BLOCK.findall(text)
        _raw.set((text, "\n".join(p.strip() for p in [*parts, *blocks] if p.strip())))
    return strip_thinking(text), tokens


def _send(ms: ModelSettings, messages: list[dict], schema: dict | None, temperature: float,
          allow: list[str] | None, watch: thinking.Call | None) -> tuple[str, int]:
    if not ms.is_cloud:
        if watch is None:
            return local_llm.chat(ms, messages, allow or [], schema=schema,
                                  temperature=temperature)
        return local_llm.chat(ms, messages, allow or [], schema=schema,
                              temperature=temperature, watch=watch)
    import litellm  # optional dependency, cloud roles only

    resp = litellm.completion(messages=messages, **call_options(ms, temperature))
    usage = getattr(resp, "usage", None)
    return resp.choices[0].message.content or "", getattr(usage, "total_tokens", 0) or 0


def _image_placeholder(path: str) -> str:
    try:
        size = f"{Path(path).stat().st_size} bytes"
    except OSError:
        size = "size unknown"
    return f"[image {Path(path).name}, {mimetypes.guess_type(path)[0] or 'image/png'}, {size}]"


def _as_sent(messages: list[dict], images: list[str]) -> list[dict]:
    """`messages` with each image replaced by a placeholder naming its file and size."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, list):
            pics = iter(images)
            content = [p if p.get("type") == "text"
                       else {"type": "image", "placeholder": _image_placeholder(next(pics, "?"))}
                       for p in content]
        out.append({**m, "content": content})
    return out


def _record(rec: Callable[[dict], None], role: str, ms: ModelSettings, messages: list[dict],
            images: list[str], schema: dict | None, **result) -> None:
    rec({"role": role, "model": f"{ms.provider}/{ms.name}",
         "schema": (schema or {}).get("title"), "messages": _as_sent(messages, images),
         **result})


def complete(role: str, settings: Settings, system: str, user: str,
             data_classes: list[DataClass], images: list[str] | None = None,
             approver: Approver | None = None, schema: dict | None = None,
             temperature: float = 0, history: list[dict] | None = None) -> tuple[str, int]:
    """Policy-checked, audited model call. Returns (reply_text, tokens_used).

    `schema` asks the server to constrain the reply to that JSON schema (local servers);
    `history` appends earlier turns, e.g. a reply and the correction asked for it. A
    temperature-0 call whose exact prompt was answered before is served from the reply
    cache: no call is made, so nothing is audited and no tokens are spent."""
    images = images or []
    if images and DataClass.screenshots not in data_classes:
        data_classes = [*data_classes, DataClass.screenshots]
    ms = thinking.effective(check(role, settings, data_classes, approver), role)

    content: str | list[dict] = user
    if images:
        content = [{"type": "text", "text": user}, *(_image_part(p) for p in images)]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content},
                *(history or [])]

    cache_key = None
    if settings.cache.enabled and temperature == 0:
        cache_key = reply_cache.key(ms, messages, schema)
        hit = reply_cache.get(settings.cache.path, cache_key)
        if hit is not None:
            thinking.cached(ms)
            if (rec := recorder.get()) is not None:
                _record(rec, role, ms, messages, images, schema, reply=hit[0], thinking="",
                        tokens=0, seconds=0.0, cached=True)
            return hit[0], 0

    sent = "\n".join([system, user, *(str(m["content"]) for m in history or [])])
    audit(settings, role, ms, data_classes, sent, images, approved=ms.approve_each_call)
    global _model_seconds
    started = time.monotonic()
    role_token = thinking.role.set(role)
    rec = recorder.get()
    _raw.set(None)
    try:
        text, tokens = _call(ms, messages, schema=schema, temperature=temperature,
                             allow=settings.network.allow)
    except Exception as exc:
        if rec is not None:
            _record(rec, role, ms, messages, images, schema, reply="", thinking="", tokens=0,
                    seconds=round(time.monotonic() - started, 2), cached=False,
                    error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        thinking.role.reset(role_token)
        with _seconds_lock:
            _model_seconds += time.monotonic() - started
    if rec is not None:
        raw, thought = _raw.get() or (text, "")
        _record(rec, role, ms, messages, images, schema, reply=raw, thinking=thought,
                tokens=tokens, seconds=round(time.monotonic() - started, 2), cached=False)
    if cache_key is not None:
        reply_cache.put(settings.cache.path, cache_key, text, tokens)
    return text, tokens


def json_object(text: str) -> str:
    """The outermost {...} of a reply; models sometimes wrap JSON in prose or fences."""
    return text[text.find("{"): text.rfind("}") + 1]


def complete_json[T: BaseModel](role: str, settings: Settings, system: str, user: str,
                  data_classes: list[DataClass], model: type[T],
                  coerce: Callable[[dict], dict] | None = None, images: list[str] | None = None,
                  approver: Approver | None = None,
                  temperature: float = 0) -> tuple[T | None, str, int]:
    """Ask for a reply that validates as `model`, with the model's JSON schema enforced by
    the server where it can be. A reply that still fails validation gets one retry that
    shows the model its own reply and the error. `coerce` repairs near-misses first (a list
    where a string belongs, 80 for 0.8). Returns (parsed or None, last raw reply, tokens)."""
    schema = model.model_json_schema()
    history: list[dict] = []
    raw, total = "", 0
    for _ in range(2):
        raw, tokens = complete(role, settings, system, user, data_classes, images=images,
                               approver=approver, schema=schema, temperature=temperature,
                               history=history)
        total += tokens
        try:
            data = json.loads(json_object(raw))
            return model.model_validate(coerce(data) if coerce else data), raw, total
        except (ValueError, TypeError, AttributeError) as exc:  # ValidationError is a ValueError
            history = [{"role": "assistant", "content": raw},
                       {"role": "user", "content": f"That reply was not valid: {str(exc)[:400]}\n"
                        "Reply again with one JSON object only, matching the schema."}]
    return None, raw, total


def embed(settings: Settings, texts: list[str], data_classes: list[DataClass],
          approver: Approver | None = None) -> list[list[float]]:
    """Policy-checked, audited embedding call with the `embedder` role. The failure text
    it embeds is evidence, so the default data policy refuses a cloud embedder."""
    ms = check("embedder", settings, data_classes, approver)
    audit(settings, "embedder", ms, data_classes, "\n".join(texts), [],
          approved=ms.approve_each_call)
    global _model_seconds
    started = time.monotonic()
    try:
        return _embed(ms, texts, settings.network.allow)
    finally:
        with _seconds_lock:
            _model_seconds += time.monotonic() - started


def _embed(ms: ModelSettings, texts: list[str], allow: list[str]) -> list[list[float]]:
    """The actual embedding call. Kept separate so tests can replace it."""
    if not ms.is_cloud:
        return local_llm.embed(ms, texts, allow)
    import litellm  # optional dependency, cloud roles only

    resp = litellm.embedding(model=f"{ms.provider}/{ms.name}", input=texts, api_base=ms.base_url,
                             timeout=ms.timeout_s)
    return [d["embedding"] for d in resp.data]


def describe_roles(settings: Settings) -> list[tuple[str, str]]:
    """(role, 'provider/name (local|cloud)') for `tessella doctor`."""
    out = []
    for role in ("reader", "vision", "fixer", "embedder"):
        ms = settings.models.get(role)
        if ms is None:
            out.append((role, "not configured"))
        else:
            where = "cloud" if ms.is_cloud else "local"
            extra = ", asks before each call" if ms.approve_each_call else ""
            out.append((role, f"{ms.provider}/{ms.name} ({where}{extra})"))
    return out


# TODO(v1): route LiteLLM's own HTTP (cloud roles) through net.client too.
