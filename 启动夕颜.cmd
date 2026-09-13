@echo off
rem ============================================================
rem  夕颜 V2 唯一推荐入口（命令行方式）
rem  作用：校验环境 -> 按需拉起 Ollama -> 停掉旧实例 -> 启动 bot.py
rem        -> 打印 [build] 自证信息 -> 失败时保留窗口并显示原因
rem  机器人日志统一写入 bot.log（本窗口只做提示与自证）
rem  图形界面请用 启动器.bat（作用见其文件头注释）
rem ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "LOGFILE=%~dp0bot.log"
set "OLLAMA=C:\Users\Lenovo\tools-bin\ollama.exe"
set "PY="

echo [launcher] 夕颜 V2 启动器
echo [launcher] 工作目录：%~dp0

rem ---- 1. 找 Python（优先环境变量，其次本机运行时，最后 PATH）----
if defined XIYAN_PYTHON set "PY=%XIYAN_PYTHON%"
if not defined PY if exist "C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PY=C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PY for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY (
    echo [launcher][ERROR] 找不到 Python 解释器。
    echo [launcher][ERROR] 请安装 Python 3.10+，或设置环境变量 XIYAN_PYTHON 指向 python.exe。
    goto :fail
)
echo [launcher] Python：!PY!
if not exist "!PY!" (
    echo [launcher][ERROR] Python 路径不存在：!PY!
    goto :fail
)

rem ---- 2. 必须有配置文件 ----
if not exist "%~dp0config.env" (
    echo [launcher][ERROR] 缺少 config.env。
    echo [launcher][ERROR] 请复制 config.example.env 为 config.env，并填入 TELEGRAM_BOT_TOKEN 与 DEEPSEEK_API_KEY。
    goto :fail
)

rem ---- 3. 本地视觉服务：有就起，没有不阻塞 ----
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if errorlevel 1 (
    if exist "!OLLAMA!" (
        echo [launcher] 拉起本地视觉服务 Ollama...
        start "" /b "!OLLAMA!" serve
        timeout /t 6 /nobreak >nul
    ) else (
        echo [launcher] 未找到 Ollama（!OLLAMA!）——图片识别会在收到图片时再尝试按需启动，不影响聊天。
    )
) else (
    echo [launcher] Ollama 已在运行
)

rem ---- 4. 停掉旧实例（只匹配本目录的 bot.py，避免误杀其它 Python 程序）----
set "STOPPED="
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like '%%python%%'\" ^| Where-Object { $_.CommandLine -like '*telegram-bot-v2*bot.py*' } ^| Select-Object -ExpandProperty ProcessId" 2^>nul') do (
    echo [launcher] 停掉旧实例 PID %%i
    taskkill /PID %%i /F >nul 2>nul
    set "STOPPED=1"
)
if defined STOPPED timeout /t 2 /nobreak >nul

rem ---- 5. 启动（后台），日志进 bot.log ----
echo [launcher] 启动 bot.py ...
echo ===== launcher %DATE% %TIME% ===== >> "!LOGFILE!"
start "" /b "!PY!" "%~dp0bot.py"
timeout /t 8 /nobreak >nul

rem ---- 6. 自证：把 [build] 指纹与错误显示出来 ----
echo [launcher] ---- bot.log 末尾 ----
powershell -NoProfile -Command "if (Test-Path '!LOGFILE!') { Get-Content '!LOGFILE!' -Tail 14 } else { Write-Host '(没有 bot.log)' }"

findstr /C:"[build] path=" "!LOGFILE!" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [launcher][ERROR] 没有在 bot.log 里看到 [build] 指纹 —— 机器人很可能没起来。
    echo [launcher][ERROR] 常见原因：依赖缺失、config.env 没填 token、或端口/token 被另一个实例占用。
    goto :fail
)

echo.
echo [launcher] bot started（上面 [build] 一行即为本次运行的代码指纹，可据此确认跑的是最新代码）
echo [launcher] 日志文件：!LOGFILE!
echo [launcher] 提示：如果 [build] handlers 里 /vision=False，说明跑的不是最新代码。
goto :end

:fail
echo.
echo [launcher] 启动失败，窗口保持打开，方便你复制上面的错误信息。
echo [launcher] 完整日志：!LOGFILE!

:end
echo.
pause
endlocal
