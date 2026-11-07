param([int]$Port = 8710)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $taskRoot '.venv\Scripts\python.exe'
$logRoot = Join-Path $taskRoot '.traceforge'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($processInfo.CommandLine -notmatch '(?i)-m\s+traceforge\s+serve') {
        throw "端口 $Port 被其他程序占用，未停止它。"
    }
    Stop-Process -Id $listener.OwningProcess -ErrorAction Stop
}
$taskServer = Start-Process -FilePath $runtime -ArgumentList @('-m','traceforge','serve','--port',"$Port") -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'server.stdout.log') -RedirectStandardError (Join-Path $logRoot 'server.stderr.log') -PassThru
Write-Output "TraceForge started: process $($taskServer.Id), http://127.0.0.1:$Port"
