@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  打包「夕颜启动器.exe」
echo  需要联网安装 PyInstaller（约 10MB）
echo ============================================
echo.

where python >nul 2>nul
if not %errorlevel%==0 (
    echo [错误] 没有找到 python，请先安装 Python 3.10 以上并勾选 Add to PATH
    pause
    exit /b 1
)

echo [1/3] 安装 / 更新 PyInstaller ...
python -m pip install --upgrade pyinstaller
if not %errorlevel%==0 (
    echo [错误] PyInstaller 安装失败，请检查网络或代理
    pause
    exit /b 1
)

echo.
echo [2/3] 开始打包 ...
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name "夕颜启动器" ^
  --hidden-import bot ^
  --hidden-import analyzer ^
  --hidden-import emotion ^
  --hidden-import state_store ^
  --hidden-import strategy ^
  --hidden-import prompting ^
  --hidden-import persona_check ^
  --hidden-import memory_book ^
  --hidden-import memos ^
  --hidden-import events ^
  --hidden-import scheduler ^
  --hidden-import special_events ^
  --hidden-import reminders ^
  --hidden-import tools ^
  --hidden-import lunar ^
  gui.py
if not %errorlevel%==0 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo.
echo [3/3] 复制到项目根目录 ...
copy /y "dist\夕颜启动器.exe" "夕颜启动器.exe" >nul

echo.
echo 打包完成！双击「夕颜启动器.exe」即可使用。
echo 注意：exe 要和项目文件放在同一个文件夹里（要用到 persona.txt、bot.py 等）。
echo 打包中间产物在 build\ 和 dist\，可以删掉。
pause
