#Requires -Version 5.1
<# Stops and removes the PolybotPaper scheduled task. The database and logs are kept. #>
param([string]$TaskName = "PolybotPaper")
$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed. Data in out\ is untouched." -ForegroundColor Green
} else {
    Write-Host "No task named '$TaskName'."
}
