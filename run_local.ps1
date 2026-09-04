$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
function Find-UsablePython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $path = (& py -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim()
        if ($LASTEXITCODE -eq 0 -and $path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    foreach ($command in @('python', 'python3')) {
        if (Get-Command $command -ErrorAction SilentlyContinue) {
            $path = (& $command -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim()
            if ($LASTEXITCODE -eq 0 -and $path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    }
    return $null
}

$pythonExe = Find-UsablePython

if (-not $pythonExe) {
    Write-Host '未找到 Python。请安装 Python 3.10 或更高版本，并在安装时勾选 Add Python to PATH。' -ForegroundColor Red
    exit 1
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonExe -m venv (Join-Path $projectRoot '.venv')
}

Write-Host '正在检查项目依赖…'
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')

Set-Location $projectRoot
Write-Host '本地项目已启动。请在浏览器打开：http://127.0.0.1:8000' -ForegroundColor Green
Write-Host '关闭此窗口或按 Ctrl+C 即可停止服务。'
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
