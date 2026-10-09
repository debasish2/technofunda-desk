@echo off
rem Carries on the IndianAPI audit where it stopped (finished stocks are remembered). Safe to run again at any time.
cd /d "%~dp0.."
set INDIANAPI_MONTHLY_LIMIT=4850
.venv\Scripts\python.exe -u -m backend.indianapi --fetch 2500 --all
.venv\Scripts\python.exe -u -m backend.indianapi --annual 2500
.venv\Scripts\python.exe -m backend.indianapi --audit-report
pause
