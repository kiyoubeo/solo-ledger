# PostToolUse hook: Audits every write_file / edit_file call into logs/tool_edit_audit.jsonl
$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

try {
    $rawInput = [Console]::In.ReadToEnd()
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    $LogsDir = Join-Path $RepoRoot "logs"
    if (-not (Test-Path $LogsDir)) {
        New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
    }
    $AuditFile = Join-Path $LogsDir "tool_edit_audit.jsonl"

    if (-not [string]::IsNullOrWhiteSpace($rawInput)) {
        $payload = $rawInput | ConvertFrom-Json
        $toolName = $payload.toolCall.name
        $targetPath = $payload.toolCall.args.Path
        # Ignore edits outside the workspace (e.g., brain artifact files)
        if ($targetPath -and ($targetPath -like "*SoloLedger*")) {
            $entry = [ordered]@{
                timestamp      = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
                conversationId = $payload.conversationId
                stepIdx        = $payload.stepIdx
                tool           = $toolName
                path           = $targetPath
            }
            $jsonLine = ($entry | ConvertTo-Json -Compress)
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::AppendAllText($AuditFile, $jsonLine + "`n", $utf8NoBom)
        }
    }
} catch {
    # Never fail the tool execution on audit logging errors
}

Write-Output "{}"
