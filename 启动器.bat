@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0gui.py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        start "" python "%~dp0gui.py"
    ) else (
        echo 没有找到 Python。请先安装 Python 3.10 以上，或使用打包好的 exe。
        pause
    )
)
