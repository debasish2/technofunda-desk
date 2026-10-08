<#
  Registers the nightly refresh with Windows Task Scheduler. Run once, from this project folder:

      powershell -ExecutionPolicy Bypass -File scripts\register_nightly.ps1            # install
      powershell -ExecutionPolicy Bypass -File scripts\register_nightly.ps1 -Remove    # uninstall

  SetupDesk Nightly  Mon-Fri 20:00   prices for the whole market (about 6 minutes)
  SetupDesk Weekly   Sat 09:00       the above + fundamentals for the Desk's stocks (about an hour)

  Both run as you, only while you are logged on, with no stored password. If the PC was off at the scheduled time
  the task runs as soon as it is next available. 20:00 is after NSE publishes its end-of-day file (usually by 18:30).
#>
param([switch]$Remove, [string]$At = "20:00", [string]$WeeklyAt = "09:00")

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$names = "SetupDesk Nightly", "SetupDesk Weekly"

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
$weekdays = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$saturday = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $WeeklyAt

Register-ScheduledTask -TaskName "SetupDesk Nightly" -Action $nightly -Trigger $weekdays -Settings $settings -Force `
    -Description "Dalal Desk: reload prices for the whole NSE market" | Out-Null
Register-ScheduledTask -TaskName "SetupDesk Weekly" -Action $weekly -Trigger $saturday -Settings $settings -Force `
    -Description "Dalal Desk: prices plus fundamentals refresh" | Out-Null

Get-ScheduledTask -TaskName $names | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Registered. Check the last run any time:  http://localhost:8000/api/status  or  data\nightly.log"
