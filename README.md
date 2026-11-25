# pdf2md

Convert PDF files into clean Markdown that AI models can read easily.

Built on [PyMuPDF4LLM](https://github.com/pymupdf/pymupdf4llm), which handles layout analysis
(reading order, headings, tables, lists). On top of that, `pdf2md` adds:

- **Header and footer removal.** Repeating page headers and footers ("Page 3 of 40", running titles) are dropped.
- **Stamp removal.** Diagonal overlay text, such as a council's "RECEIVED: 19/10/2026" stamp, is removed
  before extraction so its letters don't get mixed into the text it crosses. The PDF file itself is not changed.
- **Page markers.** `<!-- page N -->` comments, so an AI can cite page numbers.
- **YAML frontmatter.** Source filename, title, author and page count at the top of the file.
- **Token-saving cleanup.** Bold markup is removed from headings and table headers, along with trailing spaces and blank-line runs.
- **Batch mode.** Converts whole folders recursively and keeps the folder structure in the output.
- **JSON output.** One Markdown chunk per page, ready for RAG / embedding pipelines.

## Install

```powershell
cd C:\PDF
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

To use `pdf2md` from any folder, either add `C:\PDF\.venv\Scripts` to your PATH, or install it
as a global tool with `pipx install C:\PDF` (or `uv tool install C:\PDF`).

## Usage

```text
pdf2md report.pdf                  # writes report.md next to the PDF
pdf2md report.pdf -o out.md        # choose the output file
pdf2md report.pdf --stdout         # print to stdout (pipe into other tools)
pdf2md docs\ -o converted\         # every PDF in docs\ (recursive)
pdf2md big.pdf --pages 1-5,9,20-   # page selection (1-based)
pdf2md paper.pdf --format json     # per-page JSON chunks
```

| Option | Effect |
|---|---|
| `--no-page-markers` | omit `<!-- page N -->` comments |
| `--no-frontmatter` | omit the YAML metadata block |
| `--keep-headers-footers` | keep repeating page headers/footers |
| `--images save\|none` | save (default): each image becomes an `[IMAGE: …]` placeholder and is saved as a JPG in `<name>_images\`; none: placeholders only |
| `--ocr auto\|off\|force` | OCR for scanned pages using RapidOCR (installed automatically); `auto` only OCRs pages that need it, then retries any empty page that contains an image at higher resolution |
| `--keep-stamps` | don't remove diagonal overlay stamps |
| `--raw` | skip the cleanup pass |
| `-q` / `-v` | quiet / show the PDF library's own diagnostics |

The exit code is `0` on success, `1` if any file failed, and `2` for usage errors.

## Example output

```markdown
---
source: "sample.pdf"
title: "ACME Quarterly Report"
author: "Finance Team"
pages: 3
---

<!-- page 1 -->

# Section 1: Overview

This is body text for section 1. ...

## Key figures

|Region|Revenue|Growth|
|---|---|---|
|North|1,200|12%|
```
