# Stop hook: Ensures all edits are verified, logged, committed, and pushed before turn completion
$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $rawInput = [Console]::In.ReadToEnd()
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    Set-Location $RepoRoot

    if (-not [string]::IsNullOrWhiteSpace($rawInput)) {
        $payload = $rawInput | ConvertFrom-Json
        if ($payload.terminationReason -and $payload.terminationReason -ne "NO_TOOL_CALL") {
            Write-Output '{"decision":"allow"}'
            exit 0
        }
    }

    $dirty = git status --porcelain 2>$null
    $branchStatus = git status -sb 2>$null | Select-Object -First 1

    $hasUncommitted = (-not [string]::IsNullOrWhiteSpace(($dirty | Out-String)))
    $hasUnpushed = ($branchStatus -match "\[ahead\s+\d+\]")

    if ($hasUncommitted -or $hasUnpushed) {
        $LogsDir = Join-Path $RepoRoot "logs"
        $StateFile = Join-Path $LogsDir "stop_guard_state.json"
        $retryCount = 0
        if (Test-Path $StateFile) {
            try {
                $st = Get-Content -Raw -Encoding UTF8 $StateFile | ConvertFrom-Json
                $retryCount = [int]$st.retryCount
            } catch {}
        }

        if ($retryCount -lt 2) {
            $newState = @{ retryCount = ($retryCount + 1); timestamp = (Get-Date).ToString("o") } | ConvertTo-Json -Compress
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($StateFile, $newState, $utf8NoBom)

            $resp = [ordered]@{
                decision = "continue"
                reason   = "[SoloLedger Harness Guard] Uncommitted changes or unpushed commits detected. Run: powershell -ExecutionPolicy Bypass -File scripts/harness.ps1 commit-push -Message '<summary>' -Category '<feat|fix|docs|chore>' to verify, log changes, commit, and push to origin/main before ending the turn."
            } | ConvertTo-Json -Compress
            Write-Output $resp
            exit 0
        }
    } else {
        $StateFile = Join-Path $RepoRoot "logs\stop_guard_state.json"
        if (Test-Path $StateFile) {
            Remove-Item -Force $StateFile -ErrorAction SilentlyContinue
        }
    }
} catch {
    # Fallback allow on unexpected script errors
}

Write-Output '{"decision":"allow"}'
