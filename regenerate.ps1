# Regenerate ForgeOS Dispatch dashboard from live git + gh state (v3 pipeline view).
# Self-locates via $PSScriptRoot so it works from any folder depth.
# Working clone lives at: C:\Users\jaheb\IT\Applications\ForgeOS\dispatch-dashboard\
# Source repo (read-only) at: %USERPROFILE%\OneDrive - forgepromo com\IT\Applications\Sales_Pipeline
# Usage (from the dispatch-dashboard clone):  pwsh ./regenerate.ps1
$ErrorActionPreference = 'Stop'
$sourceRepo = Join-Path $env:USERPROFILE 'OneDrive - forgepromo com\IT\Applications\Sales_Pipeline'
$pagesRepo  = $PSScriptRoot

if (-not (Test-Path $sourceRepo)) {
  Write-Error "Source repo not found at $sourceRepo"
}
if (-not (Test-Path (Join-Path $pagesRepo 'regenerate.py'))) {
  Write-Error "regenerate.py not found next to this script at $pagesRepo"
}

Write-Host "Fetching latest branches..."
Push-Location $sourceRepo
git fetch --all --prune | Out-Null
Pop-Location

Write-Host "Running python generator..."
python "$pagesRepo\regenerate.py" --source "$sourceRepo" --out "$pagesRepo"

Push-Location $pagesRepo
git add dispatch.html index.html data.json regenerate.py regenerate.ps1 _pr_merged.json _pr_open.json 2>$null
$dirty = git status --porcelain
if ($dirty) {
  git commit -m ("Dashboard refresh {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm'))
  git push
  Write-Host "Dashboard regenerated. Live in ~60s at https://jhebert-forgepromo.github.io/dispatch-dashboard/dispatch.html"
} else {
  Write-Host "No changes to publish."
}
Pop-Location
