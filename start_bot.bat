@echo off
REM One-click paper "learning" bot (Windows). Paper only: it never places real orders.
REM Double-click this file. First run sets everything up (a few minutes); later runs resume.
cd /d "%~dp0"
if not exist ".venv\Scripts\polybot.exe" (
  echo First run: setting up...
  powershell -ExecutionPolicy Bypass -File "%~dp0setup.ps1" -SkipTests
)
call ".venv\Scripts\activate.bat"
echo Starting the learning bot. Leave this window open. Close it or press Ctrl+C to stop.
polybot learn run --minutes 10080
pause
