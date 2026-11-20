"""Bring a CI run's results to this machine: download them, merge them into the local store and
put the screenshots where the queue can show them. Everything after this works offline.

`load` takes a link to the run, or a file or folder someone downloaded (any other CI)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from tessella.config import Settings
from tessella.integrations import ci_artifacts
from tessella.memory import decisions_file
from tessella.memory.store import Store


def describe(run: str) -> str:
    """Where this link would download from. Raises ValueError for a link it cannot read."""
    return ci_artifacts.host_of(run)


def _adopt_screenshots(folder: Path, store: Store, settings: Settings) -> None:
    """Copy the run's screenshots next to the store and point its cases at the copies."""
    source = folder / "screenshots"
    if not source.is_dir():
        return
    target = Path(settings.storage.path).parent / "screenshots"
    for case_dir in (d for d in source.iterdir() if d.is_dir()):
        case = store.get_case(case_dir.name)
        if case is None:
            continue
        (target / case.id).mkdir(parents=True, exist_ok=True)
        for image in case_dir.iterdir():
            shutil.copy2(image, target / case.id / image.name)

        def here(path: str | None, home: Path = target / case.id) -> str | None:
            copy = home / Path(path).name if path else None
            return str(copy) if copy and copy.is_file() else path

        case.screenshots = [here(p) for p in case.screenshots]
        for ev in case.evidence:
            ev.image = here(ev.image)
        if case.draft:
            case.draft.images = [here(p) for p in case.draft.images]
        store.save_case(case)


def load(settings: Settings, store: Store, source: str) -> dict[str, int]:
    """Download or open `source` and add it to the store. Returns rows added per table."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        ci_artifacts.fetch(source, folder)
        added = store.merge_from(folder / ci_artifacts.DATABASE)
        decisions_file.load(settings, store)  # teammates' decisions, from the repository
        _adopt_screenshots(folder, store, settings)
    return added
