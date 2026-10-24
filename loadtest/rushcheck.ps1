# One compact run: seed, machine-speed check, k6, machine-speed check, container CPU used, one result row, data invariants, clean up.
# Appends the row to ..\railrush-loadtest\results\sweep2-summary.csv. No sampler loop, so it disturbs the machine as little as possible.
param(
    [int]$Users = 200,
    [int]$Runs = 2,
    [int]$Ramp = 20,
    [double]$Poll = 2,
    [int]$MaxWait = 300,
    [double]$Abandon = 0.1,
    [double]$FailPay = 0.05,
    [string]$Label = "run",
    [string]$QueueMode = "sleep",
    [string]$Journey = "full",
    [string]$K6Image = "grafana/k6:latest"
)
$ErrorActionPreference = "Continue"
. "$PSScriptRoot\common.ps1"
$outDir = Join-Path $ResultsDir ((Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $Label)
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

function Measure-Machine {
    $python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $times = @(& $python (Join-Path $PSScriptRoot "bench_cpu.py") | ForEach-Object {
        if ("$_" -match '^run \d+: ([\d.]+) s') { [double]::Parse($Matches[1], $Inv) }
    })
    if ($times.Count -eq 0) { return $null }
    return [math]::Round((($times | Sort-Object)[[int][math]::Floor($times.Count / 2)]), 2)
}
function Get-CpuSeconds([string]$Service) {
    $v2 = (docker compose exec -T $Service sh -c 'cat /sys/fs/cgroup/cpu.stat 2>/dev/null | grep usage_usec' 2>$null) -join ""
    if ($v2 -match 'usage_usec (\d+)') { return [double]$Matches[1] / 1e6 }
    $v1 = (docker compose exec -T $Service sh -c 'cat /sys/fs/cgroup/cpuacct/cpuacct.usage 2>/dev/null' 2>$null) -join ""
    if ($v1 -match '^\s*(\d+)\s*$') { return [double]$Matches[1] / 1e9 }
    return $null
}

if (-not (Wait-Health 30)) { Write-Host "The stack is not healthy through nginx." -ForegroundColor Red; exit 2 }
$secret = Get-DotEnv "PAYMENT_WEBHOOK_SECRET"
if (-not $secret) { Write-Host "PAYMENT_WEBHOOK_SECRET is missing from .env" -ForegroundColor Red; exit 2 }
$admit = if ($env:LT_ADMIT_PER_SECOND) { $env:LT_ADMIT_PER_SECOND } else { "50" }

& "$PSScriptRoot\seed.ps1" -Users $Users -Runs $Runs
if ($LASTEXITCODE -ne 0) { Write-Host "Seeding failed." -ForegroundColor Red; exit 2 }
$runIds = @((Get-Content (Join-Path $DataDir "runs.json") -Raw | ConvertFrom-Json))
$runList = $runIds -join ","

$services = @("web", "postgres", "redis", "nginx", "pgbouncer")
$benchBefore = Measure-Machine
$cpuBefore = @{}
foreach ($s in $services) { $cpuBefore[$s] = Get-CpuSeconds $s }

$env:WEBHOOK_SECRET = $secret
$argString = 'run --rm --name railrush-k6 --network railrush_edge ' +
    ('-v "{0}:/data:ro" -v "{1}:/scripts:ro" -v "{2}:/out" ' -f $DataDir, $PSScriptRoot, $outDir) +
    ('-e WEBHOOK_SECRET -e K6_NO_USAGE_REPORT=true -e BASE_URL=http://nginx:8080 -e QUEUE_MODE={0} -e JOURNEY={1} ' -f $QueueMode, $Journey) +
    ('-e USERS={0} -e RAMP_SECONDS={1} -e POLL_SECONDS={2} -e MAX_WAIT_SECONDS={3} -e SEATS_PER_USER=2 -e ABANDON_RATE={4} -e FAIL_PAY_RATE={5} ' -f `
        $Users, $Ramp, $Poll.ToString($Inv), $MaxWait, $Abandon.ToString($Inv), $FailPay.ToString($Inv)) +
    ($K6Image + ' run --quiet --no-color --summary-export=/out/summary.json /scripts/rush.js')
$began = Get-Date
$proc = Start-Process -FilePath "docker" -ArgumentList $argString -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path $outDir "k6-stdout.txt") -RedirectStandardError (Join-Path $outDir "k6-stderr.txt")
$null = $proc.Handle
$proc.WaitForExit()
$env:WEBHOOK_SECRET = $null
$elapsed = [math]::Round(((Get-Date) - $began).TotalSeconds, 0)
$cpuAfter = @{}
foreach ($s in $services) { $cpuAfter[$s] = Get-CpuSeconds $s }
$benchAfter = Measure-Machine
function Get-CpuUsed([string]$Service) {
    if ($null -eq $cpuBefore[$Service] -or $null -eq $cpuAfter[$Service]) { return $null }
    return [math]::Round($cpuAfter[$Service] - $cpuBefore[$Service], 1)
}

$row = [ordered]@{}
foreach ($k in @("Label", "Journey", "Admit", "Users", "Seconds", "ExitCode", "Requests", "ReqPerSec", "IterPerSec", "Confirmed", "SoldOut", "GaveUp",
        "joinP50", "joinP95", "meP50", "meP95", "holdP50", "holdP95", "bookP50", "bookP95", "payP50", "payP95",
        "webhookP50", "webhookP95", "WebCpuS", "PgCpuS", "RedisCpuS", "NginxCpuS", "PgbCpuS",
        "BenchBefore", "BenchAfter", "Invariants", "Bookings")) { $row[$k] = $null }
$row["Label"] = $Label
$row["Journey"] = $Journey
$row["Admit"] = $admit
$row["Users"] = $Users
$row["Seconds"] = $elapsed
$row["ExitCode"] = $proc.ExitCode
$row["WebCpuS"] = Get-CpuUsed "web"
$row["PgCpuS"] = Get-CpuUsed "postgres"
$row["RedisCpuS"] = Get-CpuUsed "redis"
$row["NginxCpuS"] = Get-CpuUsed "nginx"
$row["PgbCpuS"] = Get-CpuUsed "pgbouncer"
$row["BenchBefore"] = $benchBefore
$row["BenchAfter"] = $benchAfter

$summaryFile = Join-Path $outDir "summary.json"
if (Test-Path $summaryFile) {
    $json = Get-Content $summaryFile -Raw | ConvertFrom-Json
    $stat = {
        param($name, $field)
        $m = $json.metrics.PSObject.Properties[$name]
        if (-not $m) { return $null }
        $v = $m.Value
        if ($v.PSObject.Properties["values"]) { $v = $v.values }
        $p = $v.PSObject.Properties[$field]
        if ($p) { return [math]::Round([double]$p.Value, 1) }
        return $null
    }
    $row["Requests"] = & $stat "http_reqs" "count"
    $row["ReqPerSec"] = & $stat "http_reqs" "rate"
    $row["IterPerSec"] = & $stat "iterations" "rate"
    $row["Confirmed"] = & $stat "journeys_confirmed" "count"
    $row["SoldOut"] = & $stat "journeys_sold_out" "count"
    $row["GaveUp"] = & $stat "journeys_gave_up_in_queue" "count"
    foreach ($e in @("join", "me", "hold", "book", "pay", "webhook")) {
        $row[$e + "P50"] = & $stat ("http_req_duration{name:" + $e + "}") "med"
        $row[$e + "P95"] = & $stat ("http_req_duration{name:" + $e + "}") "p(95)"
    }
} else {
    Write-Host "k6 produced no summary. Its output:" -ForegroundColor Red
    Get-Content (Join-Path $outDir "k6-stdout.txt") -ErrorAction SilentlyContinue | Select-Object -Last 15
    Get-Content (Join-Path $outDir "k6-stderr.txt") -ErrorAction SilentlyContinue | Select-Object -Last 8
}

$inv = Invoke-Psql ([System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "verify_brief.sql")).Replace("__RUNS__", $runList))
$dataLine = @($inv | Where-Object { "$_" -match '^\s*\d+\s*\|' }) | Select-Object -Last 1
$ok = $false
if ($dataLine) {
    $nums = @(("$dataLine" -split "\|") | ForEach-Object { [int]($_.Trim()) })
    $ok = ((($nums[0..4]) | Measure-Object -Sum).Sum -eq 0)
    $row["Bookings"] = $nums[5]
}
$row["Invariants"] = if ($ok) { "OK" } else { "CHECK" }

[pscustomobject]$row | Export-Csv -Path (Join-Path $ResultsDir "sweep2-summary.csv") -Append -NoTypeInformation
Write-Host ("{0} [{1}]: admit/s={2} users={3} {4}s exit={5} requests={6} req/s={7} iterations/s={8} confirmed={9}" -f $row["Label"], $Journey, $row["Admit"], $Users, $elapsed, $row["ExitCode"], $row["Requests"], $row["ReqPerSec"], $row["IterPerSec"], $row["Confirmed"])
Write-Host ("   p50/p95 ms: join {0}/{1}  me {2}/{3}  hold {4}/{5}  book {6}/{7}  pay {8}/{9}  webhook {10}/{11}" -f $row["joinP50"], $row["joinP95"], $row["meP50"], $row["meP95"], $row["holdP50"], $row["holdP95"], $row["bookP50"], $row["bookP95"], $row["payP50"], $row["payP95"], $row["webhookP50"], $row["webhookP95"])
Write-Host ("   CPU seconds used: web {0}  postgres {1}  redis {2}  nginx {3}  pgbouncer {4}" -f $row["WebCpuS"], $row["PgCpuS"], $row["RedisCpuS"], $row["NginxCpuS"], $row["PgbCpuS"])
Write-Host ("   machine speed (2M-loop benchmark, s) before {0} after {1}   invariants {2}   bookings {3}" -f $benchBefore, $benchAfter, $row["Invariants"], $row["Bookings"])
& "$PSScriptRoot\clean.ps1" -KeepUsers
