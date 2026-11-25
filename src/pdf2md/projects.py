"""Per-application folders inside the library, and the INDEX.md that maps them.

Layout:  <library root>/<project>/{index.json, INDEX.md, failures.json,
                                    <doc>.md, <doc>_images/}
Conversions in the library root itself (from before projects existed, or made
through the chat connector) are found too, so nothing is converted twice.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from .library import Library


def project_name(folder: Path) -> str:
    """'...\\galway\\2661567' -> 'galway-2661567' (folder names can be ambiguous alone)."""
    folder = folder.resolve()
    parts = [p for p in (folder.parent.name, folder.name) if p]
    return re.sub(r"[^\w.-]+", "_", "-".join(parts)).strip("_") or "project"


def all_libraries(root: Path) -> list[Library]:
    """The root library plus every project library under it."""
    libs = [Library(root)] if (root / "index.json").exists() else []
    libs += [Library(p.parent) for p in sorted(root.glob("*/index.json"))]
    return libs


def find_anywhere(root: Path, digest: str) -> tuple[Library, Path] | None:
    for lib in all_libraries(root):
        md = lib.find(digest)
        if md:
            return lib, md
    return None


def copy_into(dest: Library, src: Library, digest: str, pdf: Path, category: str,
              move: bool = False) -> Path:
    """Reuse an existing conversion for this project instead of converting again.
    move=True takes it over (used for conversions in the library root, which belong to
    no project); otherwise it is copied and the other project keeps its own."""
    entry = src.entries[digest]
    name = dest.name_for(pdf, digest)
    src_md = src.folder / f"{entry['name']}.md"
    src_images = src.folder / f"{entry['name']}_images"
    dest_md = dest.folder / f"{name}.md"
    dest_images = dest.folder / f"{name}_images"
    dest.folder.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(src_md, dest_md)
        if src_images.is_dir():
            shutil.move(src_images, dest_images)
        src.forget(digest)
    else:
        shutil.copy2(src_md, dest_md)
        if src_images.is_dir():
            shutil.copytree(src_images, dest_images, dirs_exist_ok=True)
    dest.record(digest, name, pdf, entry.get("pages", 0), category=category,
                empty_pages=entry.get("empty_pages"))
    for other in entry.get("sources", []):  # keep the other places this PDF was seen
        if Path(other).exists() and Path(other).resolve() != pdf.resolve():
            dest.add_source(digest, Path(other))
    return dest_md


# --- failures -------------------------------------------------------------------

def _failures_path(folder: Path) -> Path:
    return folder / "failures.json"


def load_failures(folder: Path) -> dict[str, str]:
    try:
        return json.loads(_failures_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def note_failure(folder: Path, pdf: Path, error: str | None) -> None:
    """Remember (or, with error=None, forget) a PDF that could not be converted."""
    failures = load_failures(folder)
    key = str(pdf.resolve())
    if error is None and key not in failures:
        return
    if error is None:
        failures.pop(key)
    else:
        failures[key] = error
    folder.mkdir(parents=True, exist_ok=True)
    _failures_path(folder).write_text(json.dumps(failures, indent=2, ensure_ascii=False),
                                      encoding="utf-8")


# --- INDEX.md --------------------------------------------------------------------

def _describe(md_text: str, limit: int = 90) -> str:
    """A short 'what is this document' line: its title or first real line of text."""
    title = re.search(r'^title: "(.+)"$', md_text, re.M)
    if title and len(title.group(1)) > 8:
        line = title.group(1)
    else:
        body = re.sub(r"\A---\n.*?\n---\n", "", md_text, flags=re.S)
        line = ""
        for raw in body.splitlines():
            raw = raw.strip()
            if not raw or raw.startswith(("<!--", ">", "[IMAGE", "|", "Caption:", "Text in image:")):
                continue
            line = re.sub(r"[#*_`]|<[^>]+>", "", raw).strip()
            if len(re.findall(r"[A-Za-z]", line)) >= 8:
                break
    line = " ".join(line.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def write_index(folder: Path, project: str, sources: list[Path]) -> Path:
    """Write <folder>/INDEX.md: every document with category, size and a description."""
    lib = Library(folder)
    rows: dict[str, list[tuple]] = {}
    total_pages = total_tokens = 0
    for entry in lib.entries.values():
        md = folder / f"{entry['name']}.md"
        if not md.exists():
            continue
        text = md.read_text(encoding="utf-8")
        tokens = len(text) // 4
        images = len(re.findall(r"^\[IMAGE: ", text, re.M))  # not the note's mention
        pages = entry.get("pages", 0)
        total_pages += pages
        total_tokens += tokens
        notes = []
        if entry.get("empty_pages"):
            notes.append(f"no text on p.{','.join(map(str, entry['empty_pages']))}")
        rows.setdefault(entry.get("category") or "(top level)", []).append(
            (md.name, pages, tokens, images, _describe(text), "; ".join(notes))
        )

    failures = load_failures(folder)
    out = [
        f"# Document index: {project}",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} from: "
        + "; ".join(str(s) for s in sources),
        "",
        f"**{sum(len(r) for r in rows.values())} documents, {total_pages:,} pages, "
        f"~{total_tokens:,} tokens in total**"
        + (f" · {len(failures)} could not be converted (listed at the end)" if failures else ""),
        "",
        "> Note for AI: this is a map of the converted documents for this application. "
        "Don't try to read everything (a full application is usually far larger than a "
        "context window). "
        "Use the categories, sizes and descriptions to choose what to read, then read those "
        "files (each is Markdown with <!-- page N --> markers for citing pages; images are "
        "[IMAGE: ...] placeholders with the JPGs in <file>_images/).",
        "",
    ]
    for category in sorted(rows, key=str.lower):
        docs = sorted(rows[category], key=lambda r: r[0].lower())
        pages = sum(r[1] for r in docs)
        tokens = sum(r[2] for r in docs)
        out += [f"## {category.replace('_', ' ')}  ({len(docs)} docs, {pages:,} pages, "
                f"~{tokens:,} tokens)", "",
                "| File | Pages | ~Tokens | Images | Description | Notes |",
                "|---|---|---|---|---|---|"]
        for name, p, t, i, desc, notes in docs:
            desc = desc.replace("|", "/")
            out.append(f"| {name} | {p} | {t:,} | {i or ''} | {desc} | {notes} |")
        out.append("")
    if failures:
        out += ["## Could not be converted", ""]
        out += [f"- {Path(p).name}: {err}" for p, err in sorted(failures.items())]
        out.append("")
    index = folder / "INDEX.md"
    index.write_text("\n".join(out), encoding="utf-8")
    return index
