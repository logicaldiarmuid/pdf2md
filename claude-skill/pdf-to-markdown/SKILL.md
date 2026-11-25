---
name: pdf-to-markdown
description: Read PDFs cheaply by converting them to Markdown with the user's local pdf2md tool instead of reading the PDF directly. Use whenever the user gives, drops in, attaches, or mentions a .pdf file to read, summarise, analyse, quote from or answer questions about — including "@file.pdf" mentions and PDFs in C:\PDF.
---

# PDF to Markdown

Reading a PDF directly is expensive (every page is processed as an image and text).
The user has a local tool, `pdf2md`, that converts a PDF into compact Markdown plus a
folder of JPG images. Always use it instead of reading the PDF.

## Steps

1. **Do not open the PDF with the Read tool.** Convert it first:

   ```bash
   "C:/PDF/.venv/Scripts/pdf2md.exe" "<full path to the PDF>" --library "C:/PDF/converted"
   ```

   - Use a 10-minute timeout; it takes roughly 2–3 seconds per page.
   - All output goes to `C:\PDF\converted\`: `<name>.md` and `<name>_images\` (JPGs).
     The PDF's own folder is never touched.
   - PDFs are recognised by content: if this PDF (even renamed or moved) was converted
     before, the tool prints `skip ... (already converted) -> <path to .md>` instantly.
     A same-named but different PDF gets a suffix (`name_2.md`).
   - The `ok ... -> <path>` / `skip ... -> <path>` line tells you which `.md` to read.
   - For several PDFs, pass them all in one command (or a folder path).

   **Whole planning applications** (a folder of many PDFs) are converted overnight by the
   user into their own folder, e.g. `C:\PDF\converted\galway-1234567\`, which has an
   `INDEX.md` listing every document by category with pages, token size and a one-line
   description. When asked about an application, read its `INDEX.md` first and choose
   which documents to read from it — never try to read every document. To convert a
   new application folder: same command with the folder path plus `--project auto`.

2. **Read that `.md`.** It starts with YAML metadata, then `<!-- page N -->` markers
   before each page, so cite page numbers from those.

3. **Images** appear as placeholders:

   ```
   [IMAGE: page19-img1.jpg — page 19, photo
   Caption: "..."
   Text in image: "..."]
   ```

   Rely on the caption and text first. Only if you genuinely need to see an image
   (e.g. the question is about what a photo or drawing shows), Read that one JPG from
   `C:\PDF\converted\<name>_images\` yourself — don't ask the user for it, and don't open
   images you don't need.

4. **Check the command output** for:
   - `WARN ... no text found on page(s) N` — those pages are blank or unreadable scans.
     If the user's question depends on them, Read that page's JPG (if one exists) or,
     as a last resort, only that page of the PDF with the Read tool's `pages` parameter.
   - `FAIL` — e.g. a password-protected or corrupt PDF. Tell the user; only then fall
     back to reading the PDF directly, and say that this costs more.

## Notes

- The tool removes diagonal "RECEIVED"-style stamps and repeating page headers/footers;
  this is expected.
- Known quirk: hyphens at line ends are dropped ("firstfloor" = "first-floor").
- If `C:/PDF/.venv/Scripts/pdf2md.exe` is missing, tell the user the pdf2md tool in
  `C:\PDF` needs reinstalling (`C:\PDF\.venv\Scripts\python -m pip install -e C:\PDF`).
