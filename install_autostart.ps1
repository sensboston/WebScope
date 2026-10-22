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
$ErrorActionPreference = "Stop"

$dir = $PSScriptRoot
if (-not $Python) { $Python = Join-Path (Split-Path (Split-Path $dir)) "$User\python312\python.exe" }
if (-not (Test-Path $Python)) { $Python = (Get-Command python).Source }
New-Item -ItemType Directory -Force $LogDir | Out-Null
$log = Join-Path $LogDir "server.log"

# A task that runs without a logon needs the "Log on as a batch job" right, which plain users don't have.
$sid = (New-Object System.Security.Principal.NTAccount($User)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$cfg = Join-Path $env:TEMP "rigolweb_rights.inf"
secedit /export /cfg $cfg /areas USER_RIGHTS | Out-Null
$line = (Get-Content $cfg | Where-Object { $_ -like "SeBatchLogonRight*" })
if (-not $line -or $line -notmatch [regex]::Escape("*$sid")) {
    $new = if ($line) { "$line,*$sid" } else { "SeBatchLogonRight = *$sid" }
    @("[Unicode]", "Unicode=yes", "[Version]", 'signature="$CHICAGO$"', "Revision=1", "[Privilege Rights]", $new) |
        Out-File $cfg -Encoding unicode
    secedit /configure /db (Join-Path $env:TEMP "rigolweb_rights.sdb") /cfg $cfg /areas USER_RIGHTS | Out-Null
    Write-Host "Granted 'Log on as a batch job' to $User"
}
Remove-Item $cfg -ErrorAction SilentlyContinue

# The task runs this loop, so a crashed server comes back in 5 s (Task Scheduler only retries failed starts).
$runner = Join-Path $dir "run_server.cmd"
@(
    "@echo off",
    "cd /d `"$dir`"",
    ":loop",
    "`"$Python`" -u server.py --host 0.0.0.0 --port $Port >> `"$log`" 2>&1",
    "timeout /t 5 /nobreak >nul",
    "goto loop"
) | Out-File $runner -Encoding ascii

$cred = Get-Credential -UserName $User -Message "Password of user $User (the task runs as this user)"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$state = (Get-ScheduledTask -TaskName $TaskName).State
$report = "state=$state lastRun=$($info.LastRunTime) lastResult=0x{0:X}" -f $info.LastTaskResult
$report | Out-File (Join-Path $LogDir "task_status.txt") -Encoding ascii
Write-Host "Task '$TaskName': $report"
Write-Host "Server log: $log"
