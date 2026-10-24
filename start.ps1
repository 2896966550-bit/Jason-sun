param([string]$Python = $env:EQUIS_PYTHON)
$ErrorActionPreference = 'Stop'
if (-not $Python) {
    $localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython) { $Python = $localPython }
    else { $Python = (Get-Command python -ErrorAction Stop).Source }
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
Push-Location -LiteralPath $PSScriptRoot
try { & $Python -B (Join-Path $PSScriptRoot 'agent\server.py'); $exitCode = $LASTEXITCODE }
finally { Pop-Location }
exit $exitCode
