param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $taskRoot
    if (-not $SkipInstall) {
        & uv sync --extra dev --locked
        if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }
    }
    $runtime = Join-Path $taskRoot '.venv\Scripts\python.exe'
    & $runtime -X utf8 -m ruff check traceforge tests
    if ($LASTEXITCODE -ne 0) { throw 'ruff failed' }
    & $runtime -X utf8 -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'pytest failed' }
    & $runtime -X utf8 -m traceforge evaluate
    if ($LASTEXITCODE -ne 0) { throw 'offline evaluation failed' }
    & $runtime -X utf8 -m traceforge retrieval-evaluate
    if ($LASTEXITCODE -ne 0) { throw 'retrieval evaluation failed' }
    & $runtime -X utf8 -m traceforge benchmark --mode oracle
    if ($LASTEXITCODE -ne 0) { throw 'oracle benchmark failed' }
    Set-Location -LiteralPath (Join-Path $taskRoot 'web')
    if (-not $SkipInstall) {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
    }
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }
    Write-Output 'Local checks passed. Oracle results do not measure model repair accuracy.'
} finally {
    Set-Location -LiteralPath $previousLocation.Path
}
