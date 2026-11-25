@echo off
rem Drag a planning application's download folder onto this file (or run:
rem   convert-application.bat "C:\path\to\folder").
rem Every PDF in it (including subfolders) is converted into C:\PDF\converted\<area>-<number>\
rem together with an INDEX.md. Already-converted PDFs are skipped or copied, so you can
rem stop it at any time and run it again to continue. The PC is kept awake while it runs.
rem Council acknowledgment letters (Submission_Acknowledgment) are skipped: they are
rem near-identical template letters. Add more --exclude NAME to skip other folders.
if "%~1"=="" (
    echo Drag a folder onto this file to convert all the PDFs in it.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\pdf2md.exe" "%~1" --library "%~dp0converted" --project auto --exclude Submission_Acknowledgment
echo.
pause
