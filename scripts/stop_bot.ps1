<#
.SYNOPSIS
    Local stop control: gracefully requests shutdown of the running Telegram assistant.
#>
[CmdletBinding()]
param(
    [string]$WorkingDirectory = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"
$PythonExe = Join-Path $WorkingDirectory ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $WorkingDirectory "src"

Push-Location $WorkingDirectory
try {
    & $PythonExe -m telegram_assistant stop
} finally {
    Pop-Location
}
