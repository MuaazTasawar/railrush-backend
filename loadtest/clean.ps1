# Removes the load-test bookings, payments, runs (and, unless -KeepUsers, the load-test users) and their Redis keys.
param([switch]$KeepUsers)
. "$PSScriptRoot\common.ps1"
$historyFile = Join-Path $DataDir "runs-history.txt"
$ids = @()
if (Test-Path $historyFile) { $ids += @(Get-Content $historyFile | Where-Object { $_ -match '^\d+$' }) }
$runsFile = Join-Path $DataDir "runs.json"
if (Test-Path $runsFile) { $ids += @((Get-Content $runsFile -Raw | ConvertFrom-Json)) }
$ids = @($ids | ForEach-Object { [int]$_ } | Sort-Object -Unique)
$list = $ids -join ","
$userFilter = "SELECT id FROM accounts_user WHERE email LIKE 'lt-%@loadtest.example.test'"
$bookingFilter = "SELECT id FROM bookings_booking WHERE user_id IN ($userFilter)"
if ($ids.Count -gt 0) { $bookingFilter = $bookingFilter + " OR run_id IN ($list)" }
$sql = New-Object System.Collections.Generic.List[string]
$sql.Add("\set ON_ERROR_STOP on")
$sql.Add("BEGIN;")
$sql.Add("DELETE FROM bookings_payment WHERE booking_id IN ($bookingFilter);")
$sql.Add("DELETE FROM bookings_bookingseat WHERE booking_id IN ($bookingFilter);")
$sql.Add("DELETE FROM bookings_booking WHERE id IN ($bookingFilter);")
if ($ids.Count -gt 0) { $sql.Add("DELETE FROM catalog_trainrun WHERE id IN ($list);") }
if (-not $KeepUsers) { $sql.Add("DELETE FROM accounts_user WHERE email LIKE 'lt-%@loadtest.example.test';") }
$sql.Add("COMMIT;")
Invoke-Psql ($sql -join "`n")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Database cleanup failed - the history and token files are kept so you can rerun this." -ForegroundColor Red
    exit 2
}
$removed = 0
foreach ($id in $ids) {
    foreach ($pattern in @("hold:{$id}:*", "wr:{$id}:*")) {
        $cmd = "redis-cli --scan --pattern '" + $pattern + "' | xargs -r redis-cli UNLINK"
        $out = docker compose exec -T redis sh -c $cmd
        $removed += @($out).Count
    }
}
Write-Host ("cleanup done: runs " + ($ids -join ",") + ", Redis delete replies: " + $removed) -ForegroundColor Green
if (Test-Path $historyFile) { Remove-Item $historyFile -Force }
if (-not $KeepUsers) {
    foreach ($f in @("tokens.json", "runs.json")) { $p = Join-Path $DataDir $f; if (Test-Path $p) { Remove-Item $p -Force } }
}
