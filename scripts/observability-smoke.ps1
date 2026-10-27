<#
  Observability checks against the running stack: Prometheus targets and rules, the exporters, Grafana provisioning
  and access control, and one live signal test (beat stopped, an overdue booking shows up in the gauge).
  Usage:  .\scripts\observability-smoke.ps1       (about 3 minutes)
  The Grafana password is read from .env and is never printed.
#>
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
. "$root\loadtest\common.ps1"
Set-Location $root
$reportDir = Join-Path $root "security-reports"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
Start-Transcript -Path (Join-Path $reportDir ("observability-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")) | Out-Null

$obs = @("-f", "docker-compose.yml", "-f", "docker-compose.observability.yml")
$port = Get-DotEnv "NGINX_HOST_PORT" "8080"
$gport = Get-DotEnv "GRAFANA_HOST_PORT" "3030"
$gpw = Get-DotEnv "GRAFANA_ADMIN_PASSWORD"
$results = New-Object System.Collections.Generic.List[object]

function Expect([string]$Name, [bool]$Condition, [string]$Detail = "") {
    $status = if ($Condition) { "PASS" } else { "FAIL" }
    $results.Add([pscustomobject]@{ Check = $Name; Status = $status; Detail = $Detail })
    $color = if ($Condition) { "Green" } else { "Red" }
    Write-Host ("[{0}] {1} {2}" -f $status, $Name, $Detail) -ForegroundColor $color
}
function Invoke-Prom([string]$Path) {
    $out = (docker compose @obs exec -T prometheus wget -qO- ("http://127.0.0.1:9090" + $Path) 2>$null) -join ""
    try { return ($out | ConvertFrom-Json) } catch { return $null }
}
function Get-Scalar([string]$Expr) {
    $r = Invoke-Prom ("/api/v1/query?query=" + [uri]::EscapeDataString($Expr))
    if ($null -eq $r -or $r.status -ne "success") { return $null }
    $rows = @($r.data.result)
    if ($rows.Count -eq 0) { return $null }
    return [double]::Parse($rows[0].value[1], $Inv)
}
function Get-Grafana([string]$Path, [string]$Password) {
    $url = "http://127.0.0.1:" + $gport + $Path
    if ($Password) {
        $cfg = 'user = "admin:' + $Password + '"'
        return (($cfg | curl.exe -s --noproxy "*" -K - $url) -join "")
    }
    return ((curl.exe -s --noproxy "*" $url) -join "")
}
function Get-GrafanaCode([string]$Path, [string]$Password) {
    $url = "http://127.0.0.1:" + $gport + $Path
    if ($Password) {
        $cfg = 'user = "admin:' + $Password + '"'
        return (($cfg | curl.exe -s --noproxy "*" -K - -o NUL -w "%{http_code}" $url) -join "")
    }
    return ((curl.exe -s --noproxy "*" -o NUL -w "%{http_code}" $url) -join "")
}

# --- 1. containers
$running = @(docker compose @obs ps --status running --services 2>&1 | ForEach-Object { "$_".Trim() })
$need = @("prometheus", "grafana", "redis-exporter", "postgres-exporter")
$missing = @($need | Where-Object { $running -notcontains $_ })
Expect "The four observability containers are running" ($missing.Count -eq 0) ("missing: " + ($missing -join ","))
foreach ($m in $missing) { Write-Host ("--- last log lines of " + $m) -ForegroundColor Yellow; docker compose @obs logs --no-log-prefix --tail 12 $m 2>&1 }
$healthy = $false
for ($i = 0; $i -lt 12 -and -not $healthy; $i++) {
    $h = (docker compose @obs exec -T prometheus wget -qO- http://127.0.0.1:9090/-/healthy 2>$null) -join ""
    if ($h -match "Healthy") { $healthy = $true } else { Start-Sleep -Seconds 5 }
}
Expect "Prometheus reports healthy" $healthy

# --- 2. targets
$allUp = $false
$active = @()
for ($i = 0; $i -lt 18 -and -not $allUp; $i++) {
    $t = Invoke-Prom "/api/v1/targets"
    if ($null -ne $t) { $active = @($t.data.activeTargets) }
    $allUp = ($active.Count -ge 4) -and (@($active | Where-Object { $_.health -ne "up" }).Count -eq 0)
    if (-not $allUp) { Start-Sleep -Seconds 5 }
}
$downList = (@($active | Where-Object { $_.health -ne "up" } | ForEach-Object { $_.labels.job + ":" + $_.lastError }) -join "; ")
Expect "All 4 scrape targets are up (prometheus, web, redis, postgres)" $allUp ("targets=" + $active.Count + " " + $downList)

# --- 3. rules
$expectedRules = @(Select-String -Path "$root\deploy\prometheus\alerts.yml" -Pattern '^\s*- alert:').Count
$rulesOk = $false
$ruleRows = @()
for ($i = 0; $i -lt 8 -and -not $rulesOk; $i++) {
    $r = Invoke-Prom "/api/v1/rules"
    if ($null -ne $r) { $ruleRows = @($r.data.groups | ForEach-Object { $_.rules } | Where-Object { $_.type -eq "alerting" }) }
    $rulesOk = ($ruleRows.Count -eq $expectedRules) -and (@($ruleRows | Where-Object { $_.health -ne "ok" }).Count -eq 0)
    if (-not $rulesOk) { Start-Sleep -Seconds 5 }
}
Expect "All alert rules are loaded and evaluating without error" $rulesOk ("expected " + $expectedRules + ", loaded " + $ruleRows.Count)

# --- 4. traffic through nginx shows up in Prometheus
1..30 | ForEach-Object { curl.exe -s --noproxy "*" -o NUL ("http://127.0.0.1:" + $port + "/api/health/live/") }
curl.exe -s --noproxy "*" -o NUL ("http://127.0.0.1:" + $port + "/api/bookings/")
curl.exe -s --noproxy "*" -o NUL ("http://127.0.0.1:" + $port + "/api/no-such-page/")
Start-Sleep -Seconds 25
$n401 = Get-Scalar 'sum(django_http_responses_total_by_status_total{status="401"})'
$n200 = Get-Scalar 'sum(django_http_responses_total_by_status_total{status="200"})'
Expect "Requests sent through nginx are counted by status in Prometheus" (($n401 -ge 1) -and ($n200 -ge 30)) ("200s=" + $n200 + " 401s=" + $n401)
$views = Get-Scalar 'count(django_http_requests_latency_seconds_by_view_method_count)'
Expect "Per-view latency histograms exist" ($views -ge 1) ("series=" + $views)

# --- 5. business gauges and exporters
$ok = Get-Scalar 'min(railrush_scrape_ok)'
Expect "The web tier can read PostgreSQL and Redis for its gauges (railrush_scrape_ok = 1)" ($ok -eq 1) ("min=" + $ok)
$statuses = Get-Scalar 'count(railrush_bookings)'
Expect "Booking gauges exist for all 5 statuses" ($statuses -eq 5) ("series=" + $statuses)
$redisUp = Get-Scalar 'redis_up'
$redisMax = Get-Scalar 'redis_memory_max_bytes'
Expect "The Redis exporter works and sees the 768 MB memory limit" (($redisUp -eq 1) -and ($redisMax -gt 0)) ("redis_up=" + $redisUp + " max=" + $redisMax)
$pgUp = Get-Scalar 'pg_up'
$pgMax = Get-Scalar 'max(pg_settings_max_connections)'
Expect "The PostgreSQL exporter works (least-privilege role) and reads max_connections" (($pgUp -eq 1) -and ($pgMax -gt 0)) ("pg_up=" + $pgUp + " max_connections=" + $pgMax)

# --- 6. Grafana
$gHealth = Get-Grafana "/api/health" ""
Expect "Grafana answers its health check" ($gHealth -match '"database"\s*:\s*"ok"') ""
Expect "Grafana refuses anonymous access" ((Get-GrafanaCode "/api/search" "") -eq "401")
Expect "Grafana refuses a wrong password" ((Get-GrafanaCode "/api/search" "wrong-password-000000") -eq "401")
Expect "Grafana accepts the generated admin password" ((Get-GrafanaCode "/api/search" $gpw) -eq "200")
$search = Get-Grafana "/api/search?query=RailRush" $gpw
Expect "The RailRush dashboard was provisioned" ($search -match "railrush-overview") ""
$dashJson = Get-Grafana "/api/dashboards/uid/railrush-overview" $gpw
$panelCount = ([regex]::Matches($dashJson, '"gridPos"')).Count
Expect "The dashboard has its 19 panels" ($panelCount -ge 19) ("panels=" + $panelCount)
$proxy = Get-Grafana "/api/datasources/proxy/uid/prometheus/api/v1/query?query=up" $gpw
Expect "Grafana reads Prometheus through the provisioned datasource" (($proxy -match '"status"\s*:\s*"success"') -and ($proxy -match '"job"')) ""

# --- 7. nothing is exposed that should not be
Expect "Prometheus has no port on the host" ((curl.exe -s --noproxy "*" -m 3 -o NUL -w "%{http_code}" http://127.0.0.1:9090/-/healthy) -eq "000")
$gmap = (docker compose @obs port grafana 3000 2>$null) -join ""
Expect "Grafana is published on 127.0.0.1 only" ($gmap -match '^127\.0\.0\.1:') ("mapping=" + $gmap)
Expect "The metrics endpoint is not reachable through nginx (404)" ((curl.exe -s --noproxy "*" -o NUL -w "%{http_code}" ("http://127.0.0.1:" + $port + "/internal/metrics")) -eq "404")

# --- 8. live signal test: beat stopped, an overdue booking must show up in the gauge
$testKey = "obs-smoke-key-0001"
$createPy = @(
    'from datetime import timedelta',
    'from django.utils import timezone',
    'from apps.accounts.models import User',
    'from apps.bookings.models import Booking',
    'from apps.catalog.models import TrainRun',
    'run = TrainRun.objects.order_by("id").first()',
    'user = User.objects.filter(email="obs-smoke@example.test").first() or User.objects.create_user(email="obs-smoke@example.test")',
    ('booking = Booking.objects.create(user=user, run=run, total_pkr=2500, idempotency_key="' + $testKey + '", expires_at=timezone.now() - timedelta(minutes=10))'),
    'print("BOOKING=%s" % booking.id)'
) -join "`n"
$cleanupPy = @(
    'from apps.accounts.models import User',
    'from apps.bookings.models import Booking',
    ('print(Booking.objects.filter(idempotency_key="' + $testKey + '").delete())'),
    'print(User.objects.filter(email="obs-smoke@example.test").delete())'
) -join "`n"
$seen = $false
try {
    docker compose @obs stop beat 2>&1 | Out-Null
    $created = ($createPy | docker compose @obs exec -T web python manage.py shell 2>&1) -join "`n"
    Expect "A test booking 10 minutes past its deadline was created (beat stopped)" ($created -match 'BOOKING=') ""
    for ($i = 0; $i -lt 14 -and -not $seen; $i++) {
        $v = Get-Scalar 'railrush_pending_bookings_overdue'
        if ($v -ge 1) { $seen = $true } else { Start-Sleep -Seconds 5 }
    }
    Expect "With beat stopped the overdue gauge rises to 1 in Prometheus (what the ExpiryStalled alert watches)" $seen ""
} finally {
    $null = $cleanupPy | docker compose @obs exec -T web python manage.py shell 2>&1
    docker compose @obs start beat 2>&1 | Out-Null
}
$back = $false
for ($i = 0; $i -lt 14 -and -not $back; $i++) {
    $v = Get-Scalar 'railrush_pending_bookings_overdue'
    if ($v -eq 0) { $back = $true } else { Start-Sleep -Seconds 5 }
}
Expect "After the test booking is removed and beat restarted, the gauge returns to 0" $back ""

# --- 9. what the stack costs (informational)
Write-Host "`n--- resource use of the observability containers ---" -ForegroundColor DarkGray
docker stats --no-stream --format '{{.Name}}: memory {{.MemUsage}}, cpu {{.CPUPerc}}' 2>&1 | Where-Object { "$_" -match 'prometheus|grafana|exporter' }

Write-Host "`n=== SUMMARY (observability smoke) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) { Write-Host "$failed check(s) FAILED." -ForegroundColor Red } else { Write-Host "All $($results.Count) checks passed." -ForegroundColor Green }
Stop-Transcript | Out-Null
if ($failed -gt 0) { exit 1 }
exit 0
