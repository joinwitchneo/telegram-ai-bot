param([switch]$DryRun)

# 夕颜 V2 启动器（唯一命令行入口）
# 校验环境 -> 按需拉起本地视觉 -> 停掉旧实例 -> 启动 bot.py -> 打印 [build] 自证
$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$LogPath = Join-Path $Root "bot.log"

function Say($text) { Write-Host $text }

Say "[launcher] 夕颜 V2 启动器"
Say ("[launcher] 工作目录：{0}" -f $Root)
if ($DryRun) { Say "[launcher] 干跑模式：只检查，不启动也不停进程" }

# ---- 1. Python ----
$python = $env:XIYAN_PYTHON
if (-not $python) {
    foreach ($c in @("C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
                     (Join-Path $Root ".venv\Scripts\python.exe"))) {
        if (Test-Path $c) { $python = $c; break }
    }
}
if (-not $python) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source }
}
if (-not $python -or -not (Test-Path $python)) {
    Say "[launcher][ERROR] 找不到 Python。请安装 Python 3.10+，或设置环境变量 XIYAN_PYTHON。"
    exit 1
}
Say ("[launcher] Python：{0}" -f $python)

# ---- 2. config.env ----
if (-not (Test-Path (Join-Path $Root "config.env"))) {
    Say "[launcher][ERROR] 缺少 config.env（请从 config.example.env 复制并填入 token 与密钥）。"
    exit 1
}

# ---- 3. Ollama（有就起，没有不阻塞）----
$ollama = @((Join-Path $Root "tools-bin\ollama.exe"),
            "C:\Users\Lenovo\tools-bin\ollama.exe",
            (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe")) |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (Get-Process ollama -ErrorAction SilentlyContinue) {
    Say "[launcher] Ollama 已在运行"
} elseif ($ollama) {
    Say ("[launcher] 拉起本地视觉服务：{0}" -f $ollama)
    if (-not $DryRun) {
        Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 6
    }
} else {
    Say "[launcher] 未找到 ollama.exe（图片识别会在收到图片时按需启动，不影响聊天）"
}

# ---- 4. 停掉旧实例（只匹配本目录 bot.py）----
$targets = Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*telegram-bot-v2*bot.py*" }
if (-not $targets) {
    Say "[launcher] 没有正在运行的旧实例"
} else {
    foreach ($t in $targets) {
        Say ("[launcher] 停掉旧实例 PID {0}" -f $t.ProcessId)
        if (-not $DryRun) { Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    if (-not $DryRun) { Start-Sleep -Seconds 3 }
}

# ---- 5. 启动 bot.py ----
Add-Content -Path $LogPath -Value ("===== launcher {0} =====" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding UTF8
Say "[launcher] 启动 bot.py ..."
$proc = $null
if (-not $DryRun) {
    $proc = Start-Process -FilePath $python -ArgumentList "bot.py" -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 10
}

# ---- 6. 自证 ----
Say "[launcher] ---- bot.log 末尾 ----"
if (Test-Path $LogPath) { Get-Content $LogPath -Tail 16 -Encoding UTF8 } else { Say "(没有 bot.log)" }

if ($DryRun) { Say "[launcher] 干跑结束"; exit 0 }

$buildLine = Select-String -Path $LogPath -Pattern "\[build\] pid=" -Encoding UTF8 -ErrorAction SilentlyContinue |
    Select-Object -Last 1
if (-not $buildLine) {
    Say "[launcher][ERROR] bot.log 里没有 [build] pid= 指纹，机器人很可能没起来。"
    Say ("[launcher][ERROR] 可手动运行：`"{0}`" `"{1}`"" -f $python, (Join-Path $Root "bot.py"))
    exit 1
}
if ($proc -and -not $proc.HasExited) {
    Say ("[launcher] bot started（pid={0}）；上面 [build] 一行就是本次运行的代码指纹" -f $proc.Id)
} else {
    Say "[launcher] bot 进程已退出，请看上面日志定位原因。"
}
exit 0

