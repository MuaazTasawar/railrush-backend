# In-flight cap on versus off at overload (24 admissions per second). Order off, on, on, off so drift on a noisy
# machine does not favour either side. The cap is the nginx limit_conn inflight line.
param([int]$Users = 400, [int]$Runs = 2, [int]$Rate = 24)
. "$PSScriptRoot\common.ps1"
$plan = @(@("cap-off-1", "0"), @("cap-on-1", "96"), @("cap-on-2", "96"), @("cap-off-2", "0"))
foreach ($cell in $plan) {
    Write-Host ("`n##### " + $cell[0] + " (LT_INFLIGHT=" + $cell[1] + ") #####") -ForegroundColor Cyan
    $env:LT_ADMIT_PER_SECOND = "$Rate"
    $env:LT_INFLIGHT = $cell[1]
    & "$PSScriptRoot\up.ps1"
    if ($LASTEXITCODE -ne 0) { Write-Host "The stack did not come up." -ForegroundColor Red; break }
    $capLine = ((docker compose exec -T nginx grep -E "limit_conn inflight|in-flight cap disabled" /etc/nginx/nginx.conf) -join " ").Trim()
    Write-Host ("nginx cap line: " + $capLine)
    Start-Sleep -Seconds 20
    & "$PSScriptRoot\rushcheck.ps1" -Users $Users -Runs $Runs -Ramp 5 -MaxWait 500 -Label $cell[0]
    $log = @(docker compose logs --no-log-prefix nginx 2>&1 | ForEach-Object { "$_" })
    $count = { param($code) @($log | Where-Object { $_ -match ('" ' + $code + ' ') }).Count }
    Write-Host ("nginx statuses in this run: 429=" + (& $count 429) + "  502=" + (& $count 502) + "  503=" + (& $count 503) + "  504=" + (& $count 504)) -ForegroundColor Cyan
}
$env:LT_ADMIT_PER_SECOND = $null
$env:LT_INFLIGHT = $null
$csv = Join-Path $ResultsDir "sweep2-summary.csv"
Write-Host "`n=== cap on/off results ===" -ForegroundColor Green
Import-Csv $csv | Where-Object { $_.Label -like "cap-*" } | ForEach-Object {
    "{0}: {1}s req/s={2} iter/s={3} confirmed={4} gaveup={5} | p95 ms join {6} me {7} hold {8} book {9} webhook {10} | bench {11}->{12} | {13}" -f `
        $_.Label, $_.Seconds, $_.ReqPerSec, $_.IterPerSec, $_.Confirmed, $_.GaveUp, $_.joinP95, $_.meP95, $_.holdP95, $_.bookP95, $_.webhookP95, $_.BenchBefore, $_.BenchAfter, $_.Invariants
}
Write-Host "=== raw rows (CSV) ==="
Get-Content $csv | Select-String -Pattern '^"cap-' | ForEach-Object { $_.Line }
