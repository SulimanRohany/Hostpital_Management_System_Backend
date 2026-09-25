param(
    [string]$TaskName = "Hospital System Daily Database Backup",
    [string]$At = "02:00"
)

$ErrorActionPreference = "Stop"
$BackendDirectory = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $BackendDirectory "venv\Scripts\python.exe"
$Manage = Join-Path $BackendDirectory "manage.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment not found at $Python"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ('"{0}" backup_database' -f $Manage) `
    -WorkingDirectory $BackendDirectory
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Creates a dated database backup for the Hospital System." `
    -Force

Write-Host "Scheduled '$TaskName' to run daily at $At."
