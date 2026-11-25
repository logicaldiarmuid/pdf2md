"""Find the images on a page, save them as JPGs and describe them in a placeholder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

RENDER_DPI = 150
JPG_QUALITY = 80
MAX_PIXELS = 2400  # longest side of a saved image
MIN_AREA = 0.02  # ignore images smaller than 2% of the page (logos, bullets, lines)
MIN_SIDE = 40  # ... or narrower than 40pt in either direction
PHOTO_COLORS = 5000  # distinct colours in a small thumbnail above which it's a photo


@dataclass
class PageImage:
    bbox: pymupdf.Rect
    box_index: int | None  # index into the page's layout boxes; None = found by us
    kind: str = ""
    filename: str = ""


def find_images(page, page_boxes: list[dict], page_has_text: bool) -> list[PageImage]:
    """Pictures the layout analysis found, plus significant images it missed.

    The layout analysis misses e.g. full-page scans whose text it OCR'd, and
    photos on pages it treated as text. Images stored as many touching strips
    (common for drawings) are merged into one picture.
    """
    found = [
        PageImage(pymupdf.Rect(b["bbox"]), i)
        for i, b in enumerate(page_boxes)
        if b["class"] == "picture" and _big_enough(page, pymupdf.Rect(b["bbox"]))
    ]
    for rect in _merged_image_rects(page):
        if not _big_enough(page, rect):
            continue
        # Already found if a picture box covers most of it.
        if not any(_area(rect & img.bbox) >= 0.5 * _area(rect) for img in found):
            found.append(PageImage(rect, None))
    for img in found:
        img.kind = _classify(page, img.bbox, page_has_text)
    found.sort(key=lambda img: (img.bbox.y0, img.bbox.x0))
    return found


def save_image(page, img: PageImage, path: Path) -> None:
    # Rendering the region (rather than extracting the raw image) merges strips,
    # includes vector drawings on top, and leaves out stamps already removed.
    # Cap the size: an A1 drawing at full resolution would be a huge file.
    dpi = int(min(RENDER_DPI, MAX_PIXELS * 72 / max(img.bbox.width, img.bbox.height)))
    page.get_pixmap(clip=img.bbox, dpi=dpi).save(path, jpg_quality=JPG_QUALITY)


def placeholder(img: PageImage, page_number: int, caption: str, image_text: str, saved: bool) -> str:
    name = img.filename if saved else "image not saved"
    lines = [f"[IMAGE: {name} — page {page_number}, {img.kind}"]
    if caption:
        lines.append(f'Caption: "{caption}"')
    if image_text:
        lines.append(f'Text in image: "{image_text}"')
    return "\n".join(lines) + "]"


def picture_text(segment: str, limit: int = 400) -> str:
    """Text the OCR read off a picture, flattened to one line."""
    m = re.search(r"<!-- Start of picture text -->(.*?)<!-- End of picture text -->", segment, re.S)
    text = m.group(1) if m else segment
    parts = [" ".join(p.split()) for p in re.split(r"<br>|\n", text)]
    text = " / ".join(p for p in parts if p)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plain(text: str) -> str:
    """Markdown caption -> plain one-line text."""
    return " ".join(text.replace("**", "").replace("_", " ").split())


def _classify(page, rect: pymupdf.Rect, page_has_text: bool) -> str:
    if _area(rect) > 0.9 * _area(page.rect):
        # A scanned text page is noisy enough to look like a photo by colour
        # count, so decide by whether text was read from it.
        if page_has_text:
            return "full-page scan (its text is included below)"
        colors = page.get_pixmap(clip=rect, dpi=30).color_count()
        return "full-page photo" if colors > PHOTO_COLORS else "full-page scan"
    colors = page.get_pixmap(clip=rect, dpi=30).color_count()
    return "photo" if colors > PHOTO_COLORS else "drawing, map or screenshot"


# A caption the layout analysis didn't label: a short paragraph right after the
# image starting with an exhibit/figure label such as "G5 —", "Figure 3:" or "Photo 2 -".
_CAPTION = re.compile(
    r"(\[IMAGE: [^\]\n]*)((?:\nText in image: [^\n]*)?)\]\n\n"  # placeholder without a caption
    r"((?:\*\*)?(?:(?:Figure|Fig\.|Photo|Image|Plate|Exhibit|Map|Drawing|Diagram|Chart)\s*\w+"
    r"|[A-Z]{1,3}\d+[a-z]?)\s*[—–:-][^\n]{0,400})\n",
)


def attach_captions(text: str) -> str:
    return _CAPTION.sub(
        lambda m: f'{m.group(1)}\nCaption: "{plain(m.group(3))}"{m.group(2)}]\n', text
    )


def _big_enough(page, rect: pymupdf.Rect) -> bool:
    return _area(rect) >= MIN_AREA * _area(page.rect) and min(rect.width, rect.height) >= MIN_SIDE


def _merged_image_rects(page) -> list[pymupdf.Rect]:
    rects = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            rects.append(r)
    merged = True
    while merged:  # keep merging rectangles that touch until nothing changes
        merged = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                if rects[i].intersects(rects[j] + (-3, -3, 3, 3)):
                    rects[i] = rects[i] | rects.pop(j)
                    merged = True
                    break
            if merged:
                break
    return rects


def _area(r: pymupdf.Rect) -> float:
    return 0.0 if r.is_empty else r.width * r.height
