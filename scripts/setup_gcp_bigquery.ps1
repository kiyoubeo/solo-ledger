<#
.SYNOPSIS
    One-click Google Cloud SDK / ADC Setup & BigQuery Property Graph Provisioner for SoloLedger
.DESCRIPTION
    Checks/installs Google Cloud SDK, ensures Application Default Credentials (ADC) login,
    enables BigQuery API, writes SOLOLEDGER_BQ_PROJECT_ID / SOLOLEDGER_BQ_DATASET_ID into .env,
    creates the BigQuery dataset & Property Graph DDL, and optionally loads the current ledger.
#>
param(
    [string]$ProjectId = "",
    [string]$DatasetId = "sololedger_analytics",
    [string]$Location = "asia-northeast3",
    [switch]$InstallSdk,
    [switch]$SkipLoad
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Resolve-GcloudBin {
    $cmd = Get-Command gcloud -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin",
        "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin",
        "C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin"
    )
    foreach ($dir in $candidates) {
        $exe = Join-Path $dir "gcloud.cmd"
        if (Test-Path $exe) {
            $env:Path = "$dir;$env:Path"
            return $exe
        }
    }
    return $null
}

function Set-EnvFileKey([string]$EnvPath, [string]$Key, [string]$Value) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $lines = @()
    if (Test-Path $EnvPath) {
        $lines = [System.IO.File]::ReadAllLines($EnvPath, $utf8NoBom)
    }
    $found = $false
    $updated = @()
    foreach ($ln in $lines) {
        if ($ln -match "^\s*$([regex]::Escape($Key))\s*=") {
            $updated += "$Key=$Value"
            $found = $true
        } else {
            $updated += $ln
        }
    }
    if (-not $found) {
        $updated += "$Key=$Value"
    }
    [System.IO.File]::WriteAllText($EnvPath, ($updated -join "`n") + "`n", $utf8NoBom)
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " [SoloLedger GCP] BigQuery Property Graph Setup" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$gcloudExe = Resolve-GcloudBin
if (-not $gcloudExe) {
    if ($InstallSdk) {
        Write-Host "[1/6] Installing Google Cloud SDK via winget..." -ForegroundColor Yellow
        winget install -e --id Google.CloudSDK --accept-package-agreements --accept-source-agreements
        $gcloudExe = Resolve-GcloudBin
    }
    if (-not $gcloudExe) {
        Write-Host "[ERROR] Google Cloud SDK (gcloud) is not installed on this PC." -ForegroundColor Red
        Write-Host "Install it using one of the following options and re-run this script:" -ForegroundColor Yellow
        Write-Host "  1) powershell -ExecutionPolicy Bypass -File scripts/setup_gcp_bigquery.ps1 -InstallSdk -ProjectId <YOUR_GCP_PROJECT_ID>" -ForegroundColor Cyan
        Write-Host "  2) winget install -e --id Google.CloudSDK" -ForegroundColor Cyan
        exit 1
    }
}
Write-Host "[1/6] gcloud CLI found: $gcloudExe" -ForegroundColor Green

if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    $ErrorActionPreference = "Continue"
    $detectedProj = (& gcloud config get-value project 2>$null | Out-String).Trim()
    $ErrorActionPreference = "Stop"
    if ($detectedProj -and $detectedProj -ne "(unset)") {
        $ProjectId = $detectedProj
    }
}
if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    throw "GCP Project ID is required. Pass -ProjectId <YOUR_PROJECT_ID> or run 'gcloud config set project <YOUR_PROJECT_ID>'."
}
Write-Host "[2/6] Target Project : $ProjectId (Dataset: $DatasetId, Location: $Location)" -ForegroundColor Green

$AdcPath = Join-Path $env:APPDATA "gcloud\application_default_credentials.json"
if (-not (Test-Path $AdcPath)) {
    Write-Host "[3/6] Launching browser login for Application Default Credentials (ADC)..." -ForegroundColor Yellow
    & gcloud auth application-default login
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $AdcPath)) {
        throw "ADC login failed or credentials file not created at $AdcPath."
    }
} else {
    Write-Host "[3/6] ADC credentials found: $AdcPath" -ForegroundColor Green
}

& gcloud auth application-default set-quota-project $ProjectId 2>$null | Out-Null

Write-Host "[4/6] Enabling bigquery.googleapis.com API..." -ForegroundColor Yellow
& gcloud services enable bigquery.googleapis.com --project $ProjectId
if ($LASTEXITCODE -ne 0) {
    throw "Failed to enable bigquery.googleapis.com on project $ProjectId."
}

Write-Host "[5/6] Updating .env configuration..." -ForegroundColor Yellow
$RootEnv = Join-Path $RepoRoot ".env"
Set-EnvFileKey -EnvPath $RootEnv -Key "SOLOLEDGER_BQ_PROJECT_ID" -Value $ProjectId
Set-EnvFileKey -EnvPath $RootEnv -Key "SOLOLEDGER_BQ_DATASET_ID" -Value $DatasetId

$DistProdDb = Join-Path $RepoRoot "dist\sololedger_prod.db"
if (Test-Path $DistProdDb) {
    $DistEnv = Join-Path $RepoRoot "dist\.env"
    if (Test-Path $DistEnv) {
        Set-EnvFileKey -EnvPath $DistEnv -Key "SOLOLEDGER_BQ_PROJECT_ID" -Value $ProjectId
        Set-EnvFileKey -EnvPath $DistEnv -Key "SOLOLEDGER_BQ_DATASET_ID" -Value $DatasetId
    }
}

Write-Host "[6/6] Provisioning BigQuery dataset, Property Graph DDL, and syncing ledger..." -ForegroundColor Yellow
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    throw "Virtualenv python not found at $PythonExe. Run 'uv sync --extra dev' first."
}

$env:SOLOLEDGER_BQ_PROJECT_ID = $ProjectId
$env:SOLOLEDGER_BQ_DATASET_ID = $DatasetId
$skipFlag = if ($SkipLoad) { "1" } else { "0" }
$env:SOLOLEDGER_SETUP_SKIP_LOAD = $skipFlag

$pyScript = @'
import json
import os
from pathlib import Path
from sololedger.bq_graph_exporter import (
    export_ledger_graph_to_bigquery,
    provision_bigquery_dataset_and_graph,
)
from sololedger.db import get_connection, init_db
from sololedger.models import BigQueryGraphExportRequest

proj = os.environ["SOLOLEDGER_BQ_PROJECT_ID"]
ds = os.environ["SOLOLEDGER_BQ_DATASET_ID"]
skip_load = os.environ.get("SOLOLEDGER_SETUP_SKIP_LOAD", "0") == "1"

prov = provision_bigquery_dataset_and_graph(project_id=proj, dataset_id=ds)
print("PROVISION:", json.dumps(prov, ensure_ascii=False))

if not skip_load:
    dist_db = Path("dist/sololedger_prod.db")
    target_db = str(dist_db) if dist_db.exists() else None
    init_db(target_db)
    conn = get_connection(target_db)
    try:
        res = export_ledger_graph_to_bigquery(
            conn.cursor(),
            request=BigQueryGraphExportRequest(
                project_id=proj,
                dataset_id=ds,
                execute_bigquery_load=True,
                run_gql_audit=True,
            ),
        )
        print("EXPORT:", json.dumps({
            "status": res["status"],
            "node_counts": res["node_counts"],
            "edge_counts": res["edge_counts"],
            "bigquery_load": res.get("bigquery_load"),
            "gql_audit_rows": res.get("gql_audit_rows"),
        }, ensure_ascii=False))
    finally:
        conn.close()
'@

$pyScript | & $PythonExe -
if ($LASTEXITCODE -ne 0) {
    throw "BigQuery provisioning/export python script failed."
}

Write-Host "`n[COMPLETE] BigQuery Property Graph is ready for $ProjectId.$DatasetId" -ForegroundColor Green
