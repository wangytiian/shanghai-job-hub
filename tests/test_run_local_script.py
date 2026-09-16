from pathlib import Path


def test_run_local_script_uses_portable_python_discovery():
    script = Path("run_local.ps1").read_text(encoding="utf-8")

    assert "Get-Command py" in script
    assert "foreach ($command in @('python', 'python3'))" in script
    assert "C:\\Users\\a9799" not in script
    assert "Split-Path -Parent $MyInvocation.MyCommand.Path" in script


def test_readme_explains_portable_windows_startup():
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "run_local.ps1" in readme
    assert "Python 3.10" in readme


def test_run_local_script_checks_python_version_dependencies_and_port_conflicts():
    script = Path("run_local.ps1").read_text(encoding="utf-8")

    assert "Python 3.10 或更高版本" in script
    assert "Get-NetTCPConnection" in script
    assert "依赖安装失败" in script
    assert "虚拟环境创建失败" in script


def test_run_local_script_prefers_existing_project_virtual_environment():
    script = Path("run_local.ps1").read_text(encoding="utf-8")

    assert script.index("$venvPython = Join-Path $venvDirectory 'Scripts\\python.exe'") < script.index(
        "$pythonExe = Find-UsablePython"
    )
