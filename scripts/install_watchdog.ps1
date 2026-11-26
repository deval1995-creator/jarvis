# Jarvis watchdog: every 30 seconds Windows runs the supervisor. If it's already
# running, the new copy exits at once (a lock); if it died, this starts it again,
# and it brings the Core and voice back up.
#
# A Windows task repeats at most once a minute, so two triggers 30 s apart.
# Runs as you, only while you're logged in; no admin rights needed.
# Undo:  Unregister-ScheduledTask -TaskName "Jarvis watchdog" -Confirm:$false

$repo = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\pythonw.exe" `
    -Argument "`"$repo\scripts\jarvis_service.py`"" -WorkingDirectory $repo
$start = (Get-Date).AddMinutes(1).Date.AddHours((Get-Date).AddMinutes(1).Hour).AddMinutes((Get-Date).AddMinutes(1).Minute)
$triggers = @(0, 30) | ForEach-Object {
    $t = New-ScheduledTaskTrigger -Once -At $start.AddSeconds($_) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $t
}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -Hidden
Register-ScheduledTask -TaskName "Jarvis watchdog" -Action $action -Trigger $triggers -Settings $settings `
    -Description "Every 30 s: make sure Jarvis (Core + voice) is running." -Force | Out-Null
Get-ScheduledTask -TaskName "Jarvis watchdog" | Select-Object TaskName, State
