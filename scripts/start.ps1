param([int]$Port = 8710, [string]$ProjectRoot = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if ($ProjectRoot) {
    $resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $env:TRACEFORGE_ALLOWED_ROOTS = ConvertTo-Json -InputObject @($resolvedProject) -Compress
}
$runtime = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtime)) {
    throw '请先运行 uv sync --extra dev --locked'
}
& $runtime -m traceforge serve --port $Port
