# ForgeOS Dispatch Dashboard - Scheduled Task Bootstrap
# ---------------------------------------------------------------------------
# Purpose : Idempotently (re)register the two Windows scheduled tasks that
#           keep the dispatch dashboard fresh:
#             1. ForgeOS-Dashboard-Refresh          (safety-net regenerate, 15 min)
#             2. ForgeOS-Dashboard-Freshness-Check  (staleness watchdog,     5 min)
#
# Run on host provisioning or after a rebuild:
#     pwsh .\install-scheduled-tasks.ps1
#
# Options:
#     -RefreshMinutes    <int>   secondary refresh interval (default 15)
#     -WatchdogMinutes   <int>   watchdog interval          (default  5)
#     -RemoveOnly                unregister both tasks and exit
#
# Constraints: no credentials; runs under the current interactive user;
# idempotent (unregister-then-register). Verified by echoing final state.
# ---------------------------------------------------------------------------

param(
  [int]    $RefreshMinutes  = 15,
  [int]    $WatchdogMinutes = 5,
  [switch] $RemoveOnly
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$refreshScript  = Join-Path $root 'regenerate.ps1'
$watchdogScript = Join-Path $root 'freshness-check.ps1'

$refreshName  = 'ForgeOS-Dashboard-Refresh'
$watchdogName = 'ForgeOS-Dashboard-Freshness-Check'

function Remove-TaskIfPresent {
  param([string]$Name)
  if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Host "Removed task: $Name"
  }
}

function Register-RepeatingTask {
  param(
    [string]$Name,
    [string]$ScriptPath,
    [int]   $EveryMinutes,
    [string]$Description,
    [int]   $TimeLimitMinutes = 5
  )
  if (-not (Test-Path $ScriptPath)) {
    throw "Script not found: $ScriptPath"
  }
  Remove-TaskIfPresent -Name $Name

  $action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
                 -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"{0}`"" -f $ScriptPath)
  $trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                 -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
  $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
                 -LogonType Interactive -RunLevel Limited
  $settings  = New-ScheduledTaskSettingsSet `
                 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                 -StartWhenAvailable -MultipleInstances IgnoreNew `
                 -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes)

  Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description $Description | Out-Null
  Write-Host ("Registered: {0}  (every {1} min)" -f $Name, $EveryMinutes)
}

if ($RemoveOnly) {
  Remove-TaskIfPresent -Name $refreshName
  Remove-TaskIfPresent -Name $watchdogName
  Write-Host 'RemoveOnly complete.'
  return
}

Register-RepeatingTask -Name $refreshName -ScriptPath $refreshScript `
  -EveryMinutes $RefreshMinutes -TimeLimitMinutes 10 `
  -Description 'ForgeOS dispatch dashboard: safety-net regeneration. Primary trigger is the post-merge git hook.'

Register-RepeatingTask -Name $watchdogName -ScriptPath $watchdogScript `
  -EveryMinutes $WatchdogMinutes -TimeLimitMinutes 2 `
  -Description 'ForgeOS dispatch dashboard: freshness watchdog. Alerts (Event Log + freshness.log) when dispatch.html is older than 20 min.'

Write-Host ''
Write-Host '=== Final task state ==='
Get-ScheduledTask | Where-Object { $_.TaskName -in @($refreshName, $watchdogName) } | ForEach-Object {
  $t = $_
  $info = Get-ScheduledTaskInfo -TaskName $t.TaskName
  $interval = ($t.Triggers | Select-Object -First 1).Repetition.Interval
  [PSCustomObject]@{
    Task     = $t.TaskName
    State    = $t.State
    Interval = $interval
    NextRun  = $info.NextRunTime
  }
} | Format-Table -AutoSize
