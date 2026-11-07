param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $taskRoot
    $acceptanceRoot = Join-Path $taskRoot ('.traceforge\releases\' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $acceptanceRoot -Force | Out-Null
    Set-Location -LiteralPath (Join-Path $taskRoot 'web')
    if (-not $SkipInstall) {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
    }
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }
    Set-Location -LiteralPath $taskRoot
    $buildLog = Join-Path $acceptanceRoot 'build.log'
    & uv build --out-dir (Join-Path $taskRoot 'dist') *> $buildLog
    if ($LASTEXITCODE -ne 0) { throw "release build failed; see $buildLog" }
    $wheel = Join-Path $taskRoot 'dist\traceforge_agent-0.1.0-py3-none-any.whl'
    if (-not (Test-Path -LiteralPath $wheel)) { throw 'Expected wheel was not produced' }
    $venvRoot = Join-Path $acceptanceRoot 'venv'
    & uv venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw 'clean release environment creation failed' }
    $runtime = Join-Path $venvRoot 'Scripts\python.exe'
    & uv pip install --python $runtime $wheel
    if ($LASTEXITCODE -ne 0) { throw 'wheel installation failed' }
    $report = Join-Path $acceptanceRoot 'report.json'
    & $runtime -X utf8 (Join-Path $PSScriptRoot 'smoke_release.py') --python $runtime --report $report
    if ($LASTEXITCODE -ne 0) { throw "installed release verification failed; see $report" }
    Get-FileHash -LiteralPath $wheel -Algorithm SHA256 | Format-List
    Write-Output "Release verified. Report: $report"
    Write-Output 'The replay and oracle checks do not measure model repair accuracy.'
} finally {
    Set-Location -LiteralPath $previousLocation.Path
}
