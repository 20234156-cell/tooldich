$ErrorActionPreference = 'Stop'
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Get-Command python -ErrorAction Stop).Source
foreach ($relative in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe')) {
    $candidatePath = Join-Path $workspacePath $relative
    if (Test-Path -LiteralPath $candidatePath) { $pythonPath = $candidatePath; break }
}
$logDirectory = Join-Path $workspacePath 'storage\maintenance'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$launcherProcess = Start-Process -FilePath $pythonPath -ArgumentList @('scripts/run_small_server.py') -WorkingDirectory $workspacePath -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'server_launcher.log') -RedirectStandardError (Join-Path $logDirectory 'server_launcher_errors.log')
exit $launcherProcess.ExitCode
