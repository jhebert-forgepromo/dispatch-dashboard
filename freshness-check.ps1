# ForgeOS Dispatch Dashboard - Freshness Check
# ---------------------------------------------------------------------------
# Purpose : Detect stale dispatch dashboards so we notice when BOTH the
#           primary (post-merge git hook + regenerate.ps1) and the secondary
#           (ForgeOS-Dashboard-Refresh scheduled task, every 15 min) have
#           failed to publish a fresh build.
#
# Trigger : Scheduled Task "ForgeOS-Dashboard-Freshness-Check" (every 5 min).
#
# Behavior:
#   - Reads LastWriteTime of dispatch.html in this folder.
#   - If older than $StaleMinutes (default 20), emits:
#       * Windows Event Log entry (source "ForgeOS-Dashboard",
#         id 4001 warning, id 4002 error at 2x threshold).
#       * Line appended to freshness.log for humans / log parsers.
#       * Toast (BurntToast) when the module is installed.
#   - Exit code 0 = fresh, 1 = stale, 2 = missing artifact, 3 = internal error.
#
# Constraints: no credentials; single deterministic pass; no network I/O.
# ---------------------------------------------------------------------------

param(
  [int]    $StaleMinutes = 20,
  [string] $DashboardPath = (Join-Path $PSScriptRoot 'dispatch.html'),
  [string] $LogPath       = (Join-Path $PSScriptRoot 'freshness.log'),
  [string] $EventSource   = 'ForgeOS-Dashboard',
  [string] $EventLogName  = 'Application'
)

$ErrorActionPreference = 'Stop'

function Write-FreshnessLog {
  param([string]$Level, [string]$Message)
  $line = ('{0}  {1,-5}  {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
  try { Add-Content -Path $LogPath -Value $line -ErrorAction Stop } catch { Write-Host $line }
  Write-Host $line
}

function Ensure-EventSource {
  try {
    if (-not [System.Diagnostics.EventLog]::SourceExists($EventSource)) {
      New-EventLog -LogName $EventLogName -Source $EventSource -ErrorAction Stop
    }
    return $true
  } catch { return $false }
}

function Try-WriteEvent {
  param([int]$EventId, [string]$EntryType, [string]$Message)
  try {
    if (Ensure-EventSource) {
      Write-EventLog -LogName $EventLogName -Source $EventSource `
        -EntryType $EntryType -EventId $EventId -Message $Message -ErrorAction Stop
    }
  } catch { }
}

function Try-Toast {
  param([string]$Title, [string]$Message)
  try {
    if (Get-Module -ListAvailable -Name BurntToast) {
      Import-Module BurntToast -ErrorAction Stop
      New-BurntToastNotification -Text $Title, $Message | Out-Null
    }
  } catch { }
}

try {
  if (-not (Test-Path $DashboardPath)) {
    $msg = "Dashboard artifact missing: $DashboardPath"
    Write-FreshnessLog -Level 'ERROR' -Message $msg
    Try-WriteEvent -EventId 4003 -EntryType 'Error' -Message $msg
    Try-Toast -Title 'ForgeOS Dashboard' -Message 'Dispatch dashboard file is missing.'
    exit 2
  }

  $lastWrite = (Get-Item $DashboardPath).LastWriteTime
  $ageMin    = [int]((Get-Date) - $lastWrite).TotalMinutes

  if ($ageMin -le $StaleMinutes) {
    Write-FreshnessLog -Level 'OK' -Message ("dispatch.html age {0} min (<= {1})" -f $ageMin, $StaleMinutes)
    exit 0
  }

  $entryType = if ($ageMin -ge (2 * $StaleMinutes)) { 'Error' } else { 'Warning' }
  $eventId   = if ($entryType -eq 'Error') { 4002 } else { 4001 }
  $msg = "Dispatch dashboard STALE: last write {0}, age {1} min (threshold {2} min)." -f `
         $lastWrite.ToString('yyyy-MM-dd HH:mm:ss'), $ageMin, $StaleMinutes
  Write-FreshnessLog -Level 'STALE' -Message $msg
  Try-WriteEvent -EventId $eventId -EntryType $entryType -Message $msg
  Try-Toast -Title 'ForgeOS Dashboard STALE' -Message ("Age {0} min (>{1})" -f $ageMin, $StaleMinutes)
  exit 1

} catch {
  $err = "freshness-check internal error: $($_.Exception.Message)"
  Write-FreshnessLog -Level 'ERROR' -Message $err
  Try-WriteEvent -EventId 4900 -EntryType 'Error' -Message $err
  exit 3
}
