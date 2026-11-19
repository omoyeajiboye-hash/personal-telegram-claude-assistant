<#
.SYNOPSIS
    Creates a consistent local backup of the SQLite database and approved assistant folder.
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
    & $PythonExe -m telegram_assistant backup
} finally {
    Pop-Location
}
