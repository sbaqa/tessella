"""Screenshots and other image attachments that reports carry next to a failure.

Readers call these helpers so every format ends with the same thing: a list of
local image paths on the Case (Case.screenshots).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Iterator

IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")

# Where screenshots that were embedded inside a report (base64) are written out.
EXTRACT_DIR = Path(".tessella/attachments")


def is_image(content_type: str | None = None, name: str | None = None) -> bool:
    if content_type and content_type.lower() in IMAGE_TYPES:
        return True
    return bool(name and name.lower().endswith(IMAGE_SUFFIXES))


def walk(node: object, keys: tuple[str, ...] = ("steps", "testStage", "beforeStages",
                                                 "afterStages", "befores", "afters")) -> Iterator[dict]:
    """Yield every attachment dict found under `attachments` keys, at any step depth."""
    if isinstance(node, dict):
        for att in node.get("attachments") or []:
            if isinstance(att, dict):
                yield att
        for key in keys:
            yield from walk(node.get(key), keys)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, keys)


def existing(paths: list[Path]) -> list[str]:
    """Keep only files that exist, as strings, without duplicates, in order."""
    seen: list[str] = []
    for p in paths:
        s = str(p)
        if p.is_file() and s not in seen:
            seen.append(s)
    return seen


def write_embedded(data: str, suffix: str = ".png", out_dir: Path | None = None) -> Path | None:
    """Decode a base64 image embedded in a report and store it once, named by its hash."""
    raw = data.split(",", 1)[1] if data.startswith("data:") else data
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    folder = out_dir or EXTRACT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{hashlib.sha1(blob).hexdigest()[:16]}{suffix}"
    if not path.exists():
        path.write_bytes(blob)
    return path
