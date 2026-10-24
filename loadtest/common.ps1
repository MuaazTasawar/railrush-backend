# Shared helpers for the load-test scripts. Dot-source it:  . "$PSScriptRoot\common.ps1"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LtRoot = Join-Path (Split-Path -Parent $RepoRoot) "railrush-loadtest"
$DataDir = Join-Path $LtRoot "data"
$ResultsDir = Join-Path $LtRoot "results"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$Inv = [System.Globalization.CultureInfo]::InvariantCulture
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path $ResultsDir | Out-Null
Set-Location $RepoRoot
$ComposeFiles = @("-f", "docker-compose.yml", "-f", "docker-compose.loadtest.yml")

function Get-DotEnv {
    param([string]$Name, [string]$Default = "")
    $file = Join-Path $RepoRoot ".env"
    if (Test-Path $file) {
        $line = Get-Content $file | Where-Object { $_ -match ("^\s*" + [regex]::Escape($Name) + "\s*=") } | Select-Object -First 1
        if ($line) { return (($line -split "=", 2)[1]).Trim().Trim('"').Trim("'") }
    }
    return $Default
}

function Wait-Health {
    param([int]$Tries = 40)
    $url = "http://127.0.0.1:" + (Get-DotEnv "NGINX_HOST_PORT" "8080") + "/api/health/live/"
    for ($i = 1; $i -le $Tries; $i++) {
        $code = (curl.exe -s --noproxy "*" -o NUL -w "%{http_code}" $url)
        if ($code -eq "200") { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Invoke-Psql {
    param([string]$Sql)
    $user = Get-DotEnv "POSTGRES_USER" "railrush"
    $db = Get-DotEnv "POSTGRES_DB" "railrush"
    $Sql | docker compose exec -T postgres psql -U $user -d $db -X -q -P footer=off
}

function Invoke-Redis {
    & docker compose exec -T redis redis-cli @args
}
