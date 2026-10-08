<#
  Registers the nightly GitHub backup with Windows Task Scheduler. Run once, from this project folder:

      powershell -ExecutionPolicy Bypass -File scripts\register_backup.ps1            # install
      powershell -ExecutionPolicy Bypass -File scripts\register_backup.ps1 -Remove    # uninstall

  TechnoFunda Backup   every day 22:30   commit what changed and push it to GitHub (a few seconds)
  Runs as you, only while you are logged on. If the PC was off at 22:30 it runs as soon as it is next available.
#>
param([switch]$Remove, [string]$At = "22:30")
$root = Split-Path -Parent $PSScriptRoot
$name = "TechnoFunda Backup"
if ($Remove) { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue; Write-Host "Removed: $name"; return }
$script = Join-Path $root "scripts\backup_github.ps1"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force -Description "TechnoFunda Desk: commit and push the project to GitHub" | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Registered. Results are appended to data\backup.log"
