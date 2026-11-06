"""Vision: a model describes what is on a failure screenshot (a modal over a button,
an error banner, an empty list). The description becomes cited Evidence like any other.

Uses the `vision` model role. Screenshots are the `screenshots` data class, which the
default data policy never lets leave the network, so this normally runs on a local
vision-capable model.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tessella.config import Settings
from tessella.models import Case, Evidence, EvidenceKind

PROMPT = """You see the screenshot taken when an automated UI test failed.
Test: {title}
Failure message: {message}
Describe in at most three short sentences what on the screen explains the failure
(blocking dialogs, error messages, missing or disabled elements, wrong page, empty data).
Do not guess beyond what is visible."""

CROP_NOTE = """The second image is a close-up of the region that changed since the last green
run; look there first."""

PREPARED_DIR = Path(".tessella/vision")
_BOX = re.compile(r"box=\((\d+), (\d+), (\d+), (\d+)\)")


def _changed_box(case: Case) -> tuple[int, int, int, int] | None:
    """The changed region the visual diff found, if any (investigate/visual.py writes it)."""
    for ev in case.evidence:
        m = ev.kind == EvidenceKind.visual_diff and _BOX.search(ev.excerpt)
        if m:
            return tuple(int(n) for n in m.groups())  # type: ignore[return-value]
    return None


def prepare(image: str, max_px: int, box: tuple[int, int, int, int] | None = None,
            out_dir: Path = PREPARED_DIR) -> list[str]:
    """The screenshot scaled so its long side is at most max_px, plus a close-up of `box`.
    Small vision models read a 1024 px image as well as a 4K one, many times faster.
    Without Pillow the original is sent as it is."""
    try:
        from PIL import Image
    except ImportError:
        return [image]
    src = Image.open(image).convert("RGB")
    tag = hashlib.sha1(f"{image}|{max_px}|{box}".encode()).hexdigest()[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    scaled = src.copy()
    scaled.thumbnail((max_px, max_px))
    paths = [out_dir / f"{tag}.png"]
    scaled.save(paths[0])
    if box:
        x0, y0, x1, y1 = box
        pad = 24
        crop = src.crop((max(0, x0 - pad), max(0, y0 - pad),
                         min(src.width, x1 + pad), min(src.height, y1 + pad)))
        if crop.width * crop.height < 0.6 * src.width * src.height:  # only if it zooms in
            crop.thumbnail((max_px, max_px))
            paths.append(out_dir / f"{tag}-crop.png")
            crop.save(paths[1])
    return [str(p) for p in paths]


def describe(case: Case, settings: Settings, approver=None) -> Evidence | None:
    vision = settings.models.vision
    if not case.screenshots or vision is None:
        return None
    from tessella.integrations import llm
    from tessella.integrations.llm import DataClass

    image = case.screenshots[-1]
    images = prepare(image, vision.max_image_px, _changed_box(case))
    prompt = PROMPT.format(title=case.title, message=case.message[:300])
    text, _ = llm.complete(
        "vision", settings,
        system="You are a careful QA engineer reading test failure screenshots.",
        user=prompt + ("\n" + CROP_NOTE if len(images) > 1 else ""),
        data_classes=[DataClass.screenshots],
        images=images,
        approver=approver,
    )
    text = text.strip()
    if not text:
        return None
    first_sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return Evidence(kind=EvidenceKind.screenshot, summary=first_sentence[:300],
                    excerpt=text, ref=image, image=image)


REGIONS = {"top": (0, 0, 1, 0.35), "bottom": (0, 0.65, 1, 1), "left": (0, 0, 0.5, 1),
           "right": (0.5, 0, 1, 1), "center": (0.2, 0.2, 0.8, 0.8)}


def region_box(spec: str, width: int, height: int) -> tuple[int, int, int, int] | None:
    """A named region (top, bottom, left, right, center) or "x,y,w,h" as fractions 0-1."""
    spec = spec.strip().lower()
    if spec in REGIONS:
        x0, y0, x1, y1 = REGIONS[spec]
    else:
        try:
            x, y, w, h = (float(v) for v in spec.split(","))
        except ValueError:
            return None
        x0, y0, x1, y1 = x, y, x + w, y + h
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(1.0, x1), min(1.0, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    return int(x0 * width), int(y0 * height), int(x1 * width), int(y1 * height)


def describe_region(case: Case, settings: Settings, spec: str, approver=None,
                    out_dir: Path = PREPARED_DIR) -> Evidence | None:
    """A close-up of one region of the failure screenshot, read by the vision role: the
    investigator's way to look again at something small (a toast, a disabled button)."""
    vision = settings.models.vision
    if not case.screenshots or vision is None:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    from tessella.integrations import llm
    from tessella.integrations.llm import DataClass

    image = case.screenshots[-1]
    src = Image.open(image).convert("RGB")
    box = region_box(spec, src.width, src.height)
    if box is None:
        return None
    crop = src.crop(box)
    crop.thumbnail((vision.max_image_px, vision.max_image_px))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{hashlib.sha1(f'{image}|{box}'.encode()).hexdigest()[:12]}-region.png"
    crop.save(path)
    text, _ = llm.complete(
        "vision", settings,
        system="You are a careful QA engineer reading test failure screenshots.",
        user=f"This is the {spec} region of the screenshot taken when this test failed.\n"
             f"Test: {case.title}\nFailure message: {case.message[:300]}\n"
             "Describe in at most three short sentences what is visible here: text, "
             "messages, buttons and their state. Do not guess beyond what is visible.",
        data_classes=[DataClass.screenshots], images=[str(path)], approver=approver)
    text = text.strip()
    if not text:
        return None
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return Evidence(kind=EvidenceKind.screenshot, summary=f"Close-up ({spec}): {first[:280]}",
                    excerpt=text, ref=f"{image} {box}", image=str(path))
