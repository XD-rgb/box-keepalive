# install-task.ps1 —— 注册 Windows 计划任务，让 box 保活常驻
#   powershell -ExecutionPolicy Bypass -File install-task.ps1
#   powershell -ExecutionPolicy Bypass -File install-task.ps1 -IntervalMinutes 4 -Test
param(
    [string]$TaskName = "BoxKeepAlive",
    [int]$IntervalMinutes = 4,
    [switch]$Test
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSCommandPath
$script = Join-Path $root "keepalive.py"
if (-not (Test-Path $script)) { throw "找不到 $script" }

# 1. 挑一个不弹黑框的解释器
$candidates = @(
    "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe",
    "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.11.9\pythonw.exe"
)
$pythonw = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pythonw) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source } else { throw "找不到 pythonw.exe" }
}
Write-Host "解释器 : $pythonw"
Write-Host "脚本   : $script"

# 2. 先干跑一轮，确认 token 和网络没问题
Write-Host "`n--- 预检 ---"
$py = $pythonw -replace 'pythonw\.exe$', 'python.exe'
& $py $script --once -v
if ($LASTEXITCODE -ne 0) { throw "预检失败，退出码 $LASTEXITCODE，先修好再装" }
if ($Test) { Write-Host "`n-Test 模式：预检通过，不注册任务"; return }

# 3. 注册
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$script`" --loop" -WorkingDirectory $root
$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -DontStopOnIdleEnd

$registered = $false
foreach ($logon in @("S4U", "Interactive")) {
    try {
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
            -LogonType $logon -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
            -Settings $settings -Principal $principal -Force `
            -Description "让 box by Simular 云容器全天运行：每 $IntervalMinutes 分钟一次的兜底轮询 + 常驻保活循环" | Out-Null
        Write-Host "已注册（LogonType=$logon）"
        $registered = $true
        break
    } catch {
        Write-Warning "LogonType=$logon 注册失败：$($_.Exception.Message)"
    }
}
if (-not $registered) { throw "计划任务注册失败" }

# 4. 兜底：再挂一个每 N 分钟的看门狗，万一常驻进程被系统杀掉
$wdArg = "`"$script`" --once"
schtasks /Create /F /SC MINUTE /MO $IntervalMinutes /TN "$TaskName-Watchdog" `
    /TR "`"$pythonw`" $wdArg" /RL HIGHEST 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { Write-Host "已注册看门狗（每 $IntervalMinutes 分钟 --once）" }
else { Write-Warning "看门狗注册失败，主任务仍可用" }

# 5. 立刻跑起来
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 12

Write-Host "`n--- 任务状态 ---"
Get-ScheduledTask -TaskName $TaskName, "$TaskName-Watchdog" |
    Select-Object TaskName, State | Format-Table -AutoSize
Get-ScheduledTaskInfo -TaskName $TaskName |
    Select-Object LastRunTime, LastTaskResult, NextRunTime | Format-List

Write-Host "--- 最近日志 ---"
$log = Get-ChildItem (Join-Path $root "logs") -Filter "keepalive-*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) { Get-Content $log.FullName -Tail 6 } else { Write-Warning "还没有日志" }
