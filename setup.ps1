#Requires -Version 5.1
<#
.SYNOPSIS
  One-time setup of polybot on Windows: Python, git, virtual environment, packages,
  tests, and a health check.

.DESCRIPTION
  Run from PowerShell (no admin needed):
      powershell -ExecutionPolicy Bypass -File .\setup.ps1

  If the repository is not cloned yet, this script clones it into
  $HOME\CLAUDE-PAPER-TRADING (GitHub will ask you to sign in, because the repo is
  private). Re-running is safe.

.PARAMETER SkipTests
  Skip the test suite (about 3 minutes).
#>
param(
    [switch]$SkipTests,
    [string]$InstallDir = (Join-Path $HOME "CLAUDE-PAPER-TRADING"),
    [string]$RepoUrl = "https://github.com/FL1105/CLAUDE-PAPER-TRADING.git"
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) { Write-Host ""; Write-Host "== $Message" -ForegroundColor Cyan }
function Ok([string]$Message) { Write-Host "   OK  $Message" -ForegroundColor Green }
function Note([string]$Message) { Write-Host "   ..  $Message" }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Assert-Exit([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}
$HasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

# ---------------------------------------------------------------- Python 3.11+
function Find-Python {
    $candidates = @(@("py", "-3.12"), @("py", "-3.11"), @("py", "-3"), @("python"))
    foreach ($c in $candidates) {
        $exe = $c[0]
        $pyArgs = @()
        if ($c.Length -gt 1) { $pyArgs = $c[1..($c.Length - 1)] }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $exe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($v -and ([version]$v -ge [version]"3.11")) { return , $c }
    }
    return $null
}

Step "Python 3.11 or newer"
$py = Find-Python
if (-not $py) {
    if (-not $HasWinget) {
        throw "Python 3.11+ not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this script again."
    }
    Note "Installing Python 3.12 with winget..."
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $py = Find-Python
    if (-not $py) { throw "Python was installed but is not on PATH yet. Close PowerShell, open a new one, and run this script again." }
}
$pyExe = $py[0]
$pyArgs = @()
if ($py.Length -gt 1) { $pyArgs = $py[1..($py.Length - 1)] }
Ok ("Using: " + ($py -join " ") + "  (" + (& $pyExe @pyArgs --version) + ")")

# ---------------------------------------------------------------- repository
Step "Repository"
if (Test-Path (Join-Path $PSScriptRoot "pyproject.toml")) {
    $Root = $PSScriptRoot
    Ok "Running inside the repository: $Root"
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if (-not $HasWinget) { throw "git not found. Install it from https://git-scm.com/download/win and run this script again." }
        Note "Installing git with winget..."
        winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }
    if (Test-Path (Join-Path $InstallDir ".git")) {
        Note "Updating existing clone in $InstallDir"
        git -C $InstallDir pull --ff-only
        Assert-Exit "git pull"
    } else {
        Note "Cloning into $InstallDir (sign in to GitHub if asked)"
        git clone $RepoUrl $InstallDir
        Assert-Exit "git clone"
    }
    $Root = $InstallDir
    Ok "Repository at $Root"
}
Set-Location $Root

# ---------------------------------------------------------------- UTF-8 everywhere
Step "Text encoding"
[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")
$env:PYTHONUTF8 = "1"
Ok "PYTHONUTF8=1 set for your user (Python reads and writes UTF-8)"

# ---------------------------------------------------------------- virtual environment
Step "Virtual environment (.venv)"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $pyExe @pyArgs -m venv .venv
    Assert-Exit "Creating the virtual environment"
}
Ok ".venv ready"

Step "Installing packages (a few minutes the first time)"
& $venvPy -m pip install --upgrade pip --quiet
Assert-Exit "Upgrading pip"
& $venvPy -m pip install -e ".[dev]" --quiet
Assert-Exit "Installing polybot"
Ok "polybot and its dependencies are installed"

# ---------------------------------------------------------------- tests
if (-not $SkipTests) {
    Step "Running the test suite (about 3 minutes)"
    & $venvPy -m pytest -q
    Assert-Exit "Test suite"
    Ok "All tests passed"
} else {
    Note "Tests skipped (-SkipTests)"
}

# ---------------------------------------------------------------- health check
Step "Health check (polybot doctor)"
$polybot = Join-Path $Root ".venv\Scripts\polybot.exe"
& $polybot doctor
$doctorExit = $LASTEXITCODE

Write-Host ""
Write-Host "Setup finished." -ForegroundColor Green
if ($doctorExit -ne 0) {
    Write-Host "The health check found problems (see the FAIL lines above). Fix those first." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next steps (in this folder, after activating the environment):"
Write-Host "   .\.venv\Scripts\Activate.ps1"
Write-Host "   polybot probe                 # verify the real API (send the output to Claude if it reports drift)"
Write-Host "   polybot backfill --days 90    # history"
Write-Host "   polybot backtest --days 90    # can reach tier Common at most"
Write-Host "   .\scripts\windows\install-paper-task.ps1 -NoSleep   # start the 30-day forward run"
Write-Host "   polybot report                # any time; then open dashboard.html"
Write-Host ""
Write-Host "Details: docs\RUNBOOK.md (Windows section)"
