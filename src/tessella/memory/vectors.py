"""Embedding similarity over past failures, for near-duplicate recall and few-shot examples.

Embeddings come from the `embedder` model role (a local model such as Ollama's
nomic-embed-text), so no text leaves the network. One vector per cluster signature, kept in
its own SQLite file next to the store: it is derived data, so deleting it only costs
re-embedding, and `tessella merge` never needs to carry it.

Search is an exact cosine scan in Python. A team's history is thousands of signatures,
which this handles in milliseconds; sqlite-vec can replace the scan if that ever changes.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from pathlib import Path

from tessella.cluster.signature import normalize, top_project_frame
from tessella.models import Case


def text_for(case: Case) -> str:
    """What is embedded: the test, the normalized error and where it was raised. Numbers,
    ids and quoted values are already placeholders, so two runs of one bug look alike."""
    parts = [case.title, normalize(case.message or case.stack), top_project_frame(case.stack)]
    return "\n".join(p for p in parts if p)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class VectorIndex:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS vectors (signature TEXT, model TEXT, "
                            "case_id TEXT, vector TEXT, PRIMARY KEY (signature, model))")

    def add(self, signature: str, case_id: str, vector: list[float], model: str) -> None:
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)",
                            (signature, model, case_id, json.dumps(vector)))

    def has(self, signature: str, model: str) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM vectors WHERE signature = ? AND model = ?",
                                   (signature, model)).fetchone() is not None

    def nearest(self, vector: list[float], model: str, k: int = 5,
                exclude: str | None = None) -> list[tuple[str, str, float]]:
        """(signature, case_id, cosine) for the k most similar signatures embedded by the same
        model (vectors from different models are not comparable), most similar first."""
        with self._lock:
            rows = self.db.execute("SELECT signature, case_id, vector FROM vectors "
                                   "WHERE model = ?", (model,)).fetchall()
        scored = [(sig, cid, cosine(vector, json.loads(v))) for sig, cid, v in rows
                  if sig != exclude]
        return sorted(scored, key=lambda r: r[2], reverse=True)[:k]


def reindex(settings, store, batch: int = 16) -> dict:
    """Embed every decided failure the index does not have yet, for `tessella reindex`:
    after turning on the embedder, after `tessella merge`, or after switching embedder
    models. Through the policy gateway like every model call."""
    from tessella.integrations import llm
    from tessella.integrations.llm import DataClass

    ms = settings.models.embedder
    if ms is None:
        raise ValueError("no embedder role is configured (models.embedder)")
    model = f"{ms.provider}/{ms.name}"
    index = VectorIndex(settings.memory.path)
    todo: dict[str, Case] = {}
    for case, _d in store.recent_decisions(limit=100_000):
        if case.signature not in todo and not index.has(case.signature, model):
            todo[case.signature] = case
    cases = list(todo.values())
    for i in range(0, len(cases), batch):
        chunk = cases[i:i + batch]
        vectors = llm.embed(settings, [text_for(c) for c in chunk], [DataClass.evidence_excerpts])
        for c, v in zip(chunk, vectors):
            index.add(c.signature, c.id, v, model)
    return {"embedded": len(cases), "model": model}
