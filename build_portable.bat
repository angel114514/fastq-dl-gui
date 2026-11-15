@echo off
chcp 65001 >nul
cd /d "%~dp0"
python build_portable.py
if errorlevel 1 (
    echo.
    echo Build failed.
) else (
    echo.
    echo Build finished. See the dist folder.
)
pause
