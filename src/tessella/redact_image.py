"""Screenshot redaction: black out text that matches the redaction rules before a
screenshot is attached to a ticket, the moment it leaves this machine.

Words and their boxes come from OCR (tesseract, through pytesseract: `pip install
tessella[ocr]` plus the tesseract program). A word is masked when the text redaction
(redact.py) would change it, when it follows a label from the `redact` list ("password:
hunter2"), or when it is part of a line whose secret spans several words (a card number in
groups of four). Without OCR nothing can be found, so `screenshots.redact` decides: auto
attaches as before, required leaves the screenshot out.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tessella.config import Settings
from tessella.models import Draft
from tessella.redact import redact_text

Box = tuple[int, int, int, int]
Word = tuple[str, Box, tuple[int, int, int]]  # text, (x0, y0, x1, y1), (block, par, line)


def ocr_words(path: str) -> list[Word] | None:
    """Words with boxes, or None when OCR is not installed."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None
    try:
        data = pytesseract.image_to_data(Image.open(path), output_type=pytesseract.Output.DICT)
    except (OSError, RuntimeError, getattr(pytesseract, "TesseractNotFoundError", OSError)):
        return None
    out = []
    for i, text in enumerate(data["text"]):
        if text and text.strip():
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            out.append((text, (x, y, x + w, y + h),
                        (data["block_num"][i], data["par_num"][i], data["line_num"][i])))
    return out


def secret_boxes(words: list[Word], keys: list[str]) -> list[Box]:
    labels = {k.lower() for k in keys}
    lines: dict[tuple[int, int, int], list[Word]] = {}
    for w in words:
        lines.setdefault(w[2], []).append(w)
    boxes: list[Box] = []
    for line in lines.values():
        flagged = set()
        for i, (text, _box, _key) in enumerate(line):
            prev = line[i - 1][0].lower().strip(":=\"' ") if i else ""
            if redact_text(text, keys) != text or prev in labels:
                flagged.add(i)
        joined = " ".join(t for t, _, _ in line)
        if not flagged and redact_text(joined, keys) != joined:
            flagged = {i for i, (t, _, _) in enumerate(line) if any(ch.isdigit() for ch in t)}
            flagged = flagged or set(range(len(line)))
        boxes.extend(line[i][1] for i in sorted(flagged))
    return boxes


def redact_image(path: str, keys: list[str], out_dir: str | Path) -> tuple[str | None, int]:
    """(path of a redacted copy, boxes masked); (None, 0) when OCR is unavailable."""
    words = ocr_words(path)
    if words is None:
        return None, 0
    from PIL import Image, ImageDraw

    boxes = secret_boxes(words, keys)
    img = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle((x0 - 2, y0 - 2, x1 + 2, y1 + 2), fill="black")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16]}.png"
    img.save(target)
    return str(target), len(boxes)


def for_upload(draft: Draft, settings: Settings) -> Draft:
    """The draft as it may be filed: each screenshot replaced by its redacted copy, or
    left out when `screenshots.redact: required` and it could not be redacted."""
    policy = settings.screenshots.redact
    if policy == "off" or not draft.images:
        return draft
    images = []
    for p in draft.images:
        redacted, _n = redact_image(p, settings.redact, settings.screenshots.path) \
            if Path(p).is_file() else (None, 0)
        if redacted:
            images.append(redacted)
        elif policy == "auto":
            images.append(p)
    return draft.model_copy(update={"images": images})
