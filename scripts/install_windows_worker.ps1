param(
    [string]$InstallDir = "$env:LOCALAPPDATA\ExplainBotRender",
    [string]$Python = (Get-Command python).Source
)
$ErrorActionPreference = 'Stop'
# config.json and the dedicated SSH key are configured before enabling the worker.
if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'config.json'))) {
    throw 'Create config.json first; see the Windows GPU worker section in README.md.'
}
Copy-Item (Join-Path $PSScriptRoot 'three_d_windows_worker.py'),(Join-Path $PSScriptRoot 'three_d_gpu_render.py') -Destination $InstallDir -Force
$pythonw = $Python -replace 'python.exe$','pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'pythonw.exe must be installed beside python.exe.' }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + (Join-Path $InstallDir 'three_d_windows_worker.py') + '" --config "' + (Join-Path $InstallDir 'config.json') + '"') -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ExplainBot-3D-Worker' -Action $action -Trigger $trigger -Settings $settings -Description 'Renders explain-bot 3D scenes over private Tailscale SSH.' -User "$env:USERDOMAIN\$env:USERNAME" -RunLevel Limited -Force | Out-Null
Start-ScheduledTask -TaskName 'ExplainBot-3D-Worker'
Get-ScheduledTask -TaskName 'ExplainBot-3D-Worker' | Select-Object TaskName,State
