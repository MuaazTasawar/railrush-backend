# Puts nginx and web back to the normal (production-style) configuration.
. "$PSScriptRoot\common.ps1"
docker compose up -d --force-recreate --no-build nginx web
if (-not (Wait-Health 60)) { Write-Host "The stack did not become healthy through nginx." -ForegroundColor Red; exit 2 }
$realip = ((docker compose exec -T nginx grep -c real_ip_header /etc/nginx/nginx.conf) -join "").Trim()
Write-Host ("normal mode is on. nginx real_ip lines: " + $realip + " (expected 0). payment window = " + ((docker compose exec -T web printenv BOOKING_PAYMENT_SECONDS) -join "") + " s") -ForegroundColor Green
