# Setup for Windows. Run from inside the stk folder:
#   powershell -ExecutionPolicy Bypass -File setup.ps1          terminal app
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Web     terminal app + web app
param([switch]$Web)
$ErrorActionPreference = "Stop"

Write-Host "==> stk setup"
if (-not (Test-Path "requirements.txt")) {
    Write-Host "Run this from inside the stk folder (the one that contains requirements.txt)."
    exit 1
}

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Host "Python was not found."
    Write-Host "Install Python 3.12 or 3.13 from https://www.python.org/downloads/ and tick 'Add python.exe to PATH'."
    Write-Host "Then open a NEW PowerShell window and run this again."
    exit 1
}

Write-Host "==> Creating a private Python environment in .venv"
& $py.Source -m venv .venv
if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not create the environment. If Windows opened the Microsoft Store, install Python from python.org instead."
    exit 1
}
$venvPy = ".\.venv\Scripts\python.exe"

Write-Host "==> Installing Python packages"
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt
if ($Web) {
    Write-Host "==> Installing the web app (Streamlit)"
    & $venvPy -m pip install -r requirements-web.txt
}

Write-Host ""
Write-Host "Done. Try:"
Write-Host "  .\bin\stk doctor"
Write-Host "  .\bin\stk quote RELIANCE"
if ($Web) { Write-Host "  .\bin\stk web" }
Write-Host ""
Write-Host "To type just 'stk' from any folder, add this folder's 'bin' to your PATH (see the README)."
