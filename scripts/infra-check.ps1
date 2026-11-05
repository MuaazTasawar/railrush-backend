<#
  Infrastructure checks, the same on a PC and in CI. Needs Docker. Changes nothing in the repository.
  Usage:  .\scripts\infra-check.ps1        (on Linux: pwsh ./scripts/infra-check.ps1)
  Checks: every compose file combination renders, the Prometheus configuration and alert rules load,
          the alert unit tests pass (promtool), nginx accepts its configuration, the Grafana dashboard is valid JSON.
  Secrets: compose needs many variables, so a throwaway env file of random values is generated and deleted
           afterwards. The real .env is never read.
#>
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$results = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param([string]$Check, [bool]$Passed, [string]$Detail = "")
    $status = "FAIL"
    if ($Passed) { $status = "PASS" }
    $results.Add([pscustomobject]@{ Check = $Check; Status = $status; Detail = $Detail })
}

function New-Hex {
    param([int]$Bytes)
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($buffer)
    $rng.Dispose()
    return (-join ($buffer | ForEach-Object { $_.ToString("x2") }))
}

function Read-EnvFile {
    param([string]$Path)
    $values = @{}
    foreach ($line in (Get-Content $Path -Encoding UTF8)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $values[$Matches[1]] = $Matches[2] }
    }
    return $values
}

function Test-Compose {
    param([string]$Name, [string[]]$ComposeArgs)
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    $output = & docker compose --env-file $envFile @ComposeArgs config -q 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    $output | ForEach-Object { Write-Host "  $_" }
    Add-Result $Name $ok
}

function Test-Native {
    param([string]$Name, [scriptblock]$Block)
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Block
    Add-Result $Name ($LASTEXITCODE -eq 0)
}

docker version --format '{{.Server.Version}}' *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is not available (is the engine running?)." -ForegroundColor Red
    exit 2
}

$pinnedPath = Join-Path $root "deploy/observability-images.env"
$pinned = Read-EnvFile $pinnedPath
foreach ($name in @("PROMETHEUS_IMAGE", "GRAFANA_IMAGE", "REDIS_EXPORTER_IMAGE", "POSTGRES_EXPORTER_IMAGE")) {
    if (-not $pinned.ContainsKey($name)) {
        Write-Host "deploy/observability-images.env has no $name." -ForegroundColor Red
        exit 2
    }
}

$envFile = Join-Path ([IO.Path]::GetTempPath()) ("railrush-infra-" + [guid]::NewGuid().ToString("N") + ".env")
try {
    $envLines = New-Object System.Collections.Generic.List[string]
    $envLines.Add("POSTGRES_USER=railrush")
    $envLines.Add("POSTGRES_DB=railrush")
    $envLines.Add("POSTGRES_PASSWORD=" + (New-Hex 16))
    $envLines.Add("REDIS_PASSWORD=" + (New-Hex 16))
    $envLines.Add("DJANGO_SECRET_KEY=" + (New-Hex 32))
    $envLines.Add("PAYMENT_WEBHOOK_SECRET=" + (New-Hex 16))
    $envLines.Add("POSTGRES_EXPORTER_PASSWORD=" + (New-Hex 16))
    $envLines.Add("GRAFANA_ADMIN_PASSWORD=" + (New-Hex 16))
    $envLines.Add("PGBOUNCER_IMAGE=edoburu/pgbouncer:v1.26.0-p0")
    foreach ($key in $pinned.Keys) { $envLines.Add($key + "=" + $pinned[$key]) }
    [IO.File]::WriteAllText($envFile, (($envLines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))

    Test-Compose "Compose: main stack" @("-f", "docker-compose.yml")
    Test-Compose "Compose: main stack with the test profile" @("--profile", "test", "-f", "docker-compose.yml")
    Test-Compose "Compose: with observability" @("-f", "docker-compose.yml", "-f", "docker-compose.observability.yml")
    Test-Compose "Compose: with load-test overrides" @("-f", "docker-compose.yml", "-f", "docker-compose.loadtest.yml")

    $promDir = (Resolve-Path (Join-Path $root "deploy/prometheus")).Path
    $promMount = $promDir + ":/etc/prometheus:ro"
    $promImage = $pinned["PROMETHEUS_IMAGE"]
    Test-Native "Prometheus: configuration and rule files load" {
        docker run --rm --entrypoint promtool -v $promMount $promImage check config /etc/prometheus/prometheus.yml
    }
    Test-Native "Prometheus: alert rule unit tests" {
        docker run --rm --entrypoint promtool -v $promMount $promImage test rules /etc/prometheus/alerts_test.yml
    }

    $nginxConf = (Resolve-Path (Join-Path $root "deploy/nginx/nginx.conf")).Path
    $nginxMount = $nginxConf + ":/etc/nginx/nginx.conf:ro"
    Test-Native "nginx: configuration syntax" {
        docker run --rm --add-host web:127.0.0.1 -v $nginxMount nginxinc/nginx-unprivileged:stable-alpine nginx -t
    }

    Write-Host ""
    Write-Host "=== Grafana: dashboard JSON ===" -ForegroundColor Cyan
    $dashFiles = @(Get-ChildItem (Join-Path $root "deploy/grafana/dashboards") -Filter *.json -ErrorAction SilentlyContinue)
    if ($dashFiles.Count -eq 0) {
        Add-Result "Grafana: dashboard JSON" $false "no .json file in deploy/grafana/dashboards"
    } else {
        $bad = @()
        foreach ($file in $dashFiles) {
            try { $null = Get-Content $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { $bad += $file.Name }
        }
        Add-Result "Grafana: dashboard JSON" ($bad.Count -eq 0) ("files: " + $dashFiles.Count + "; invalid: " + ($bad -join ", "))
    }
}
finally {
    Remove-Item $envFile -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "=== SUMMARY (infrastructure checks) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) {
    Write-Host "$failed check(s) FAILED." -ForegroundColor Red
    exit 1
}
Write-Host "All checks passed." -ForegroundColor Green
exit 0