<#
  Registers the nightly refresh with Windows Task Scheduler. Run once, from this project folder:

      powershell -ExecutionPolicy Bypass -File scripts\register_nightly.ps1            # install
      powershell -ExecutionPolicy Bypass -File scripts\register_nightly.ps1 -Remove    # uninstall

  SetupDesk Nightly  Mon-Fri 18:45   prices for the whole market (about 6 minutes)
  SetupDesk TopUp    Mon-Fri 20:15   delivery file, desk bars and saved screens again (a few seconds), in case NSE's file came late
  SetupDesk Weekly   Sat 09:00       the above + fundamentals for the Desk's stocks (about an hour)

  Both run as you, only while you are logged on, with no stored password. If the PC was off at the scheduled time
  the task runs as soon as it is next available. 18:45 is after NSE publishes its end-of-day file with the delivery figures (it was out before 18:35 on the day this was checked); the 20:15 top-up covers a late night.
#>
param([switch]$Remove, [string]$At = "18:45", [string]$TopUpAt = "20:15", [string]$WeeklyAt = "09:00")

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$names = "SetupDesk Nightly", "SetupDesk TopUp", "SetupDesk Weekly"

if ($Remove) {
    foreach ($n in $names) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Removed: $($names -join ', ')"
    return
}
if (-not (Test-Path $py)) { throw "Cannot find $py - create the virtual environment first." }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)

$nightly = New-ScheduledTaskAction -Execute $py -Argument "-m backend.nightly" -WorkingDirectory $root
$weekly  = New-ScheduledTaskAction -Execute $py -Argument "-m backend.nightly --weekly" -WorkingDirectory $root
$topup   = New-ScheduledTaskAction -Execute $py -Argument "-m backend.nightly --only extras,desk,screens" -WorkingDirectory $root
$weekdays = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$later    = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $TopUpAt
$saturday = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $WeeklyAt

Register-ScheduledTask -TaskName "SetupDesk Nightly" -Action $nightly -Trigger $weekdays -Settings $settings -Force `
    -Description "TechnoFunda Desk: reload prices for the whole NSE market" | Out-Null
Register-ScheduledTask -TaskName "SetupDesk TopUp" -Action $topup -Trigger $later -Settings $settings -Force `
    -Description "TechnoFunda Desk: pick up the delivery file and today's late bars if the first run was early" | Out-Null
Register-ScheduledTask -TaskName "SetupDesk Weekly" -Action $weekly -Trigger $saturday -Settings $settings -Force `
    -Description "TechnoFunda Desk: prices plus fundamentals refresh" | Out-Null

Get-ScheduledTask -TaskName $names | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Registered. Check the last run any time:  http://localhost:8000/api/status  or  data\nightly.log"
