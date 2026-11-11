# uninstall-task.ps1 —— 拆掉保活，回到默认的「闲置即休眠」
param([string]$TaskName = "BoxKeepAlive", [switch]$PurgeLogs)

$root = Split-Path -Parent $PSCommandPath

foreach ($t in @($TaskName, "$TaskName-Watchdog")) {
    try {
        Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop
        Write-Host "已删除任务 $t"
    } catch {
        Write-Host "任务 $t 不存在或已删除"
    }
}

# 常驻进程可能还在跑（任务被删不会杀子进程）
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*keepalive.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
        Write-Host "已结束残留进程 PID=$($_.ProcessId)"
    }

if ($PurgeLogs) {
    $logs = Join-Path $root "logs"
    if (Test-Path $logs) { Remove-Item $logs -Recurse -Force; Write-Host "已清空日志 $logs" }
}

Write-Host "`n保活已撤。box 现在会在空闲后自己休眠；连睡 14 天会被回收。"
