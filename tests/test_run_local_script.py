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
