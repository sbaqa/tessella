"""Pre-flight for local model servers, for `tessella doctor --models`.

For each configured role it checks, without sending any test data: the host is on the
network allowlist, the server answers, the model is pulled, a vision role's model can read
images, the configured num_ctx fits the model, and the model fits in free memory. Ollama is
checked through its own API; other local servers (vLLM, LM Studio, llama.cpp) through the
OpenAI-compatible /v1/models. Cloud roles are listed, never called.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from tessella import net
from tessella.config import ModelSettings, Settings

OLLAMA = {"ollama", "ollama_chat"}


@dataclass
class Check:
    role: str
    ok: bool | None  # None = not checked (cloud) or only a warning
    message: str


def _free_memory_bytes() -> int | None:
    """MemAvailable from /proc/meminfo (Linux); None elsewhere, and the check is skipped."""
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return None
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return None


def _tagged(name: str) -> str:
    """Ollama names without a tag mean ':latest'."""
    return name if ":" in name else f"{name}:latest"


def _same_model(wanted: str, listed: str) -> bool:
    return _tagged(wanted) == _tagged(listed)


def _ollama(role: str, ms: ModelSettings, c: httpx.Client, base: str) -> list[Check]:
    tags = c.get(f"{base}/api/tags").json().get("models") or []
    pulled = next((m for m in tags if _same_model(ms.name, m.get("name", ""))), None)
    if pulled is None:
        return [Check(role, False, f"{ms.name} is not pulled on {base}; run `ollama pull {ms.name}`"
                                   " (with OLLAMA_HOST set to that server)")]
    out = [Check(role, True, f"{ms.name} is pulled on {base}")]

    show = c.post(f"{base}/api/show", json={"model": ms.name}).json()
    caps = show.get("capabilities")
    if role == "vision" and caps is not None:
        if "vision" in caps:
            out.append(Check(role, True, f"{ms.name} reads images"))
        else:
            out.append(Check(role, False, f"{ms.name} cannot read images; pick a vision model"))
    context = next((v for k, v in (show.get("model_info") or {}).items()
                    if k.endswith(".context_length")), None)
    if context and ms.num_ctx and ms.num_ctx > context:
        out.append(Check(role, False, f"num_ctx {ms.num_ctx} is larger than the model's "
                                      f"context length {context}"))

    size, free = pulled.get("size"), _free_memory_bytes()
    if size and free is not None and size * 1.2 > free:
        out.append(Check(role, None, f"{ms.name} needs about {size * 1.2 / 2**30:.1f} GiB; "
                                     f"{free / 2**30:.1f} GiB is free now, so it may swap or fail"))
    return out


def _openai_compatible(role: str, ms: ModelSettings, c: httpx.Client, base: str) -> list[Check]:
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    ids = [m.get("id", "") for m in c.get(url).json().get("data") or []]
    if ms.name not in ids:
        return [Check(role, False, f"{ms.name} is not served at {url} (served: "
                                   f"{', '.join(ids) or 'nothing'})")]
    return [Check(role, True, f"{ms.name} is served at {url}")]


def check_role(role: str, ms: ModelSettings, settings: Settings) -> list[Check]:
    if ms.is_cloud:
        return [Check(role, None,
                      f"cloud model {ms.provider}/{ms.name}: not checked, no call made")]
    if not ms.base_url:
        return [Check(role, False, "local model has no base_url")]
    base = ms.base_url.rstrip("/")
    if not net.is_allowed(base, settings.network.allow):
        return [Check(role, False,
                      f"{base} is not in network.allow, so every call would be refused")]
    try:
        from tessella.integrations.local_llm import headers

        with net.client(settings.network.allow, timeout=5, headers=headers(ms)) as c:
            if ms.provider in OLLAMA:
                return _ollama(role, ms, c, base)
            return _openai_compatible(role, ms, c, base)
    except (httpx.HTTPError, net.EgressBlocked, ValueError) as exc:
        return [Check(role, False, f"no answer from {base} ({type(exc).__name__}); is the "
                                   "model server running?")]
    except RuntimeError as exc:  # the API key variable is not set
        return [Check(role, False, str(exc))]


def check(settings: Settings) -> list[Check]:
    out: list[Check] = []
    for role in ("reader", "vision", "fixer", "embedder"):
        ms = settings.models.get(role)
        if ms is not None:
            out += check_role(role, ms, settings)
    return out
