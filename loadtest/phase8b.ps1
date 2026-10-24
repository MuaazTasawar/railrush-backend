# Phase 8 part B: how fast can tickets be issued (join burst), and which process model serves the purchase flow best?
param([int]$Users = 400, [int]$Runs = 2, [int]$Rate = 6)
. "$PSScriptRoot\common.ps1"
$csv = Join-Path $ResultsDir "sweep2-summary.csv"
function Reset-WebEnv {
    $env:WEB_CONCURRENCY = $null
    $env:GUNICORN_WORKER_CLASS = $null
    $env:APP_MODULE = $null
    $env:LT_ADMIT_PER_SECOND = $null
}

Write-Host "`n##### part 1: join burst (join only, default settings) #####" -ForegroundColor Cyan
Reset-WebEnv
& "$PSScriptRoot\up.ps1"
if ($LASTEXITCODE -eq 0) {
    Start-Sleep -Seconds 20
    foreach ($n in @(800, 1500)) {
        & "$PSScriptRoot\rushcheck.ps1" -Users $n -Runs 1 -Ramp 4 -MaxWait 120 -Journey join -Label ("join-burst-" + $n)
    }
} else {
    Write-Host "The stack did not come up; skipping part 1." -ForegroundColor Red
}

$cells = @(
    @{ Name = "gthread-w4"; Class = "gthread"; App = "config.wsgi:application"; Workers = 4 },
    @{ Name = "asgi-w3"; Class = "uvicorn_worker.UvicornWorker"; App = "config.asgi:application"; Workers = 3 },
    @{ Name = "gthread-w2"; Class = "gthread"; App = "config.wsgi:application"; Workers = 2 },
    @{ Name = "gthread-w3"; Class = "gthread"; App = "config.wsgi:application"; Workers = 3 }
)
foreach ($c in $cells) {
    Write-Host ("`n##### part 2: " + $c.Name + " at " + $Rate + " admissions per second #####") -ForegroundColor Cyan
    $env:WEB_CONCURRENCY = "$($c.Workers)"
    $env:GUNICORN_WORKER_CLASS = $c.Class
    $env:APP_MODULE = $c.App
    $env:LT_ADMIT_PER_SECOND = "$Rate"
    & "$PSScriptRoot\up.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("The stack did not become healthy with " + $c.Name + ". Last web log lines:") -ForegroundColor Red
        docker compose logs --no-log-prefix --tail 12 web
        continue
    }
    docker compose logs --no-log-prefix web 2>&1 | Select-String -Pattern "Using worker" | Select-Object -Last 1 | ForEach-Object { $_.Line }
    Start-Sleep -Seconds 20
    & "$PSScriptRoot\rushcheck.ps1" -Users $Users -Runs $Runs -Ramp 5 -MaxWait 500 -Label ("proc-" + $c.Name)
}
Reset-WebEnv
& "$PSScriptRoot\up.ps1"

Write-Host "`n=== raw rows (CSV): paste this part ===" -ForegroundColor Green
Get-Content $csv
