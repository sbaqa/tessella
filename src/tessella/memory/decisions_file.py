"""Decisions as a small file committed in the repository, so CI and teammates know them.

One JSON line per failure: its ids, the decision, the reason and the ticket. Never logs,
messages, names or anything else from the failure. A laptop writes it when someone decides;
every triage reads it, and what a store already decided for a failure stands."""

from __future__ import annotations

import json
from pathlib import Path

from tessella.config import Settings
from tessella.memory.store import Store
from tessella.models import Case, Decision

ENABLED = True  # tests turn this off (conftest), so they never write into the repository


def _path(settings: Settings) -> Path | None:
    name = settings.storage.decisions_file
    return Path(settings.inputs.repo) / name if name and ENABLED else None


def _read(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a merge conflict marker or a damaged line costs only that line
        if isinstance(entry, dict) and {"case_id", "signature", "action", "at"} <= entry.keys():
            entries.append(entry)
    return entries


def _write(path: Path, entries: list[dict]) -> None:
    """Sorted, one failure per line, so two people's commits rarely conflict."""
    ordered = sorted(entries, key=lambda e: (e["signature"], e["case_id"]))
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in ordered), encoding="utf-8")


def load(settings: Settings, store: Store) -> int:
    """Bring the committed decisions into the store. Returns how many were new."""
    path = _path(settings)
    if path is None or not path.is_file():
        return 0
    return store.import_decisions(_read(path))


def save(settings: Settings, case: Case, decision: Decision) -> None:
    """Add or replace this failure's line. Filing or deciding never fails because of it."""
    path = _path(settings)
    if path is None or decision.action == "delete":
        return
    entry = {"case_id": case.id, "signature": case.signature, "action": str(decision.action),
             "at": decision.at.isoformat()}
    for name, value in (("reason", decision.reason), ("board", decision.board),
                        ("ticket_key", decision.ticket_key), ("ticket_url", decision.ticket_url)):
        if value:
            entry[name] = str(value)
    if decision.pending:
        entry["pending"] = True
    try:
        kept = [e for e in (_read(path) if path.is_file() else [])
                if (e["case_id"], e["signature"]) != (case.id, case.signature)]
        _write(path, [*kept, entry])
    except OSError:
        pass


def remove(settings: Settings, signature: str) -> None:
    """Drop a failure's lines when its decision is forgotten, so triage cannot restore it."""
    path = _path(settings)
    if path is None or not path.is_file():
        return
    try:
        _write(path, [e for e in _read(path) if e["signature"] != signature])
    except OSError:
        pass
