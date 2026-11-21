<#
.SYNOPSIS
    Non-destructive Pull Backup from Google Cloud Storage to Local PC / ipDISK NAS (Z:\SoloLedger).
.DESCRIPTION
    Mirrors SoloLedger SQLite snapshots, receipts, and exports from GCS down to local NAS storage
    without opening router ports and without deleting existing local archive files.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Bucket,

    [string]$Prefix = "sololedger",

    [string]$TargetNasDir = "Z:\HDD1\Data\SoloLedger",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$cleanBucket = ($Bucket.Trim() -replace '^gs://', '').Trim("/")
$cleanPrefix = $Prefix.Trim().Replace("\", "/").Trim("/")
$sourceUri = if ($cleanPrefix) { "gs://$cleanBucket/$cleanPrefix" } else { "gs://$cleanBucket" }

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "Google Cloud CLI ('gcloud') is not installed or not on PATH."
}

if (-not $DryRun -and -not (Test-Path $TargetNasDir)) {
    New-Item -ItemType Directory -Force -Path $TargetNasDir | Out-Null
}

Write-Host "[SoloLedger Pull Backup] Source : $sourceUri" -ForegroundColor Cyan
Write-Host "[SoloLedger Pull Backup] Target : $TargetNasDir" -ForegroundColor Cyan

# Strictly additive rsync (no --delete-unmatched-destination-objects) to protect historical archives
$argsList = @("storage", "rsync", $sourceUri, $TargetNasDir, "--recursive")
if ($DryRun) {
    $argsList += "--dry-run"
    Write-Host "[SoloLedger Pull Backup] Mode   : DRY-RUN" -ForegroundColor Yellow
}

& gcloud @argsList
if ($LASTEXITCODE -ne 0) {
    throw "gcloud storage rsync failed with exit code $LASTEXITCODE"
}

Write-Host "[SoloLedger Pull Backup] Completed successfully." -ForegroundColor Green
