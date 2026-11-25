"""A central folder of conversions that remembers which PDFs it already holds.

PDFs are recognised by a SHA-256 hash of their contents, so the same file is
found again even after it has been renamed, moved or downloaded twice.
The folder holds <name>.md, <name>_images/ and index.json.

Several processes may use the folder at once (a batch run in a terminal plus the
desktop-app connector, say), so every change to index.json is made under a lock
file, against a freshly re-read copy, and saved before the lock is released.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

LOCK_STALE_SECONDS = 60  # a lock older than this was left by a crashed process


def _retry(action, seconds: float = 10.0):
    """Run action, retrying on Windows' transient 'access denied' sharing errors."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Library:
    def __init__(self, folder: Path):
        self.folder = folder
        self.index_path = folder / "index.json"
        self.lock_path = folder / "index.lock"
        self.entries: dict[str, dict] = {}
        self._load()

    def find(self, digest: str) -> Path | None:
        """The existing .md for a PDF with these contents, if there is one."""
        self._load()  # another process may have added it since
        entry = self.entries.get(digest)
        if entry:
            md = self.folder / f"{entry['name']}.md"
            if md.exists():
                return md
        return None

    def add_source(self, digest: str, pdf: Path) -> None:
        with self._locked():
            sources = self.entries[digest].setdefault("sources", [])
            if str(pdf.resolve()) not in sources:
                sources.append(str(pdf.resolve()))

    def name_for(self, pdf: Path, digest: str) -> str:
        """Output name for a new conversion. A changed version of a PDF already
        converted from the same path takes over the old name (and the old
        conversion is dropped); otherwise the PDF's own name, made unique."""
        source = str(pdf.resolve())
        with self._locked():
            for old_digest, entry in list(self.entries.items()):
                if source in entry.get("sources", []) and old_digest != digest:
                    self._remove(old_digest)
                    return entry["name"]
            taken = {e["name"].lower() for e in self.entries.values()}
            name, n = pdf.stem, 1
            while name.lower() in taken or (self.folder / f"{name}.md").exists():
                n += 1
                name = f"{pdf.stem}_{n}"
            return name

    def record(self, digest: str, name: str, pdf: Path, pages: int, category: str = "",
               empty_pages: list[int] | None = None) -> None:
        with self._locked():
            self.entries[digest] = {
                "name": name,
                "sources": [str(pdf.resolve())],
                "pages": pages,
                "converted": datetime.now().isoformat(timespec="seconds"),
            }
            if category:
                self.entries[digest]["category"] = category
            if empty_pages:
                self.entries[digest]["empty_pages"] = empty_pages

    def forget(self, digest: str) -> dict | None:
        """Drop an entry from the index without deleting its files (they are being moved)."""
        with self._locked():
            return self.entries.pop(digest, None)

    def remove(self, digest: str) -> None:
        with self._locked():
            self._remove(digest)

    def _remove(self, digest: str) -> None:
        entry = self.entries.pop(digest, None)
        if entry:
            (self.folder / f"{entry['name']}.md").unlink(missing_ok=True)
            shutil.rmtree(self.folder / f"{entry['name']}_images", ignore_errors=True)

    def _load(self) -> None:
        # Windows briefly refuses access while another process replaces the file;
        # retry rather than mistake that for an empty index (and later save it).
        self.entries = _retry(self._read_index)

    def _read_index(self) -> dict:
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    @contextlib.contextmanager
    def _locked(self):
        """Hold the index lock; re-read the index first and save it afterwards."""
        self.folder.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except (FileExistsError, PermissionError):  # held, or being deleted
                with contextlib.suppress(OSError):
                    if time.time() - self.lock_path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self.lock_path.unlink()
                        continue
                time.sleep(0.05)
        try:
            self._load()
            yield
            tmp = self.index_path.with_name(f"index.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False), encoding="utf-8")
            _retry(lambda: tmp.replace(self.index_path))  # fails while a reader has it open
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                self.lock_path.unlink()
