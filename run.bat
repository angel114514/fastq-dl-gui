@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0fastq_dl_gui.py"
) else (
    python "%~dp0fastq_dl_gui.py"
    if errorlevel 1 pause
)
endlocal
