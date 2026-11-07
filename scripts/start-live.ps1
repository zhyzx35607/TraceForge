param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Model,
    [ValidateSet('chat', 'responses')][string]$ApiStyle = 'chat',
    [int]$Port = 8710,
    [string]$ProjectRoot = ''
)
$ErrorActionPreference = 'Stop'
$savedProvider = @{}
foreach ($name in @('TRACEFORGE_API_KEY', 'TRACEFORGE_BASE_URL', 'TRACEFORGE_MODEL', 'TRACEFORGE_API_STYLE')) {
    $savedProvider[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    if (-not $env:TRACEFORGE_API_KEY) {
        $secureKey = Read-Host 'Provider API key (hidden, process memory only)' -AsSecureString
        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        try {
            $env:TRACEFORGE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
            $secureKey.Dispose()
        }
    }
    if (-not $env:TRACEFORGE_API_KEY) { throw 'Provider API key cannot be empty' }
    $env:TRACEFORGE_BASE_URL = $BaseUrl.TrimEnd('/')
    $env:TRACEFORGE_MODEL = $Model
    $env:TRACEFORGE_API_STYLE = $ApiStyle
    & (Join-Path $PSScriptRoot 'start.ps1') -Port $Port -ProjectRoot $ProjectRoot
} finally {
    foreach ($name in $savedProvider.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedProvider[$name], 'Process')
    }
}
