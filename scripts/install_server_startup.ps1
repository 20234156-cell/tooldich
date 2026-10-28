param([switch]$Remove, [switch]$ConfigureFirewall)
$ErrorActionPreference = 'Stop'
$workspacePath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskName = 'DuanLayLink Small Server'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Removed automatic startup. No running process or project was stopped/deleted.'
    exit
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -File "' + (Join-Path $workspacePath 'scripts\start_server_background.ps1') + '"') -WorkingDirectory $workspacePath
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description 'Single server instance under the current Windows account; models and DPAPI stay with this account.' -Force | Out-Null
if ($ConfigureFirewall) {
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Startup task installed. Firewall change needs an Administrator PowerShell; rerun with -ConfigureFirewall there.'
    }
    $pythonPath = (Get-Command python).Source
    foreach ($relative in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe')) {
        $candidatePath = Join-Path $workspacePath $relative
        if (Test-Path -LiteralPath $candidatePath) { $pythonPath = $candidatePath; break }
    }
    if (-not (Get-NetFirewallRule -DisplayName 'DuanLayLink Private LAN 8088' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'DuanLayLink Private LAN 8088' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8088 -RemoteAddress LocalSubnet -Profile Private -Program $pythonPath | Out-Null
    }
}
Write-Output 'Installed automatic startup at sign-in for the current Windows user. It does not start before Windows sign-in. No router/Internet access was enabled.'
