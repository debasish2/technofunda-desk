<#
  Sends the latest committed code to the server and restarts the app. Run on your PC, from the project folder, after committing:

      powershell -ExecutionPolicy Bypass -File deploy\update_server.ps1 -Server ubuntu@203.0.113.7

  Only code goes: the server's data (prices, logins, everyone's settings) is never touched.
  Needs the OpenSSH client that comes with Windows 10/11 (the ssh and scp commands).
#>
param([Parameter(Mandatory = $true)][string]$Server)

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
Set-Location $root
& $py deploy\pack.py --app
if ($LASTEXITCODE -ne 0) { throw "packing failed" }
scp deploy\out\app.tar.gz "${Server}:/tmp/app.tar.gz"
if ($LASTEXITCODE -ne 0) { throw "copy failed: check the server address and your ssh key" }
ssh $Server "sudo bash /opt/technofunda/deploy/apply_update.sh /tmp/app.tar.gz"
