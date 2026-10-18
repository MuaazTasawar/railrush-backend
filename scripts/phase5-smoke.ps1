<#
  Phase 5 end-to-end checks for the waiting room, against the running Docker stack through nginx.
  Needs the Phase 4 demo data (train 101).   Usage:  .\scripts\phase5-smoke.ps1
  Takes about two minutes: it creates a run whose booking opens in 25 seconds and waits for the
  first admission. It creates users named smoke5-*@example.test and one run; both are removed
  at the end, along with the run's Redis keys.
#>
param(
    [string]$BaseUrl = "http://127.0.0.1:8088"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$reportDir = Join-Path $root "security-reports"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
Start-Transcript -Path (Join-Path $reportDir ("phase-5-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")) | Out-Null

Add-Type -AssemblyName System.Net.Http
$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseProxy = $false
$http = New-Object System.Net.Http.HttpClient -ArgumentList $handler
$http.Timeout = [TimeSpan]::FromSeconds(30)

$results = New-Object System.Collections.Generic.List[object]

function Invoke-Api {
    param([string]$Method, [string]$Path, $Body = $null, [hashtable]$Headers = @{}, [switch]$NoPace)
    $ctorArgs = @([System.Net.Http.HttpMethod]::new($Method), "$BaseUrl$Path")
    $request = New-Object System.Net.Http.HttpRequestMessage -ArgumentList $ctorArgs
    if ($null -ne $Body) {
        $json = ConvertTo-Json -InputObject $Body -Compress -Depth 5
        $request.Content = New-Object System.Net.Http.StringContent -ArgumentList $json, ([Text.Encoding]::UTF8), "application/json"
    }
    foreach ($name in $Headers.Keys) {
        $request.Headers.TryAddWithoutValidation($name, [string]$Headers[$name]) | Out-Null
    }
    $response = $http.SendAsync($request).GetAwaiter().GetResult()
    $status = [int]$response.StatusCode
    $text = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    $hdr = @{}
    foreach ($h in $response.Headers) { $hdr[$h.Key.ToLower()] = ($h.Value -join ",") }
    foreach ($h in $response.Content.Headers) { $hdr[$h.Key.ToLower()] = ($h.Value -join ",") }
    $request.Dispose()
    $response.Dispose()
    $parsed = $null
    try { $parsed = $text | ConvertFrom-Json } catch { $parsed = $null }
    # Stay under nginx's 5 requests/second auth limiter when registering.
    if (-not $NoPace -and $Path.StartsWith("/api/auth/")) { Start-Sleep -Milliseconds 250 }
    [pscustomobject]@{ Status = $status; Json = $parsed; Text = $text; Headers = $hdr }
}

function Expect([string]$Name, [bool]$Condition, [string]$Detail = "") {
    $status = if ($Condition) { "PASS" } else { "FAIL" }
    $results.Add([pscustomobject]@{ Check = $Name; Status = $status; Detail = $Detail })
    $color = if ($Condition) { "Green" } else { "Red" }
    Write-Host ("[{0}] {1} {2}" -f $status, $Name, $Detail) -ForegroundColor $color
}

# Timestamps are read from the raw JSON text, because Windows PowerShell 5.1 turns them into local DateTime objects.
function Get-Instant([string]$Text, [string]$Name) {
    $m = [regex]::Match($Text, '"' + $Name + '"\s*:\s*"([^"]+)"')
    if (-not $m.Success) { return $null }
    return [DateTimeOffset]::Parse($m.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture)
}

function Get-StatusLogLines([string]$RunId) {
    $raw = docker compose logs --no-log-prefix nginx 2>&1 | ForEach-Object { "$_" }
    $needle = '"GET /api/waitroom/' + $RunId + '/status/" '
    return @($raw | Where-Object { $_.Contains($needle) })
}

# Lines added since a snapshot. Counting lines avoids comparing the Docker VM clock to this PC.
function Get-NewLines($Before, $After) {
    if ($After.Count -le $Before.Count) { return @() }
    return @($After[$Before.Count..($After.Count - 1)])
}

# --- Pre-flight and setup -------------------------------------------------------------------
$live = Invoke-Api GET "/api/health/live/"
if ($live.Status -ne 200) {
    Write-Host "Stack not reachable at $BaseUrl (status $($live.Status)). Start it with: docker compose up -d" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 2
}

$tag = [guid]::NewGuid().ToString("N").Substring(0, 8)
$pw = "Zebra-Quartz-Lantern-" + (Get-Random -Minimum 1000 -Maximum 9999)
Write-Host "`n=== Setup ===" -ForegroundColor Cyan

$users = @{}
foreach ($name in @("a", "b", "c", "d")) {
    $r = Invoke-Api POST "/api/auth/register/" @{ email = "smoke5-$name-$tag@example.test"; password = $pw }
    if ($r.Status -ne 201) {
        Write-Host "Could not register test user $name (status $($r.Status)). Wait a minute and rerun." -ForegroundColor Red
        Stop-Transcript | Out-Null
        exit 2
    }
    $users[$name] = @{ Authorization = "Bearer $($r.Json.access)" }
}
Expect "Setup: 4 test users registered" ($users.Count -eq 4)

$setup = @(
    'from datetime import timedelta',
    'from django.utils import timezone',
    'from apps.catalog.models import Train, TrainRun',
    'train = Train.objects.get(number="101")',
    'now = timezone.now()',
    'departure = (now + timedelta(days=3)).replace(microsecond=0)',
    'run = TrainRun.objects.create(train=train, departure_at=departure, arrival_at=departure + timedelta(hours=18), booking_opens_at=now + timedelta(seconds=25))',
    'print("RUN_ID=%d" % run.id)'
) -join "`n"
$setupOut = ($setup | docker compose exec -T web python manage.py shell 2>&1 | ForEach-Object { "$_" }) -join "`n"
$m = [regex]::Match($setupOut, 'RUN_ID=(\d+)')
if (-not $m.Success) {
    Write-Host "Could not create the test run (is the demo data loaded? run: docker compose exec -T web python manage.py seed_demo)" -ForegroundColor Red
    Write-Host $setupOut
    Stop-Transcript | Out-Null
    exit 2
}
$runId = $m.Groups[1].Value
Write-Host "  test run id $runId opens in about 25 seconds" -ForegroundColor DarkGray

# --- 1. Public status ---------------------------------------------------------------------------
Write-Host "`n=== 1. Public status ===" -ForegroundColor Cyan
$statusUrl = "/api/waitroom/$runId/status/"
$st0 = Invoke-Api GET $statusUrl
$fields = @("run_id", "state", "opens_at", "joins_open_at", "admit_per_second", "tickets_issued", "people_waiting", "estimated_wait_seconds", "server_time")
$diff = @(Compare-Object @($st0.Json.PSObject.Properties.Name) $fields)
Expect "Status: 200 for anyone, open for joining, queue empty" (($st0.Status -eq 200) -and ($st0.Json.state -eq "open") -and ($st0.Json.tickets_issued -eq 0)) "status=$($st0.Status) state=$($st0.Json.state)"
Expect "Status: exactly the documented fields" ($diff.Count -eq 0) "unexpected=$($diff.Count)"
Expect "Status: public and cacheable for 1 second" (($st0.Headers["cache-control"] -match "public") -and ($st0.Headers["cache-control"] -match "max-age=1")) "cache-control=$($st0.Headers['cache-control'])"
$strict = Invoke-Api GET "$statusUrl`?x=1"
Expect "Status: an unknown query parameter is 400" ($strict.Status -eq 400) "status=$($strict.Status)"
$unknownRun = Invoke-Api GET "/api/waitroom/999999999/status/"
Expect "Status: unknown run is 404 in the JSON envelope" (($unknownRun.Status -eq 404) -and ($unknownRun.Json.error.code -eq "not_found")) "status=$($unknownRun.Status)"

# --- 2. Sign-in required ------------------------------------------------------------------------
Write-Host "`n=== 2. Sign-in required ===" -ForegroundColor Cyan
$noAuthJoin = Invoke-Api POST "/api/waitroom/$runId/join/"
$noAuthMe = Invoke-Api GET "/api/waitroom/$runId/me/"
Expect "Join and me without a token are 401" (($noAuthJoin.Status -eq 401) -and ($noAuthMe.Status -eq 401)) "join=$($noAuthJoin.Status) me=$($noAuthMe.Status)"

# --- 3. Queueing ------------------------------------------------------------------------------------
Write-Host "`n=== 3. Queueing ===" -ForegroundColor Cyan
$ja = Invoke-Api POST "/api/waitroom/$runId/join/" $null $users["a"]
Expect "User A joins: 201, waiting, ticket 1" (($ja.Status -eq 201) -and ($ja.Json.state -eq "waiting") -and ($ja.Json.ticket -eq 1)) "status=$($ja.Status) state=$($ja.Json.state) ticket=$($ja.Json.ticket)"
Expect "The wait is counted from the opening time (about 25 seconds)" (($ja.Json.wait_seconds -gt 3) -and ($ja.Json.wait_seconds -lt 26)) "wait_seconds=$($ja.Json.wait_seconds)"
Expect "Join response is private (no-store) and has no pass yet" (($ja.Headers["cache-control"] -match "no-store") -and ($null -eq $ja.Json.waitroom_pass))
$ja2 = Invoke-Api POST "/api/waitroom/$runId/join/" $null $users["a"]
Expect "Joining twice returns the same ticket (200)" (($ja2.Status -eq 200) -and ($ja2.Json.ticket -eq 1)) "status=$($ja2.Status) ticket=$($ja2.Json.ticket)"
$jb = Invoke-Api POST "/api/waitroom/$runId/join/" $null $users["b"]
$jc = Invoke-Api POST "/api/waitroom/$runId/join/" $null $users["c"]
Expect "Users B and C get tickets 2 and 3" (($jb.Json.ticket -eq 2) -and ($jc.Json.ticket -eq 3)) "b=$($jb.Json.ticket) c=$($jc.Json.ticket)"
$ia = Get-Instant $ja.Text "admit_at"
$ib = Get-Instant $jb.Text "admit_at"
$ic = Get-Instant $jc.Text "admit_at"
$gap = ($ic - $ib).TotalMilliseconds
Expect "Admissions are in join order and spaced 1/rate (20 ms) apart" (($ia -lt $ib) -and ($ib -lt $ic) -and ($gap -gt 10) -and ($gap -lt 40)) "gap=$gap ms"
Start-Sleep -Seconds 3   # nginx expires cache entries on whole seconds, so a 1 s entry can live almost 2 s   # the public status is micro-cached for 1 second, so let the cached copy expire first
$st1 = Invoke-Api GET $statusUrl
Expect "Status counts the queue (3 tickets, people waiting)" (($st1.Json.tickets_issued -eq 3) -and ($st1.Json.people_waiting -ge 1)) "issued=$($st1.Json.tickets_issued) waiting=$($st1.Json.people_waiting)"
$meD = Invoke-Api GET "/api/waitroom/$runId/me/" $null $users["d"]
Expect "Me for someone who has not joined is 404" (($meD.Status -eq 404) -and ($meD.Json.error.code -eq "not_found")) "status=$($meD.Status)"
$meA0 = Invoke-Api GET "/api/waitroom/$runId/me/" $null $users["a"]
Expect "Me before admission: waiting, no pass" (($meA0.Json.state -eq "waiting") -and ($null -eq $meA0.Json.waitroom_pass)) "state=$($meA0.Json.state)"

# --- 4. nginx micro-cache on the public status ------------------------------------------------------
Write-Host "`n=== 4. nginx micro-cache ===" -ForegroundColor Cyan
$before = Get-StatusLogLines $runId
$codes = @()
for ($i = 1; $i -le 30; $i++) {
    $r = Invoke-Api GET $statusUrl
    $codes += $r.Status
}
Start-Sleep -Seconds 1
$newLines = Get-NewLines $before (Get-StatusLogLines $runId)
$hits = @($newLines | Where-Object { $_ -match "cache=HIT" }).Count
$reached = @($newLines | Where-Object { $_ -match "cache=MISS|cache=EXPIRED|cache=UPDATING" }).Count
Write-Host "  30 identical status requests: nginx HIT=$hits, reached Django=$reached" -ForegroundColor DarkGray
Expect "30 identical status requests all returned 200" (@($codes | Where-Object { $_ -ne 200 }).Count -eq 0)
Expect "Most were served by nginx, Django saw only a handful" (($hits -ge 15) -and ($reached -le 5)) "HIT=$hits reached Django=$reached"
$before2 = Get-StatusLogLines $runId
$authStatus = Invoke-Api GET $statusUrl $null $users["a"]
Start-Sleep -Seconds 1
$authLines = @(Get-NewLines $before2 (Get-StatusLogLines $runId))
Expect "A status request carrying Authorization bypasses the cache" (($authStatus.Status -eq 200) -and ($authLines.Count -eq 1) -and ($authLines[0] -match "cache=BYPASS")) "log=$($authLines -join ' | ')"

# --- 5. Admission and the pass ---------------------------------------------------------------------
Write-Host "`n=== 5. Admission and the pass ===" -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(80)
$meA = $null
do {
    $meA = Invoke-Api GET "/api/waitroom/$runId/me/" $null $users["a"]
    if ($meA.Json.state -eq "admitted") { break }
    Start-Sleep -Seconds 2
} while ((Get-Date) -lt $deadline)
Expect "User A is admitted once booking opens, with a pass" (($meA.Json.state -eq "admitted") -and [bool]$meA.Json.waitroom_pass) "state=$($meA.Json.state)"
$window = ((Get-Instant $meA.Text "pass_expires_at") - (Get-Instant $meA.Text "admit_at")).TotalSeconds
Expect "The pass is valid for 10 minutes from the admission time" (($window -gt 598) -and ($window -lt 602)) "window=$window s"
Expect "Me responses are private (no-store)" ($meA.Headers["cache-control"] -match "no-store")
Start-Sleep -Seconds 1
$meC = Invoke-Api GET "/api/waitroom/$runId/me/" $null $users["c"]
Expect "User C, 40 ms behind A, is admitted too" ($meC.Json.state -eq "admitted") "state=$($meC.Json.state)"
$stEnd = Invoke-Api GET $statusUrl
Expect "Status now shows nobody left waiting" ($stEnd.Json.people_waiting -eq 0) "waiting=$($stEnd.Json.people_waiting)"

# --- 6. Join throttle (per user) ----------------------------------------------------------------------
Write-Host "`n=== 6. Join throttle ===" -ForegroundColor Cyan
$codes = @()
$last = $null
for ($i = 1; $i -le 21; $i++) {
    $last = Invoke-Api POST "/api/waitroom/$runId/join/" $null $users["d"] -NoPace
    $codes += $last.Status
}
$earlyOk = @($codes[0..19] | Where-Object { $_ -ge 400 }).Count -eq 0
Expect "21 joins by one user: the first 20 pass (201 then 200s), the 21st is 429" ($earlyOk -and ($codes[0] -eq 201) -and ($codes[20] -eq 429)) "first=$($codes[0]) last=$($codes[20])"
Expect "The 429 carries retry_after from Django" ($null -ne $last.Json.error.details.retry_after)

# --- Cleanup ---------------------------------------------------------------------------------------------------
$cleanup = @(
    'from apps.catalog.models import TrainRun',
    'from apps.accounts.models import User',
    "print(TrainRun.objects.filter(pk=$runId).delete())",
    "print(User.objects.filter(email__startswith='smoke5-').delete())"
) -join "`n"
$null = $cleanup | docker compose exec -T web python manage.py shell 2>&1
$keys = @(docker compose exec -T redis redis-cli --scan --pattern "wr:{$runId}:*" 2>&1 | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
foreach ($k in $keys) { docker compose exec -T redis redis-cli DEL $k 2>&1 | Out-Null }
Write-Host "  cleanup: removed the test run, the test users and $($keys.Count) Redis keys" -ForegroundColor DarkGray

# --- Summary ---------------------------------------------------------------------------------------------------
Write-Host "`n=== SUMMARY (phase 5 smoke) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) { Write-Host "$failed check(s) FAILED." -ForegroundColor Red } else { Write-Host "All $($results.Count) checks passed." -ForegroundColor Green }
Stop-Transcript | Out-Null
if ($failed -gt 0) { exit 1 }
exit 0
