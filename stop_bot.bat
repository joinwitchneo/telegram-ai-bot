@echo off
chcp 65001 >nul
echo 正在停止 Telegram 机器人...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*telegram-bot*bot.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output ('已停止 PID ' + $_.ProcessId) }"
echo 完成。
pause
