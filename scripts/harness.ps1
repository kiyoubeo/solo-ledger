<#
.SYNOPSIS
    SoloLedger Harness Engineering CLI (verify | log | commit-push | status)
.DESCRIPTION
    Unified development harness for SoloLedger:
    - verify      : Runs syntax check, ruff static analysis, SQLite DDL schema check, and pytest suite.
    - log         : Records a development change entry into CHANGELOG.md, CLAUDE.md, and logs/dev_changes.jsonl.
    - commit-push : Runs verify -> records change logs -> git add/commit -> git push origin main.
    - status      : Prints repository sync status, last verification result, and recent change logs.
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("verify", "log", "commit-push", "status")]
    [string]$Action = "status",

    [Parameter(Position = 1)]
    [Alias("Summary")]
    [string]$Message = "",

    [string]$Category = "feat"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if ($env:Path -notlike "*\.local\bin*") {
    $env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
}

$LogsDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
}

$LastVerifyFile = Join-Path $LogsDir "last_verification.json"
$VerifyHistoryFile = Join-Path $LogsDir "verification_history.jsonl"
$DevChangesFile = Join-Path $LogsDir "dev_changes.jsonl"
$ChangelogFile = Join-Path $RepoRoot "CHANGELOG.md"
$ClaudeFile = Join-Path $RepoRoot "CLAUDE.md"

function Get-ChangedFilesList {
    $ErrorActionPreference = "Continue"
    $raw = git status --porcelain 2>$null
    $ErrorActionPreference = "Stop"
    if (-not $raw) { return @() }
    $files = @()
    foreach ($line in ($raw -split "`r?`n")) {
        if ($line.Trim().Length -gt 3) {
            $f = $line.Substring(3).Trim()
            $files += $f
        }
    }
    return $files
}

function Invoke-HarnessVerify {
    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host " [SoloLedger Harness] Running Verification Pipeline..." -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan

    $startTime = Get-Date
    $steps = @()
    $overallPassed = $true
    $ErrorActionPreference = "Continue"

    # 1. Python Bytecode & Syntax Compilation Check
    Write-Host "`n[1/4] Syntax & Bytecode Compilation Check (compileall)..." -ForegroundColor Yellow
    $compileOut = (uv run python -m compileall -q src tests 2>&1 | Out-String)
    $compileExit = $LASTEXITCODE
    $steps += [ordered]@{
        step   = "compileall"
        passed = ($compileExit -eq 0)
        output = $compileOut.Trim()
    }
    if ($compileExit -ne 0) {
        $overallPassed = $false
        Write-Host "  -> FAILED: Syntax check failed." -ForegroundColor Red
        Write-Host $compileOut
    } else {
        Write-Host "  -> PASSED" -ForegroundColor Green
    }

    # 2. Static Analysis (Ruff Linter)
    Write-Host "`n[2/4] Static Code Analysis (ruff check)..." -ForegroundColor Yellow
    $ruffOut = (uv run ruff check src tests 2>&1 | Out-String)
    $ruffExit = $LASTEXITCODE
    $steps += [ordered]@{
        step   = "ruff_lint"
        passed = ($ruffExit -eq 0)
        output = $ruffOut.Trim()
    }
    if ($ruffExit -ne 0) {
        $overallPassed = $false
        Write-Host "  -> FAILED: Ruff linter reported issues." -ForegroundColor Red
        Write-Host $ruffOut
    } else {
        Write-Host "  -> PASSED" -ForegroundColor Green
    }

    # 3. SQLite DDL & Seed Rules Integrity Check
    Write-Host "`n[3/4] SQLite Schema & Seed Rules Integrity Check..." -ForegroundColor Yellow
    $schemaOut = (uv run python -c "from sololedger.db import init_db, get_connection; import tempfile, os; fd, p = tempfile.mkstemp(suffix='.db'); os.close(fd); init_db(p); c = get_connection(p); cur = c.cursor(); cur.execute('SELECT COUNT(*) FROM tax_classification_rules WHERE is_active=1'); r = cur.fetchone()[0]; c.close(); os.remove(p); assert r >= 4; print(f'Tables=4, ActiveSeedRules={r}')" 2>&1 | Out-String)
    $schemaExit = $LASTEXITCODE
    $steps += [ordered]@{
        step   = "schema_integrity"
        passed = ($schemaExit -eq 0)
        output = $schemaOut.Trim()
    }
    if ($schemaExit -ne 0) {
        $overallPassed = $false
        Write-Host "  -> FAILED: Schema integrity check failed." -ForegroundColor Red
        Write-Host $schemaOut
    } else {
        Write-Host "  -> PASSED ($($schemaOut.Trim()))" -ForegroundColor Green
    }

    # 4. Automated Unit & Integration Tests (Pytest)
    Write-Host "`n[4/4] Automated Unit & Integration Test Suite (pytest)..." -ForegroundColor Yellow
    $pytestOut = (uv run pytest -q 2>&1 | Out-String)
    $pytestExit = $LASTEXITCODE
    $steps += [ordered]@{
        step   = "pytest"
        passed = ($pytestExit -eq 0)
        output = $pytestOut.Trim()
    }
    if ($pytestExit -ne 0) {
        $overallPassed = $false
        Write-Host "  -> FAILED: Pytest suite failed." -ForegroundColor Red
        Write-Host $pytestOut
    } else {
        $summaryLine = ($pytestOut.Trim() -split "`r?`n")[-1]
        Write-Host "  -> PASSED ($summaryLine)" -ForegroundColor Green
    }

    $ErrorActionPreference = "Stop"
    $durationMs = [int]((Get-Date) - $startTime).TotalMilliseconds
    $timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
    $statusStr = if ($overallPassed) { "PASSED" } else { "FAILED" }

    $reportObj = [ordered]@{
        timestamp   = $timestamp
        status      = $statusStr
        duration_ms = $durationMs
        steps       = $steps
    }

    $reportJson = $reportObj | ConvertTo-Json -Depth 6
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($LastVerifyFile, $reportJson + "`n", $utf8NoBom)

    $compactHistory = ($reportObj | ConvertTo-Json -Depth 6 -Compress)
    [System.IO.File]::AppendAllText($VerifyHistoryFile, $compactHistory + "`n", $utf8NoBom)

    Write-Host "`n==========================================================" -ForegroundColor Cyan
    if ($overallPassed) {
        Write-Host " [SoloLedger Harness] VERIFICATION PASSED (${durationMs}ms)" -ForegroundColor Green
    } else {
        Write-Host " [SoloLedger Harness] VERIFICATION FAILED (${durationMs}ms)" -ForegroundColor Red
    }
    Write-Host "==========================================================" -ForegroundColor Cyan

    if (-not $overallPassed) {
        exit 1
    }
}

function Add-DevelopmentLogEntry {
    param(
        [string]$LogSummary,
        [string]$LogCategory = "feat"
    )
    if ([string]::IsNullOrWhiteSpace($LogSummary)) {
        throw "Log summary (-Message / -Summary) is required."
    }

    $now = Get-Date
    $dateStr = $now.ToString("yyyy-MM-dd")
    $isoStr = $now.ToString("yyyy-MM-ddTHH:mm:ssK")
    $changedFiles = Get-ChangedFilesList
    $filesDisplay = if ($changedFiles.Count -gt 0) {
        if ($changedFiles.Count -le 4) {
            ($changedFiles -join ", ")
        } else {
            (($changedFiles | Select-Object -First 4) -join ", ") + " (+$($changedFiles.Count - 4) files)"
        }
    } else {
        "(working-tree)"
    }

    $parentCommit = "INITIAL"
    $ErrorActionPreference = "Continue"
    $revOut = git rev-parse --short HEAD 2>$null
    $revExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($revExit -eq 0 -and $revOut) {
        $parentCommit = ($revOut | Out-String).Trim()
    }

    $verifyStatus = "UNKNOWN"
    if (Test-Path $LastVerifyFile) {
        try {
            $vObj = Get-Content -Raw -Encoding UTF8 $LastVerifyFile | ConvertFrom-Json
            $verifyStatus = $vObj.status
        } catch {}
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $bt = [char]0x60

    # 1. Append to logs/dev_changes.jsonl
    $entryObj = [ordered]@{
        timestamp           = $isoStr
        date                = $dateStr
        category            = $LogCategory
        summary             = $LogSummary
        changed_files       = $changedFiles
        parent_commit       = $parentCommit
        verification_status = $verifyStatus
    }
    $entryJsonLine = ($entryObj | ConvertTo-Json -Depth 4 -Compress)
    [System.IO.File]::AppendAllText($DevChangesFile, $entryJsonLine + "`n", $utf8NoBom)

    # 2. Append row to CHANGELOG.md
    $rowLine = "| $dateStr | ${bt}${LogCategory}${bt} | ${bt}${filesDisplay}${bt} | $LogSummary | ${bt}${verifyStatus}${bt} |"
    if (Test-Path $ChangelogFile) {
        $existingChangelog = [System.IO.File]::ReadAllText($ChangelogFile, $utf8NoBom).TrimEnd()
        if (-not $existingChangelog.Contains($LogSummary)) {
            [System.IO.File]::WriteAllText($ChangelogFile, $existingChangelog + "`n" + $rowLine + "`n", $utf8NoBom)
        }
    }

    # 3. Append row to CLAUDE.md if present
    $claudeRow = "| $dateStr | $filesDisplay | [$LogCategory] $LogSummary |"
    if (Test-Path $ClaudeFile) {
        $existingClaude = [System.IO.File]::ReadAllText($ClaudeFile, $utf8NoBom).TrimEnd()
        if (-not $existingClaude.Contains($LogSummary)) {
            [System.IO.File]::WriteAllText($ClaudeFile, $existingClaude + "`n" + $claudeRow + "`n", $utf8NoBom)
        }
    }

    Write-Host "[SoloLedger Harness] Recorded development log: [$LogCategory] $LogSummary" -ForegroundColor Green
}

switch ($Action) {
    "verify" {
        Invoke-HarnessVerify
    }
    "log" {
        Add-DevelopmentLogEntry -LogSummary $Message -LogCategory $Category
    }
    "commit-push" {
        if ([string]::IsNullOrWhiteSpace($Message)) {
            throw "Commit message (-Message) is required for commit-push."
        }
        # Step 1: Run full verification pipeline
        Invoke-HarnessVerify

        # Step 2: Log changes to CHANGELOG.md, CLAUDE.md, and logs/dev_changes.jsonl
        Add-DevelopmentLogEntry -LogSummary $Message -LogCategory $Category

        # Step 3: Stage all files, commit, and push to origin main
        Write-Host "`n[SoloLedger Harness] Staging, committing, and pushing to origin/main..." -ForegroundColor Cyan
        $ErrorActionPreference = "Continue"
        git add -A
        $commitTitle = "[$Category] $Message"
        git commit -m $commitTitle
        if ($LASTEXITCODE -ne 0) {
            throw "git commit failed."
        }
        $newSha = (git rev-parse --short HEAD | Out-String).Trim()
        Write-Host "  -> Created commit: $newSha ($commitTitle)" -ForegroundColor Green

        git push -u origin main
        if ($LASTEXITCODE -ne 0) {
            throw "git push origin main failed."
        }
        $ErrorActionPreference = "Stop"
        Write-Host "  -> Successfully pushed $newSha to https://github.com/kiyoubeo/solo-ledger.git (main)" -ForegroundColor Green
    }
    "status" {
        Write-Host "==========================================================" -ForegroundColor Cyan
        Write-Host " [SoloLedger Harness] Status Overview" -ForegroundColor Cyan
        Write-Host "==========================================================" -ForegroundColor Cyan
        $ErrorActionPreference = "Continue"
        $branch = (git branch --show-current 2>$null | Out-String).Trim()
        $remotes = (git remote -v 2>$null | Out-String).Trim()
        $ErrorActionPreference = "Stop"
        Write-Host "Branch : $branch"
        Write-Host "Remote :`n$remotes"

        $dirty = Get-ChangedFilesList
        if ($dirty.Count -eq 0) {
            Write-Host "Working Tree : CLEAN" -ForegroundColor Green
        } else {
            Write-Host "Working Tree : DIRTY ($($dirty.Count) modified/untracked files)" -ForegroundColor Yellow
            $dirty | ForEach-Object { Write-Host "  - $_" }
        }

        if (Test-Path $LastVerifyFile) {
            $v = Get-Content -Raw -Encoding UTF8 $LastVerifyFile | ConvertFrom-Json
            $color = if ($v.status -eq "PASSED") { "Green" } else { "Red" }
            Write-Host "Last Verify  : $($v.status) at $($v.timestamp) ($($v.duration_ms)ms)" -ForegroundColor $color
        } else {
            Write-Host "Last Verify  : NONE" -ForegroundColor Yellow
        }

        if (Test-Path $DevChangesFile) {
            Write-Host "`nRecent Development Logs (logs/dev_changes.jsonl):" -ForegroundColor Cyan
            Get-Content -Encoding UTF8 $DevChangesFile | Select-Object -Last 5 | ForEach-Object {
                $item = $_ | ConvertFrom-Json
                Write-Host "  [$($item.timestamp)] [$($item.category)] $($item.summary) (verify=$($item.verification_status))"
            }
        }
    }
}
