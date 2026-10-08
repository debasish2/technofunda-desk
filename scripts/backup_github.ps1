<#
  Commits whatever changed in the project and pushes it to GitHub (the repository set as "origin").
  The databases, logs and caches in data\ are ignored by .gitignore, so this only ever sends code and your themes.
  Run by hand any time:   powershell -ExecutionPolicy Bypass -File scripts\backup_github.ps1
  The result of each run is appended to data\backup.log.
#>
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "data\backup.log"
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm"), $m) }
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null
try {
    git add -A | Out-Null
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -q -m ("Nightly backup {0}" -f (Get-Date -Format "yyyy-MM-dd")) | Out-Null
        Log "committed changes"
    }
    $ahead = (git rev-list --count "origin/main..HEAD" 2>$null)
    git push -q origin main 2>&1 | ForEach-Object { Log "push: $_" }
    if ($LASTEXITCODE -eq 0) { Log ("pushed ({0} commit(s) sent)" -f $ahead) } else { Log "PUSH FAILED (exit $LASTEXITCODE) - check your internet and your GitHub sign-in" }
} catch {
    Log "ERROR: $($_.Exception.Message)"
}
