# A/B of the throttle implementation: old, new, old, new, so drift on a noisy machine does not favour one side.
param([int]$Users = 200, [int]$Runs = 2, [int]$Ramp = 20)
. "$PSScriptRoot\common.ps1"
$csv = Join-Path $ResultsDir "ab-summary.csv"
$plan = @(@("old-1", "false"), @("new-1", "true"), @("old-2", "false"), @("new-2", "true"))
foreach ($cell in $plan) {
    Write-Host ("`n##### " + $cell[0] + " (FAST_THROTTLES=" + $cell[1] + ") #####") -ForegroundColor Cyan
    $env:LT_FAST_THROTTLES = $cell[1]
    & "$PSScriptRoot\up.ps1"
    if ($LASTEXITCODE -ne 0) { Write-Host "The stack did not come up." -ForegroundColor Red; break }
    Start-Sleep -Seconds 20
    & "$PSScriptRoot\rushcheck.ps1" -Users $Users -Runs $Runs -Ramp $Ramp -Label ("throttle-" + $cell[0])
}
$env:LT_FAST_THROTTLES = $null
& "$PSScriptRoot\up.ps1"
Write-Host "`n=== A/B results (old = DRF cache throttles, new = fast throttles) ===" -ForegroundColor Green
Import-Csv $csv | Where-Object { $_.Label -like "throttle-*" } |
    Format-Table Label, ReqPerSec, Confirmed, SoldOut, GaveUp, AllP50, AllP95, holdP50, bookP50, meP50, webhookP50, Invariants -AutoSize
