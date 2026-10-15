<#
  Reusable security gate.   Usage:  .\scripts\security-check.ps1 -Phase 0 -AllowNoTests
  Runs: lint, SAST (bandit), dependency audit, secrets scan, Django checks,
        migration drift, tests. Writes a transcript to security-reports\ (git-ignored).
  Paste the SUMMARY table (and any FAIL output) back to Claude for the phase report.
#>
param(
    [Parameter(Mandatory = $true)][string]$Phase,
    [switch]$SkipAudit,
    [switch]$AllowNoTests
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Virtual environment not found at $py" -ForegroundColor Red
    exit 2
}

$reportDir = Join-Path $root "security-reports"
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Start-Transcript -Path (Join-Path $reportDir "phase-$Phase-$stamp.txt") | Out-Null

$results = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param([string]$Check, [string]$Status, [string]$Detail = "")
    $results.Add([pscustomobject]@{ Check = $Check; Status = $Status; Detail = $Detail })
}

function Invoke-Check {
    param([string]$Name, [scriptblock]$Block, [int[]]$OkCodes = @(0))
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Block
    $code = $LASTEXITCODE
    if ($OkCodes -contains $code) {
        $detail = ""
        if ($code -ne 0) { $detail = "exit code $code (accepted)" }
        Add-Result $Name "PASS" $detail
    } else {
        Add-Result $Name "FAIL" "exit code $code"
    }
}

function Invoke-WithEnv {
    param([hashtable]$Vars, [scriptblock]$Block)
    $saved = @{}
    foreach ($k in $Vars.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
        [Environment]::SetEnvironmentVariable($k, $Vars[$k], "Process")
    }
    try { & $Block }
    finally {
        foreach ($k in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process")
        }
    }
}

# 1. Lint
Invoke-Check "Lint (ruff)" { & $py -m ruff check apps config manage.py --select E,F,W,B --ignore E501 }

# 2. SAST
Invoke-Check "SAST (bandit, medium+)" { & $py -m bandit -r apps config manage.py -c bandit.yaml -x "*test_*.py" -ll }

# 3. Dependency audit (audits the packages installed in this venv)
if ($SkipAudit) {
    Add-Result "Dependency audit (pip-audit)" "SKIPPED" "re-run without -SkipAudit when online"
} else {
    Invoke-Check "Dependency audit (pip-audit)" { & $py -m pip_audit --progress-spinner off }
}

# 4. Secrets scan
Write-Host ""
Write-Host "=== Secrets scan (detect-secrets) ===" -ForegroundColor Cyan
$exclude = '(^|[\\/])(\.venv|\.git|__pycache__|staticfiles|security-reports|\.pytest_cache|\.ruff_cache|htmlcov)([\\/]|$)|(^|[\\/])\.env$|\.sqlite3$'
$raw = & $py -m detect_secrets scan --all-files --exclude-files $exclude 2>$null | Out-String
$hits = $null
try {
    $json = $raw | ConvertFrom-Json
    $hits = @($json.results.PSObject.Properties)
} catch {
    $hits = $null
}
if ($null -eq $hits) {
    Add-Result "Secrets scan (detect-secrets)" "FAIL" "scan output could not be parsed"
} elseif ($hits.Count -eq 0) {
    Write-Host "No potential secrets found."
    Add-Result "Secrets scan (detect-secrets)" "PASS"
} else {
    foreach ($h in $hits) {
        Write-Host "  $($h.Name): $($h.Value.Count) potential secret(s)" -ForegroundColor Yellow
    }
    Add-Result "Secrets scan (detect-secrets)" "FAIL" "$($hits.Count) file(s) flagged"
}

# 5. Django checks
Invoke-Check "Django system check" { & $py manage.py check }

$prodEnv = @{
    DJANGO_SETTINGS_MODULE = "config.settings.prod"
    DJANGO_SECRET_KEY      = (& $py -c "import secrets; print(secrets.token_urlsafe(64))")
    DJANGO_ALLOWED_HOSTS   = "railrush.example.com"
}
Invoke-Check "Django deploy check (prod settings)" {
    Invoke-WithEnv $prodEnv { & $py manage.py check --deploy --fail-level WARNING }
}

# 6. Migration drift
Invoke-Check "Migrations in sync" { & $py manage.py makemigrations --check --dry-run }

# 7. Tests
if ($AllowNoTests) {
    Invoke-Check "Tests (pytest)" { & $py -m pytest -q } -OkCodes @(0, 5)
} else {
    Invoke-Check "Tests (pytest)" { & $py -m pytest -q }
}

# Summary
Write-Host ""
Write-Host "=== SUMMARY (phase $Phase) ===" -ForegroundColor Green
$results | Format-Table -AutoSize | Out-String | Write-Host
$failed = @($results | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) {
    Write-Host "$failed check(s) FAILED." -ForegroundColor Red
} else {
    Write-Host "All checks passed." -ForegroundColor Green
}
Stop-Transcript | Out-Null
if ($failed -gt 0) { exit 1 }
exit 0
