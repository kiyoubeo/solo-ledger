<#
.SYNOPSIS
    Build Standalone SoloLedger Windows Desktop Executable (SoloLedger.exe) via PyInstaller
.DESCRIPTION
    Packages FastAPI + SQLite + Dashboard + pywebview (Edge WebView2) into a native
    windowless Windows desktop executable (dist\SoloLedger.exe) with custom icon,
    creates a convenient launcher shortcut, and runs an automated readiness self-check.
#>
param(
    [ValidateSet("onefile", "onedir")]
    [string]$Mode = "onefile",

    [switch]$SkipVerify,
    [switch]$CreateDesktopShortcut
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$PyInstallerExe = Join-Path $RepoRoot ".venv\Scripts\pyinstaller.exe"
$EntryScript = Join-Path $RepoRoot "scripts\desktop_entry.py"
$IconPath = Join-Path $RepoRoot "assets\sololedger.ico"
$BuildDir = Join-Path $RepoRoot "build"
$DistDir = Join-Path $RepoRoot "dist"

if (-not (Test-Path $PyInstallerExe)) {
    throw "PyInstaller not found at $PyInstallerExe. Run 'uv sync --extra dev' first."
}

if (-not (Test-Path $IconPath)) {
    Write-Host "[SoloLedger Desktop] Generating application icon..." -ForegroundColor Cyan
    & $PythonExe "scripts\generate_icon.py"
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " [SoloLedger Desktop] Building Standalone Windows App ($Mode)" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$modeFlag = if ($Mode -eq "onefile") { "--onefile" } else { "--onedir" }

$SchemaFile = Join-Path $RepoRoot "sql\schema.sql"
$ManualFile = Join-Path $RepoRoot "docs\USER_MANUAL.html"

$pyiArgs = @(
    "--noconfirm",
    "--clean",
    "--windowed",
    $modeFlag,
    "--name", "SoloLedger",
    "--icon", $IconPath,
    "--paths", (Join-Path $RepoRoot "src"),
    "--specpath", $BuildDir,
    "--workpath", (Join-Path $BuildDir "pyinstaller"),
    "--distpath", $DistDir,
    "--add-data", "${SchemaFile};sql",
    "--add-data", "${ManualFile};docs",
    "--add-data", "${IconPath};assets",
    "--hidden-import", "webview.platforms.edgechromium",
    "--hidden-import", "webview.platforms.winforms",
    $EntryScript
)

& $PyInstallerExe @pyiArgs
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE."
}

$ExePath = if ($Mode -eq "onefile") {
    Join-Path $DistDir "SoloLedger.exe"
} else {
    Join-Path $DistDir "SoloLedger\SoloLedger.exe"
}

if (-not (Test-Path $ExePath)) {
    throw "Expected executable not found at $ExePath"
}

$sizeMb = [Math]::Round((Get-Item $ExePath).Length / 1MB, 2)
Write-Host "`n[Build Complete] $ExePath (${sizeMb} MB)" -ForegroundColor Green

if (-not $SkipVerify) {
    Write-Host "[Verify] Running headless self-check on compiled SoloLedger.exe..." -ForegroundColor Yellow
    $SmokeJson = Join-Path $BuildDir "desktop_smoke_check.json"
    if (Test-Path $SmokeJson) {
        Remove-Item $SmokeJson -Force
    }
    $proc = Start-Process `
        -FilePath $ExePath `
        -ArgumentList "--check-only --port 18765 --check-output `"$SmokeJson`"" `
        -WorkingDirectory $RepoRoot `
        -PassThru `
        -Wait

    if ($proc.ExitCode -ne 0 -or -not (Test-Path $SmokeJson)) {
        throw "Compiled SoloLedger.exe failed self-check (ExitCode=$($proc.ExitCode))."
    }
    $smokeData = Get-Content $SmokeJson -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($smokeData.status -ne "ok" -or $smokeData.dashboard_status -ne 200) {
        throw "Compiled SoloLedger.exe reported unhealthy status: $($smokeData | ConvertTo-Json -Compress)"
    }
    Write-Host "  -> PASSED (Version=v$($smokeData.version), Health=$($smokeData.health_status), Dashboard=$($smokeData.dashboard_status))" -ForegroundColor Green
}

function New-AppShortcut([string]$ShortcutPath, [string]$TargetExe, [string]$WorkDir, [string]$Icon) {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($ShortcutPath)
    $sc.TargetPath = $TargetExe
    $sc.WorkingDirectory = $WorkDir
    $sc.IconLocation = "$Icon,0"
    $sc.Description = "SoloLedger - 법인 회계·세무증빙 데스크톱 원장"
    $sc.Save()
}

$RootShortcut = Join-Path $RepoRoot "SoloLedger.lnk"
New-AppShortcut -ShortcutPath $RootShortcut -TargetExe $ExePath -WorkDir $RepoRoot -Icon $IconPath
Write-Host "[Shortcut] Workspace shortcut created : $RootShortcut" -ForegroundColor Cyan

if ($CreateDesktopShortcut) {
    $DesktopDir = [Environment]::GetFolderPath("Desktop")
    if ($DesktopDir -and (Test-Path $DesktopDir)) {
        $DesktopShortcut = Join-Path $DesktopDir "SoloLedger.lnk"
        New-AppShortcut -ShortcutPath $DesktopShortcut -TargetExe $ExePath -WorkDir $RepoRoot -Icon $IconPath
        Write-Host "[Shortcut] Desktop shortcut created   : $DesktopShortcut" -ForegroundColor Cyan
    }
}
