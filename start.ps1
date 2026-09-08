$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$ProjectPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $ProjectPython)) {
    py -3 -m venv .venv
    & $ProjectPython -m pip install --upgrade pip
    & $ProjectPython -m pip install -e .
}

& $ProjectPython main.py --source emulator
exit $LASTEXITCODE
