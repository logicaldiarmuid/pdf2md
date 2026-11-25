"""MCP server so the Claude desktop app's chat can use pdf2md.

Run with:  pdf2md-mcp [--library DIR]     (default library: C:\\PDF\\converted)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pymupdf
from mcp.server.fastmcp import FastMCP, Image

from .convert import ConvertOptions, convert_pdf
from .library import Library, file_hash
from . import projects

LIBRARY = Path(r"C:\PDF\converted")

mcp = FastMCP(
    "pdf2md",
    instructions=(
        "Use these tools to read PDF files on the user's computer cheaply. When the user "
        "gives a PDF's location (path), call read_pdf instead of asking them to upload it. "
        "Images in the document appear as [IMAGE: ...] placeholders; only call "
        "get_pdf_image when you genuinely need to see one."
    ),
)


def _convert(pdf: Path, force: bool) -> tuple[Path, str]:
    """Convert into the library unless already there. Returns (md path, status)."""
    lib = Library(LIBRARY)
    digest = file_hash(pdf)
    known = lib.find(digest)
    if known and not force:
        lib.add_source(digest, pdf)
        return known, "already converted earlier"
    found = None if force else projects.find_anywhere(LIBRARY, digest)
    if found:  # converted by an overnight run into an application's folder
        return found[1], "already converted earlier"
    name = lib.entries[digest]["name"] if known else lib.name_for(pdf, digest)
    md = LIBRARY / f"{name}.md"
    result = convert_pdf(pdf, ConvertOptions(image_dir=LIBRARY / f"{name}_images"))
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(result.content, encoding="utf-8")
    lib.record(digest, name, pdf, result.page_count)
    status = f"converted now ({result.page_count} pages, {result.image_count} images)"
    if result.empty_pages:
        status += f"; no text found on page(s) {result.empty_pages}"
    return md, status


def _select_pages(markdown: str, spec: str) -> str:
    """Keep the frontmatter/note plus the pages in a spec like '1-5,9'."""
    wanted: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            wanted.update(range(int(a), int(b) + 1))
        elif part.strip():
            wanted.add(int(part))
    chunks = re.split(r"(?=<!-- page \d+ -->)", markdown)
    keep = [chunks[0]] + [
        c for c in chunks[1:] if int(re.match(r"<!-- page (\d+) -->", c).group(1)) in wanted
    ]
    return "".join(keep)


def _find_md(name: str) -> Path | None:
    """A converted document by name, in the library root or an application folder.
    An application folder's name on its own means its INDEX.md."""
    for candidate in (LIBRARY / f"{name}.md", LIBRARY / name / "INDEX.md"):
        if candidate.exists():
            return candidate
    return next(LIBRARY.glob(f"*/{name}.md"), None)


def _resolve(document: str) -> Path:
    """A PDF path, or the name of a document already in the library."""
    path = Path(document.strip().strip('"'))
    if path.suffix.lower() == ".pdf" and path.exists():
        return path
    md = _find_md(path.stem)
    if md:
        return md
    raise ValueError(
        f"{document!r} is not an existing PDF path or a converted document name. "
        "Use list_converted_pdfs to see converted documents."
    )


WAIT_SECONDS = 40  # stay safely under the chat's 60-second tool time limit
SECONDS_PER_PAGE = 2.5  # rough conversion speed, for estimates
_executor = ThreadPoolExecutor(max_workers=1)  # one conversion at a time; others queue
_jobs: dict[str, tuple[Future, float, int]] = {}  # resolved path -> (job, start time, pages)


def _folder_listing(folder: Path) -> str:
    known = {s for lib in projects.all_libraries(LIBRARY)
             for e in lib.entries.values() for s in e.get("sources", [])}
    pdfs = sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")
    if not pdfs:
        return f"No PDF files in {folder}."
    rows = [
        f"- {p.name} ({p.stat().st_size / 1e6:.1f} MB)"
        + (" — already converted" if str(p.resolve()) in known else "")
        for p in pdfs
    ]
    return (f"{folder} is a folder containing {len(pdfs)} PDF(s). Call read_pdf with the "
            f"full path of the one you need:\n" + "\n".join(rows))


@mcp.tool()
async def read_pdf(document: str, pages: str | None = None, force: bool = False) -> str:
    """Read a PDF from the user's computer as compact Markdown (much cheaper than the PDF).

    document: full path to a .pdf file, the name of an already converted document
              (see list_converted_pdfs), or a folder path (lists the PDFs in it).
    pages: optional 1-based page selection such as "1-5,9" for very long documents.
    force: convert again even if this PDF was converted before.

    The PDF is converted once and remembered by its contents; later calls are instant.
    A long first conversion continues in the background: if this returns "still
    converting", wait the suggested time and call read_pdf again with the same document.
    Each page starts with <!-- page N -->. Images are [IMAGE: file — page, type, caption,
    text in image] placeholders; fetch one with get_pdf_image only if really needed.
    """
    path = Path(document.strip().strip('"'))
    if path.is_dir():
        return _folder_listing(path)
    target = _resolve(document)
    if target.suffix.lower() == ".md":
        md, status = target, "already converted earlier"
    else:
        key = str(target.resolve())
        if key not in _jobs or (force and _jobs[key][0].done()):
            with pymupdf.open(target) as d:
                page_count = d.page_count
            _jobs[key] = (_executor.submit(_convert, target, force), time.monotonic(), page_count)
        job, started, page_count = _jobs[key]
        try:
            md, status = await asyncio.wait_for(asyncio.wrap_future(job), WAIT_SECONDS)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            left = max(15, page_count * SECONDS_PER_PAGE - elapsed)
            return (f"[pdf2md: still converting {target.name} ({page_count} pages) in the "
                    f"background, {elapsed:.0f}s so far. This only happens the first time. "
                    f"Wait about {left:.0f} seconds, then call read_pdf again with the same "
                    f"document. Tell the user it's converting.]")
        except Exception:
            del _jobs[key]  # let a later call retry
            raise
        _jobs.pop(key, None)
    text = md.read_text(encoding="utf-8")
    if pages:
        text = _select_pages(text, pages)
    header = (f"[pdf2md: {status}. Document name: \"{md.stem}\". "
              f"Images: get_pdf_image(document=\"{md.stem}\", image=\"<file name>\").]\n\n")
    return header + text


@mcp.tool()
def get_pdf_image(document: str, image: str) -> Image:
    """Fetch one image from a converted PDF, e.g. document="report", image="page19-img1.jpg".
    Only use this when the placeholder's caption and text are not enough."""
    name = Path(document.strip().strip('"')).stem
    md = _find_md(name)
    path = md.with_name(f"{name}_images") / Path(image).name if md else None
    if path is None or not path.exists():
        raise ValueError(f"No image {image!r} for document {name!r}")
    return Image(path=path)


@mcp.tool()
def list_converted_pdfs() -> str:
    """List converted documents. Planning applications converted overnight have their own
    folder with an INDEX.md map — open it with read_pdf(document="<application name>")
    and pick the documents to read from there, rather than reading everything."""
    out = []
    for folder in sorted(LIBRARY.glob("*/INDEX.md")):
        lib = Library(folder.parent)
        pages = sum(e.get("pages", 0) for e in lib.entries.values())
        out.append(f"- APPLICATION {folder.parent.name}: {len(lib.entries)} documents, "
                   f"{pages:,} pages — read_pdf(document=\"{folder.parent.name}\") for its index")
    root = Library(LIBRARY)
    out += [
        f"- {e['name']} ({e.get('pages', '?')} pages, converted {e.get('converted', '?')})"
        f" — from {e.get('sources', ['?'])[-1]}"
        for e in sorted(root.entries.values(), key=lambda e: e.get("converted", ""), reverse=True)
    ]
    return "\n".join(out) if out else "No PDFs converted yet."


def main() -> None:
    global LIBRARY
    parser = argparse.ArgumentParser(prog="pdf2md-mcp")
    parser.add_argument("--library", type=Path, default=LIBRARY)
    LIBRARY = parser.parse_args().library
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
