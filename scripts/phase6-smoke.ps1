<#
  Phase 6 end-to-end checks for seat holds, against the running Docker stack through nginx.
  Needs the Phase 4 demo data (train 101).   Usage:  .\scripts\phase6-smoke.ps1
  It creates two users named smoke6-*@example.test and one run whose booking is already open,
  gets both users admitted through the real waiting room, and removes everything at the end
  (run, users and the Redis keys of the run).
#>
param(
    [string]$BaseUrl = "http://127.0.0.1:8088"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$reportDir = Join-Path $root "security-reports"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
Start-Transcript -Path (Join-Path $reportDir ("phase-6-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")) | Out-Null

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

function Get-SeatMapLogLines([string]$RunId) {
    $raw = docker compose logs --no-log-prefix nginx 2>&1 | ForEach-Object { "$_" }
    $needle = '"GET /api/catalog/runs/' + $RunId + '/seats/" '
    return @($raw | Where-Object { $_.Contains($needle) })
}

# Lines added since a snapshot. Counting lines avoids comparing the Docker VM clock to this PC.
function Get-NewLines($Before, $After) {
    if ($After.Count -le $Before.Count) { return @() }
    return @($After[$Before.Count..($After.Count - 1)])
}

function Get-Available($Map, [string]$SeatClass) {
    return (@($Map.Json.availability) | Where-Object { $_.seat_class -eq $SeatClass }).available
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
foreach ($name in @("a", "b")) {
    $r = Invoke-Api POST "/api/auth/register/" @{ email = "smoke6-$name-$tag@example.test"; password = $pw }
    if ($r.Status -ne 201) {
        Write-Host "Could not register test user $name (status $($r.Status)). Wait a minute and rerun." -ForegroundColor Red
        Stop-Transcript | Out-Null
        exit 2
    }
    $users[$name] = @{ Authorization = "Bearer $($r.Json.access)" }
}

$setup = @(
    'from datetime import timedelta',
    'from django.utils import timezone',
    'from apps.catalog.models import Train, TrainRun',
    'train = Train.objects.get(number="101")',
    'now = timezone.now()',
    'departure = (now + timedelta(days=3)).replace(microsecond=0)',
    'run = TrainRun.objects.create(train=train, departure_at=departure, arrival_at=departure + timedelta(hours=18), booking_opens_at=now - timedelta(hours=1))',
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

# Get both users admitted through the real waiting room (booking is open, so admission is immediate).
$hold = @{}
foreach ($name in @("a", "b")) {
    $null = Invoke-Api POST "/api/waitroom/$runId/join/" $null $users[$name]
    $me = $null
    for ($i = 1; $i -le 20; $i++) {
        $me = Invoke-Api GET "/api/waitroom/$runId/me/" $null $users[$name]
        if ($me.Json.state -eq "admitted") { break }
        Start-Sleep -Milliseconds 150
    }
    $hold[$name] = @{ Authorization = $users[$name].Authorization; "X-Waitroom-Pass" = $me.Json.waitroom_pass }
}
Expect "Setup: two users registered and admitted by the waiting room" ([bool]$hold["a"]["X-Waitroom-Pass"] -and [bool]$hold["b"]["X-Waitroom-Pass"]) "run id $runId"

$holdUrl = "/api/holds/$runId/"
$releaseUrl = "/api/holds/$runId/release/"
$mapUrl = "/api/catalog/runs/$runId/seats/"

# --- 1. Seat map ---------------------------------------------------------------------------------
Write-Host "`n=== 1. Public seat map ===" -ForegroundColor Cyan
$map0 = Invoke-Api GET $mapUrl $null @{ Authorization = "Bearer not-a-real-token" }
$avail0 = (@($map0.Json.availability) | Sort-Object seat_class | ForEach-Object { "$($_.seat_class)=$($_.available)/$($_.total)" }) -join ","
Expect "Seat map: public (junk Authorization ignored), 8 coaches, everything available" (($map0.Status -eq 200) -and (@($map0.Json.coaches).Count -eq 8) -and ($avail0 -eq "ac_business=40/40,ac_standard=192/192,economy=288/288")) $avail0
Expect "Seat map: public and cacheable for 1 second" (($map0.Headers["cache-control"] -match "public") -and ($map0.Headers["cache-control"] -match "max-age=1")) "cache-control=$($map0.Headers['cache-control'])"
$strict = Invoke-Api GET "$mapUrl`?x=1"
Expect "Seat map: an unknown query parameter is 400" ($strict.Status -eq 400) "status=$($strict.Status)"
$unknown = Invoke-Api GET "/api/catalog/runs/999999999/seats/"
Expect "Seat map: unknown run is 404 in the JSON envelope" (($unknown.Status -eq 404) -and ($unknown.Json.error.code -eq "not_found")) "status=$($unknown.Status)"

# --- 2. Holding ----------------------------------------------------------------------------------
Write-Host "`n=== 2. Holding ===" -ForegroundColor Cyan
$noAuth = Invoke-Api POST $holdUrl @{ seats = @("1-1A") }
Expect "Hold without a token is 401" ($noAuth.Status -eq 401) "status=$($noAuth.Status)"
$noPass = Invoke-Api POST $holdUrl @{ seats = @("1-1A") } $users["a"]
Expect "Hold with sign-in but no waiting-room pass is 403" ($noPass.Status -eq 403) "status=$($noPass.Status)"
$h1 = Invoke-Api POST $holdUrl @{ seats = @("1-1A", "1-1B") } $hold["a"]
$left = ((Get-Instant $h1.Text "expires_at") - (Get-Instant $h1.Text "server_time")).TotalSeconds
Expect "User A holds two seats: 201, expiring in 300 seconds" (($h1.Status -eq 201) -and (@($h1.Json.seats).Count -eq 2) -and ($left -gt 298) -and ($left -lt 302)) "status=$($h1.Status) left=$left s"
Expect "Hold responses are private (no-store)" ($h1.Headers["cache-control"] -match "no-store")
$bad = Invoke-Api POST $holdUrl @{ seats = @("99-99Z") } $hold["a"]
Expect "A seat that does not exist is 400 and is not echoed back" (($bad.Status -eq 400) -and ($bad.Text -notmatch "99-99Z")) "status=$($bad.Status)"
$again = Invoke-Api POST $holdUrl @{ seats = @("1-1A") } $hold["a"]
Expect "Holding your own seat again is 200 and does not extend it" (($again.Status -eq 200) -and ((Get-Instant $again.Text "expires_at") -eq (Get-Instant $h1.Text "expires_at"))) "status=$($again.Status)"

# --- 3. Conflicts ----------------------------------------------------------------------------------
Write-Host "`n=== 3. Conflicts ===" -ForegroundColor Cyan
$conflict = Invoke-Api POST $holdUrl @{ seats = @("1-1C", "1-1A") } $hold["b"]
Expect "User B asking for a free seat and a taken seat gets 409 naming the taken one" (($conflict.Status -eq 409) -and ($conflict.Json.error.code -eq "seats_unavailable") -and ((@($conflict.Json.error.details.unavailable) -join ",") -eq "1-1A")) "status=$($conflict.Status)"
$mineB = Invoke-Api GET $holdUrl $null $users["b"]
Expect "...and got nothing at all (all or nothing); listing needs no pass" (($mineB.Status -eq 200) -and (@($mineB.Json.seats).Count -eq 0)) "seats=$(@($mineB.Json.seats).Count)"
$a2 = Invoke-Api POST $holdUrl @{ seats = @("1-2A", "1-2B") } $hold["a"]
$a3 = Invoke-Api POST $holdUrl @{ seats = @("1-3A") } $hold["a"]
Expect "A fifth seat for user A is refused (limit 4) with hold_limit_reached" (($a2.Status -eq 201) -and ($a3.Status -eq 409) -and ($a3.Json.error.code -eq "hold_limit_reached")) "second=$($a2.Status) fifth=$($a3.Status)"

# --- 4. Seat map reflects holds and nginx caches it ---------------------------------------------------------
Write-Host "`n=== 4. Seat map reflects holds, nginx caches it ===" -ForegroundColor Cyan
Start-Sleep -Seconds 3   # nginx expires 1 s cache entries on whole seconds, so a stale entry can live almost 2 s
$map1 = Invoke-Api GET $mapUrl
$taken = (@($map1.Json.coaches)[0].unavailable) -join ","
Expect "Seat map shows A's four seats as unavailable and economy down to 284" (($taken -eq "1-1A,1-1B,1-2A,1-2B") -and ((Get-Available $map1 "economy") -eq 284)) "unavailable=$taken economy=$(Get-Available $map1 'economy')"
$before = Get-SeatMapLogLines $runId
$codes = @()
for ($i = 1; $i -le 30; $i++) { $codes += (Invoke-Api GET $mapUrl).Status }
Start-Sleep -Seconds 1
$newLines = Get-NewLines $before (Get-SeatMapLogLines $runId)
$hits = @($newLines | Where-Object { $_ -match "cache=HIT" }).Count
$reached = @($newLines | Where-Object { $_ -match "cache=MISS|cache=EXPIRED|cache=UPDATING" }).Count
Write-Host "  30 identical seat map requests: nginx HIT=$hits, reached Django=$reached" -ForegroundColor DarkGray
Expect "30 identical seat map requests: all 200, most served by nginx" ((@($codes | Where-Object { $_ -ne 200 }).Count -eq 0) -and ($hits -ge 15) -and ($reached -le 5)) "HIT=$hits reached Django=$reached"
$before2 = Get-SeatMapLogLines $runId
$authMap = Invoke-Api GET $mapUrl $null $users["a"]
Start-Sleep -Seconds 1
$authLines = @(Get-NewLines $before2 (Get-SeatMapLogLines $runId))
Expect "A seat map request carrying Authorization bypasses the cache" (($authMap.Status -eq 200) -and ($authLines.Count -eq 1) -and ($authLines[0] -match "cache=BYPASS")) "log=$($authLines -join ' | ')"

# --- 5. Releasing ---------------------------------------------------------------------------------------
Write-Host "`n=== 5. Releasing ===" -ForegroundColor Cyan
$stranger = Invoke-Api POST $releaseUrl @{ seats = @("1-1A") } $users["b"]
$mineA = Invoke-Api GET $holdUrl $null $users["a"]
Expect "User B cannot release A's seat" (($stranger.Status -eq 200) -and (@($stranger.Json.released).Count -eq 0) -and (@($mineA.Json.seats).Count -eq 4)) "released=$(@($stranger.Json.released).Count) a_holds=$(@($mineA.Json.seats).Count)"
$one = Invoke-Api POST $releaseUrl @{ seats = @("1-2B") } $users["a"]
$bTakes = Invoke-Api POST $holdUrl @{ seats = @("1-2B") } $hold["b"]
Expect "A releases one seat and B can hold it straight away" (((@($one.Json.released) -join ",") -eq "1-2B") -and ($bTakes.Status -eq 201)) "release=$($one.Status) b_hold=$($bTakes.Status)"
$all = Invoke-Api POST $releaseUrl @{} $users["a"]
$mineA2 = Invoke-Api GET $holdUrl $null $users["a"]
Expect "A releases everything that is left (3 seats) and holds nothing afterwards" ((@($all.Json.released).Count -eq 3) -and (@($mineA2.Json.seats).Count -eq 0)) "released=$(@($all.Json.released).Count)"

# --- 6. Throttle (per user) ------------------------------------------------------------------------------
Write-Host "`n=== 6. Hold throttle ===" -ForegroundColor Cyan
$codes = @()
$last = $null
for ($i = 1; $i -le 35; $i++) {
    $last = Invoke-Api POST $holdUrl @{ seats = @("1-2B") } $hold["b"] -NoPace
    $codes += $last.Status
}
$first429 = [array]::IndexOf($codes, 429)
Expect "Repeated hold requests by one user hit the limit (429) within 30 requests" (($first429 -ge 20) -and ($first429 -le 30)) "first 429 at request $($first429 + 1)"
Expect "The 429 carries retry_after from Django" ($null -ne $last.Json.error.details.retry_after)

# --- Cleanup ---------------------------------------------------------------------------------------------------
$cleanup = @(
    'from apps.catalog.models import TrainRun',
    'from apps.accounts.models import User',
    "print(TrainRun.objects.filter(pk=$runId).delete())",
    "print(User.objects.filter(email__startswith='smoke6-').delete())"
) -join "`n"
$null = $cleanup | docker compose exec -T web python manage.py shell 2>&1
$removed = 0
foreach ($pattern in @("hold:{$runId}:*", "wr:{$runId}:*")) {
    $keys = @(docker compose exec -T redis redis-cli --scan --pattern $pattern 2>&1 | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    foreach ($k in $keys) { docker compose exec -T redis redis-cli DEL $k 2>&1 | Out-Null; $removed++ }
}
Write-Host "  cleanup: removed the test run, the test users and $removed Redis keys" -ForegroundColor DarkGray

# --- Summary ---------------------------------------------------------------------------------------------------
Write-Host "`n=== SUMMARY (phase 6 smoke) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) { Write-Host "$failed check(s) FAILED." -ForegroundColor Red } else { Write-Host "All $($results.Count) checks passed." -ForegroundColor Green }
Stop-Transcript | Out-Null
if ($failed -gt 0) { exit 1 }
exit 0
