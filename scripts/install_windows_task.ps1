<#
.SYNOPSIS
    Registers a Windows Task Scheduler task to auto-start and restart the Personal Telegram Assistant
    under the current standard user account.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PersonalTelegramClaudeAssistant",
    [string]$WorkingDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

$PythonExe = Join-Path $WorkingDirectory ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    throw "Virtual environment Python not found at $PythonExe. Create .venv and install requirements first."
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m telegram_assistant run" `
    -WorkingDirectory $WorkingDirectory

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Personal Telegram Assistant powered by Claude (standard user account, long polling)." `
    -Force

Write-Host "Registered scheduled task '$TaskName' under standard user '$env:USERNAME'."
Write-Host "Start immediately with: Start-ScheduledTask -TaskName '$TaskName'"
