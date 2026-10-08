@echo off
rem Starts the Setup Desk server (if it is not already running) and opens the site.
cd /d "%~dp0"
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://localhost:8000/api/status -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
  echo Starting the Setup Desk server...
  start "Setup Desk server" /min ".venv\Scripts\python.exe" -m uvicorn backend.app:app --host 0.0.0.0 --port 8000
  timeout /t 8 /nobreak >nul
)
start "" http://localhost:8000/
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -like 'Wi-Fi*' -or $_.InterfaceAlias -like 'Ethernet*' } | Where-Object { $_.IPAddress -notlike '169.254*' } | Select-Object -First 1).IPAddress"`) do echo On your phone (same Wi-Fi) open: http://%%i:8000
