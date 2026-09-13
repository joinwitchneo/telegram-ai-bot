@echo off
rem ASCII-only wrapper. All logic and Chinese messages live in launch.ps1.
rem Reason: batch + chcp 65001 mangles multibyte content, which produced
rem "Windows cannot find '!OLLAMA!'" when this .cmd contained Chinese text.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1"
echo.
pause
