"""Command-line interface for pdf2md."""

from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf2md",
        description="Convert PDF files into clean Markdown that is easy for AI models to read.",
        epilog=(
            "examples:\n"
            "  pdf2md report.pdf                 writes report.md next to the PDF\n"
            "  pdf2md report.pdf -o out.md       writes to out.md\n"
            "  pdf2md report.pdf --stdout        prints markdown (pipe it to an LLM tool)\n"
            "  pdf2md docs/ -o converted/        converts every PDF in docs/ (recursively)\n"
            "  pdf2md big.pdf --pages 1-5,9      only some pages\n"
            "  pdf2md paper.pdf --format json    per-page JSON chunks (good for RAG)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", type=Path, help="PDF files and/or directories")
    out = p.add_mutually_exclusive_group()
    out.add_argument(
        "-o", "--output", type=Path,
        help="output file (single input) or directory (multiple inputs / a directory)",
    )
    out.add_argument("--stdout", action="store_true", help="write the result to stdout")
    out.add_argument(
        "--library", type=Path, metavar="DIR",
        help="keep all conversions in one folder that remembers which PDFs it holds "
             "(recognised by content, so renamed/moved copies are not converted twice)",
    )
    p.add_argument(
        "--project", metavar="NAME",
        help="with --library: keep this application's documents in their own subfolder "
             "and write an INDEX.md there; 'auto' names it after the input folder "
             "(e.g. galway-2661567)",
    )
    p.add_argument("--force", action="store_true", help="with --library: convert again even if known")
    p.add_argument("--format", choices=["md", "json"], default="md", help="output format (default: md)")
    p.add_argument("--pages", metavar="SPEC", help="1-based pages to convert, e.g. '1-3,7,10-'")
    p.add_argument("--no-page-markers", action="store_true", help="omit <!-- page N --> markers")
    p.add_argument("--no-frontmatter", action="store_true", help="omit the YAML metadata header")
    p.add_argument(
        "--keep-headers-footers", action="store_true",
        help="keep repeating page headers/footers (removed by default)",
    )
    p.add_argument(
        "--images", choices=["save", "none"], default="save",
        help="save (default): replace each image with a placeholder and save it as a JPG "
             "in <name>_images/; none: placeholders only, no files",
    )
    p.add_argument(
        "--ocr", choices=["auto", "off", "force"], default="auto",
        help="OCR for scanned pages (default: auto)",
    )
    p.add_argument(
        "--keep-stamps", action="store_true",
        help="keep diagonal overlay text such as 'RECEIVED' stamps (removed by default)",
    )
    p.add_argument("--raw", action="store_true", help="skip the markdown cleanup pass")
    p.add_argument(
        "--exclude", action="append", default=[], metavar="PATTERN",
        help="skip PDFs whose file name or any folder name matches PATTERN (wildcards * ? "
             "allowed, not case-sensitive); can be given several times, "
             "e.g. --exclude Submission_Acknowledgment",
    )
    p.add_argument("-r", "--recursive", action=argparse.BooleanOptionalAction, default=True,
                   help="search directories recursively (default: on)")
    p.add_argument("--skip-existing", action="store_true",
                   help="skip PDFs whose output file already exists and is newer than the PDF")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    p.add_argument("-v", "--verbose", action="store_true", help="show the PDF library's own messages")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def excluded(pdf: Path, base: Path, patterns: list[str]) -> bool:
    """True if the PDF's name, or a folder between base and it, matches a pattern."""
    parts = pdf.relative_to(base).parts if pdf.is_relative_to(base) else (pdf.name,)
    return any(fnmatch.fnmatch(part.lower(), pat.lower()) for part in parts for pat in patterns)


def collect_pdfs(inputs: list[Path], recursive: bool) -> list[tuple[Path, Path]]:
    """Return (pdf_path, base_dir) pairs; base_dir preserves layout under -o DIR."""
    found: list[tuple[Path, Path]] = []
    for item in inputs:
        if item.is_dir():
            pattern = "**/*" if recursive else "*"
            for f in sorted(item.glob(pattern)):
                if f.is_file() and f.suffix.lower() == ".pdf":
                    found.append((f, item))
        elif item.is_file():
            found.append((item, item.parent))
        else:
            raise FileNotFoundError(f"no such file or directory: {item}")
    return found


def format_pages(pages: list[int]) -> str:
    """[1, 2, 3, 7] -> '1-3, 7'"""
    ranges: list[str] = []
    start = prev = pages[0]
    for p in pages[1:] + [None]:
        if p is not None and p == prev + 1:
            prev = p
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        if p is not None:
            start = prev = p
    return ", ".join(ranges)


def keep_awake(on: bool) -> None:
    """Stop Windows going to sleep while a (possibly overnight) run is working."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nstopped. Finished PDFs are kept; run the same command again to continue.",
              file=sys.stderr)
        return 130


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    try:
        pdfs = collect_pdfs(args.inputs, args.recursive)
    except FileNotFoundError as e:
        print(f"pdf2md: error: {e}", file=sys.stderr)
        return 2
    if args.exclude:
        kept = [(pdf, base) for pdf, base in pdfs if not excluded(pdf, base, args.exclude)]
        if not args.quiet and len(kept) < len(pdfs):
            print(f"excluding {len(pdfs) - len(kept)} PDF(s) matching: {', '.join(args.exclude)}",
                  file=sys.stderr)
        pdfs = kept
    if not pdfs:
        print("pdf2md: error: no PDF files found", file=sys.stderr)
        return 2

    many = len(pdfs) > 1 or any(i.is_dir() for i in args.inputs)
    if args.output and many and args.output.suffix.lower() in (".md", ".json"):
        print("pdf2md: error: with multiple inputs, -o must be a directory", file=sys.stderr)
        return 2

    # Import lazily so --help stays fast.
    from .convert import ConvertOptions, convert_pdf, parse_page_spec
    from .library import Library, file_hash
    from . import projects
    import pymupdf

    project = None
    if args.project and not args.library:
        print("pdf2md: error: --project needs --library", file=sys.stderr)
        return 2
    if args.project == "auto":
        folders = [i for i in args.inputs if i.is_dir()]
        if len(folders) != 1:
            print("pdf2md: error: --project auto needs exactly one input folder", file=sys.stderr)
            return 2
        project = projects.project_name(folders[0])
    elif args.project:
        project = args.project
    out_dir = args.library / project if project else args.library
    library = Library(out_dir) if args.library else None
    if project:
        log(f"project {project}: output in {out_dir}")
    keep_awake(True)

    if args.stdout:
        sys.stdout.reconfigure(encoding="utf-8")

    ext = ".json" if args.format == "json" else ".md"
    failures = skipped = warned = 0
    for pdf, base in pdfs:
        try:
            pages = None
            if args.pages:
                with pymupdf.open(pdf) as d:
                    pages = parse_page_spec(args.pages, d.page_count)

            digest = None
            category = pdf.parent.relative_to(base).as_posix() if pdf.is_relative_to(base) else ""
            category = "" if category == "." else category
            if library is not None:
                digest = file_hash(pdf)
                known = library.find(digest)
                if known and not args.force and not args.pages:
                    library.add_source(digest, pdf)
                    skipped += 1
                    log(f"skip {pdf} (already converted) -> {known}")
                    continue
                elsewhere = None
                if not (known or args.force or args.pages):
                    elsewhere = projects.find_anywhere(args.library, digest)
                if elsewhere:  # converted before: reuse it rather than convert again
                    from_root = elsewhere[0].folder.resolve() == args.library.resolve()
                    reused = projects.copy_into(library, elsewhere[0], digest, pdf, category,
                                                move=bool(project) and from_root)
                    skipped += 1
                    log(f"{'move' if project and from_root else 'copy'} {pdf} "
                        f"(converted before) -> {reused}")
                    continue
                if known:  # --force: convert again under the same name
                    name = library.entries[digest]["name"]
                else:
                    name = library.name_for(pdf, digest)
                dest = out_dir / f"{name}{ext}"
            elif args.stdout:
                dest = None
            elif args.output and not many:
                dest = args.output
            elif args.output:
                dest = args.output / pdf.relative_to(base).with_suffix(ext)
            else:
                dest = pdf.with_suffix(ext)

            if (args.skip_existing and dest is not None and dest.exists()
                    and dest.stat().st_mtime >= pdf.stat().st_mtime):
                skipped += 1
                log(f"skip {pdf} (already converted)")
                continue

            image_dir = None
            if args.images == "save":
                anchor = dest if dest is not None else pdf
                image_dir = anchor.with_name(anchor.stem + "_images")

            opts = ConvertOptions(
                pages=pages,
                page_markers=not args.no_page_markers,
                strip_headers_footers=not args.keep_headers_footers,
                images=args.images,
                image_dir=image_dir,
                frontmatter=not args.no_frontmatter,
                ocr=args.ocr,
                clean=not args.raw,
                remove_stamps=not args.keep_stamps,
                output_format=args.format,
                progress=not args.quiet and sys.stderr.isatty(),
            )
            result = convert_pdf(pdf, opts)
            if args.verbose and result.log.strip():
                print(result.log.rstrip(), file=sys.stderr)

            if dest is None:
                sys.stdout.write(result.content)
                sys.stdout.flush()
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(result.content, encoding="utf-8")
                if library is not None and not args.pages:  # partial conversions aren't remembered
                    library.record(digest, dest.stem, pdf, result.page_count, category=category,
                                   empty_pages=result.empty_pages)
                    projects.note_failure(out_dir, pdf, None)

            tokens = len(result.content) // 4  # rough rule of thumb for English text
            where = "stdout" if dest is None else str(dest)
            images = ""
            if result.image_count:
                saved = f" saved to {result.image_dir.name}" if result.image_dir else ""
                images = f", {result.image_count} image{'s' if result.image_count != 1 else ''}{saved}"
            log(f"ok   {pdf} -> {where}  ({result.pages_converted}/{result.page_count} pages, "
                f"~{tokens:,} tokens{images})")
            if result.empty_pages:
                warned += 1
                print(f"WARN {pdf}: no text found on page(s) {format_pages(result.empty_pages)}"
                      " (blank page, or a scan OCR could not read)", file=sys.stderr)
        except Exception as e:  # keep going on batch runs
            failures += 1
            print(f"FAIL {pdf}: {e}", file=sys.stderr)
            if library is not None:
                projects.note_failure(out_dir, pdf, f"{type(e).__name__}: {e}")

    keep_awake(False)
    if project:
        index = projects.write_index(out_dir, project, [i.resolve() for i in args.inputs])
        log(f"index: {index}")

    if many:
        log(f"done: {len(pdfs) - failures - skipped} converted, {skipped} skipped, {failures} failed"
            + (f", {warned} with blank pages" if warned else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
