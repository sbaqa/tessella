"""`tessella models recommend` and `tessella models bench`: which local model fits this
machine, and how fast it actually runs here.

The recommendation is a size rule, not a promise: the largest Qwen 3.5 tag whose weights
plus working memory fit the memory the model would run in (GPU memory, Apple unified
memory, or RAM on a CPU-only machine, where speed caps it lower). Qwen 3.5 reads images, so
one model serves the reader and vision roles. `tessella models bench` and `tessella eval`
then tell you whether it is fast and accurate enough on your own failures.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Ollama library tags and their download sizes in GB (ollama.com/library/qwen3.5).
QWEN = [("qwen3.5:0.8b", 1.2), ("qwen3.5:2b", 2.7), ("qwen3.5:4b", 3.4), ("qwen3.5:9b", 6.6),
        ("qwen3.5:27b", 17.0), ("qwen3.5:35b", 22.0), ("qwen3.5:122b", 81.0)]
EMBEDDER = "nomic-embed-text"
OVERHEAD = 1.25  # weights to resident memory, before the context's KV cache
CPU_CAP = "qwen3.5:9b"  # bigger models answer too slowly without a GPU
SMALL_CPU_CAP = "qwen3.5:4b"  # under 8 cores


@dataclass
class Hardware:
    ram_gb: float | None
    cpus: int
    gpu: str | None = None  # "nvidia", "amd", "apple" or None
    vram_gb: float | None = None

    @property
    def budget_gb(self) -> float | None:
        """Memory the model may use: most of a GPU, 70% of Apple unified memory (the system
        keeps the rest), 60% of RAM on a CPU-only machine."""
        if self.gpu in ("nvidia", "amd") and self.vram_gb:
            return self.vram_gb * 0.9
        if self.ram_gb is None:
            return None
        return self.ram_gb * (0.7 if self.gpu == "apple" else 0.6)


def _run(cmd: list[str]) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _ram_gb() -> float | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 2**20
    out = _run(["sysctl", "-n", "hw.memsize"])  # macOS
    return int(out) / 2**30 if out and out.strip().isdigit() else None


def _nvidia_gb() -> float | None:
    out = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    sizes = [float(x) for x in (out or "").split() if x.replace(".", "", 1).isdigit()]
    return max(sizes) / 1024 if sizes else None  # MiB; the largest card holds the model


def _amd_gb() -> float | None:
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--json"])
    try:
        cards = json.loads(out or "{}")
    except ValueError:
        return None
    sizes = [int(v) for card in cards.values() if isinstance(card, dict)
             for k, v in card.items() if "total memory" in k.lower() and str(v).isdigit()]
    return max(sizes) / 2**30 if sizes else None


def detect() -> Hardware:
    hw = Hardware(ram_gb=_ram_gb(), cpus=os.cpu_count() or 1)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        hw.gpu = "apple"
    elif (vram := _nvidia_gb()) is not None:
        hw.gpu, hw.vram_gb = "nvidia", vram
    elif (vram := _amd_gb()) is not None:
        hw.gpu, hw.vram_gb = "amd", vram
    return hw


@dataclass
class Recommendation:
    reader: str | None
    num_ctx: int = 8192
    parallel: int = 1
    embedder: str = EMBEDDER
    notes: list[str] = field(default_factory=list)

    def yaml(self, base_url: str = "http://127.0.0.1:11435") -> str:
        common = f'provider: ollama_chat, base_url: "{base_url}"'
        return (f"models:\n"
                f"  reader: {{{common}, name: {self.reader}, num_ctx: {self.num_ctx}, "
                f"parallel: {self.parallel}}}\n"
                f"  vision: {{{common}, name: {self.reader}, num_ctx: {self.num_ctx}}}\n"
                f"  embedder: {{{common}, name: {self.embedder}}}\n")


def _size(tag: str) -> float:
    return dict(QWEN)[tag]


def recommend(hw: Hardware, pulled: list[str] | None = None) -> Recommendation:
    budget = hw.budget_gb
    if budget is None:
        return Recommendation("qwen3.5:4b", notes=[
            "Could not read this machine's memory, so this is the safe default."])
    cap = None
    if hw.gpu is None:
        cap = SMALL_CPU_CAP if hw.cpus < 8 else CPU_CAP
    tags = [t for t, _ in QWEN]
    allowed = tags[: tags.index(cap) + 1] if cap else tags
    fits = [t for t in allowed if _size(t) * OVERHEAD <= budget]
    if not fits:
        return Recommendation(None, notes=[(
            f"About {budget:.1f} GB is usable for a model here, less than the smallest "
            "Qwen 3.5 needs. Run Tessella with --no-model, or point it at a model server on "
            "another machine.")])
    pick = fits[-1]
    spare = budget - _size(pick) * OVERHEAD
    r = Recommendation(pick)
    r.num_ctx = 32768 if spare >= 6 else 16384 if spare >= 2.5 else 8192
    r.parallel = 2 if hw.gpu and spare >= 8 else 1
    where = {"nvidia": f"{hw.vram_gb:.0f} GB of NVIDIA GPU memory" if hw.vram_gb else "",
             "amd": f"{hw.vram_gb:.0f} GB of AMD GPU memory" if hw.vram_gb else "",
             "apple": f"{hw.ram_gb:.0f} GB of Apple unified memory" if hw.ram_gb else ""}
    r.notes.append(f"Sized for {where.get(hw.gpu or '') or f'{hw.ram_gb:.0f} GB of RAM, no GPU'}"
                   f" (about {budget:.1f} GB usable for the model).")
    if cap and pick == cap and len(allowed) < len(tags):
        r.notes.append(f"Without a GPU, models above {cap} answer too slowly for triage, "
                       "even when they fit.")
    if pick == "qwen3.5:0.8b":
        r.notes.append("0.8b is very small: expect more `unknown` verdicts. Check with "
                       "`tessella eval`.")
    if pulled is not None:
        have = {p if ":" in p else f"{p}:latest" for p in pulled}
        if pick in have:
            r.notes.append(f"{pick} is already pulled.")
        else:
            near = [t for t in reversed(fits[:-1]) if t in have]
            also = f", or start with {near[0]}, which you already have." if near else "."
            r.notes.append(f"Pull it with `ollama pull {pick}`{also}")
        if EMBEDDER not in {h.split(":")[0] for h in have}:
            r.notes.append(f"For similarity recall, also `ollama pull {EMBEDDER}` (about 0.3 GB).")
    return r


BENCH_PROMPT = """You are a senior SDET. Classify this failed test in one sentence.

[0] report: test_cancel_paid_discounted_order_refunds_in_full failed
    AssertionError: cancel returned 500: Internal Server Error
[1] log: POST /api/orders/1042/cancel 500
    Traceback (most recent call last):
      File "shop/store.py", line 118, in cancel
        refund = order["discounted_total"]
    KeyError: 'discounted_total'
[2] test_source: tests/api/test_orders_api.py
    res = api.post(f"/api/orders/{order['id']}/cancel", headers=anna)
    assert res.status_code == 200, f"cancel returned {res.status_code}: {res.text}"
"""


def bench(ms, allow: list[str], runs: int = 2) -> dict:
    """Load time, prompt and generation speed for one local Ollama model. The first call
    pays the load; later calls show the warm speed triage sees. The prompt is a fixed,
    made-up failure, so no data of yours is sent."""
    from tessella.integrations import local_llm

    first = local_llm.generate_stats(ms, BENCH_PROMPT, allow)
    warm = [local_llm.generate_stats(ms, BENCH_PROMPT, allow) for _ in range(max(0, runs - 1))]
    speeds = warm or [first]

    def avg(key: str) -> float | None:
        xs = [s[key] for s in speeds if s[key]]
        return sum(xs) / len(xs) if xs else None

    return {"model": ms.name, "load_s": first["load_s"], "prompt_tps": avg("prompt_tps"),
            "gen_tps": avg("gen_tps")}


def estimate_seconds(stats: dict, prompt_tokens: int = 3000, reply_tokens: int = 250) -> float | None:
    """Rough seconds for one judgement of a typical evidence pack, from measured speeds."""
    if not stats.get("prompt_tps") or not stats.get("gen_tps"):
        return None
    return prompt_tokens / stats["prompt_tps"] + reply_tokens / stats["gen_tps"]
