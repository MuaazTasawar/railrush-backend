<#
  Phase 7 end-to-end checks for bookings, payments and expiry, against the running Docker stack
  through nginx. Needs the Phase 4 demo data (train 101 with fares) and the beat service.
  Usage:  .\scripts\phase7-smoke.ps1      (about 2-3 minutes: it waits for Celery beat to expire bookings)
  It creates two users named smoke7-*@example.test and one run whose booking is already open,
  and removes everything at the end (bookings, payments, run, users and the run's Redis keys).
  The webhook secret is read from .env and is never printed.
#>
param(
    [string]$BaseUrl = "http://127.0.0.1:8088"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$reportDir = Join-Path $root "security-reports"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
Start-Transcript -Path (Join-Path $reportDir ("phase-7-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")) | Out-Null

Add-Type -AssemblyName System.Net.Http
$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseProxy = $false
$http = New-Object System.Net.Http.HttpClient -ArgumentList $handler
$http.Timeout = [TimeSpan]::FromSeconds(30)

$results = New-Object System.Collections.Generic.List[object]

function Invoke-Api {
    param([string]$Method, [string]$Path, $Body = $null, [hashtable]$Headers = @{}, [string]$RawBody = "", [switch]$NoPace)
    $ctorArgs = @([System.Net.Http.HttpMethod]::new($Method), "$BaseUrl$Path")
    $request = New-Object System.Net.Http.HttpRequestMessage -ArgumentList $ctorArgs
    if ($RawBody) {
        $request.Content = New-Object System.Net.Http.StringContent -ArgumentList $RawBody, ([Text.Encoding]::UTF8), "application/json"
    } elseif ($null -ne $Body) {
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

function Get-Available($Map, [string]$SeatClass) {
    return (@($Map.Json.availability) | Where-Object { $_.seat_class -eq $SeatClass }).available
}

function Get-Items($Response) {
    if ($Response.Json -is [System.Array]) { return @($Response.Json) }
    if ($null -ne $Response.Json.results) { return @($Response.Json.results) }
    return @()
}

function Invoke-Shell([string[]]$Lines) {
    $code = $Lines -join "`n"
    return (($code | docker compose exec -T web python manage.py shell 2>&1 | ForEach-Object { "$_" }) -join "`n")
}

function Get-Signature([string]$Secret, [string]$Body) {
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [Text.Encoding]::UTF8.GetBytes($Secret)
    $hash = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($Body))
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
}

function New-Event([string]$Ref, [string]$Status, [int]$Amount) {
    return '{"provider_ref":"' + $Ref + '","status":"' + $Status + '","amount_pkr":' + $Amount + '}'
}

function Send-Webhook([string]$Raw, [string]$Signature) {
    $headers = @{}
    if ($Signature) { $headers["X-Gateway-Signature"] = $Signature }
    return (Invoke-Api POST "/api/payments/webhook/" $null $headers -RawBody $Raw)
}

function Get-BookHeaders([string]$Name, [string]$Key) {
    return @{ Authorization = $users[$Name].Authorization; "Idempotency-Key" = $Key }
}

function New-Booking([string]$Name, [string]$Key, $Body = $null) {
    if ($null -eq $Body) { $Body = @{ run_id = [int64]$runId } }
    return (Invoke-Api POST "/api/bookings/" $Body (Get-BookHeaders $Name $Key))
}

function Get-Booking([string]$Name, [string]$Id) {
    return (Invoke-Api GET "/api/bookings/$Id/" $null $users[$Name])
}

function Start-Pay([string]$Name, [string]$Id) {
    return (Invoke-Api POST "/api/bookings/$Id/pay/" @{} $users[$Name])
}

# --- Pre-flight ----------------------------------------------------------------------------------
$live = Invoke-Api GET "/api/health/live/"
if ($live.Status -ne 200) {
    Write-Host "Stack not reachable at $BaseUrl (status $($live.Status)). Start it with: docker compose up -d" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 2
}
$running = @(docker compose ps --status running --services 2>&1 | ForEach-Object { "$_".Trim() })
if ($running -notcontains "beat") {
    Write-Host "The beat service is not running. Start it with: docker compose up -d --build" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 2
}
$secret = $null
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*PAYMENT_WEBHOOK_SECRET\s*=' } | Select-Object -First 1
    if ($line) { $secret = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'") }
}
if (-not $secret) {
    Write-Host "PAYMENT_WEBHOOK_SECRET is missing from .env." -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 2
}

$tag = [guid]::NewGuid().ToString("N").Substring(0, 8)
$pw = "Zebra-Quartz-Lantern-" + (Get-Random -Minimum 1000 -Maximum 9999)
$runId = $null
$users = @{}
$hold = @{}

try {
Write-Host "`n=== Setup ===" -ForegroundColor Cyan
foreach ($name in @("a", "b")) {
    $r = Invoke-Api POST "/api/auth/register/" @{ email = "smoke7-$name-$tag@example.test"; password = $pw }
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
    'from apps.catalog.models import Fare, Train, TrainRun',
    'train = Train.objects.get(number="101")',
    'now = timezone.now()',
    'departure = (now + timedelta(days=3)).replace(microsecond=0)',
    'run = TrainRun.objects.create(train=train, departure_at=departure, arrival_at=departure + timedelta(hours=18), booking_opens_at=now - timedelta(hours=1))',
    'print("RUN_ID=%d" % run.id)',
    'for fare in Fare.objects.filter(train=train):',
    '    print("FARE_%s=%d" % (fare.seat_class, fare.amount_pkr))'
)
$setupOut = Invoke-Shell $setup
$m = [regex]::Match($setupOut, 'RUN_ID=(\d+)')
$f = [regex]::Match($setupOut, 'FARE_economy=(\d+)')
if (-not $m.Success -or -not $f.Success) {
    Write-Host "Could not create the test run or find the economy fare (is the demo data loaded? run: docker compose exec -T web python manage.py seed_demo)" -ForegroundColor Red
    Write-Host $setupOut
    Stop-Transcript | Out-Null
    exit 2
}
$runId = $m.Groups[1].Value
$fare = [int]$f.Groups[1].Value

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
Expect "Setup: two users admitted by the waiting room, beat running" ([bool]$hold["a"]["X-Waitroom-Pass"] -and [bool]$hold["b"]["X-Waitroom-Pass"]) "run id $runId, economy fare $fare PKR"

$holdUrl = "/api/holds/$runId/"
$releaseUrl = "/api/holds/$runId/release/"
$mapUrl = "/api/catalog/runs/$runId/seats/"
$bookUrl = "/api/bookings/"

# --- 1. Input and sign-in checks -----------------------------------------------------------------
Write-Host "`n=== 1. Sign-in and input checks ===" -ForegroundColor Cyan
$noTok = Invoke-Api POST $bookUrl @{ run_id = [int64]$runId } @{ "Idempotency-Key" = "smoke7-anon-$tag" }
Expect "Booking without a token is 401" ($noTok.Status -eq 401) "status=$($noTok.Status)"
$noKey = Invoke-Api POST $bookUrl @{ run_id = [int64]$runId } $users["a"]
Expect "Booking without an Idempotency-Key is 400" ($noKey.Status -eq 400) "status=$($noKey.Status)"
$badKey = Invoke-Api POST $bookUrl @{ run_id = [int64]$runId } (Get-BookHeaders "a" "bad key!")
Expect "Booking with a malformed Idempotency-Key is 400" ($badKey.Status -eq 400) "status=$($badKey.Status)"
$noHolds = New-Booking "a" "smoke7-noholds-$tag"
Expect "Booking with no seat holds is 409" ($noHolds.Status -eq 409) "status=$($noHolds.Status)"

# --- 2. Booking 1: hold, book, retry ----------------------------------------------------------------
Write-Host "`n=== 2. Booking from holds ===" -ForegroundColor Cyan
$h1 = Invoke-Api POST $holdUrl @{ seats = @("1-1A", "1-1B") } $hold["a"]
$key1 = "smoke7-b1-$tag"
$b1 = New-Booking "a" $key1 @{ run_id = [int64]$runId; total_pkr = 1; price_pkr = 1 }
$id1 = $b1.Json.id
$left1 = ((Get-Instant $b1.Text "expires_at") - (Get-Instant $b1.Text "created_at")).TotalSeconds
Expect "Booking is 201 pending_payment with exactly the held seats" (($h1.Status -eq 201) -and ($b1.Status -eq 201) -and ($b1.Json.status -eq "pending_payment") -and (@($b1.Json.seats).Count -eq 2)) "hold=$($h1.Status) booking=$($b1.Status)"
Expect "The price comes from the catalog, not the client (total = 2 x fare)" ($b1.Json.total_pkr -eq (2 * $fare)) "total=$($b1.Json.total_pkr) expected=$(2 * $fare)"
Expect "The payment window is about 600 seconds" (($left1 -gt 595) -and ($left1 -lt 605)) "window=$left1 s"
Expect "Booking responses are private (no-store)" ($b1.Headers["cache-control"] -match "no-store")
$mine = Invoke-Api GET $holdUrl $null $users["a"]
Expect "The Redis holds were released after the booking was saved" (($mine.Status -eq 200) -and (@($mine.Json.seats).Count -eq 0)) "holds=$(@($mine.Json.seats).Count)"
$retry = New-Booking "a" $key1
$listA = @(Get-Items (Invoke-Api GET $bookUrl $null $users["a"]))
Expect "Retrying with the same key is 200, the same booking, and no duplicate" (($retry.Status -eq 200) -and ($retry.Json.id -eq $id1) -and ($listA.Count -eq 1)) "status=$($retry.Status) bookings=$($listA.Count)"
Start-Sleep -Seconds 3
$map1 = Invoke-Api GET $mapUrl
$taken1 = (@($map1.Json.coaches)[0].unavailable) -join ","
Expect "The seat map shows the booked seats as unavailable (economy 286)" (($taken1 -eq "1-1A,1-1B") -and ((Get-Available $map1 "economy") -eq 286)) "unavailable=$taken1 economy=$(Get-Available $map1 'economy')"
$bHold = Invoke-Api POST $holdUrl @{ seats = @("1-1A") } $hold["b"]
Expect "Someone else cannot hold a booked seat (409 seats_unavailable)" (($bHold.Status -eq 409) -and ($bHold.Json.error.code -eq "seats_unavailable")) "status=$($bHold.Status)"

# --- 3. Ownership ------------------------------------------------------------------------------------
Write-Host "`n=== 3. Ownership (other users get 404) ===" -ForegroundColor Cyan
$sGet = Get-Booking "b" $id1
$sPay = Start-Pay "b" $id1
$sCancel = Invoke-Api POST "/api/bookings/$id1/cancel/" @{} $users["b"]
$listB = @(Get-Items (Invoke-Api GET $bookUrl $null $users["b"]))
Expect "User B cannot read, pay or cancel A's booking (all 404) and sees none in the list" (($sGet.Status -eq 404) -and ($sPay.Status -eq 404) -and ($sCancel.Status -eq 404) -and ($listB.Count -eq 0)) "get=$($sGet.Status) pay=$($sPay.Status) cancel=$($sCancel.Status) b_list=$($listB.Count)"

# --- 4. Payment and webhook security ----------------------------------------------------------------------
Write-Host "`n=== 4. Payment and webhook ===" -ForegroundColor Cyan
$pay1 = Start-Pay "a" $id1
$pay1b = Start-Pay "a" $id1
$ref1 = $pay1.Json.payment.provider_ref
$total1 = [int]$pay1.Json.payment.amount_pkr
Expect "Pay opens a payment for the full amount; paying again reuses it" (($pay1.Status -eq 201) -and ($pay1b.Status -eq 200) -and ($pay1b.Json.payment.id -eq $pay1.Json.payment.id) -and ($total1 -eq $b1.Json.total_pkr)) "first=$($pay1.Status) second=$($pay1b.Status) amount=$total1"
$raw1 = New-Event $ref1 "succeeded" $total1
$forged = @(
    (Send-Webhook $raw1 ("0" * 64)),
    (Send-Webhook $raw1 ""),
    (Send-Webhook $raw1 (Get-Signature ("wrong-" + $secret) $raw1)),
    (Send-Webhook (New-Event $ref1 "succeeded" 1) (Get-Signature $secret $raw1))
)
$forgedCodes = ($forged | ForEach-Object { $_.Status }) -join ","
$stillPending = Get-Booking "a" $id1
Expect "Forged webhooks (bad, missing, wrong-key and tampered signatures) are all 400" (($forgedCodes -eq "400,400,400,400") -and ($stillPending.Json.status -eq "pending_payment")) "codes=$forgedCodes booking=$($stillPending.Json.status)"
$garbage = "not json"
$big = '{"provider_ref":"' + ("x" * 17000) + '"}'
$gCodes = @((Send-Webhook $garbage (Get-Signature $secret $garbage)).Status, (Send-Webhook $big (Get-Signature $secret $big)).Status) -join ","
Expect "Signed garbage and an oversized body are rejected (400)" ($gCodes -eq "400,400") "codes=$gCodes"
$unknownRaw = New-Event "mock_does_not_exist" "succeeded" 100
$unknown = Send-Webhook $unknownRaw (Get-Signature $secret $unknownRaw)
Expect "A signed event for an unknown payment is 404" ($unknown.Status -eq 404) "status=$($unknown.Status)"
$ok = Send-Webhook $raw1 (Get-Signature $secret $raw1)
Expect "A correctly signed success is 200 and confirms the booking" (($ok.Status -eq 200) -and ($ok.Json.result -eq "confirmed")) "status=$($ok.Status) result=$($ok.Json.result)"
$confirmed = Get-Booking "a" $id1
Expect "The booking is confirmed with a confirmation time" (($confirmed.Json.status -eq "confirmed") -and ($null -ne $confirmed.Json.confirmed_at)) "status=$($confirmed.Json.status)"
$replay = Send-Webhook $raw1 (Get-Signature $secret $raw1)
$afterReplay = Get-Booking "a" $id1
Expect "Replaying the webhook changes nothing (duplicate)" (($replay.Status -eq 200) -and ($replay.Json.result -eq "duplicate") -and ($afterReplay.Json.confirmed_at -eq $confirmed.Json.confirmed_at)) "result=$($replay.Json.result)"
$cancelPaid = Invoke-Api POST "/api/bookings/$id1/cancel/" @{} $users["a"]
$payPaid = Start-Pay "a" $id1
Expect "A confirmed booking can be neither cancelled nor paid again (409)" (($cancelPaid.Status -eq 409) -and ($payPaid.Status -eq 409)) "cancel=$($cancelPaid.Status) pay=$($payPaid.Status)"

# --- 5. Booking 2: wrong amount, failed payment, cancel, late payment -----------------------------------------
Write-Host "`n=== 5. Wrong amount, failure, cancel, refund flag ===" -ForegroundColor Cyan
$null = Invoke-Api POST $holdUrl @{ seats = @("1-2A") } $hold["a"]
$b2 = New-Booking "a" "smoke7-b2-$tag"
$id2 = $b2.Json.id
$p2a = Start-Pay "a" $id2
$rawBad = New-Event $p2a.Json.payment.provider_ref "succeeded" ($fare - 1)
$bad = Send-Webhook $rawBad (Get-Signature $secret $rawBad)
$after2 = Get-Booking "a" $id2
Expect "A correctly signed success with the wrong amount is rejected (400) and confirms nothing" (($bad.Status -eq 400) -and ($after2.Json.status -eq "pending_payment")) "status=$($bad.Status) booking=$($after2.Json.status)"
$p2b = Start-Pay "a" $id2
$rawFail = New-Event $p2b.Json.payment.provider_ref "failed" $fare
$failed = Send-Webhook $rawFail (Get-Signature $secret $rawFail)
$p2c = Start-Pay "a" $id2
Expect "After a failed payment the booking can be paid again with a new payment" (($p2b.Status -eq 201) -and ($failed.Json.result -eq "failed") -and ($p2c.Status -eq 201) -and ($p2c.Json.payment.provider_ref -ne $p2b.Json.payment.provider_ref)) "retry=$($p2c.Status)"
$cancel2 = Invoke-Api POST "/api/bookings/$id2/cancel/" @{} $users["a"]
$bTake = Invoke-Api POST $holdUrl @{ seats = @("1-2A") } $hold["b"]
Expect "Cancelling frees the seat at once: B can hold it" (($cancel2.Status -eq 200) -and ($cancel2.Json.status -eq "cancelled") -and ($bTake.Status -eq 201)) "cancel=$($cancel2.Status) b_hold=$($bTake.Status)"
$null = Invoke-Api POST $releaseUrl @{} $users["b"]
$rawLateCancelled = New-Event $p2c.Json.payment.provider_ref "succeeded" $fare
$lateCancelled = Send-Webhook $rawLateCancelled (Get-Signature $secret $rawLateCancelled)
$after2b = Get-Booking "a" $id2
Expect "Money arriving for a cancelled booking is flagged refund_required, not revived" (($lateCancelled.Json.result -eq "refund_required") -and ($after2b.Json.status -eq "refund_required")) "result=$($lateCancelled.Json.result) booking=$($after2b.Json.status)"

# --- 6. Bookings 3 and 4: Celery beat expires them, then late payments --------------------------------------
Write-Host "`n=== 6. Expiry by Celery beat, then late payments ===" -ForegroundColor Cyan
$null = Invoke-Api POST $holdUrl @{ seats = @("1-3A") } $hold["a"]
$b3 = New-Booking "a" "smoke7-b3-$tag"
$id3 = $b3.Json.id
$p3 = Start-Pay "a" $id3
$null = Invoke-Api POST $holdUrl @{ seats = @("1-3B") } $hold["a"]
$b4 = New-Booking "a" "smoke7-b4-$tag"
$id4 = $b4.Json.id
$p4 = Start-Pay "a" $id4
Expect "Two more unpaid bookings exist, each with a payment started" (($b3.Status -eq 201) -and ($b4.Status -eq 201) -and ($p3.Status -eq 201) -and ($p4.Status -eq 201)) "b3=$($b3.Status) b4=$($b4.Status)"

# Move their deadline into the past (stands in for 10 minutes passing), then wait for beat and the worker.
$expire = @(
    'from datetime import timedelta',
    'from django.utils import timezone',
    'from apps.bookings.models import Booking',
    ('ids = ["{0}", "{1}"]' -f $id3, $id4),
    'print("EXPIRED_ROWS=%d" % Booking.objects.filter(pk__in=ids, status="pending_payment").update(expires_at=timezone.now() - timedelta(minutes=1)))'
)
$expireOut = Invoke-Shell $expire
Write-Host "  waiting for beat and the worker to expire them (up to 100 s)..." -ForegroundColor DarkGray
$deadline = (Get-Date).AddSeconds(100)
$state3 = $null
$state4 = $null
$waited = 0
while ((Get-Date) -lt $deadline) {
    $state3 = (Get-Booking "a" $id3).Json.status
    $state4 = (Get-Booking "a" $id4).Json.status
    if (($state3 -eq "expired") -and ($state4 -eq "expired")) { break }
    Start-Sleep -Seconds 3
    $waited += 3
}
Expect "Beat and the worker expired both unpaid bookings by themselves" (($expireOut -match "EXPIRED_ROWS=2") -and ($state3 -eq "expired") -and ($state4 -eq "expired")) "after about $waited s: $state3 / $state4"
$workerLog = (docker compose logs --no-log-prefix --since 10m worker 2>&1 | ForEach-Object { "$_" }) -join "`n"
Expect "The worker log shows expire_pending_bookings running and succeeding" (($workerLog -match "expire_pending_bookings") -and ($workerLog -match "succeeded")) ""
Start-Sleep -Seconds 3
$map2 = Invoke-Api GET $mapUrl
$taken2 = (@($map2.Json.coaches)[0].unavailable) -join ","
Expect "Expired bookings freed their seats on the seat map" ($taken2 -eq "1-1A,1-1B") "unavailable=$taken2"

$bHold3 = Invoke-Api POST $holdUrl @{ seats = @("1-3B") } $hold["b"]
$b4b = New-Booking "b" "smoke7-b4b-$tag"
Expect "B takes the seat that booking 4 had (hold then booking, 201)" (($bHold3.Status -eq 201) -and ($b4b.Status -eq 201)) "hold=$($bHold3.Status) booking=$($b4b.Status)"

$raw3 = New-Event $p3.Json.payment.provider_ref "succeeded" $fare
$late3 = Send-Webhook $raw3 (Get-Signature $secret $raw3)
$after3 = Get-Booking "a" $id3
Expect "Late payment, seat still free: the booking is revived and confirmed (late_confirmed)" (($late3.Json.result -eq "late_confirmed") -and ($after3.Json.status -eq "confirmed")) "result=$($late3.Json.result) booking=$($after3.Json.status)"
$raw4 = New-Event $p4.Json.payment.provider_ref "succeeded" $fare
$late4 = Send-Webhook $raw4 (Get-Signature $secret $raw4)
$after4 = Get-Booking "a" $id4
$after4b = Get-Booking "b" $b4b.Json.id
Expect "Late payment, seat taken by B: flagged refund_required and B's booking is untouched" (($late4.Json.result -eq "refund_required") -and ($after4.Json.status -eq "refund_required") -and ($after4b.Json.status -eq "pending_payment")) "result=$($late4.Json.result) a=$($after4.Json.status) b=$($after4b.Json.status)"
$replay4 = Send-Webhook $raw4 (Get-Signature $secret $raw4)
Expect "Replaying that late payment changes nothing (duplicate)" ($replay4.Json.result -eq "duplicate") "result=$($replay4.Json.result)"

# --- 7. The database guard -------------------------------------------------------------------------------------
Write-Host "`n=== 7. Database guard against double-selling ===" -ForegroundColor Cyan
$guard = @(
    'from django.db import IntegrityError, transaction',
    'from apps.accounts.models import User',
    'from apps.bookings.models import Booking, BookingSeat',
    'from apps.catalog.models import TrainRun',
    ('run = TrainRun.objects.get(pk={0})' -f $runId),
    'user = User.objects.filter(email__startswith="smoke7-b-").first()',
    'try:',
    '    with transaction.atomic():',
    '        booking = Booking.objects.create(user=user, run=run, total_pkr=2500, idempotency_key="smoke7-guard-0001", expires_at=run.departure_at)',
    '        BookingSeat.objects.create(booking=booking, run=run, seat_label="1-1A", seat_class="economy", price_pkr=2500)',
    '    print("GUARD=missing")',
    'except IntegrityError:',
    '    print("GUARD=ok")'
)
$guardOut = Invoke-Shell $guard
Expect "PostgreSQL itself refuses a second active booking for a sold seat" ($guardOut -match "GUARD=ok") ""

# --- 8. Throttle --------------------------------------------------------------------------------------------------
Write-Host "`n=== 8. Booking throttle ===" -ForegroundColor Cyan
$codes = @()
$last = $null
for ($i = 1; $i -le 25; $i++) {
    $last = New-Booking "b" ("smoke7-t$i-$tag")
    $codes += $last.Status
}
$first429 = [array]::IndexOf($codes, 429)
Expect "Repeated booking requests by one user hit the limit (429) within 21 requests" (($first429 -ge 10) -and ($first429 -le 20)) "first 429 at request $($first429 + 1)"
Expect "The 429 carries retry_after from Django" ($null -ne $last.Json.error.details.retry_after)
}
finally {
    $cleanupLines = @(
        'from apps.accounts.models import User',
        'from apps.bookings.models import Booking, Payment',
        'from apps.catalog.models import TrainRun'
    )
    if ($runId) {
        $cleanupLines += "print(Payment.objects.filter(booking__run_id=$runId).delete())"
        $cleanupLines += "print(Booking.objects.filter(run_id=$runId).delete())"
        $cleanupLines += "print(TrainRun.objects.filter(pk=$runId).delete())"
    }
    $cleanupLines += "print(User.objects.filter(email__startswith='smoke7-').delete())"
    $null = Invoke-Shell $cleanupLines
    $removed = 0
    if ($runId) {
        foreach ($pattern in @("hold:{$runId}:*", "wr:{$runId}:*")) {
            $keys = @(docker compose exec -T redis redis-cli --scan --pattern $pattern 2>&1 | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
            foreach ($k in $keys) { docker compose exec -T redis redis-cli DEL $k 2>&1 | Out-Null; $removed++ }
        }
    }
    Write-Host "  cleanup: removed the test bookings, payments, run, users and $removed Redis keys" -ForegroundColor DarkGray
}

# --- Summary ---------------------------------------------------------------------------------------------------------
Write-Host "`n=== SUMMARY (phase 7 smoke) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) { Write-Host "$failed check(s) FAILED." -ForegroundColor Red } else { Write-Host "All $($results.Count) checks passed." -ForegroundColor Green }
Stop-Transcript | Out-Null
if ($failed -gt 0) { exit 1 }
exit 0
