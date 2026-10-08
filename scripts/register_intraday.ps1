<#
  Registers the hourly breadth refresh with Windows Task Scheduler. Run once, from this project folder:

      powershell -ExecutionPolicy Bypass -File scripts\register_intraday.ps1            # install
      powershell -ExecutionPolicy Bypass -File scripts\register_intraday.ps1 -Remove    # uninstall

  SetupDesk Intraday   Mon-Fri 09:30, then every hour until 16:30   one refresh of the market-wide breadth (about 2 minutes)

  Runs as you, only while you are logged on. backend.live does nothing outside NSE hours (09:15-16:35 IST), so a missed or
  late run is harmless. If the PC was off at a scheduled time, the task does NOT catch up: the next hourly run just takes over.
#>
param([switch]$Remove, [string]$At = "09:30")

$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$name = "SetupDesk Intraday"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed: $name"
    return
}
if (-not (Test-Path $py)) { throw "Cannot find $py - create the virtual environment first." }

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$action = New-ScheduledTaskAction -Execute $py -Argument "-m backend.live" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
$rep = New-ScheduledTaskTrigger -Once -At $At -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 7)
$trigger.Repetition = $rep.Repetition

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "TechnoFunda Desk: hourly intraday snapshot for the market breadth" | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "Registered. Runs hourly 09:30-16:30 on weekdays."
