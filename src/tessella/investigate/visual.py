"""Visual evidence: compare a failure screenshot with the last green screenshot of the same test.

Deterministic, no model needed: it says how much of the screen changed and where,
and writes a copy of the failure screenshot with the changed region outlined.
Needs Pillow: `pip install tessella[vision]`.

Baselines live in .tessella/baselines/, one PNG per test. Save one with
`tessella baseline --test "<test title>" path/to/green.png`.
TODO(v1): collect baselines automatically from green runs that keep screenshots
(Playwright `screenshot: "on"`).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from tessella.models import Case, Evidence, EvidenceKind

BASELINE_DIR = Path(".tessella/baselines")
DIFF_DIR = Path(".tessella/diffs")


def _key(test_title: str) -> str:
    return hashlib.sha1(test_title.encode()).hexdigest()[:16]


def baseline_for(test_title: str, folder: Path = BASELINE_DIR) -> Path | None:
    path = folder / f"{_key(test_title)}.png"
    return path if path.is_file() else None


def save_baseline(test_title: str, image: str | Path, folder: Path = BASELINE_DIR) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_key(test_title)}.png"
    shutil.copyfile(image, target)
    return target


def diff(failed: str | Path, baseline: str | Path, out_dir: Path = DIFF_DIR,
         threshold: int = 32) -> Evidence:
    """Pixel difference between two screenshots, as Evidence with an outlined image."""
    from PIL import Image, ImageChops, ImageDraw

    a = Image.open(failed).convert("RGB")
    b = Image.open(baseline).convert("RGB")
    size_note = ""
    if a.size != b.size:
        size_note = f" (screen size changed from {b.size[0]}x{b.size[1]} to {a.size[0]}x{a.size[1]})"
        b = b.resize(a.size)

    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > threshold else 0)
    changed = mask.histogram()[255]
    share = changed / (a.size[0] * a.size[1])
    box = mask.getbbox()

    if not box:
        return Evidence(kind=EvidenceKind.visual_diff,
                        summary="Screenshot matches the last green run: the failure is not visual"
                                + size_note,
                        ref=str(baseline))

    out_dir.mkdir(parents=True, exist_ok=True)
    marked = a.copy()
    ImageDraw.Draw(marked).rectangle(box, outline=(220, 38, 38), width=4)
    out = out_dir / f"{Path(failed).stem}-diff.png"
    marked.save(out)
    x0, y0, x1, y1 = box
    return Evidence(
        kind=EvidenceKind.visual_diff,
        summary=f"{share:.1%} of the screen changed versus the last green run, "
                f"in the region x {x0}-{x1}, y {y0}-{y1}{size_note}",
        excerpt=f"changed_pixels={changed} box={box}",
        ref=str(baseline),
        image=str(out),
    )


def visual_evidence(case: Case, folder: Path = BASELINE_DIR) -> Evidence | None:
    """Diff the case's last screenshot against its baseline, if both exist."""
    if not case.screenshots:
        return None
    baseline = baseline_for(case.title, folder)
    if baseline is None:
        return None
    return diff(case.screenshots[-1], baseline)
