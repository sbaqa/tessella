"""Replies to deterministic model calls, so re-triaging evidence the model already judged
costs nothing. One small SQLite file on this machine; nothing in it leaves.

The key is the model (provider, name, server, context, thinking), the exact messages
(images included) and the reply schema. Only temperature-0 calls are cached: a sampled
reply (agreement voting) is meant to differ.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from tessella.config import ModelSettings

_lock = threading.Lock()


def key(ms: ModelSettings, messages: list[dict], schema: dict | None) -> str:
    ident = [ms.provider, ms.name, ms.base_url, ms.num_ctx, ms.think, messages, schema]
    return hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode()).hexdigest()


def _db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS replies (key TEXT PRIMARY KEY, text TEXT, tokens INT)")
    return db


def get(path: str, k: str) -> tuple[str, int] | None:
    with _lock, _db(path) as db:
        row = db.execute("SELECT text, tokens FROM replies WHERE key = ?", (k,)).fetchone()
    return (row[0], row[1]) if row else None


def put(path: str, k: str, text: str, tokens: int) -> None:
    with _lock, _db(path) as db:
        db.execute("INSERT OR REPLACE INTO replies VALUES (?, ?, ?)", (k, text, tokens))
