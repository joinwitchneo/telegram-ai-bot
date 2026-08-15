@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
echo 正在启动 Telegram 机器人（DeepSeek / Ollama）...
echo 关闭本窗口即停止机器人。日志见 bot.log
python "%~dp0bot.py"
echo.
pause
