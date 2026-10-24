# One load-test run: seed, measure, run the k6 journey, collect evidence, verify the data.
param(
    [int]$Users = 200,
    [int]$Runs = 2,
    [int]$Ramp = 20,
    [double]$Poll = 2,
    [int]$MaxWait = 300,
    [int]$SeatsPerUser = 2,
    [double]$Abandon = 0.1,
    [double]$FailPay = 0.05,
    [string]$Label = "baseline",
    [string]$K6Image = "grafana/k6:latest",
    [switch]$NoSeed,
    [switch]$NoSampler,
    [int]$SampleSeconds = 5,
    [switch]$Clean
)
$ErrorActionPreference = "Continue"
. "$PSScriptRoot\common.ps1"
$outDir = Join-Path $ResultsDir ((Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $Label)
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
Start-Transcript -Path (Join-Path $outDir "run.txt") | Out-Null

function Stop-Run([string]$Message) {
    Write-Host $Message -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 2
}
function ConvertTo-MiB([string]$Text) {
    if ($Text -match '^([\d.]+)\s*(B|KiB|kB|MiB|MB|GiB|GB)') {
        $v = [double]::Parse($Matches[1], $Inv)
        switch ($Matches[2]) {
            "B" { return $v / 1MB }
            "KiB" { return $v / 1024 }
            "kB" { return $v / 1024 }
            "GiB" { return $v * 1024 }
            "GB" { return $v * 1024 }
            default { return $v }
        }
    }
    return 0
}
function Get-Percentile($Sorted, [double]$P) {
    if ($Sorted.Count -eq 0) { return 0 }
    $i = [math]::Min($Sorted.Count - 1, [int][math]::Floor($P * ($Sorted.Count - 1) + 0.5))
    return $Sorted[$i]
}

Write-Host "`n=== Pre-flight ===" -ForegroundColor Cyan
$realip = ((docker compose exec -T nginx grep -c real_ip_header /etc/nginx/nginx.conf) -join "").Trim()
if ($realip -notmatch '^[1-9]') { Stop-Run "nginx is not in load-test mode. Run .\loadtest\up.ps1 first." }
if (-not (Wait-Health 30)) { Stop-Run "The stack is not healthy through nginx." }
$window = ((docker compose exec -T web printenv BOOKING_PAYMENT_SECONDS) -join "").Trim()
$secret = Get-DotEnv "PAYMENT_WEBHOOK_SECRET"
if (-not $secret) { Stop-Run "PAYMENT_WEBHOOK_SECRET is missing from .env" }
Write-Host "users=$Users runs=$Runs ramp=${Ramp}s poll=${Poll}s payment window=${window}s abandon=$Abandon failpay=$FailPay"

if (-not $NoSeed) {
    & "$PSScriptRoot\seed.ps1" -Users $Users -Runs $Runs
    if ($LASTEXITCODE -ne 0) { Stop-Run "Seeding failed." }
}
$runIds = @((Get-Content (Join-Path $DataDir "runs.json") -Raw | ConvertFrom-Json))
$runList = $runIds -join ","

Write-Host "`n=== Reset counters ===" -ForegroundColor Cyan
$null = Invoke-Psql "CREATE EXTENSION IF NOT EXISTS pg_stat_statements; SELECT pg_stat_statements_reset();"
$null = Invoke-Redis CONFIG RESETSTAT
$null = Invoke-Redis SLOWLOG RESET
$startUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

$statsFile = Join-Path $outDir "docker-stats.csv"
$sampler = $null
if (-not $NoSampler) {
    $sampler = Start-Job -ScriptBlock {
        param($file, $every)
        while ($true) {
            $ts = (Get-Date).ToString("o")
            docker stats --no-stream --format '{{.Name}};{{.CPUPerc}};{{.MemUsage}}' | ForEach-Object { Add-Content -Path $file -Value ($ts + ";" + $_) }
            Start-Sleep -Seconds $every
        }
    } -ArgumentList $statsFile, $SampleSeconds
}

Write-Host "`n=== k6 journey ===" -ForegroundColor Cyan
docker pull $K6Image | Select-Object -Last 2
Write-Host ("k6 image id: " + ((docker image inspect $K6Image --format '{{.Id}}') -join ""))
$env:WEBHOOK_SECRET = $secret
$argString = 'run --rm --name railrush-k6 --network railrush_edge ' +
    ('-v "{0}:/data:ro" -v "{1}:/scripts:ro" -v "{2}:/out" ' -f $DataDir, $PSScriptRoot, $outDir) +
    '-e WEBHOOK_SECRET -e K6_NO_USAGE_REPORT=true -e BASE_URL=http://nginx:8080 ' +
    ('-e USERS={0} -e RAMP_SECONDS={1} -e POLL_SECONDS={2} -e MAX_WAIT_SECONDS={3} -e SEATS_PER_USER={4} -e ABANDON_RATE={5} -e FAIL_PAY_RATE={6} ' -f `
        $Users, $Ramp, $Poll.ToString($Inv), $MaxWait, $SeatsPerUser, $Abandon.ToString($Inv), $FailPay.ToString($Inv)) +
    ($K6Image + ' run --quiet --no-color --summary-export=/out/summary.json /scripts/rush.js')
$k6Out = Join-Path $outDir "k6-stdout.txt"
$k6Err = Join-Path $outDir "k6-stderr.txt"
$began = Get-Date
$proc = Start-Process -FilePath "docker" -ArgumentList $argString -NoNewWindow -PassThru -RedirectStandardOutput $k6Out -RedirectStandardError $k6Err
$null = $proc.Handle   # needed so ExitCode is available afterwards
while (-not $proc.HasExited) { Start-Sleep -Seconds 5; Write-Host -NoNewline "." }
$proc.WaitForExit()
Write-Host ""
$env:WEBHOOK_SECRET = $null
$elapsed = [math]::Round(((Get-Date) - $began).TotalSeconds, 0)
Write-Host "k6 finished in ${elapsed}s with exit code $($proc.ExitCode) (0 = thresholds held, 99 = a threshold was crossed)"
Get-Content $k6Out -Encoding UTF8
$errTail = @(Get-Content $k6Err -ErrorAction SilentlyContinue | Where-Object { $_ -match 'level=(error|warn)|ERRO|WARN' } | Select-Object -First 8)
if ($errTail.Count -gt 0) { Write-Host "--- k6 warnings and errors (first 8) ---"; $errTail }

if ($sampler) {
    Stop-Job $sampler -ErrorAction SilentlyContinue
    Remove-Job $sampler -Force -ErrorAction SilentlyContinue
}

Write-Host "`n=== Container CPU and memory during the run (CPU is per core: 400% = all 4) ===" -ForegroundColor Cyan
$rows = New-Object System.Collections.Generic.List[object]
if (Test-Path $statsFile) {
    foreach ($line in Get-Content $statsFile) {
        $p = $line -split ";"
        if ($p.Count -lt 4 -or $p[1] -notlike "railrush-*") { continue }
        try {
            $cpu = [double]::Parse(($p[2] -replace "%", ""), $Inv)
            $rows.Add([pscustomobject]@{ Name = $p[1]; Cpu = $cpu; Mem = (ConvertTo-MiB (($p[3] -split "/")[0].Trim())) })
        } catch { continue }
    }
    $rows | Group-Object Name | ForEach-Object {
        [pscustomobject]@{
            Container = $_.Name
            PeakCpuPct = [math]::Round(($_.Group | Measure-Object Cpu -Maximum).Maximum, 0)
            AvgCpuPct = [math]::Round(($_.Group | Measure-Object Cpu -Average).Average, 0)
            PeakMemMiB = [math]::Round(($_.Group | Measure-Object Mem -Maximum).Maximum, 0)
        }
    } | Sort-Object PeakCpuPct -Descending | Format-Table -AutoSize
}

Write-Host "=== Container health ===" -ForegroundColor Cyan
docker compose ps -q | ForEach-Object { docker inspect -f '{{.Name}} restarts={{.RestartCount}} oomkilled={{.State.OOMKilled}}' $_ }

Write-Host "`n=== nginx view: requests by endpoint (rt = full request time in ms) ===" -ForegroundColor Cyan
$logLines = @(docker compose logs --no-log-prefix --since $startUtc nginx)
$re = [regex]'^\S+ "(?<m>[A-Z]+) (?<u>\S+)" (?<s>\d{3}) \d+ rt=(?<rt>[\d.]+) urt=\S+ cache=(?<c>\S+)'
$uuid = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
$groups = @{}
$cacheCount = @{}
foreach ($line in $logLines) {
    $m = $re.Match("$line")
    if (-not $m.Success) { continue }
    $path = [regex]::Replace([regex]::Replace($m.Groups["u"].Value, $uuid, "<id>"), '/\d+', '/<n>')
    $key = $m.Groups["m"].Value + " " + $path
    if (-not $groups.ContainsKey($key)) {
        $groups[$key] = [pscustomobject]@{ Endpoint = $key; Count = 0; S2xx = 0; S4xx = 0; S429 = 0; S5xx = 0; Rt = (New-Object System.Collections.Generic.List[double]) }
    }
    $g = $groups[$key]
    $g.Count++
    $status = [int]$m.Groups["s"].Value
    if ($status -eq 429) { $g.S429++ } elseif ($status -ge 500) { $g.S5xx++ } elseif ($status -ge 400) { $g.S4xx++ } else { $g.S2xx++ }
    $g.Rt.Add([double]::Parse($m.Groups["rt"].Value, $Inv))
    $c = $m.Groups["c"].Value
    $cacheCount[$c] = 1 + [int]$cacheCount[$c]
}
$groups.Values | Sort-Object Count -Descending | Select-Object -First 16 | ForEach-Object {
    $sorted = @($_.Rt | Sort-Object)
    [pscustomobject]@{
        Endpoint = $_.Endpoint; Count = $_.Count; Ok2xx = $_.S2xx; Err4xx = $_.S4xx; Throttled429 = $_.S429; Err5xx = $_.S5xx
        P50ms = [math]::Round((Get-Percentile $sorted 0.5) * 1000, 0); P95ms = [math]::Round((Get-Percentile $sorted 0.95) * 1000, 0)
    }
} | Format-Table -AutoSize
Write-Host ("nginx cache statuses: " + (($cacheCount.GetEnumerator() | ForEach-Object { $_.Key + "=" + $_.Value }) -join "  "))

Write-Host "`n=== Where the time goes (ms): nginx total vs upstream wait vs gunicorn service time ===" -ForegroundColor Cyan
$ngRe2 = [regex]'^\S+ "(?<m>[A-Z]+) (?<u>\S+)" (?<s>\d{3}) \d+ rt=(?<rt>[\d.]+) urt=(?<urt>[\d.]+)'
$gunRe = [regex]'^\S+ "(?<m>[A-Z]+) (?<u>\S+)" (?<s>\d{3}) \d+ (?<t>[\d.]+)\s*$'
$ngTotal = @{}
$ngUpstream = @{}
$gunService = @{}
function Add-Sample($Table, [string]$Key, [double]$Value) {
    if (-not $Table.ContainsKey($Key)) { $Table[$Key] = New-Object System.Collections.Generic.List[double] }
    $Table[$Key].Add($Value)
}
function Get-EndpointKey([string]$Method, [string]$Path) {
    return $Method + " " + [regex]::Replace([regex]::Replace($Path, $uuid, "<id>"), '/\d+', '/<n>')
}
foreach ($line in $logLines) {
    $m = $ngRe2.Match("$line")
    if (-not $m.Success) { continue }
    $key = Get-EndpointKey $m.Groups["m"].Value $m.Groups["u"].Value
    Add-Sample $ngTotal $key ([double]::Parse($m.Groups["rt"].Value, $Inv))
    Add-Sample $ngUpstream $key ([double]::Parse($m.Groups["urt"].Value, $Inv))
}
foreach ($line in @(docker compose logs --no-log-prefix --since $startUtc web)) {
    $m = $gunRe.Match("$line")
    if (-not $m.Success) { continue }
    Add-Sample $gunService (Get-EndpointKey $m.Groups["m"].Value $m.Groups["u"].Value) ([double]::Parse($m.Groups["t"].Value, $Inv))
}
@($ngTotal.Keys | Sort-Object { $ngTotal[$_].Count } -Descending | Select-Object -First 10) | ForEach-Object {
    $k = $_
    $a = @($ngTotal[$k] | Sort-Object)
    $b = @($ngUpstream[$k] | Sort-Object)
    $c = @($gunService[$k] | Sort-Object)
    [pscustomobject]@{
        Endpoint = $k
        N = $a.Count
        NginxP50 = [math]::Round((Get-Percentile $a 0.5) * 1000, 0)
        NginxP95 = [math]::Round((Get-Percentile $a 0.95) * 1000, 0)
        UpstreamP50 = [math]::Round((Get-Percentile $b 0.5) * 1000, 0)
        UpstreamP95 = [math]::Round((Get-Percentile $b 0.95) * 1000, 0)
        GunicornP50 = [math]::Round((Get-Percentile $c 0.5) * 1000, 0)
        GunicornP95 = [math]::Round((Get-Percentile $c 0.95) * 1000, 0)
    }
} | Format-Table -AutoSize
Write-Host "Reading it: nginx close to upstream = the time is inside the web tier. Upstream well above gunicorn = requests waited for a free worker thread. Gunicorn alone = real service time."

Write-Host "`n=== web container: errors since the start ===" -ForegroundColor Cyan
$webErrors = @(docker compose logs --no-log-prefix --since $startUtc web | Select-String -Pattern 'Traceback|WORKER TIMEOUT|Unhandled exception|CRITICAL')
Write-Host ("error lines: " + $webErrors.Count)
$webErrors | Select-Object -First 5 | ForEach-Object { $_.Line }

Write-Host "`n=== PostgreSQL ===" -ForegroundColor Cyan
Invoke-Psql ([System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "pgstats.sql")))

Write-Host "`n=== Data checks: the invariants that must hold ===" -ForegroundColor Cyan
Invoke-Psql ([System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "verify.sql")).Replace("__RUNS__", $runList))

Write-Host "`n=== Redis ===" -ForegroundColor Cyan
Invoke-Redis INFO memory | Where-Object { $_ -match '^(used_memory_human|used_memory_peak_human):' }
Write-Host "slowlog entries: $((Invoke-Redis SLOWLOG LEN) -join '')   celery queue length: $((Invoke-Redis -n 1 LLEN celery) -join '')"
Write-Host "slowest Redis commands (SLOWLOG GET 6: id, time, microseconds, command):"
Invoke-Redis SLOWLOG GET 6 | Select-Object -First 60
Write-Host "busiest commands (calls, microseconds per call):"
Invoke-Redis INFO commandstats | ForEach-Object {
    if ("$_" -match '^cmdstat_(\w+):calls=(\d+),usec=(\d+),usec_per_call=([\d.]+)') {
        [pscustomobject]@{ Command = $Matches[1]; Calls = [int64]$Matches[2]; TotalMs = [math]::Round([double]$Matches[3] / 1000, 0); UsecPerCall = [double]::Parse($Matches[4], $Inv) }
    }
} | Sort-Object TotalMs -Descending | Select-Object -First 8 | Format-Table -AutoSize
foreach ($id in $runIds) {
    $left = ((Invoke-Redis ZCARD ("hold:{" + $id + "}:z")) -join "").Trim()
    Write-Host "run $id live holds left in Redis: $left"
}

if ($Clean) {
    Write-Host "`n=== Cleanup ===" -ForegroundColor Cyan
    & "$PSScriptRoot\clean.ps1" -KeepUsers
}
Write-Host "`nResults saved in $outDir" -ForegroundColor Green
Stop-Transcript | Out-Null
