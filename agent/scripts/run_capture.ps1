param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CaptureArguments)
$ErrorActionPreference = 'Stop'
$captureProject = Split-Path -Parent $PSScriptRoot
$capturePython = $env:EQUIS_CAPTURE_PYTHON
$environmentPath = Join-Path $captureProject 'capture-environment.json'
if (-not $capturePython -and (Test-Path -LiteralPath $environmentPath)) {
    $capturePython = (Get-Content -LiteralPath $environmentPath -Raw | ConvertFrom-Json).python
    if (-not [System.IO.Path]::IsPathRooted($capturePython)) { $capturePython = Join-Path $captureProject $capturePython }
}
if (-not $capturePython) {
    $projectPython = Join-Path (Split-Path -Parent $captureProject) '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $projectPython) { $capturePython = $projectPython }
    else { $capturePython = (Get-Command python -ErrorAction Stop).Source }
}
if (-not (Test-Path -LiteralPath $capturePython)) { throw '标定解释器不存在，请安装项目依赖或设置EQUIS_CAPTURE_PYTHON' }
$env:EQUIS_ENV_FILE = ''
$env:PYTHONDONTWRITEBYTECODE = '1'
Push-Location -LiteralPath $captureProject
try {
    & $capturePython -B (Join-Path $PSScriptRoot 'capture_stereo.py') @CaptureArguments
    $captureExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $captureExitCode
