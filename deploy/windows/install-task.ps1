param(
  [string]$TaskName = "Harness2Supervisor",
  [string]$HarnessPath = "harness"
)
$ErrorActionPreference = "Stop"
$action = New-ScheduledTaskAction -Execute $HarnessPath -Argument "supervise --interval 10"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Harness v2 supervisor" -Force | Out-Null
Write-Host "Installed scheduled task: $TaskName"
