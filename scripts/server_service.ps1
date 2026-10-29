<#
.SYNOPSIS
    SoloLedger 24/7 Windows Laptop / Mini-PC Server Auto-Start & NAS Backup Provisioner
.DESCRIPTION
    Commands:
      status           : Check registration status of SoloLedger scheduled tasks (read-only)
      install          : Register AtLogOn API server task + Daily 02:00 NAS backup task
      uninstall        : Remove registered SoloLedger scheduled tasks
      configure-power  : Set Windows AC power policy for 24/7 laptop server (never sleep, screen off 5m)
      start-background : Start FastAPI server in background without blocking terminal
      stop-background  : Stop background FastAPI server
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("status", "install", "uninstall", "configure-power", "start-background", "stop-background")]
    [string]$Action = "status",

    [int]$Port = 8000,
    [string]$BackupTime = "02:00"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$UvicornExe = Join-Path $RepoRoot ".venv\Scripts\uvicorn.exe"
$PidFile = Join-Path $RepoRoot "logs\server.pid"

$ApiTaskName = "SoloLedger-API-Server"
$NasTaskName = "SoloLedger-Daily-NAS-Backup"

function Get-TaskStatusSafe([string]$Name) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        return "NOT_INSTALLED"
    }
    return [string]$task.State
}

function Invoke-Status {
    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host " [SoloLedger 24/7 Server Service] Status" -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan
    $apiState = Get-TaskStatusSafe -Name $ApiTaskName
    $nasState = Get-TaskStatusSafe -Name $NasTaskName
    Write-Host "Repository Root        : $RepoRoot"
    Write-Host "API Auto-Start Task    : $ApiTaskName [$apiState]"
    Write-Host "Daily NAS Backup Task  : $NasTaskName [$nasState]"

    if (Test-Path $PidFile) {
        $bgPid = (Get-Content $PidFile -Raw).Trim()
        $proc = Get-Process -Id $bgPid -ErrorAction SilentlyContinue
        if ($null -ne $proc) {
            Write-Host "Background Server PID  : $bgPid (RUNNING)" -ForegroundColor Green
        } else {
            Write-Host "Background Server PID  : $bgPid (STOPPED)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "Background Server PID  : NONE"
    }
}

function Invoke-Install {
    if (-not (Test-Path $UvicornExe)) {
        throw "Uvicorn executable not found at $UvicornExe. Run 'uv sync' first."
    }

    Write-Host "[SoloLedger Server] Registering Windows Scheduled Tasks..." -ForegroundColor Cyan

    # 1. API Server Task (AtLogOn)
    $apiAction = New-ScheduledTaskAction `
        -Execute $UvicornExe `
        -Argument "sololedger.api:app --host 0.0.0.0 --port $Port" `
        -WorkingDirectory $RepoRoot
    $apiTrigger = New-ScheduledTaskTrigger -AtLogOn
    $apiSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask `
        -TaskName $ApiTaskName `
        -Action $apiAction `
        -Trigger $apiTrigger `
        -Settings $apiSettings `
        -Description "SoloLedger 24/7 FastAPI Webhook & Dashboard Server" `
        -Force | Out-Null
    Write-Host "  -> Registered: $ApiTaskName (AtLogOn, Port=$Port)" -ForegroundColor Green

    # 2. Daily NAS Backup Task (Daily at $BackupTime)
    $nasAction = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "-m sololedger.cli backup-nas" `
        -WorkingDirectory $RepoRoot
    $nasTrigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
    $nasSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries

    Register-ScheduledTask `
        -TaskName $NasTaskName `
        -Action $nasAction `
        -Trigger $nasTrigger `
        -Settings $nasSettings `
        -Description "SoloLedger Daily SQLite & Receipt Archive Sync to khomeNAS (Z:\)" `
        -Force | Out-Null
    Write-Host "  -> Registered: $NasTaskName (Daily at $BackupTime)" -ForegroundColor Green
}

function Invoke-Uninstall {
    Write-Host "[SoloLedger Server] Removing Windows Scheduled Tasks..." -ForegroundColor Cyan
    foreach ($tName in @($ApiTaskName, $NasTaskName)) {
        $existing = Get-ScheduledTask -TaskName $tName -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            Unregister-ScheduledTask -TaskName $tName -Confirm:$false
            Write-Host "  -> Removed: $tName" -ForegroundColor Green
        } else {
            Write-Host "  -> Already absent: $tName" -ForegroundColor DarkGray
        }
    }
}

function Invoke-ConfigurePower {
    Write-Host "[SoloLedger Server] Applying 24/7 Laptop AC Power Settings..." -ForegroundColor Cyan
    & powercfg /change standby-timeout-ac 0
    & powercfg /change hibernate-timeout-ac 0
    & powercfg /change monitor-timeout-ac 5
    Write-Host "  -> AC Standby=Never(0), Hibernate=Never(0), Monitor Off=5 min" -ForegroundColor Green
}

function Invoke-StartBackground {
    if (-not (Test-Path $UvicornExe)) {
        throw "Uvicorn executable not found at $UvicornExe."
    }
    $proc = Start-Process `
        -FilePath $UvicornExe `
        -ArgumentList "sololedger.api:app --host 0.0.0.0 --port $Port" `
        -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -Path $PidFile -Value $proc.Id -Encoding UTF8
    Write-Host "[SoloLedger Server] Started background server (PID=$($proc.Id), Port=$Port)" -ForegroundColor Green
}

function Invoke-StopBackground {
    if (Test-Path $PidFile) {
        $bgPid = (Get-Content $PidFile -Raw).Trim()
        Stop-Process -Id $bgPid -Force -ErrorAction SilentlyContinue
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        Write-Host "[SoloLedger Server] Stopped background server (PID=$bgPid)" -ForegroundColor Green
    } else {
        Write-Host "[SoloLedger Server] No PID file found at $PidFile" -ForegroundColor Yellow
    }
}

switch ($Action) {
    "status"           { Invoke-Status }
    "install"          { Invoke-Install }
    "uninstall"        { Invoke-Uninstall }
    "configure-power"  { Invoke-ConfigurePower }
    "start-background" { Invoke-StartBackground }
    "stop-background"  { Invoke-StopBackground }
}
