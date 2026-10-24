# Admission-rate sweep: the waiting room's rate decides how fast users reach hold, book and pay,
# so it is the capacity control. Run the rates in the order given (shuffled on purpose).
param([int]$Users = 400, [int]$Runs = 2, [int[]]$Rates = @(6, 24, 3, 12))
. "$PSScriptRoot\common.ps1"
foreach ($rate in $Rates) {
    Write-Host ("`n##### admission rate " + $rate + " per second, " + $Users + " users #####") -ForegroundColor Cyan
    $env:LT_ADMIT_PER_SECOND = "$rate"
    & "$PSScriptRoot\up.ps1"
    if ($LASTEXITCODE -ne 0) { Write-Host "The stack did not come up." -ForegroundColor Red; break }
    Write-Host ("web sees WAITROOM_ADMIT_PER_SECOND=" + ((docker compose exec -T web printenv WAITROOM_ADMIT_PER_SECOND) -join ""))
    Start-Sleep -Seconds 20
    & "$PSScriptRoot\rushcheck.ps1" -Users $Users -Runs $Runs -Ramp 5 -MaxWait 500 -Label ("admit-" + $rate)
}
$env:LT_ADMIT_PER_SECOND = $null
& "$PSScriptRoot\up.ps1"
Write-Host "`n=== sweep results ===" -ForegroundColor Green
$rows = @(Import-Csv (Join-Path $ResultsDir "sweep-summary.csv") | Where-Object { $_.Label -like "admit-*" })
$rows | Format-Table Label, Admit, Seconds, ReqPerSec, Confirmed, joinP95, meP95, holdP95, bookP95, webhookP95, BenchBefore, BenchAfter, Invariants -AutoSize
Write-Host "=== raw rows (CSV) ==="
Get-Content (Join-Path $ResultsDir "sweep-summary.csv") | Select-Object -Last ($rows.Count + 1)
