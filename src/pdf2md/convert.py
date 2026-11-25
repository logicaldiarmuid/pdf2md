"""Core conversion: PDF -> Markdown / JSON page chunks."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import re
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pymupdf4llm

from . import images as img

# The OCR engine logs a warning whenever part of a page has no text ("The text
# detection result is empty"), which is normal and would clutter the progress bar.
# The engine resets its logger's level every time it starts, so use a filter
# (which survives that) to let only real errors through.
logging.getLogger("RapidOCR").addFilter(lambda record: record.levelno >= logging.ERROR)
# Likewise numpy warns ("Mean of empty slice") when the OCR-decision step measures a blank
# patch of a page; the library copes with the result, so the warning is just noise.
for _message in (r"Mean of empty slice", r"invalid value encountered"):
    warnings.filterwarnings("ignore", message=_message, category=RuntimeWarning)


@dataclass
class ConvertOptions:
    pages: list[int] | None = None  # 0-based page numbers; None = all
    page_markers: bool = True  # insert <!-- page N --> between pages
    strip_headers_footers: bool = True  # drop repeating page headers/footers
    images: str = "save"  # save: JPGs + placeholders | none: placeholders only
    image_dir: Path | None = None  # where JPGs go; default <pdf name>_images next to the PDF
    frontmatter: bool = True  # YAML metadata block at the top
    ocr: str = "auto"  # auto | off | force
    clean: bool = True  # tidy markdown for LLM consumption
    remove_stamps: bool = True  # drop diagonal overlay text such as "RECEIVED" stamps
    output_format: str = "md"  # md | json
    progress: bool = False  # show a per-page progress bar on stderr (long PDFs)


@dataclass
class ConvertResult:
    content: str
    page_count: int
    pages_converted: int
    log: str  # messages the underlying library printed
    empty_pages: list[int]  # 1-based pages with no text and no images
    image_count: int = 0
    image_dir: Path | None = None  # set when images were saved


def parse_page_spec(spec: str, page_count: int) -> list[int]:
    """Parse a 1-based spec like '1-3,7,10-' into sorted 0-based page indices."""
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s) if start_s.strip() else 1
            end = int(end_s) if end_s.strip() else page_count
        else:
            start = end = int(part)
        if start < 1 or end < start:
            raise ValueError(f"invalid page range: {part!r}")
        pages.update(range(start, min(end, page_count) + 1))
    if not pages:
        raise ValueError(f"page spec {spec!r} selects no pages (document has {page_count})")
    return sorted(p - 1 for p in pages)


_HEADING_BOLD = re.compile(r"^(#{1,6} )\*\*(.+?)\*\*[ \t]*$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\|.*\|\s*$")
_BOLD_CELL = re.compile(r"\*\*([^*|]+?)\*\*")


def clean_markdown(md: str) -> str:
    """Remove formatting noise that costs tokens but adds no meaning for an LLM."""
    md = _HEADING_BOLD.sub(r"\1\2", md)  # "# **Title**" -> "# Title"
    lines = [line.rstrip() for line in md.splitlines()]
    # A table header row is followed by a |---| separator; bold there is redundant.
    for i in range(len(lines) - 1):
        if _TABLE_ROW.match(lines[i]) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            lines[i] = _BOLD_CELL.sub(r"\1", lines[i])
    md = "\n".join(lines)
    md = re.sub(r"\n{3,}", "\n\n", md)  # collapse runs of blank lines
    return md.strip() + "\n"


_BT_ET = re.compile(rb"BT\b.*?\bET\b", re.DOTALL)
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>")


def _decode_pdf_strings(block: bytes) -> str:
    """Best-effort decode of the literal/hex strings in a BT..ET block."""
    out = []
    for m in _PDF_STRING.finditer(block):
        s = m.group()
        if s.startswith(b"<"):
            raw = bytes.fromhex(re.sub(rb"\s", b"", s[1:-1]).decode())
        else:
            raw = re.sub(rb"\\([()\\])", rb"\1", s[1:-1])
        out.append(raw.decode("latin-1"))
    return "".join(out)


def _text_lines(page, diagonal: bool) -> list[str]:
    """Text of each line on the page that is (or is not) drawn at a diagonal."""
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line["dir"]
            if (abs(dx) > 0.15 and abs(dy) > 0.15) == diagonal:
                lines.append(" ".join("".join(s["text"] for s in line["spans"]).split()))
    return lines


def remove_diagonal_stamps(doc) -> int:
    """Delete diagonal overlay text (e.g. a 'RECEIVED: 19/10/2026' stamp) in memory.

    Such stamps are drawn across the page and their letters get spliced into
    whatever text they cross. Only text-drawing blocks in the page's own content
    streams whose text exactly matches a diagonal line are removed, and the page
    is restored if any other text changed. Returns the number of pages cleaned.
    """
    cleaned = 0
    for page in doc:
        stamps = set(_text_lines(page, diagonal=True)) - {""}
        if not stamps:
            continue
        before = _text_lines(page, diagonal=False)
        originals = {xref: doc.xref_stream(xref) for xref in page.get_contents()}
        changed = False
        for xref, stream in originals.items():
            new = _BT_ET.sub(
                lambda m: b"" if " ".join(_decode_pdf_strings(m.group()).split()) in stamps else m.group(),
                stream,
            )
            if new != stream:
                doc.update_stream(xref, new)
                changed = True
        if not changed:
            continue
        if _text_lines(page, diagonal=False) == before and not _text_lines(page, diagonal=True):
            cleaned += 1
        else:  # something unexpected went away: put the page back
            for xref, stream in originals.items():
                doc.update_stream(xref, stream)
    return cleaned


def _frontmatter(meta: dict, source: Path, page_count: int, pages_converted: int) -> str:
    fields = {
        "source": source.name,
        "title": meta.get("title") or None,
        "author": meta.get("author") or None,
        "subject": meta.get("subject") or None,
        "pages": page_count,
    }
    if pages_converted != page_count:
        fields["pages_converted"] = pages_converted
    lines = ["---"]
    for key, value in fields.items():
        if value is not None:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _is_empty(text: str) -> bool:
    """Fewer than ~20 letters/digits means nothing useful was extracted:
    typically a scanned page that OCR could not read, or a pure image."""
    return len(re.findall(r"\w", text)) < 20


def _open(path: Path, opts: ConvertOptions):
    doc = pymupdf.open(path)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF is password-protected")
    if opts.remove_stamps:
        remove_diagonal_stamps(doc)  # in memory only; the PDF file is untouched
    return doc


HEAVY_PAGE_PATHS = 40_000  # vector paths on a page above which layout analysis is skipped (slow on drawings)


def _heavy_page_chunk(page) -> dict:
    lines = [" ".join(line.split()) for line in page.get_text("text").splitlines()]
    text = "\n".join(line for line in lines if line)
    return {"metadata": {"page_number": page.number + 1}, "text": text, "heavy": True}


def _replace_images(chunk: dict, page, image_dir: Path | None) -> tuple[str, int]:
    """Swap each image on the page for an [IMAGE: ...] placeholder (saving it as
    a JPG when image_dir is given). Returns the new text and the image count."""
    text, boxes = chunk["text"], chunk.get("page_boxes", [])
    if chunk.get("heavy"):
        image = img.PageImage(page.rect, None, kind="full-page drawing (too complex to "
                              "analyse; the text found on it follows)")
        image.filename = f"page{page.number + 1}-img1.jpg"
        if image_dir is not None:
            img.save_image(page, image, image_dir / image.filename)
        ph = img.placeholder(image, page.number + 1, "", "", image_dir is not None)
        return f"{ph}\n\n{text}", 1
    other_text = "".join(text[slice(*b["pos"])] for b in boxes if b["class"] != "picture")
    found = img.find_images(page, boxes, page_has_text=len(re.findall(r"\w", other_text)) > 300)
    edits = []  # (start, end, replacement) on the original text
    top = []  # placeholders for images the layout analysis missed
    for n, image in enumerate(found, start=1):
        image.filename = f"page{page.number + 1}-img{n}.jpg"
        if image_dir is not None:
            img.save_image(page, image, image_dir / image.filename)
        if image.box_index is None:
            top.append(img.placeholder(image, page.number + 1, "", "", image_dir is not None))
            continue
        start, end = boxes[image.box_index]["pos"]
        caption = ""
        following = boxes[image.box_index + 1] if image.box_index + 1 < len(boxes) else None
        if following and following["class"] == "caption":
            caption = img.plain(text[slice(*following["pos"])])
            edits.append((*following["pos"], ""))
        image_text = img.picture_text(text[start:end])
        ph = img.placeholder(image, page.number + 1, caption, image_text, image_dir is not None)
        edits.append((start, end, f"\n\n{ph}\n\n"))
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    if top:
        text = "\n\n".join(top) + "\n\n" + text
    if found:
        text = img.attach_captions(re.sub(r"\n{3,}", "\n\n", text))
    return text, len(found)


def _images_note(count: int, image_dir: Path | None) -> str:
    if image_dir is not None:
        where = f'are saved as JPG files in the folder "{image_dir.name}"'
        ask = "If you need to see an image, ask the user for it by its file name."
    else:
        where, ask = "were not saved", ""
    return (
        f"> Note for AI: this document's {count} image{'s have' if count != 1 else ' has'} "
        f"been replaced by [IMAGE: ...] placeholders giving the file name, page, "
        f"type, caption and any text in the image. The images {where}. {ask}".rstrip()
        + "\n\n"
    )


def convert_pdf(path: Path, opts: ConvertOptions) -> ConvertResult:
    doc = _open(path, opts)
    try:
        page_count = doc.page_count
        pages = opts.pages if opts.pages is not None else list(range(page_count))
        pages = [p for p in pages if p < page_count]
        # Drawings made of hundreds of thousands of line segments make the layout
        # analysis run for hours; such pages get a fast text-plus-picture treatment.
        heavy = {p for p in pages if len(doc[p].get_cdrawings()) > HEAVY_PAGE_PATHS}
        normal = [p for p in pages if p not in heavy]

        kwargs = dict(
            pages=normal,
            page_chunks=True,
            header=not opts.strip_headers_footers,
            footer=not opts.strip_headers_footers,
            use_ocr=opts.ocr != "off",
            force_ocr=opts.ocr == "force",
            show_progress=opts.progress,
        )

        # The library prints diagnostics to stdout/stderr; capture them so they never
        # end up mixed into markdown written to stdout.
        captured = io.StringIO()
        pymupdf.set_messages(stream=captured)
        try:
            # The progress bar is drawn on stderr, so leave stderr alone when it's wanted.
            stderr = contextlib.nullcontext() if opts.progress else contextlib.redirect_stderr(captured)
            with contextlib.redirect_stdout(captured), stderr:
                chunks = pymupdf4llm.to_markdown(doc, **kwargs) if normal else []
                # Pages that came out empty but contain images (photos with a caption
                # baked in, scanned pages the OCR model skipped) get a second, forced
                # OCR pass at higher resolution. It needs a freshly opened copy: the
                # first pass leaves its failed OCR attempt in the in-memory document.
                retry = [
                    c["metadata"]["page_number"] - 1
                    for c in chunks
                    if _is_empty(c["text"]) and doc[c["metadata"]["page_number"] - 1].get_images()
                ]
                if retry and opts.ocr == "auto":
                    with _open(path, opts) as fresh:
                        redo = pymupdf4llm.to_markdown(
                            # Keep header/footer text here: on an otherwise empty page, a
                            # caption at the bottom edge is easily mistaken for a footer.
                            fresh, **{**kwargs, "pages": retry, "force_ocr": True,
                                      "ocr_dpi": 300, "header": True, "footer": True}
                        )
                    better = {c["metadata"]["page_number"]: c for c in redo if not _is_empty(c["text"])}
                    chunks = [better.get(c["metadata"]["page_number"], c) for c in chunks]
        finally:
            pymupdf.set_messages(stream=sys.stderr)

        chunks += [_heavy_page_chunk(doc[p]) for p in sorted(heavy)]
        chunks.sort(key=lambda c: c["metadata"]["page_number"])

        image_dir = None
        if opts.images == "save":
            image_dir = opts.image_dir or path.with_name(path.stem + "_images")
            if image_dir.is_dir():  # clear images left over from an earlier run
                for old in image_dir.glob("page*-img*.jpg"):
                    old.unlink()
            image_dir.mkdir(parents=True, exist_ok=True)

        page_texts, image_count, empty_pages = [], 0, []
        for chunk in chunks:
            number = chunk["metadata"]["page_number"]
            text, n = _replace_images(chunk, doc[number - 1], image_dir)
            image_count += n
            if _is_empty(text):  # no text and no images (placeholders count as text)
                empty_pages.append(number)
            page_texts.append((number, clean_markdown(text) if opts.clean else text))

        if image_dir is not None and image_count == 0:
            with contextlib.suppress(OSError):
                image_dir.rmdir()  # don't leave an empty folder behind
            image_dir = None

        if opts.output_format == "json":
            content = json.dumps(
                {
                    "source": path.name,
                    "metadata": {k: v for k, v in doc.metadata.items() if v},
                    "page_count": page_count,
                    "image_folder": image_dir.name if image_dir else None,
                    "pages": [{"page": n, "markdown": t} for n, t in page_texts],
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n"
        else:
            parts = []
            if opts.frontmatter:
                parts.append(_frontmatter(doc.metadata, path, page_count, len(pages)))
            if image_count:
                parts.append(_images_note(image_count, image_dir))
            for number, text in page_texts:
                if opts.page_markers:
                    parts.append(f"<!-- page {number} -->\n\n")
                parts.append(text.rstrip() + "\n\n")
            content = "".join(parts).rstrip() + "\n"

        return ConvertResult(
            content, page_count, len(pages), captured.getvalue(), empty_pages, image_count, image_dir
        )
    finally:
        doc.close()
