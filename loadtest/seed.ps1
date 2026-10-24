# Creates N load-test users (reused between tests) and R fresh runs, and writes tokens.json and runs.json
# to E:\Goooo\railrush-loadtest\data (outside the repo, so tokens are never committed).
param([int]$Users = 200, [int]$Runs = 2, [int]$TokenHours = 3)
. "$PSScriptRoot\common.ps1"
$template = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot "seed_users.py"))
$code = $template.Replace("__USERS__", "$Users").Replace("__RUNS__", "$Runs").Replace("__HOURS__", "$TokenHours")
Write-Host "seeding $Users users and $Runs runs (this can take a minute)..."
$lines = @($code | docker compose exec -T web python manage.py shell)
$runIds = New-Object System.Collections.Generic.List[int]
$tokens = New-Object System.Collections.Generic.List[string]
foreach ($line in $lines) {
    $s = "$line"
    if ($s -match '^RUN=(\d+)$') { $runIds.Add([int]$Matches[1]) }
    elseif ($s -match '^TOKEN=(\S+)$') { $tokens.Add($Matches[1]) }
    elseif ($s -match '^USERS=') { Write-Host $s }
}
if ($runIds.Count -ne $Runs -or $tokens.Count -ne $Users) {
    Write-Host ("Seeding failed: got " + $runIds.Count + " runs and " + $tokens.Count + " tokens. Last lines:") -ForegroundColor Red
    $lines | Select-Object -Last 12
    exit 2
}
$tokenJson = "[" + (($tokens | ForEach-Object { '"' + $_ + '"' }) -join ",") + "]"
[System.IO.File]::WriteAllText((Join-Path $DataDir "tokens.json"), $tokenJson, $Utf8)
[System.IO.File]::WriteAllText((Join-Path $DataDir "runs.json"), ("[" + ($runIds -join ",") + "]"), $Utf8)
Add-Content -Path (Join-Path $DataDir "runs-history.txt") -Value $runIds
Write-Host ("tokens: " + $tokens.Count + ", runs: " + ($runIds -join ",") + " -> " + $DataDir) -ForegroundColor Green
