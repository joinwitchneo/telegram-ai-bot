@echo off
rem ASCII-only wrapper for the V3 launcher.
rem All logic and Chinese messages live in launch_v3.ps1 (UTF-8 with BOM).
rem Reason: batch + chcp 65001 mangles multibyte content.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_v3.ps1"
echo.
pause
