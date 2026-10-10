# One command for the first real-data session (Windows PowerShell). Run from the compoundbot folder:
#   powershell -ExecutionPolicy Bypass -File .\scripts\tonight.ps1
# Needs Python 3.11+. Put PAPER keys in .env first (copy .env.example). Never paste keys anywhere else.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path .venv)) { python -m venv .venv }
. .\.venv\Scripts\Activate.ps1
pip install -q -e ".[dev,dash]"
if ($LASTEXITCODE -ne 0) { Write-Host "install failed"; exit 1 }
Write-Host "== tests (about a minute)"; python -m pytest -q -m "not slow"
if ($LASTEXITCODE -ne 0) { Write-Host "TESTS FAILED: continue only if you understand why" }
Write-Host "== Alpaca probe (read-only)"; python -m bot.cli probe
$src = if ($env:SRC) { $env:SRC } else { "yahoo" }
Write-Host "== data from $src (set `$env:SRC='csv' or 'alpaca' to change)"
python -m bot.cli data --source $src --start 2003-01-01
if ($LASTEXITCODE -ne 0) { Write-Host "data step failed"; python -m bot.cli summary; exit 1 }
Write-Host "== research (10-30 min)"; python -m bot.cli research
python -m bot.cli select       # writes selected.yaml WITHOUT approval: the bot will not trade yet
python -m bot.cli backtest
python -m bot.cli summary
Write-Host "`nPaste reports\TONIGHT_SUMMARY.md back to Claude. It contains no keys or account ids."
