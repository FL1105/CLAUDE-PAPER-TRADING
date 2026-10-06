#Requires -Version 5.1
<#
.SYNOPSIS
  Keep `polybot paper` running: a Windows scheduled task that starts at log-on and
  restarts the loop within a minute if it ever crashes.

.DESCRIPTION
  Run from the repository folder (no admin needed):
      powershell -ExecutionPolicy Bypass -File .\scripts\windows\install-paper-task.ps1 -NoSleep

  Output goes to out\paper.log. Manage the task with:
      Get-ScheduledTask PolybotPaper | Get-ScheduledTaskInfo
      Stop-ScheduledTask PolybotPaper ; Start-ScheduledTask PolybotPaper
      .\scripts\windows\uninstall-paper-task.ps1

.PARAMETER NoSleep
  Stop the PC from sleeping while plugged in (the run needs ~30 days of uptime).
#>
param(
    [string]$TaskName = "PolybotPaper",
    [switch]$NoSleep
)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = Join-Path $Root ".venv\Scripts\polybot.exe"
if (-not (Test-Path $Exe)) { throw "polybot is not installed in $Root\.venv. Run setup.ps1 first." }
$OutDir = Join-Path $Root "out"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Log = Join-Path $OutDir "paper.log"

# cmd.exe sets UTF-8 for Python and appends all output to the log file.
$Arg = "/c set PYTHONUTF8=1&& `"$Exe`" paper >> `"$Log`" 2>&1"
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $Arg -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "polybot forward paper-trading loop (no real money)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' registered and started. Log: $Log" -ForegroundColor Green

if ($NoSleep) {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    Write-Host "Sleep and hibernate disabled while plugged in." -ForegroundColor Green
} else {
    Write-Host "Tip: re-run with -NoSleep, or the PC sleeping will leave gaps in the forward run." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Watch it:   Get-Content `"$Log`" -Tail 20 -Wait"
Write-Host "Check it:   .\.venv\Scripts\polybot.exe doctor"
