$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-PythonExecutable([string]$commandName, [string[]]$arguments) {
    if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) { return $null }
    $result = & $commandName @arguments 2>$null | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or $null -eq $result) { return $null }
    $path = ([string]$result).Trim()
    if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    return $null
}

function Find-UsablePython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $pyPath = Get-PythonExecutable 'py' @('-3', '-c', 'import sys; print(sys.executable)')
        if ($pyPath) { return $pyPath }
    }
    foreach ($command in @('python', 'python3')) {
        $path = Get-PythonExecutable $command @('-c', 'import sys; print(sys.executable)')
        if ($path) { return $path }
    }
    return $null
}

$venvDirectory = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvDirectory 'Scripts\python.exe'
$pythonExe = $venvPython
if (-not (Test-Path -LiteralPath $venvPython)) {
    $pythonExe = Find-UsablePython
    if (-not $pythonExe) {
        Write-Host '未找到 Python。请安装 Python 3.10 或更高版本，并在安装时勾选 Add Python to PATH。' -ForegroundColor Red
        exit 1
    }
}

$versionText = & $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or -not $versionText) {
    Write-Host '无法读取 Python 版本，请重新安装 Python 后再试。' -ForegroundColor Red
    exit 1
}
$versionParts = ([string]$versionText).Trim().Split('.')
if ([int]$versionParts[0] -ne 3 -or [int]$versionParts[1] -lt 10) {
    Write-Host "当前 Python 为 $versionText；项目需要 Python 3.10 或更高版本。" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host '正在创建项目专用虚拟环境…'
    & $pythonExe -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        Write-Host '虚拟环境创建失败。请确认项目目录可写，并重新运行脚本。' -ForegroundColor Red
        exit 1
    }
}

Write-Host '正在检查项目依赖…'
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    Write-Host '依赖安装失败。请检查网络、Python 环境和 requirements.txt 后重试。' -ForegroundColor Red
    exit 1
}

if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        Write-Host '端口 8000 已被占用。请关闭旧项目窗口，或在浏览器确认 http://127.0.0.1:8000 是否已是当前项目。' -ForegroundColor Yellow
        exit 1
    }
}

Set-Location $projectRoot
Write-Host '正在启动本地项目；服务就绪后请打开：http://127.0.0.1:8000' -ForegroundColor Green
Write-Host '关闭此窗口或按 Ctrl+C 即可停止服务。'
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if ($LASTEXITCODE -ne 0) {
    Write-Host '本地服务未能启动。请查看上方错误信息；常见原因是端口占用或依赖不完整。' -ForegroundColor Red
    exit $LASTEXITCODE
}
