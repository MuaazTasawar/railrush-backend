# Puts nginx and web into load-test mode (trusts X-Forwarded-For, 60 s payment window).
# Experiment switch: LT_INFLIGHT=<n> adds an in-flight cap of n requests (payment webhooks exempt) to the generated
# load-test nginx file only. The production config has no cap (see docs/CAPACITY.md for why).
. "$PSScriptRoot\common.ps1"
$prod = [System.IO.File]::ReadAllText((Join-Path $RepoRoot "deploy\nginx\nginx.conf"))
$snippet = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "nginx_realip.snippet"))
$anchor = "server_name _;"
if ([regex]::Matches($prod, [regex]::Escape($anchor)).Count -ne 1) { throw "Could not find a single '$anchor' in deploy\nginx\nginx.conf" }
$nl = if ($prod.Contains("`r`n")) { "`r`n" } else { "`n" }
$block = (($snippet -replace "`r`n", "`n").TrimEnd()).Replace("`n", $nl)
$loadConf = $prod.Replace($anchor, $anchor + $nl + $block)

if ($env:LT_INFLIGHT -and $env:LT_INFLIGHT -ne "0") {
    $zoneAnchor = 'limit_conn_zone $binary_remote_addr zone=connperip:20m;'
    $capAnchor = 'limit_conn connperip 300;'
    $locAnchor = '        location / {'
    foreach ($a in @($zoneAnchor, $capAnchor, $locAnchor)) {
        if (-not $loadConf.Contains($a)) { throw "Could not find this line in deploy\nginx\nginx.conf: $a" }
    }
    $webhook = @(
        '        # Payment confirmations are never shed by the in-flight cap.',
        '        location = /api/payments/webhook/ {',
        '            limit_req zone=perip burst=200 nodelay;',
        '            limit_conn connperip 300;',
        '            proxy_pass http://web;',
        '        }',
        ''
    ) -join $nl
    $loadConf = $loadConf.Replace($zoneAnchor, $zoneAnchor + $nl + '    limit_conn_zone $server_name zone=inflight:1m;')
    $loadConf = $loadConf.Replace($capAnchor, $capAnchor + $nl + '        limit_conn inflight ' + $env:LT_INFLIGHT + ';')
    $loadConf = $loadConf.Replace($locAnchor, $webhook + $nl + $locAnchor)
}
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "nginx.loadtest.conf"), $loadConf, $Utf8)
Write-Host "generated loadtest\nginx.loadtest.conf from the production config" -ForegroundColor Green

docker compose @ComposeFiles config --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "compose override is not valid" -ForegroundColor Red; exit 2 }
docker compose @ComposeFiles up -d --force-recreate --no-build nginx web
if (-not (Wait-Health 60)) { Write-Host "The stack did not become healthy through nginx." -ForegroundColor Red; exit 2 }
Write-Host ("load-test mode is on. payment window = " + ((docker compose exec -T web printenv BOOKING_PAYMENT_SECONDS) -join "") + " s") -ForegroundColor Green
