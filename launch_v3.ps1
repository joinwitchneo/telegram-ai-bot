param([switch]$DryRun, [switch]$NoScheduler, [switch]$SelfTest)

# 夕颜 V3 启动器（唯一命令行入口）
# 点一下就应该"用新进程替换旧进程"：先精确找出并停掉旧的 bot / scheduler，再启动新的。
#
# 为什么不能只看命令行里的目录：老启动器是用 `-ArgumentList "bot.py"` 起的，
# 命令行里根本没有目录，所以必须再加两条识别途径（锁文件 + 裸脚本名）。
$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
$LogPath = Join-Path $Root "bot.log"
$SchedulerLog = Join-Path $Root "scheduler.log"
$SchedulerLock = Join-Path $Root "data\v3\scheduler.lock"
$LauncherLog = Join-Path $Root "launcher.log"

# 开机自启是隐藏窗口：所有输出必须也落一份到 launcher.log，否则出问题没法查
function Say($text) {
    Write-Host $text
    try {
        Add-Content -Path $LauncherLog -Value $text -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}

function Get-ScriptArgument([string]$CommandLine) {
    # 去掉 python.exe 那一段，剩下的是脚本参数（用来判断是不是"裸脚本名"启动）
    $match = [regex]::Match([string]$CommandLine, '(?i)pythonw?\.exe"?\s+(.*)$')
    if ($match.Success) { return $match.Groups[1].Value.Trim() }
    return ""
}

function Test-XiyanProcess {
    param($Proc, [string]$Root)
    $cmd = [string]$Proc.CommandLine
    if (-not $cmd) { return $false }
    if ($cmd -notmatch '(?i)bot\.py|scheduler\.py') { return $false }
    if ($cmd -match [regex]::Escape($Root)) { return $true }        # 命令行里带本项目路径
    if ($cmd -match '(?i)telegram-bot') { return $true }            # 同工作区的其它副本
    # 只看脚本名那一段：`bot.py --config C:\...\config.env` 也算"裸脚本名启动"
    $scriptToken = (@((Get-ScriptArgument $cmd) -split '\s+') | Select-Object -First 1).Trim('"')
    if ($scriptToken -and $scriptToken -notmatch '[\\/]') { return $true }
    return $false
}

function Get-RecordedPids {
    param([string]$Root)
    $pids = @()
    foreach ($rel in @("data\bot.lock", "data\v3\scheduler.lock")) {
        $path = Join-Path $Root $rel
        if (-not (Test-Path $path)) { continue }
        try {
            $info = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($info.pid) { $pids += [int]$info.pid }
        } catch { }
    }
    return $pids
}

function Find-OldInstances {
    param([string]$Root)
    $found = @(Get-RecordedPids -Root $Root)
    foreach ($proc in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        if (Test-XiyanProcess -Proc $proc -Root $Root) { $found += [int]$proc.ProcessId }
    }
    return @($found | Where-Object { $_ -and $_ -ne $PID } | Sort-Object -Unique)
}

function Stop-Instances {
    param([int[]]$Ids, [switch]$DryRun)
    if (-not $Ids -or $Ids.Count -eq 0) { return 0 }
    foreach ($id in $Ids) {
        $info = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        $desc = if ($info -and $info.CommandLine) { $info.CommandLine } else { "（进程已不存在）" }
        Say ("[launcher] 停掉旧实例 PID {0}  {1}" -f $id, $desc)
        if (-not $DryRun) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    }
    if ($DryRun) { return 0 }
    # 等它们真的退出，避免新进程又被单实例锁挡住
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        $still = @($Ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($still.Count -eq 0) { return 0 }
        Start-Sleep -Milliseconds 300
    }
    foreach ($id in @($Ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })) {
        Say ("[launcher][WARN] PID {0} 没能在 10 秒内退出，稍后会自动重试" -f $id)
    }
    return 0
}

# ---- 自检：证明"找旧进程"的判断在关键场景下是对的 ----
if ($SelfTest) {
    $py = '"C:\Users\Lenovo\.cache\codex-runtimes\python.exe"'
    $cases = @(
        @{ Name = "裸脚本名 bot.py（老启动器就是这样起的）"; CommandLine = "$py bot.py"; Expect = $true },
        @{ Name = "本目录完整路径 bot.py"; CommandLine = ('{0} "{1}\bot.py"' -f $py, $Root); Expect = $true },
        @{ Name = "同工作区 V2 的 bot.py"; CommandLine = ('{0} "{1}\..\telegram-bot-v2\bot.py"' -f $py, $Root); Expect = $true },
        @{ Name = "裸脚本名 scheduler.py"; CommandLine = "$py scheduler.py"; Expect = $true },
        @{ Name = "裸脚本名 bot.py + 带路径的 --config"; CommandLine = ("$py bot.py --config `"C:\x\config.env`""); Expect = $true },
        @{ Name = "本目录 scheduler.py 完整路径"; CommandLine = ('{0} "{1}\scheduler.py"' -f $py, $Root); Expect = $true },
        @{ Name = "别处目录的 bot.py（不该动它）"; CommandLine = ("$py `"D:\work\bot.py`""); Expect = $false },
        @{ Name = "无关脚本"; CommandLine = "$py mytool.py"; Expect = $false },
        @{ Name = "一次性认知进程 v3.cycle_runner"; CommandLine = "$py -m v3.cycle_runner --wake-id wake_000001"; Expect = $false },
        @{ Name = "启动器自己（powershell）"; CommandLine = "powershell -File launch_v3.ps1"; Expect = $false }
    )
    $failed = 0
    foreach ($case in $cases) {
        $got = Test-XiyanProcess -Proc ([pscustomobject]@{ CommandLine = $case.CommandLine }) -Root $Root
        $ok = ($got -eq $case.Expect)
        if (-not $ok) { $failed++ }
        Say ("[{0}] {1}  ->  {2}" -f $(if ($ok) { "OK  " } else { "FAIL" }), $case.Name, $got)
    }
    Say ("[launcher] 自检结束：{0} 个用例，失败 {1} 个" -f $cases.Count, $failed)
    exit $(if ($failed -eq 0) { 0 } else { 1 })
}

Say "[launcher] 夕颜 V3 启动器"
Say ("===== launcher(V3) {0} =====" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Say ("[launcher] 工作目录：{0}" -f $Root)
$VersionLabel = "V3"
$VersionPath = Join-Path $Root "VERSION"
if (Test-Path $VersionPath) {
    $first = Get-Content $VersionPath -Encoding UTF8 | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1
    if ($first) { $VersionLabel = $first.Trim() }
}
Say ("[launcher] 版本：{0}（Telegram 里发 /version 可随时确认）" -f $VersionLabel)
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

# ---- 2. config.env 与 V3 档位 ----
$ConfigPath = Join-Path $Root "config.env"
if (-not (Test-Path $ConfigPath)) {
    Say "[launcher][ERROR] 缺少 config.env（请从 config.example.env 复制并填入 token 与密钥）。"
    exit 1
}
$v3Enabled = $false
$v3Mode = "observe"
foreach ($line in (Get-Content $ConfigPath -Encoding UTF8)) {
    if ($line -match '^\s*V3_ENABLED\s*=\s*(.+)$') {
        $v3Enabled = @("1", "true", "yes", "on") -contains $Matches[1].Trim().ToLower()
    }
    if ($line -match '^\s*V3_MODE\s*=\s*(.+)$') { $v3Mode = $Matches[1].Trim() }
}
if ($v3Enabled) {
    Say ("[launcher] V3：已启用（基础档 {0}；运行时覆盖见 /wakestatus）" -f $v3Mode)
} else {
    Say "[launcher] V3：未启用（V3_ENABLED=false，只会跑 V2 的聊天链路）"
}

# ---- 3. 停掉旧实例（V2 与 V3 共用 token，只能跑一个）----
$oldIds = Find-OldInstances -Root $Root
if ($oldIds.Count -eq 0) {
    Say "[launcher] 没有正在运行的旧实例"
} else {
    Say ("[launcher] 发现 {0} 个旧实例，开始替换" -f $oldIds.Count)
    Stop-Instances -Ids $oldIds -DryRun:$DryRun
}

# ---- 4. 启动 bot.py（最多两次：万一还有漏网的旧实例，第二次先清掉再起）----
$proc = $null
for ($attempt = 1; $attempt -le 2; $attempt++) {
    if ($DryRun) { Say "[launcher] 启动 bot.py ..."; break }
    $before = 0
    if (Test-Path $LogPath) { $before = @(Get-Content $LogPath -Encoding UTF8).Count }
    Add-Content -Path $LogPath -Value ("===== launcher(V3) {0} =====" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding UTF8
    Say ("[launcher] 启动 bot.py ...（第 {0} 次）" -f $attempt)
    $proc = Start-Process -FilePath $python -ArgumentList ('"{0}"' -f (Join-Path $Root "bot.py")) `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 10

    $newLines = @(Get-Content $LogPath -Encoding UTF8 | Select-Object -Skip $before)
    $blocked = $newLines | Select-String -Pattern "\[instance\] 已经有一个夕颜在运行：pid=(\d+)" |
        Select-Object -Last 1
    if ($blocked -and $attempt -eq 1) {
        $blockerPid = [int]$blocked.Matches[0].Groups[1].Value
        Say ("[launcher] 单实例锁被 PID {0} 占着，先清掉它再重来一次" -f $blockerPid)
        Stop-Instances -Ids @($blockerPid)
        $leftovers = Find-OldInstances -Root $Root
        if ($leftovers.Count -gt 0) { Stop-Instances -Ids $leftovers }
        continue
    }
    break
}

if ($DryRun) {
    Say "[launcher] 干跑结束（没有启动任何进程）"
    exit 0
}

# ---- 5. 自证 ----
Say "[launcher] ---- bot.log 末尾 ----"
if (Test-Path $LogPath) { Get-Content $LogPath -Tail 16 -Encoding UTF8 } else { Say "(没有 bot.log)" }

$instanceWarn = Select-String -Path $LogPath -Pattern "\[instance\] 已经有一个夕颜在运行" -Encoding UTF8 -ErrorAction SilentlyContinue |
    Select-Object -Last 1
if ($instanceWarn -and $attempt -ge 2) {
    Say "[launcher][ERROR] 清掉旧实例后仍然被单实例锁挡住，请手动结束那个进程。"
    exit 2
}

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
    exit 1
}

# ---- 6. Scheduler（V3 开启时才有意义）----
if (-not $v3Enabled) {
    Say "[launcher] V3 未启用，不启动 Scheduler。"
} elseif ($NoScheduler) {
    Say "[launcher] 按参数要求跳过 Scheduler。"
} else {
    Say "[launcher] 启动 Scheduler（常驻闹钟，独立进程，不调 LLM）..."
    if (Test-Path $SchedulerLog) { Remove-Item $SchedulerLog -ErrorAction SilentlyContinue }
    Start-Process -FilePath $python -ArgumentList ('"{0}"' -f (Join-Path $Root "scheduler.py")) `
        -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 3
    if (Test-Path $SchedulerLock) {
        try {
            $lock = Get-Content $SchedulerLock -Raw -Encoding UTF8 | ConvertFrom-Json
            Say ("[launcher] scheduler started（pid={0}，日志 scheduler.log）" -f $lock.pid)
        } catch {
            Say "[launcher] scheduler started（日志 scheduler.log）"
        }
    } else {
        Say "[launcher][WARN] 没看到 scheduler.lock，可能已经有一个调度器在跑，看 scheduler.log。"
    }
}

Say "[launcher] 夕颜 V3 已启动：日志 bot.log / scheduler.log；用 Telegram 发 /version 与 /wakestatus 确认"
exit 0
