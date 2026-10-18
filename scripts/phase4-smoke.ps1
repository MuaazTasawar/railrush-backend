<#
  Phase 4 end-to-end checks for the public catalog, against the running Docker stack through nginx.
  Needs the demo data:  docker compose exec -T web python manage.py seed_demo
  Usage:  .\scripts\phase4-smoke.ps1
          .\scripts\phase4-smoke.ps1 -BaseUrl http://127.0.0.1:8088
  Takes under a minute. It uses up this IP's anonymous throttle for about a minute at the end.
#>
param(
    [string]$BaseUrl = "http://127.0.0.1:8088"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$reportDir = Join-Path $root "security-reports"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
Start-Transcript -Path (Join-Path $reportDir ("phase-4-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")) | Out-Null

Add-Type -AssemblyName System.Net.Http
$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseProxy = $false
$http = New-Object System.Net.Http.HttpClient -ArgumentList $handler
$http.Timeout = [TimeSpan]::FromSeconds(30)

$results = New-Object System.Collections.Generic.List[object]

function Invoke-Api {
    param([string]$Method, [string]$Path, [hashtable]$Headers = @{})
    $ctorArgs = @([System.Net.Http.HttpMethod]::new($Method), "$BaseUrl$Path")
    $request = New-Object System.Net.Http.HttpRequestMessage -ArgumentList $ctorArgs
    foreach ($name in $Headers.Keys) {
        $request.Headers.TryAddWithoutValidation($name, [string]$Headers[$name]) | Out-Null
    }
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $response = $http.SendAsync($request).GetAwaiter().GetResult()
    $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    $watch.Stop()
    $status = [int]$response.StatusCode
    $hdr = @{}
    foreach ($h in $response.Headers) { $hdr[$h.Key.ToLower()] = ($h.Value -join ",") }
    foreach ($h in $response.Content.Headers) { $hdr[$h.Key.ToLower()] = ($h.Value -join ",") }
    $request.Dispose()
    $response.Dispose()
    $parsed = $null
    try { $parsed = $text | ConvertFrom-Json } catch { $parsed = $null }
    [pscustomobject]@{ Status = $status; Json = $parsed; Text = $text; Headers = $hdr; Ms = $watch.Elapsed.TotalMilliseconds }
}

function Expect([string]$Name, [bool]$Condition, [string]$Detail = "") {
    $status = if ($Condition) { "PASS" } else { "FAIL" }
    $results.Add([pscustomobject]@{ Check = $Name; Status = $status; Detail = $Detail })
    $color = if ($Condition) { "Green" } else { "Red" }
    Write-Host ("[{0}] {1} {2}" -f $status, $Name, $Detail) -ForegroundColor $color
}

function Get-RunLogLines {
    $raw = docker compose logs --no-log-prefix nginx 2>&1 | ForEach-Object { "$_" }
    return @($raw | Where-Object { $_ -match '"GET /api/catalog/runs/" ' })
}

# Lines added since a snapshot. Counting lines avoids comparing the Docker VM clock to this PC.
function Get-NewLines($Before, $After) {
    if ($After.Count -le $Before.Count) { return @() }
    return @($After[$Before.Count..($After.Count - 1)])
}

function Get-NginxLog([datetime]$Since) {
    $stamp = $Since.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $lines = docker compose logs --no-log-prefix --since $stamp nginx 2>&1
    return @($lines | ForEach-Object { "$_" })
}

# --- Pre-flight ---------------------------------------------------------------------
$live = Invoke-Api GET "/api/health/live/"
if ($live.Status -ne 200) {
    Write-Host "Stack not reachable at $BaseUrl (status $($live.Status)). Start it with: docker compose up -d" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 2
}

# Pakistan is UTC+5 all year, so the local date is simply UTC plus five hours.
$today = [DateTime]::UtcNow.AddHours(5)
$day1 = $today.AddDays(1).ToString("yyyy-MM-dd")
$day3 = $today.AddDays(3).ToString("yyyy-MM-dd")
$day4 = $today.AddDays(4).ToString("yyyy-MM-dd")
$search = "/api/catalog/runs/?origin=KHI&destination=LHE&date=$day1"

# --- 1. Content ---------------------------------------------------------------------
Write-Host "`n=== 1. Catalog content ===" -ForegroundColor Cyan

$stations = Invoke-Api GET "/api/catalog/stations/" @{ Authorization = "Bearer not-a-real-token" }
Expect "Stations: 200 and a plain list of 10, even with a junk Authorization header" (($stations.Status -eq 200) -and (@($stations.Json).Count -eq 10)) "status=$($stations.Status)"
Expect "Stations: public, cacheable for 60 seconds" (($stations.Headers["cache-control"] -match "public") -and ($stations.Headers["cache-control"] -match "max-age=60")) "cache-control=$($stations.Headers['cache-control'])"

$found = Invoke-Api GET $search
$first = $found.Json.results[0]
Expect "Search KHI to LHE tomorrow: 200 with at least one run" (($found.Status -eq 200) -and ($found.Json.count -ge 1)) "status=$($found.Status) count=$($found.Json.count)"
Expect "Search: public, cacheable for 1 second" (($found.Headers["cache-control"] -match "public") -and ($found.Headers["cache-control"] -match "max-age=1")) "cache-control=$($found.Headers['cache-control'])"
$expectedKeys = @("id", "train_number", "train_name", "origin", "destination", "departure_at", "arrival_at", "booking_opens_at", "status", "classes")
$diff = @(Compare-Object @($first.PSObject.Properties.Name) $expectedKeys)
Expect "Search: each run has exactly the documented fields" ($diff.Count -eq 0) "unexpected=$($diff.Count)"
$cap = @{}
foreach ($c in $first.classes) { $cap[$c.seat_class] = $c.seat_count }
$capText = ($cap.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ","
Expect "Search: seat capacity per class matches the seeded coaches" ($capText -eq "ac_business=40,ac_standard=192,economy=288") $capText

$detail = Invoke-Api GET "/api/catalog/runs/$($first.id)/"
Expect "Run detail: 200 with all 8 coaches" (($detail.Status -eq 200) -and (@($detail.Json.coaches).Count -eq 8)) "status=$($detail.Status)"
$missing = Invoke-Api GET "/api/catalog/runs/999999999/"
Expect "Unknown run: 404 in the JSON error envelope" (($missing.Status -eq 404) -and ($missing.Json.error.code -eq "not_found")) "status=$($missing.Status)"

# --- 2. Input validation ----------------------------------------------------------------
Write-Host "`n=== 2. Input validation ===" -ForegroundColor Cyan
$noDate = Invoke-Api GET "/api/catalog/runs/?origin=KHI&destination=LHE"
Expect "Search without a date is 400" (($noDate.Status -eq 400) -and ($noDate.Json.error.code -eq "validation_error")) "status=$($noDate.Status)"
$pastDay = $today.AddDays(-1).ToString("yyyy-MM-dd")
$past = Invoke-Api GET "/api/catalog/runs/?origin=KHI&destination=LHE&date=$pastDay"
Expect "Search for a past date is 400" ($past.Status -eq 400) "status=$($past.Status)"
$unknown = Invoke-Api GET "/api/catalog/runs/?origin=KHI&destination=LHE&date=$day1&evilparam=zzq9"
Expect "Unknown query parameter is 400 and is not echoed back" (($unknown.Status -eq 400) -and ($unknown.Text -notmatch "zzq9") -and ($unknown.Text -notmatch "evilparam")) "status=$($unknown.Status)"
Expect "Error bodies never contain a traceback or file path" (($unknown.Text -notmatch "Traceback") -and ($unknown.Text -notmatch "\.py"))

# --- 3. nginx micro-cache ---------------------------------------------------------------
Write-Host "`n=== 3. nginx micro-cache ===" -ForegroundColor Cyan
$before = Get-RunLogLines
Start-Sleep -Seconds 2
$times = @()
$codes = @()
for ($i = 1; $i -le 30; $i++) {
    $r = Invoke-Api GET "/api/catalog/runs/?origin=KHI&destination=LHE&date=$day3"
    $codes += $r.Status
    $times += $r.Ms
}
Start-Sleep -Seconds 1
$lines = @(Get-NewLines $before (Get-RunLogLines) | Where-Object { $_ -match '" 200 ' })
$hits = @($lines | Where-Object { $_ -match "cache=HIT" }).Count
$miss = @($lines | Where-Object { $_ -match "cache=MISS|cache=EXPIRED|cache=UPDATING" }).Count
$median = [math]::Round((($times | Sort-Object)[[int]($times.Count / 2)]), 1)
Write-Host "  30 identical requests: median $median ms, nginx HIT=$hits, reached Django=$miss" -ForegroundColor DarkGray
Expect "Micro-cache: 30 identical searches all returned 200" (@($codes | Where-Object { $_ -ne 200 }).Count -eq 0) "codes ok=$(@($codes | Where-Object { $_ -eq 200 }).Count)/30"
Expect "Micro-cache: most identical searches were served by nginx (HIT)" ($hits -ge 15) "HIT=$hits MISS-or-refresh=$miss"
Expect "Micro-cache: Django saw only a handful of the 30" ($miss -le 5) "reached Django=$miss"

$before2 = Get-RunLogLines
Start-Sleep -Seconds 2
$auth = Invoke-Api GET "/api/catalog/runs/?origin=KHI&destination=LHE&date=$day4" @{ Authorization = "Bearer not-a-real-token" }
Start-Sleep -Seconds 1
$authLines = @(Get-NewLines $before2 (Get-RunLogLines))
Expect "A request carrying Authorization is never served from or stored in the cache" (($auth.Status -eq 200) -and ($authLines.Count -eq 1) -and ($authLines[0] -match "cache=BYPASS")) "log=$($authLines -join ' | ')"

# --- 4. Cache-busting query strings (last: it uses up this IP's anonymous throttle) --------
Write-Host "`n=== 4. Cache-busting query strings ===" -ForegroundColor Cyan
$codes = @()
$lastBust = $null
for ($i = 1; $i -le 70; $i++) {
    $lastBust = Invoke-Api GET "/api/catalog/runs/?origin=KHI&destination=LHE&date=$day1&bust=$i"
    $codes += $lastBust.Status
}
$distinct = ($codes | Sort-Object -Unique) -join ","
Expect "70 requests with a different junk parameter each never get a 200 (no cache bypass, no database work)" (($codes -notcontains 200) -and ($codes -contains 400)) "statuses seen=$distinct"
Expect "...and once the anonymous limit is hit they get 429 with retry_after" (($codes -contains 429) -and ($null -ne $lastBust.Json.error.details.retry_after)) "last=$($lastBust.Status)"

# --- Summary -----------------------------------------------------------------------------
Write-Host "`n=== SUMMARY (phase 4 smoke) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) { Write-Host "$failed check(s) FAILED." -ForegroundColor Red } else { Write-Host "All $($results.Count) checks passed." -ForegroundColor Green }
Stop-Transcript | Out-Null
if ($failed -gt 0) { exit 1 }
exit 0
