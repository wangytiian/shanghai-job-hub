$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCandidates = @(
    'C:\Users\a9799\AppData\Local\Programs\Python\Python312\python.exe',
    'C:\Program Files\Python312\python.exe'
)
$pythonExe = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $pythonExe) {
    throw 'Python 3.12 was not found. Install Python and run this script again.'
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonExe -m venv (Join-Path $projectRoot '.venv')
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')

Set-Location $projectRoot
Write-Host 'Local app started. Open: http://127.0.0.1:8000'
Write-Host 'Press Ctrl+C to stop the service.'
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
