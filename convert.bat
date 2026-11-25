@echo off
rem Converts every PDF in this folder to Markdown + JPG images in the "converted" folder.
rem PDFs that were already converted (recognised by content, even if renamed) are skipped.
"%~dp0.venv\Scripts\pdf2md.exe" "%~dp0." --no-recursive --library "%~dp0converted"
echo.
pause
