<#
  Phase 3 end-to-end checks against the running Docker stack, through nginx.
  Usage:  .\scripts\phase3-smoke.ps1
          .\scripts\phase3-smoke.ps1 -BaseUrl http://127.0.0.1:8088
  Takes about 3 minutes because it waits out the one-minute throttle windows.
  Creates users named smoke-*@example.test (cleanup command is in the Phase 3 walkthrough).
#>
param(
    [string]$BaseUrl = "http://127.0.0.1:8088",
    [int]$WindowSeconds = 65
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$reportDir = Join-Path $root "security-reports"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
Start-Transcript -Path (Join-Path $reportDir ("phase-3-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")) | Out-Null

Add-Type -AssemblyName System.Net.Http
$handler = New-Object System.Net.Http.HttpClientHandler
$handler.UseProxy = $false
$http = New-Object System.Net.Http.HttpClient -ArgumentList $handler
$http.Timeout = [TimeSpan]::FromSeconds(30)

$results = New-Object System.Collections.Generic.List[object]

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null,
        [hashtable]$Headers = @{},
        [switch]$NoPace
    )
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
    # Stay under nginx's 5 requests/second auth limiter unless a test wants to hit it.
    if (-not $NoPace -and $Path.StartsWith("/api/auth/")) { Start-Sleep -Milliseconds 250 }
    [pscustomobject]@{ Status = $status; Json = $parsed; Text = $text; Headers = $hdr }
}

function Expect([string]$Name, [bool]$Condition, [string]$Detail = "") {
    $status = if ($Condition) { "PASS" } else { "FAIL" }
    $results.Add([pscustomobject]@{ Check = $Name; Status = $status; Detail = $Detail })
    $color = if ($Condition) { "Green" } else { "Red" }
    Write-Host ("[{0}] {1} {2}" -f $status, $Name, $Detail) -ForegroundColor $color
}

function Wait-Window([string]$Why) {
    Write-Host "Waiting $WindowSeconds s for the throttle window to reset ($Why)..." -ForegroundColor DarkGray
    Start-Sleep -Seconds $WindowSeconds
}

function ConvertTo-B64Url([byte[]]$Bytes) {
    [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-ForgedToken([string]$UserId, [string]$Alg, [string]$SigningText) {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $header = ConvertTo-Json -InputObject @{ alg = $Alg; typ = "JWT" } -Compress
    $claims = ConvertTo-Json -InputObject @{
        token_type = "access"; exp = $now + 600; iat = $now; jti = [guid]::NewGuid().ToString("N"); user_id = $UserId
    } -Compress
    $h = ConvertTo-B64Url ([Text.Encoding]::UTF8.GetBytes($header))
    $p = ConvertTo-B64Url ([Text.Encoding]::UTF8.GetBytes($claims))
    $signingInput = "$h.$p"
    if ($Alg -eq "none") { return "$signingInput." }
    $keyBytes = [Text.Encoding]::UTF8.GetBytes($SigningText)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, $keyBytes)
    $sig = ConvertTo-B64Url ($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($signingInput)))
    return "$signingInput.$sig"
}

# --- Pre-flight ---------------------------------------------------------------
$live = Invoke-Api GET "/api/health/live/"
if ($live.Status -ne 200) {
    Write-Host "Stack not reachable at $BaseUrl (status $($live.Status)). Start it with: docker compose up -d" -ForegroundColor Red
    Stop-Transcript | Out-Null
    exit 2
}

$run = [guid]::NewGuid().ToString("N").Substring(0, 8)
$pw = "Zebra-Quartz-Lantern-" + (Get-Random -Minimum 1000 -Maximum 9999)
$wrongPw = "Wrong-Horse-Staple-" + (Get-Random -Minimum 1000 -Maximum 9999)
$weakPw = "Qx7-Zk"
$junk = "y"
$emailA = "smoke-a-$run@example.test"
$emailB = "smoke-b-$run@example.test"

# --- 1. Functional checks -------------------------------------------------------
Write-Host "`n=== 1. Functional checks ===" -ForegroundColor Cyan

$reg = Invoke-Api POST "/api/auth/register/" @{ email = "  $($emailA.ToUpper()) "; password = $pw; full_name = "Smoke Rider" }
Expect "Register: 201 with user and both tokens" (($reg.Status -eq 201) -and [bool]$reg.Json.access -and [bool]$reg.Json.refresh) "status=$($reg.Status)"
Expect "Register: email is normalized to lower case" ($reg.Json.user.email -ceq $emailA)
Expect "Register: password never echoed" ($reg.Text -notmatch [regex]::Escape($pw))
Expect "Register: token response is no-store" ($reg.Headers["cache-control"] -match "no-store") "cache-control=$($reg.Headers['cache-control'])"

$dup = Invoke-Api POST "/api/auth/register/" @{ email = $emailA; password = $pw }
Expect "Register: duplicate email is 409 conflict" (($dup.Status -eq 409) -and ($dup.Json.error.code -eq "conflict")) "status=$($dup.Status)"

$weak = Invoke-Api POST "/api/auth/register/" @{ email = $emailB; password = $weakPw }
Expect "Register: weak password is 400 with password details" (($weak.Status -eq 400) -and ($null -ne $weak.Json.error.details.password)) "status=$($weak.Status)"
Expect "Register: weak password is not echoed back" ($weak.Text -notmatch [regex]::Escape($weakPw))

$priv = Invoke-Api POST "/api/auth/register/" @{ email = $emailB; password = $pw; is_staff = $true; is_superuser = $true }
Expect "Register: privilege fields are ignored and never returned" (($priv.Status -eq 201) -and ($null -eq $priv.Json.user.is_staff) -and ($null -eq $priv.Json.user.is_superuser)) "status=$($priv.Status)"

$login = Invoke-Api POST "/api/auth/login/" @{ email = $emailA; password = $pw }
Expect "Login: valid credentials give 200 and both tokens" (($login.Status -eq 200) -and [bool]$login.Json.access -and [bool]$login.Json.refresh) "status=$($login.Status)"
Expect "Login: token response is no-store" ($login.Headers["cache-control"] -match "no-store")

$wrong = Invoke-Api POST "/api/auth/login/" @{ email = $emailA; password = $wrongPw }
$ghost = Invoke-Api POST "/api/auth/login/" @{ email = "smoke-ghost-$run@example.test"; password = $wrongPw }
Expect "Login: wrong password and unknown email are both 401" (($wrong.Status -eq 401) -and ($ghost.Status -eq 401)) "wrong=$($wrong.Status) unknown=$($ghost.Status)"
Expect "Login: the two failures have identical bodies (no account enumeration)" ($wrong.Text -ceq $ghost.Text)

$loginUpper = Invoke-Api POST "/api/auth/login/" @{ email = " $($emailA.ToUpper()) "; password = $pw }
Expect "Login: email is case- and whitespace-insensitive" ($loginUpper.Status -eq 200) "status=$($loginUpper.Status)"

$tokA = $reg.Json.access
$refA = $reg.Json.refresh
$userId = $reg.Json.user.id

$me0 = Invoke-Api GET "/api/users/me/"
Expect "Profile: no token is 401 with a Bearer challenge" (($me0.Status -eq 401) -and ($me0.Headers["www-authenticate"] -match "Bearer")) "status=$($me0.Status)"

$authHeader = @{ Authorization = "Bearer $tokA" }
$me1 = Invoke-Api GET "/api/users/me/" $null $authHeader
$keys = (@($me1.Json.PSObject.Properties.Name) | Sort-Object) -join ","
Expect "Profile: 200 with exactly id, email, full_name, date_joined" (($me1.Status -eq 200) -and ($keys -eq "date_joined,email,full_name,id")) "keys=$keys"
Expect "Profile: response is no-store" ($me1.Headers["cache-control"] -match "no-store")

$bad = [ordered]@{
    "refresh token used as an access token" = $refA
    "unsigned token (alg none)"             = (New-ForgedToken $userId "none" "")
    "token signed with a different key"     = (New-ForgedToken $userId "HS256" "not-the-servers-signing-key-0123456789abcdef")
    "tampered signature"                    = ($tokA.Substring(0, $tokA.Length - 4) + "AAAA")
    "garbage"                               = "not.a.jwt"
}
foreach ($label in $bad.Keys) {
    $r = Invoke-Api GET "/api/users/me/" $null @{ Authorization = "Bearer $($bad[$label])" }
    Expect "Profile rejects: $label" ($r.Status -eq 401) "status=$($r.Status)"
}
$scheme = Invoke-Api GET "/api/users/me/" $null @{ Authorization = "Token $tokA" }
Expect "Profile rejects: wrong auth scheme" ($scheme.Status -eq 401) "status=$($scheme.Status)"

$patch = Invoke-Api PATCH "/api/users/me/" @{ full_name = "Smoke Renamed"; email = "hijack-$run@example.test"; is_staff = $true } $authHeader
Expect "Profile PATCH: name changes, email and is_staff do not" (($patch.Status -eq 200) -and ($patch.Json.full_name -ceq "Smoke Renamed") -and ($patch.Json.email -ceq $emailA)) "status=$($patch.Status)"
foreach ($m in @("PUT", "DELETE", "POST")) {
    $r = Invoke-Api $m "/api/users/me/" @{} $authHeader
    Expect "Profile: $m is 405" ($r.Status -eq 405) "status=$($r.Status)"
}

$ref = Invoke-Api POST "/api/auth/refresh/" @{ refresh = $refA }
Expect "Refresh: valid refresh token gives a new access token" (($ref.Status -eq 200) -and [bool]$ref.Json.access) "status=$($ref.Status)"
$me2 = Invoke-Api GET "/api/users/me/" $null @{ Authorization = "Bearer $($ref.Json.access)" }
Expect "Refresh: the new access token works" ($me2.Status -eq 200) "status=$($me2.Status)"
$refBad1 = Invoke-Api POST "/api/auth/refresh/" @{ refresh = $tokA }
Expect "Refresh rejects: access token" ($refBad1.Status -eq 401) "status=$($refBad1.Status)"
$refBad2 = Invoke-Api POST "/api/auth/refresh/" @{ refresh = "not.a.jwt" }
Expect "Refresh rejects: garbage" ($refBad2.Status -eq 401) "status=$($refBad2.Status)"
$refBad3 = Invoke-Api POST "/api/auth/refresh/" @{}
Expect "Refresh rejects: missing field with 400" ($refBad3.Status -eq 400) "status=$($refBad3.Status)"

# --- 2. Register and per-account throttles ---------------------------------------
Wait-Window "register and per-account throttles"
Write-Host "`n=== 2. Register and per-account throttles ===" -ForegroundColor Cyan

$codes = @()
$lastReg = $null
for ($i = 1; $i -le 6; $i++) {
    $lastReg = Invoke-Api POST "/api/auth/register/" @{ email = "x"; password = $junk } @{ "X-Forwarded-For" = "198.51.100.$i" }
    $codes += $lastReg.Status
}
Expect "Register throttle: 5 requests pass the limiter, the 6th is 429" ((($codes[0..4] -notcontains 429)) -and ($codes[5] -eq 429)) "codes=$($codes -join ',')"
Expect "Register throttle: 429 carries retry_after from Django" ($null -ne $lastReg.Json.error.details.retry_after)

$codes = @()
$lastLogin = $null
for ($i = 1; $i -le 6; $i++) {
    $lastLogin = Invoke-Api POST "/api/auth/login/" @{ email = $emailA; password = $wrongPw } @{ "X-Forwarded-For" = "198.51.100.$i" }
    $codes += $lastLogin.Status
}
Expect "Account throttle: 5 attempts get 401, the 6th is 429 (forged X-Forwarded-For changes nothing)" ((($codes[0..4] -notcontains 429)) -and ($codes[5] -eq 429)) "codes=$($codes -join ',')"
$rightPw = Invoke-Api POST "/api/auth/login/" @{ email = $emailA; password = $pw } @{ "X-Forwarded-For" = "198.51.100.77" }
Expect "Account throttle: even the right password is refused while throttled" ($rightPw.Status -eq 429) "status=$($rightPw.Status)"

# --- 3. Per-IP login throttle with forged X-Forwarded-For --------------------------
Wait-Window "per-IP login throttle"
Write-Host "`n=== 3. Per-IP login throttle ===" -ForegroundColor Cyan

$codes = @()
$lastIp = $null
for ($i = 1; $i -le 11; $i++) {
    $lastIp = Invoke-Api POST "/api/auth/login/" @{ email = "smoke-ip$i-$run@example.test"; password = $wrongPw } @{ "X-Forwarded-For" = "203.0.113.$i" }
    $codes += $lastIp.Status
}
Expect "IP throttle: 10 attempts get 401, the 11th is 429 although every request forged a different X-Forwarded-For" ((($codes[0..9] -notcontains 429)) -and ($codes[10] -eq 429)) "codes=$($codes -join ',')"
Expect "IP throttle: 429 carries retry_after from Django" ($null -ne $lastIp.Json.error.details.retry_after)

# --- 4. nginx edge limiter ----------------------------------------------------------
Write-Host "`n=== 4. nginx edge limiter ===" -ForegroundColor Cyan
$codes = @()
$sample429 = $null
for ($i = 1; $i -le 40; $i++) {
    $r = Invoke-Api GET "/api/auth/nope/" $null @{} -NoPace
    $codes += $r.Status
    if (($r.Status -eq 429) -and ($null -eq $sample429)) { $sample429 = $r }
}
$n429 = @($codes | Where-Object { $_ -eq 429 }).Count
Expect "nginx: a burst against /api/auth/ is limited (429s seen)" (($codes[0] -eq 404) -and ($n429 -ge 5)) "first=$($codes[0]) 429s=$n429 of 40"
Expect "nginx: its 429 uses the shared JSON envelope with Retry-After" (($sample429.Json.error.code -eq "throttled") -and ($null -eq $sample429.Json.error.details) -and [bool]$sample429.Headers["retry-after"])
Start-Sleep -Seconds 3
$metrics = Invoke-Api GET "/internal/metrics"
Expect "nginx: /internal/ is not reachable from outside" ($metrics.Status -eq 404) "status=$($metrics.Status)"

# --- Summary ----------------------------------------------------------------------------
Write-Host "`n=== SUMMARY (phase 3 smoke) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) { Write-Host "$failed check(s) FAILED." -ForegroundColor Red } else { Write-Host "All $($results.Count) checks passed." -ForegroundColor Green }
Stop-Transcript | Out-Null
if ($failed -gt 0) { exit 1 }
exit 0
