# Register a scheduled task that starts the web panel at boot, as the given user, without a logon.
# Run once from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 [-User leo] [-Port 8765]
# The server output goes to <LogDir>\server.log, the task state after start to <LogDir>\task_status.txt.
param(
    [string]$User = $env:USERNAME,
    [int]$Port = 8765,
    [string]$Python = "",
    [string]$TaskName = "Rigol Web Panel",
    [string]$LogDir = "C:\ProgramData\RigolWeb"
)

$dir = $PSScriptRoot
if (-not $Python) { $Python = Join-Path (Split-Path (Split-Path $dir)) "$User\python312\python.exe" }
if (-not (Test-Path $Python)) { $Python = (Get-Command python -ErrorAction Stop).Source }
New-Item -ItemType Directory -Force $LogDir | Out-Null
$log = Join-Path $LogDir "server.log"

$cred = Get-Credential -UserName $User -Message "Password of user $User (the task runs as this user)"
# cmd.exe only to redirect the server output into the log
$action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $dir `
    -Argument "/c `"`"$Python`" -u server.py --host 0.0.0.0 --port $Port >> `"$log`" 2>&1`""
$trigger = New-ScheduledTaskTrigger -AtStartup
# no time limit, restart a crashed server after a minute
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$state = (Get-ScheduledTask -TaskName $TaskName).State
$report = "state=$state lastRun=$($info.LastRunTime) lastResult=0x{0:X}" -f $info.LastTaskResult
$report | Out-File (Join-Path $LogDir "task_status.txt") -Encoding ascii
Write-Host "Task '$TaskName' registered: $report"
Write-Host "Server log: $log"
