param(
    [string]$TaskName = "LixinRecruiting-DueCollection"
)

$ProjectRoot = $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ScriptPath = Join-Path $ProjectRoot "scripts\run_due_collection.py"

if (!(Test-Path -LiteralPath $PythonPath) -or !(Test-Path -LiteralPath $ScriptPath)) {
    throw "未找到项目 Python 环境或每日采集脚本，请先完成本地安装。"
}

$TaskCommand = '"' + $PythonPath + '" "' + $ScriptPath + '"'
schtasks /Create /TN $TaskName /TR $TaskCommand /SC HOURLY /MO 4 /ST 00:30 /F | Out-Host
Write-Host "已创建计划任务：$TaskName。电脑开机且登录时，每 4 小时唤醒一次；是否实际采集由来源的季节频率决定。"
